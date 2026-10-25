"""指令解析：`!我的票券`、`!刪除`、`!說明`（docs/architecture.md 第 11.1 節）。

指令完全由程式用固定規則解析，從不經過 Claude。看不懂的指令回傳 None（不是我們的指令，直接忽略，
免得誤吃別的 bot 的指令）；格式錯的我們的指令回傳 BadCommand，附上用法說明。
這裡只負責「看懂指令」；誰可以刪、刪哪一筆，由呼叫端依上傳者ID判斷。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

LIST_COMMAND = "我的票券"
DELETE_COMMAND = "刪除"
HELP_COMMAND = "說明"
COMMAND_WORDS = (LIST_COMMAND, DELETE_COMMAND, HELP_COMMAND)  # 說明文字與解析器要保持一致（有測試檢查）
LIST_USAGE = "用法：`!我的票券`（本月）、`!我的票券 2026-09`（指定月份）、`!我的票券 全部`"
DELETE_USAGE = "用法：`!刪除 編號`，例如 `!刪除 12`"
HELP_USAGE = "用法：`!說明`（後面不用加任何內容）"
# 指令名稱後面可以「緊貼」著參數（例如 `!刪除#1`、`!我的票券2026-09`），但只限 # 或數字開頭，而且只有帶參數的指令可以。
# 其他緊貼的字（`!刪除了`、`!說明書`）一律不是我們的指令，維持忽略。
ATTACHABLE_COMMANDS = (LIST_COMMAND, DELETE_COMMAND)
ATTACH_CHARS = "#0123456789"
_MONTH = re.compile(r"(\d{4})-(0[1-9]|1[0-2])")
_SERIAL = re.compile(r"#?(\d{1,9})")


@dataclass(frozen=True)
class ListCommand:
    period: str  # 'month'（目前這個月）、'all'，或 'YYYY-MM'


@dataclass(frozen=True)
class DeleteCommand:
    serial: int


@dataclass(frozen=True)
class HelpCommand:
    pass


@dataclass(frozen=True)
class BadCommand:
    usage: str


def parse_command(text: str) -> ListCommand | DeleteCommand | HelpCommand | BadCommand | None:
    normalized = unicodedata.normalize("NFKC", text or "").strip()  # 全形的「！」「２０２６」會統一成半形
    if not normalized.startswith("!"):
        return None
    split = _split_command(normalized[1:].lstrip())  # 驚嘆號後面的空白可有可無
    if split is None:
        return None
    word, args = split
    if word == LIST_COMMAND:
        return _parse_list(args)
    if word == DELETE_COMMAND:
        return _parse_delete(args)
    if word == HELP_COMMAND:
        return HelpCommand() if not args else BadCommand(HELP_USAGE)  # 和其他指令一樣：多帶參數就回覆用法
    return None


def _split_command(body: str) -> tuple[str, list[str]] | None:
    """把驚嘆號後面的內容切成（指令名稱, 參數清單）。不是我們的指令就回傳 None。
    指令名稱後面必須是：結束、空白，或（只有帶參數的指令）緊貼的 # 或數字。"""
    for word in COMMAND_WORDS:
        if not body.startswith(word):
            continue
        rest = body[len(word):]
        if rest == "" or rest[0].isspace() or (word in ATTACHABLE_COMMANDS and rest[0] in ATTACH_CHARS):
            return word, rest.split()
    return None


def _parse_list(args: list[str]) -> ListCommand | BadCommand:
    if not args:
        return ListCommand("month")
    if len(args) == 1:
        if args[0] == "全部":
            return ListCommand("all")
        if _MONTH.fullmatch(args[0]):
            return ListCommand(args[0])
    return BadCommand(LIST_USAGE)


def _parse_delete(args: list[str]) -> DeleteCommand | BadCommand:
    if len(args) == 1:
        match = _SERIAL.fullmatch(args[0])
        if match and int(match.group(1)) >= 1:
            return DeleteCommand(int(match.group(1)))
    return BadCommand(DELETE_USAGE)
