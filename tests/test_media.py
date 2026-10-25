"""附件檢查與圖片前處理的測試。全部使用合成圖片。"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from synthetic import jpeg_bytes, make_attachment, pdf_bytes, png_bytes, webp_bytes
from ticket_agent import media
from ticket_agent.media import MediaError, attachment_looks_supported, is_allowed_attachment_url, prepare_image, sniff_kind

MAX = 10 * 1024 * 1024


def decode(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


# ---------- 下載前的初步檢查 ----------

@pytest.mark.parametrize("filename,content_type,ok", [
    ("a.jpg", "image/jpeg", True), ("A.JPEG", "image/jpeg", True), ("a.png", "image/png", True),
    ("a.webp", "image/webp", True), ("ticket.pdf", "application/pdf", True), ("a.jpg", None, True),
    ("a.jpg", "image/jpeg; charset=binary", True),
    ("a.txt", "text/plain", False), ("a.gif", "image/gif", False), ("a.mp4", "video/mp4", False),
    ("a.svg", "image/svg+xml", False), ("a.exe", None, False), ("a", None, False), ("a.jpg", "text/html", False),
    ("a.jpg.exe", "image/jpeg", False), ("a.html", "text/html", False),
])
def test_attachment_precheck_by_name_and_type(filename, content_type, ok):
    assert attachment_looks_supported(make_attachment(filename=filename, content_type=content_type), MAX) is ok


def test_attachment_precheck_size_limits():
    assert attachment_looks_supported(make_attachment(size=MAX), MAX)
    assert not attachment_looks_supported(make_attachment(size=MAX + 1), MAX)
    assert not attachment_looks_supported(make_attachment(size=0), MAX)


@pytest.mark.parametrize("url,ok", [
    ("https://cdn.discordapp.com/attachments/1/2/a.jpg", True),
    ("https://media.discordapp.net/attachments/1/2/a.jpg?ex=1", True),
    ("http://cdn.discordapp.com/a.jpg", False),
    ("https://evil.example.com/a.jpg", False),
    ("https://cdn.discordapp.com.evil.example.com/a.jpg", False),
    ("https://evilcdn.discordapp.com/a.jpg", False),
    ("https://user:pw@cdn.discordapp.com/a.jpg", False),
    ("https://cdn.discordapp.com:8443/a.jpg", False),
    ("https://cdn.discordapp.com@evil.example.com/a.jpg", False),
    ("https://127.0.0.1/a.jpg", False), ("https://localhost/a.jpg", False),
    ("file:///etc/passwd", False), ("ftp://cdn.discordapp.com/a", False), ("//cdn.discordapp.com/a", False),
    ("", False), ("not a url", False), ("https://[::1]/a.jpg", False),
])
def test_only_discord_cdn_urls_are_allowed(url, ok):
    assert is_allowed_attachment_url(url) is ok


def test_precheck_rejects_a_foreign_url_even_if_everything_else_is_fine():
    assert not attachment_looks_supported(make_attachment(url="https://evil.example.com/a.jpg"), MAX)


# ---------- 判斷真正的格式（不信任檔名）----------

@pytest.mark.parametrize("data,kind", [
    (jpeg_bytes(), "jpeg"), (png_bytes(), "png"), (webp_bytes(), "webp"), (pdf_bytes(), "pdf"),
    (b"GIF89a....", None), (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", None), (b"<html></html>", None),
    (b"MZ\x90\x00", None), (b"", None), (b"just text", None), (b"RIFF1234WAVEfmt ", None),
])
def test_sniff_kind_uses_the_content(data, kind):
    assert sniff_kind(data) == kind


# ---------- 前處理：正常情況 ----------

@pytest.mark.parametrize("make", [jpeg_bytes, png_bytes, webp_bytes, pdf_bytes])
def test_every_supported_kind_becomes_a_jpeg(make):
    out = prepare_image(make(), max_bytes=MAX)
    image = decode(out)
    assert image.format == "JPEG" and max(image.size) <= media.MAX_EDGE


def test_large_images_are_shrunk_to_the_maximum_edge_keeping_the_ratio():
    out = decode(prepare_image(jpeg_bytes(size=(4000, 2000)), max_bytes=MAX))
    assert out.size == (2000, 1000)


def test_small_images_are_not_enlarged():
    assert decode(prepare_image(jpeg_bytes(size=(300, 200)), max_bytes=MAX)).size == (300, 200)


def test_exif_is_removed_and_orientation_is_applied():
    source = Image.new("RGB", (400, 200), "white")
    exif = Image.Exif()
    exif[0x0112] = 6  # 要順時針轉 90 度
    exif[0x010F] = "SyntheticCameraMaker"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    buffer = io.BytesIO()
    source.save(buffer, "JPEG", exif=exif)
    out = decode(prepare_image(buffer.getvalue(), max_bytes=MAX))
    assert out.size == (200, 400)  # 已依 EXIF 轉正
    assert len(out.getexif()) == 0
    assert b"SyntheticCameraMaker" not in prepare_image(buffer.getvalue(), max_bytes=MAX)


def test_transparent_png_gets_a_white_background():
    out = decode(prepare_image(png_bytes(alpha=True), max_bytes=MAX)).convert("RGB")
    assert out.getpixel((0, 0))[0] > 240  # 透明處變白色，不是黑色


def test_pdf_uses_only_the_first_page():
    out = decode(prepare_image(pdf_bytes(pages=3, size=(300, 200)), max_bytes=MAX))
    assert out.size[0] > out.size[1]  # 300x200 的橫式頁面


def test_pdf_with_a_huge_page_is_scaled_down():
    out = decode(prepare_image(pdf_bytes(size=(14000, 9000)), max_bytes=MAX))
    assert max(out.size) <= media.MAX_EDGE


# ---------- 前處理：壞資料一律 MediaError ----------

def test_empty_and_oversized_input():
    for bad in (b"", None, "text", 5):
        with pytest.raises(MediaError):
            prepare_image(bad, max_bytes=MAX)
    with pytest.raises(MediaError, match="太大"):
        prepare_image(jpeg_bytes(), max_bytes=100)


@pytest.mark.parametrize("data", [
    b"this is just a text file pretending to be a photo", b"<html><script>alert(1)</script></html>",
    b"GIF89a" + b"\x00" * 50, b"MZ" + b"\x00" * 200, b"%PDF-1.4 this is not really a pdf",
])
def test_files_that_are_not_really_images_are_rejected(data):
    with pytest.raises(MediaError):
        prepare_image(data, max_bytes=MAX)


def test_the_file_name_does_not_matter_only_the_content_does():
    """內容是 PDF，就算 Discord 說它是 .jpg 也照 PDF 處理；內容是文字，就算叫 .jpg 也拒絕。"""
    assert decode(prepare_image(pdf_bytes(), max_bytes=MAX)).format == "JPEG"
    with pytest.raises(MediaError):
        prepare_image(b"hello" * 100, max_bytes=MAX)


@pytest.mark.parametrize("cut", [20, 100, 300])
def test_truncated_or_corrupted_images_are_rejected(cut):
    good = jpeg_bytes()
    with pytest.raises(MediaError):
        prepare_image(good[:cut], max_bytes=MAX)
    with pytest.raises(MediaError):
        prepare_image(good[:100] + b"\x00" * 500, max_bytes=MAX)


def test_image_with_too_many_pixels_is_rejected_before_it_is_fully_decoded():
    bomb = Image.new("1", (12000, 12000))  # 1.44 億像素，壓縮後很小
    buffer = io.BytesIO()
    bomb.save(buffer, "PNG")
    assert len(buffer.getvalue()) < MAX
    with pytest.raises(MediaError, match="像素|無法"):
        prepare_image(buffer.getvalue(), max_bytes=MAX)


def test_too_small_image_is_rejected():
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, "PNG")
    with pytest.raises(MediaError, match="太小"):
        prepare_image(buffer.getvalue(), max_bytes=MAX)


def test_encrypted_and_broken_pdfs_are_rejected():
    with pytest.raises(MediaError, match="加密"):
        prepare_image(pdf_bytes(encrypted=True), max_bytes=MAX)
    good = pdf_bytes()
    for broken in (good[:60], b"%PDF-1.7\n" + b"\x00" * 300):  # PyMuPDF 會盡量修復輕微損壞的 PDF，所以只測修不起來的
        with pytest.raises(MediaError):
            prepare_image(broken, max_bytes=MAX)


def test_error_messages_never_contain_file_content():
    secret = b"SECRET-FILE-CONTENT-12345"
    for data in (secret * 10, b"%PDF-1.4" + secret, b"\xff\xd8\xff" + secret):
        with pytest.raises(MediaError) as info:
            prepare_image(data, max_bytes=MAX)
        assert "SECRET" not in str(info.value)
