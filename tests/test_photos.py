"""防護 3（檔名清洗）的測試：惡意檔名、路徑逃逸、檔名重複。全部使用合成資料。"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from ticket_agent import photos
from ticket_agent.photos import FALLBACK_LABEL, UnsafePathError, confine_to_dir, sanitize_label, store_photo

STAMP = "20261024-153012"
FORBIDDEN = set('/\\:*?"<>|')


@pytest.fixture
def photos_dir(tmp_path):
    folder = tmp_path / "data" / "photos"
    folder.mkdir(parents=True)
    return folder


def all_files(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# ---------- sanitize_label ----------

@pytest.mark.parametrize("raw,expected", [
    ("五月天演唱會", "五月天演唱會"),
    ("Taipei Concert 2099", "Taipei Concert 2099"),
    ("../../evil", "evil"),
    ("..", FALLBACK_LABEL),
    ("...", FALLBACK_LABEL),
    ("/", FALLBACK_LABEL),
    ("///", FALLBACK_LABEL),
    ("a/b\\c:d*e", "a_b_c_d_e"),
    ('say "hi" <x> | y?', "say _hi_ _x_ _ y"),
    (".bashrc", "bashrc"),
    ("  name  ", "name"),
    ("a\nb\tc", "a b c"),
    ("", FALLBACK_LABEL),
    ("   ", FALLBACK_LABEL),
    ("CON", "CON_"),
    ("nul", "nul_"),
    ("COM1", "COM1_"),
    ("a\x00b", "ab"),
])
def test_sanitize_known_cases(raw, expected):
    assert sanitize_label(raw) == expected


def test_fullwidth_slash_is_normalized_then_removed():
    assert "／" not in sanitize_label("a／b") and "/" not in sanitize_label("a／b")
    assert sanitize_label("..／..／x") == "x"


def test_invisible_direction_override_is_removed():
    cleaned = sanitize_label("evil\u202egpj.exe")
    assert "\u202e" not in cleaned


def test_zero_width_and_bom_are_removed():
    assert sanitize_label("a\u200bb\ufeffc") == "abc"


@pytest.mark.parametrize("raw", [None, 123, ["a"], b"bytes", object()])
def test_non_string_input_gives_fallback(raw):
    assert sanitize_label(raw) == FALLBACK_LABEL


def test_long_name_is_truncated_and_clean():
    cleaned = sanitize_label("演" * 500)
    assert len(cleaned) == photos.MAX_LABEL_LEN


def test_truncation_does_not_leave_trailing_dots_or_spaces():
    cleaned = sanitize_label("a" * 19 + " .b")
    assert cleaned == "a" * 19


EVIL = [
    "../../etc/passwd", "..\\..\\windows\\system32", "/etc/passwd", "C:\\boot.ini", "a/../../b", "x\x00.jpg",
    "con.txt", "aux", "%2e%2e%2f", "....//....//x", "~/.ssh/id_rsa", "\u202etxt.exe", "a|b>c", "\n\r\t", "｜＼／",
    "." * 50, "a" * 1000, "名\u0000稱/../", "=cmd|' /C calc'!A0", "../" * 30 + "x",
]


@pytest.mark.parametrize("raw", EVIL)
def test_cleaned_names_are_always_safe(raw):
    cleaned = sanitize_label(raw)
    assert cleaned
    assert not (set(cleaned) & FORBIDDEN)
    assert ".." not in cleaned
    assert len(cleaned) <= photos.MAX_LABEL_LEN
    assert cleaned == cleaned.lstrip(" ._")
    assert not cleaned.endswith((" ", "."))
    assert all(ord(ch) >= 32 and ch != "\x7f" for ch in cleaned)


# ---------- confine_to_dir ----------

@pytest.mark.parametrize("name", ["", ".", "..", "../x", "a/b", "a\\b", "x\0y", "/abs"])
def test_confine_rejects_path_syntax(photos_dir, name):
    with pytest.raises(UnsafePathError):
        confine_to_dir(photos_dir, name)


def test_confine_accepts_plain_name(photos_dir):
    assert confine_to_dir(photos_dir, "ok.jpg") == photos_dir / "ok.jpg"


def test_confine_rejects_symlink_pointing_outside(photos_dir, tmp_path):
    outside = tmp_path / "outside-secret.jpg"
    outside.write_bytes(b"secret")
    (photos_dir / "link.jpg").symlink_to(outside)
    with pytest.raises(UnsafePathError):
        confine_to_dir(photos_dir, "link.jpg")


# ---------- store_photo ----------

def test_store_photo_normal(photos_dir):
    path = store_photo(photos_dir, STAMP, "五月天演唱會", b"IMG")
    assert path == photos_dir / f"{STAMP}_五月天演唱會.jpg"
    assert path.read_bytes() == b"IMG"
    assert (path.stat().st_mode & 0o777) == 0o600


def test_store_photo_with_format_stamp(photos_dir):
    stamp = photos.format_stamp(datetime(2026, 10, 24, 15, 30, 12))
    assert stamp == STAMP
    assert store_photo(photos_dir, stamp, "x", b"1").name == f"{STAMP}_x.jpg"


@pytest.mark.parametrize("raw", EVIL)
def test_evil_labels_only_ever_create_one_file_inside_photos_dir(tmp_path, photos_dir, raw):
    before = all_files(tmp_path)
    path = store_photo(photos_dir, STAMP, raw, b"IMG")
    created = set(all_files(tmp_path)) - set(before)
    assert path.parent == photos_dir
    assert len(created) == 1 and next(iter(created)).startswith(os.path.join("data", "photos"))


def test_same_second_same_name_never_overwrites(photos_dir):
    first = store_photo(photos_dir, STAMP, "演唱會", b"ONE")
    second = store_photo(photos_dir, STAMP, "演唱會", b"TWO")
    third = store_photo(photos_dir, STAMP, "演唱會", b"THREE")
    assert [p.name for p in (first, second, third)] == [
        f"{STAMP}_演唱會.jpg", f"{STAMP}_演唱會-2.jpg", f"{STAMP}_演唱會-3.jpg"]
    assert (first.read_bytes(), second.read_bytes(), third.read_bytes()) == (b"ONE", b"TWO", b"THREE")


def test_labels_that_clean_to_the_same_name_do_not_collide(photos_dir):
    a = store_photo(photos_dir, STAMP, "../x", b"A")
    b = store_photo(photos_dir, STAMP, "x", b"B")
    assert a != b and a.read_bytes() == b"A" and b.read_bytes() == b"B"


def test_existing_symlink_at_target_is_not_followed(tmp_path, photos_dir):
    outside = tmp_path / "outside-secret.jpg"
    outside.write_bytes(b"ORIGINAL")
    (photos_dir / f"{STAMP}_x.jpg").symlink_to(outside)
    path = store_photo(photos_dir, STAMP, "x", b"NEW")
    assert outside.read_bytes() == b"ORIGINAL"  # 沒有透過連結覆蓋到外面的檔案
    assert path.name == f"{STAMP}_x-2.jpg" and path.read_bytes() == b"NEW"


def test_dangling_symlink_at_target_is_not_followed(tmp_path, photos_dir):
    (photos_dir / f"{STAMP}_x.jpg").symlink_to(tmp_path / "does-not-exist.jpg")
    path = store_photo(photos_dir, STAMP, "x", b"NEW")
    assert not (tmp_path / "does-not-exist.jpg").exists()
    assert path.name == f"{STAMP}_x-2.jpg"


@pytest.mark.parametrize("stamp", ["", "2026-10-24", "../../x", "20261024-15301", "20261024-153012/x", "abc"])
def test_bad_stamp_is_rejected(photos_dir, stamp):
    with pytest.raises(UnsafePathError):
        store_photo(photos_dir, stamp, "x", b"1")


@pytest.mark.parametrize("ext", ["", ".exe", "jpg", "/../x.jpg", ".jpg/../../x", ".JPG "])
def test_bad_extension_is_rejected(photos_dir, ext):
    with pytest.raises(UnsafePathError):
        store_photo(photos_dir, STAMP, "x", b"1", ext=ext)


def test_png_extension_is_allowed(photos_dir):
    assert store_photo(photos_dir, STAMP, "x", b"1", ext=".png").suffix == ".png"


def test_failed_label_uses_fallback_name_style(photos_dir):
    assert store_photo(photos_dir, STAMP, "處理失敗", b"1").name == f"{STAMP}_處理失敗.jpg"
    assert store_photo(photos_dir, STAMP, "非票券-一杯咖啡", b"1").name == f"{STAMP}_非票券-一杯咖啡.jpg"


# ---------- 拿掉防護 3 的對照（證明防護有在作用，且拿掉後程式仍然能跑）----------

def test_without_guard_the_module_still_runs_but_is_unsafe(tmp_path, photos_dir, load_without_guard):
    unsafe = load_without_guard("photos.py", 3)

    # 1. 仍然能跑
    ok = unsafe.store_photo(photos_dir, STAMP, "normal", b"IMG")
    assert ok.read_bytes() == b"IMG"

    # 2. 檔名裡的路徑符號沒被清掉：路徑含有不存在的子資料夾而當掉（拒絕服務），有子資料夾就寫進去了
    with pytest.raises(FileNotFoundError):
        unsafe.store_photo(photos_dir, STAMP, "a/b", b"1")

    # 3. 同名會被覆蓋，舊照片就遺失了
    unsafe.store_photo(photos_dir, STAMP, "dup", b"OLD")
    unsafe.store_photo(photos_dir, STAMP, "dup", b"NEW")
    assert (photos_dir / f"{STAMP}_dup.jpg").read_bytes() == b"NEW"

    # 4. 目標若是指到外面的符號連結，會順著連結覆蓋外面的檔案
    outside = tmp_path / "outside-secret.jpg"
    outside.write_bytes(b"ORIGINAL")
    (photos_dir / f"{STAMP}_link.jpg").symlink_to(outside)
    unsafe.store_photo(photos_dir, STAMP, "link", b"OVERWRITTEN")
    assert outside.read_bytes() == b"OVERWRITTEN"
