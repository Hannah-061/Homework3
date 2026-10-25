"""四段防護標記的檢查（docs/architecture.md 12.1）。

- 掃描器本身的行為，用合成的小檔案測。
- 真正的原始碼：標記格式一定要正確，而且四段防護必須全部到齊、各在預期的檔案裡。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ticket_agent import guards
from ticket_agent.main import main

REAL_SRC = Path(guards.__file__).resolve().parent


def write(tmp_path: Path, name: str, text: str) -> Path:
    (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def block(number: int, body: str = "    x = 1\n", name: str | None = None) -> str:
    name = name or guards.GUARD_NAMES[number]
    return f"# ==== 防護 {number}：{name}　開始 ====\n{body}# ==== 防護 {number} 結束 ====\n"


# ---------- 掃描器 ----------

def test_finds_a_valid_block_with_lines(tmp_path):
    src = write(tmp_path, "a.py", "import os\n" + block(3, "    y = 2\n    z = 3\n"))
    found, problems = guards.scan(src)
    assert problems == []
    assert [(g.number, g.file, g.start_line, g.end_line, g.lines) for g in found] == [(3, "a.py", 2, 5, 4)]


def test_all_four_blocks_in_different_files(tmp_path):
    for number in (1, 2, 3, 4):
        write(tmp_path, f"m{number}.py", block(number))
    found, problems = guards.scan(tmp_path)
    assert problems == []
    assert [g.number for g in found] == [1, 2, 3, 4]


def test_two_adjacent_blocks_in_one_file(tmp_path):
    src = write(tmp_path, "runner.py", block(1) + "\n" + block(2))
    found, problems = guards.scan(src)
    assert problems == []
    assert [g.number for g in found] == [1, 2]


@pytest.mark.parametrize("text,expect", [
    ("# ==== 防護 1：Claude 的檔案範圍隔離　開始 ====\nx = 1\n", "沒有結束標記"),
    ("x = 1\n# ==== 防護 1 結束 ====\n", "沒有開始標記"),
    (block(1, name="亂取的名字"), "名稱應為"),
    (block(5, name="不存在"), "不存在"),
    (block(1, body="    # x = 1\n    # y = 2\n\n"), "空的，或被整段註解掉"),
    (block(1, body=""), "空的，或被整段註解掉"),
    ("# >>> 防護 1 開始：舊格式 <<<\nx = 1\n# >>> 防護 1 結束 <<<\n", "格式不符"),
    ("# ==== 防護 1：Claude 的檔案範圍隔離 開始 ====\nx = 1\n# ==== 防護 1 結束 ====\n", "格式不符"),  # 半形空格
    (block(1) + block(1), "不只一組"),
])
def test_detects_problems(tmp_path, text, expect):
    src = write(tmp_path, "a.py", text)
    _, problems = guards.scan(src)
    assert any(expect in p for p in problems), problems


def test_nesting_is_rejected(tmp_path):
    inner = block(2)
    text = f"# ==== 防護 1：{guards.GUARD_NAMES[1]}　開始 ====\nx = 1\n{inner}# ==== 防護 1 結束 ====\n"
    _, problems = guards.scan(write(tmp_path, "a.py", text))
    assert any("不可巢狀" in p for p in problems)


def test_mismatched_end_is_rejected(tmp_path):
    text = f"# ==== 防護 1：{guards.GUARD_NAMES[1]}　開始 ====\nx = 1\n# ==== 防護 2 結束 ====\n"
    _, problems = guards.scan(write(tmp_path, "a.py", text))
    assert any("結束的是防護 2" in p for p in problems)


def test_report_marks_missing_guards(tmp_path):
    found, problems = guards.scan(write(tmp_path, "a.py", block(1)))
    report = "\n".join(guards.format_report(found, problems))
    assert "a.py:1-3" in report
    assert report.count("尚未實作") == 3


# ---------- 真正的原始碼 ----------

def test_real_source_markers_are_well_formed():
    """已經寫出來的標記，格式一定要對（不管四段有沒有到齊）。"""
    _, problems = guards.scan(REAL_SRC)
    assert problems == []


def test_cli_guards_flag_runs_without_env(capsys):
    code = main(["--guards"])
    out = capsys.readouterr().out
    assert code == 0
    for name in guards.GUARD_NAMES.values():
        assert name in out


def test_all_four_guards_exist_in_real_source():
    found, problems = guards.scan(REAL_SRC)
    assert problems == []
    assert [g.number for g in found] == [1, 2, 3, 4]
    expected_files = {1: "claude_runner.py", 2: "claude_runner.py", 3: "photos.py", 4: "excel_store.py"}
    assert {g.number: g.file for g in found} == expected_files
