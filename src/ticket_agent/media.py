"""附件檢查與圖片前處理（docs/architecture.md 第 3.6、4 節）。

附件是別人送進來的檔案，一律不信任檔名、副檔名和 Discord 回報的類型：
- 先用「檔案內容的開頭」判斷真正的格式，只收 JPEG / PNG / WebP / PDF。
- 圖片用 Pillow 真的打開、驗證、限制像素總量（防止「小檔案解開變超大圖」）、依 EXIF 轉正後移除 EXIF（含 GPS 位置）、
  縮到最長邊 2000 像素、統一轉成 JPEG。
- PDF 只取第一頁，轉成圖片；有加密的拒絕。
- 任何問題都丟 MediaError，訊息只說類型，不含檔案內容。
"""

from __future__ import annotations

import io
import logging
import warnings
from urllib.parse import urlparse

import pymupdf
from PIL import Image, ImageOps, UnidentifiedImageError

from .messaging import Attachment

log = logging.getLogger(__name__)

MAX_EDGE = 2000
MAX_PIXELS = 50_000_000
JPEG_QUALITY = 90
MIN_EDGE = 8
ALLOWED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".pdf")
ALLOWED_CONTENT_TYPES = ("image/jpeg", "image/png", "image/webp", "application/pdf")
ALLOWED_HOSTS = ("cdn.discordapp.com", "media.discordapp.net")
_ALLOWED_PIL_FORMATS = ("JPEG", "PNG", "WEBP")

Image.MAX_IMAGE_PIXELS = MAX_PIXELS  # 超過會警告、超過兩倍直接丟錯；下面兩種都當成拒絕


class MediaError(Exception):
    """附件不能處理（格式不對、太大、毀損、加密……）。訊息不含檔案內容。"""


def is_allowed_attachment_url(url: str) -> bool:
    """只從 Discord 官方檔案網址下載：https、主機名稱完全相符、沒有帳號密碼、沒有特殊埠號。"""
    try:
        parsed = urlparse(url)
        return (parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS
                and parsed.username is None and parsed.password is None and parsed.port in (None, 443))
    except ValueError:
        return False


def attachment_looks_supported(attachment: Attachment, max_bytes: int) -> bool:
    """下載「之前」的初步檢查（只看檔名、Discord 回報的類型與大小，這些都能被偽造，所以下載後還會用內容再確認）。"""
    name = attachment.filename.lower()
    if not name.endswith(ALLOWED_EXTENSIONS):
        return False
    if attachment.content_type and attachment.content_type.split(";")[0].strip().lower() not in ALLOWED_CONTENT_TYPES:
        return False
    return 0 < attachment.size <= max_bytes and is_allowed_attachment_url(attachment.url)


def sniff_kind(data: bytes) -> str | None:
    """用檔案內容的開頭判斷真正的格式。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:1024].lstrip(b"\x00\t\r\n ").startswith(b"%PDF-"):
        return "pdf"
    return None


def prepare_image(data: bytes, *, max_bytes: int) -> bytes:
    """把下載的附件變成可以交給 Claude、也可以存檔的 JPEG（最長邊 2000 像素、沒有 EXIF）。"""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise MediaError("檔案是空的")
    if len(data) > max_bytes:
        raise MediaError("檔案太大")
    kind = sniff_kind(bytes(data))
    if kind is None:
        raise MediaError("不支援的檔案內容")
    raw = _render_pdf_first_page(bytes(data)) if kind == "pdf" else bytes(data)
    return _to_jpeg(raw)


def _render_pdf_first_page(data: bytes) -> bytes:
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass or doc.is_encrypted:
                raise MediaError("PDF 有加密")
            if doc.page_count < 1:
                raise MediaError("PDF 沒有頁面")
            page = doc[0]
            width, height = page.rect.width, page.rect.height
            if not (width > 0 and height > 0):
                raise MediaError("PDF 頁面大小不合理")
            zoom = min(MAX_EDGE / max(width, height), 4.0)
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            if pixmap.width * pixmap.height > MAX_PIXELS:
                raise MediaError("PDF 轉出來的圖太大")
            return pixmap.tobytes("png")
    except MediaError:
        raise
    except Exception as error:  # noqa: BLE001 - PyMuPDF 對壞檔案可能丟各種例外
        raise MediaError("PDF 無法讀取") from error


def _to_jpeg(raw: bytes) -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as probe:
                if probe.format not in _ALLOWED_PIL_FORMATS:
                    raise MediaError("不支援的圖片格式")
                probe.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.seek(0)  # 動態圖片只取第一格
                image = ImageOps.exif_transpose(image)  # 依 EXIF 方向轉正，之後 EXIF 就不留了
                image.load()
                rgb = _flatten_to_rgb(image)
        if min(rgb.size) < MIN_EDGE:
            raise MediaError("圖片太小")
        rgb.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        rgb.save(output, "JPEG", quality=JPEG_QUALITY, optimize=True)  # 不帶 exif、不帶色彩描述檔
        return output.getvalue()
    except MediaError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise MediaError("圖片像素太多") from error
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError, MemoryError) as error:
        raise MediaError("圖片無法讀取") from error


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    """透明的部分墊上白色底，再轉成 RGB。"""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")
