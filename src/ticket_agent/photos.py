"""存照片：檔名規則與安全寫入（docs/architecture.md 第 9 節、防護 3）。

檔名格式：`日期時間_名稱簡稱.jpg`，例如 `20261024-153012_五月天演唱會.jpg`。
名稱簡稱來自 Claude 的辨識結果（外部輸入），絕不能直接當檔名用。
使用者附件的原始檔名完全不使用，一律由程式自己產生檔名。
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

STAMP_FORMAT = "%Y%m%d-%H%M%S"
MAX_LABEL_LEN = 20
FALLBACK_LABEL = "未命名"
ALLOWED_EXTENSIONS = (".jpg", ".png")


class UnsafePathError(ValueError):
    """算出來的檔案位置不在允許的資料夾裡，或名稱不合規定。"""


def format_stamp(moment: datetime) -> str:
    return moment.strftime(STAMP_FORMAT)


def store_photo(photos_dir: Path, stamp: str, label: str, data: bytes, ext: str = ".jpg") -> Path:
    """存一張照片，回傳實際存放的完整路徑。

    這是「沒有任何防護」的基本版本：直接把外部給的名稱接在資料夾後面，而且會覆蓋舊檔。
    下面的防護 3 區塊會用安全的版本取代它；單獨拿掉防護 3，程式仍然能跑，只是不再安全。
    """
    path = photos_dir / f"{stamp}_{label}{ext}"
    path.write_bytes(data)
    return path


# ==== 防護 3：檔名清洗　開始 ====
_FORBIDDEN_CHARS = frozenset('/\\:*?"<>|')
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)
_STAMP_PATTERN = re.compile(r"\d{8}-\d{6}")
_MAX_COLLISION_SUFFIX = 999


def sanitize_label(raw: object, max_len: int = MAX_LABEL_LEN) -> str:
    """把外部給的名稱簡稱，清成可以安全放進檔名的字串。清完是空的就回傳「未命名」。"""
    text = unicodedata.normalize("NFKC", raw if isinstance(raw, str) else "")  # 全形的 ／ 會變成 /，再被清掉
    cleaned: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if char in _FORBIDDEN_CHARS:
            cleaned.append("_")
        elif char.isspace() or category in ("Zs", "Zl", "Zp"):
            cleaned.append(" ")
        elif category[0] == "C":  # 控制字元、不可見的格式字元（如 U+202E 方向覆寫、零寬字元）、未指派字元
            continue
        else:
            cleaned.append(char)
    text = "".join(cleaned)
    text = re.sub(r"\.{2,}", "_", text)  # 連續的點（..）
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"_{2,}", "_", text)
    text = text.strip(" ._")
    if len(text) > max_len:
        text = text[:max_len].strip(" ._")
    if not re.search(r"\w", text):  # 清完只剩符號，或本來就是空的
        return FALLBACK_LABEL
    if text.split(".")[0].upper() in _WINDOWS_RESERVED:
        text += "_"
    return text


def confine_to_dir(base_dir: Path, filename: str) -> Path:
    """把檔名接在 base_dir 後面，並確認「解析後的最終位置」仍然在 base_dir 裡面。"""
    if not filename or filename in (".", "..") or any(sep in filename for sep in ("/", "\\", "\0")):
        raise UnsafePathError("檔名含有路徑符號")
    base = base_dir.resolve()
    target = (base_dir / filename).resolve()  # 會順著符號連結解析，所以指到外面的連結也會被抓到
    if target.parent != base:
        raise UnsafePathError("檔案位置不在允許的資料夾內")
    return base_dir / filename


def store_photo(photos_dir: Path, stamp: str, label: str, data: bytes, ext: str = ".jpg") -> Path:  # noqa: F811
    """存一張照片：清洗名稱、確認路徑在 photos_dir 內、用「不存在才建立」的方式寫入（不覆蓋舊檔）。"""
    if not _STAMP_PATTERN.fullmatch(stamp):
        raise UnsafePathError("時間戳記格式不對")
    if ext not in ALLOWED_EXTENSIONS:
        raise UnsafePathError("不支援的副檔名")
    safe_label = sanitize_label(label)
    for number in range(1, _MAX_COLLISION_SUFFIX + 2):
        suffix = "" if number == 1 else f"-{number}"
        filename = f"{stamp}_{safe_label}{suffix}{ext}"
        if os.path.lexists(photos_dir / filename):  # 這個名字已經被占用（包括符號連結）：換下一個，絕不碰它
            continue
        target = confine_to_dir(photos_dir, filename)
        try:
            # O_EXCL：檔案已存在就失敗，不會蓋掉；O_NOFOLLOW：目標若是符號連結也不跟隨
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return target
    raise UnsafePathError("同一秒、同一名稱的檔案太多，無法建立")
# ==== 防護 3 結束 ====
