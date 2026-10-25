"""票號在「其他資訊」欄位裡的存放規則，以及保護它的幾個小工具（docs/architecture.md 8.2、11.2）。

票號不是獨立的 Excel 欄位，而是收在「其他資訊」儲存格的**最前面**，格式固定：

    票號：<票號>；<其餘備註…>

規則：
- 只有「整段文字最開頭」的 `票號：…；` 才算票號，其餘一律是備註。程式自己組字串時永遠把票號放最前面。
- 票號只在「第一次辨識」時寫入。一般更正不能動它（「其他資訊」不在可更正欄位裡），
  重複上傳時的自動補齊也不會覆蓋或刪除已經記錄的票號（見 dedupe.merge_ticket_data）。
- 票號本身不允許含有分隔字元「；」，備註裡也不允許出現會冒充票號的開頭，所以切開來的結果不會有歧義。
"""

from __future__ import annotations

import re

PREFIX = "票號："
SEPARATOR = "；"
_SEPARATOR_LIKE = re.compile(r"[；;]")
_SPOOF = re.compile(r"票號\s*[:：]")


def split_other_info(text: str) -> tuple[str, str]:
    """把「其他資訊」切成 (票號, 其餘備註)。沒有票號時，票號是空字串。"""
    if not text.startswith(PREFIX):
        return "", text
    number, _, rest = text[len(PREFIX):].partition(SEPARATOR)
    return number.strip(), rest.strip()


def join_other_info(number: str, notes: str) -> str:
    """票號永遠放在最前面，後面接備註。"""
    parts = ([PREFIX + number] if number else []) + ([notes] if notes else [])
    return SEPARATOR.join(parts)


def clean_number(number: str) -> str:
    """票號裡不可以有分隔字元，否則切開時會把票號的一部分當成備註。"""
    return _SEPARATOR_LIKE.sub("-", number).strip()


def neutralize_note(note: str) -> str:
    """備註裡出現的「票號：」改成「票號 」，免得備註開頭冒充成票號（或在別的程式裡被誤認）。"""
    return _SPOOF.sub("票號 ", note)
