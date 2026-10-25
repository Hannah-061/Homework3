"""掃描程式裡的四段防護標記，列出位置與行數，並檢查標記格式（見 docs/architecture.md 12.1）。

標記格式固定，寫法見 docs/architecture.md 12.1（這裡不放範例，免得掃描器把說明文字當成真的標記）。

這個檔案只讀原始碼文字，不載入也不執行其他模組。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

GUARD_NAMES = {
    1: "Claude 的檔案範圍隔離",
    2: "使用者輸入隔離",
    3: "檔名清洗",
    4: "Excel 公式注入防護",
}

_START = re.compile(r"^\s*# ==== 防護 (\d+)：(.+?)　開始 ====\s*$")
_END = re.compile(r"^\s*# ==== 防護 (\d+) 結束 ====\s*$")
_LOOKS_LIKE_MARKER = re.compile(r"防護\s*\d.*(開始|結束)|====.*防護|>>>.*防護")


@dataclass(frozen=True)
class Guard:
    number: int
    name: str
    file: str  # 相對於掃描根目錄
    start_line: int
    end_line: int

    @property
    def lines(self) -> int:
        return self.end_line - self.start_line + 1


def scan(src_dir: Path) -> tuple[list[Guard], list[str]]:
    """掃描 src_dir 底下所有 .py。回傳 (找到的防護區塊, 問題清單)。"""
    guards: list[Guard] = []
    problems: list[str] = []
    for path in sorted(src_dir.rglob("*.py")):
        rel = str(path.relative_to(src_dir))
        _scan_file(path, rel, guards, problems)

    seen: dict[int, Guard] = {}
    for guard in guards:
        if guard.number in seen:
            problems.append(f"防護 {guard.number} 出現了不只一組標記（{seen[guard.number].file} 與 {guard.file}）")
        seen[guard.number] = guard
    return sorted(guards, key=lambda g: g.number), problems


def _scan_file(path: Path, rel: str, guards: list[Guard], problems: list[str]) -> None:
    open_guard: tuple[int, str, int] | None = None  # (編號, 名稱, 開始行)
    body: list[str] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        start = _START.match(line)
        end = _END.match(line)
        if start:
            number, name = int(start.group(1)), start.group(2)
            if open_guard is not None:
                problems.append(f"{rel}:{line_no} 防護 {open_guard[0]} 還沒結束就開始了新的一段（不可巢狀）")
            if number not in GUARD_NAMES:
                problems.append(f"{rel}:{line_no} 防護編號 {number} 不存在（只有 1～4）")
            elif name != GUARD_NAMES[number]:
                problems.append(f"{rel}:{line_no} 防護 {number} 的名稱應為「{GUARD_NAMES[number]}」，不是「{name}」")
            open_guard, body = (number, name, line_no), []
        elif end:
            number = int(end.group(1))
            if open_guard is None:
                problems.append(f"{rel}:{line_no} 防護 {number} 有結束標記，但前面沒有開始標記")
            elif open_guard[0] != number:
                problems.append(f"{rel}:{line_no} 結束的是防護 {number}，但目前開著的是防護 {open_guard[0]}")
                open_guard = None
            else:
                if not any(_is_code(text) for text in body):
                    problems.append(f"{rel}:{open_guard[2]} 防護 {number} 區塊裡沒有任何程式（空的，或被整段註解掉了）")
                guards.append(Guard(number, open_guard[1], rel, open_guard[2], line_no))
                open_guard = None
        elif _LOOKS_LIKE_MARKER.search(line) and line.lstrip().startswith("#"):
            problems.append(f"{rel}:{line_no} 看起來像防護標記，但格式不符（請用 docs/architecture.md 12.1 的格式）")
        elif open_guard is not None:
            body.append(line)
    if open_guard is not None:
        problems.append(f"{rel}:{open_guard[2]} 防護 {open_guard[0]} 有開始標記，但沒有結束標記")


def _is_code(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def format_report(guards: list[Guard], problems: list[str]) -> list[str]:
    lines = ["防護  名稱                      位置（檔案:開始行-結束行，約幾行）"]
    found = {g.number: g for g in guards}
    for number, name in GUARD_NAMES.items():
        guard = found.get(number)
        if guard is None:
            lines.append(f"{number}     {name}  ❌ 尚未實作（找不到標記）")
        else:
            lines.append(f"{number}     {name}  {guard.file}:{guard.start_line}-{guard.end_line}（{guard.lines} 行）")
    for problem in problems:
        lines.append(f"⚠️ {problem}")
    return lines
