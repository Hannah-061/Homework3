"""重複判斷與補齊（docs/architecture.md 第 8.5 節）。純比對，不碰檔案。

規則（這是推測性的啟發式規則，可能誤判或漏判，見 architecture 附錄第 5 點）：
- 只看四個關鍵欄位：票券種類、名稱、日期時間、座位或票種。
- 兩邊都有值的欄位，必須相同；有任何一個欄位兩邊都有值卻不同 → 不是重複。
- 某欄位其中一邊是空的，視為「未知」，不當作不同（讓模糊的舊紀錄能被清楚的新照片補齊）。
- 兩邊都有值而且相同的關鍵欄位，至少要有兩個，才判定為重複。
- 日期時間：一邊只有日期、另一邊有日期加時間，而日期相同，算相符。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import replace

from .models import STATUS_ACTIVE, TicketData, TicketRecord
from .ticket_number import join_other_info, split_other_info

MIN_MATCHING_FIELDS = 2
_TEXT_FIELDS = ("ticket_type", "name", "seat_or_type")


def normalize(text: str) -> str:
    """全形半形統一、英文不分大小寫、空白壓成一個。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).casefold().strip()


def _datetimes_agree(a: str, b: str) -> bool:
    a, b = normalize(a), normalize(b)
    if a == b:
        return True
    date_a, date_b = a.split(" ")[0], b.split(" ")[0]
    return date_a == date_b and (" " not in a or " " not in b)


def match_score(existing: TicketData, new: TicketData) -> int | None:
    """回傳相符的關鍵欄位數；有衝突就回傳 None。"""
    matches = 0
    for name in _TEXT_FIELDS:
        old_value, new_value = normalize(getattr(existing, name)), normalize(getattr(new, name))
        if not old_value or not new_value:
            continue
        if old_value != new_value:
            return None
        matches += 1
    if existing.event_datetime and new.event_datetime:
        if not _datetimes_agree(existing.event_datetime, new.event_datetime):
            return None
        matches += 1
    return matches


def find_duplicate(candidates: Iterable[TicketRecord], uploader_id: str, new: TicketData) -> TicketRecord | None:
    """在同一位上傳者、狀態有效的紀錄裡找重複的。找到多筆時，取相符欄位最多、流水號最大的。"""
    best: tuple[int, int, TicketRecord] | None = None
    for record in candidates:
        if record.uploader_id != uploader_id or record.status != STATUS_ACTIVE:
            continue
        score = match_score(record.data, new)
        if score is None or score < MIN_MATCHING_FIELDS:
            continue
        key = (score, record.serial)
        if best is None or key > best[:2]:
            best = (score, record.serial, record)
    return best[2] if best else None


def merge_ticket_data(old: TicketData, new: TicketData) -> TicketData:
    """用新資料補齊舊資料：新資料有值的欄位覆蓋舊的，新資料是空的欄位不會把舊值洗掉。"""
    changes: dict[str, object] = {}
    for name in ("ticket_type", "name", "event_datetime", "location", "seat_or_type", "currency"):
        value = getattr(new, name)
        if value:
            changes[name] = value
    if new.amount is not None:
        changes["amount"] = new.amount
    changes["other_info"] = _merge_other_info(old.other_info, new.other_info)
    return replace(old, **changes)


def _merge_other_info(old: str, new: str) -> str:
    """票號：已經記錄的絕對不動（新照片看不到、或讀成別的，都不會覆蓋或刪除它）；舊的沒有才採用新的。
    備註：描述的是「最新那張照片」（共幾張、幣別是推測、哪些看不清楚），所以用新的；
    新的沒有任何備註（例如這張很清楚）時，舊的過時備註（例如「看不清楚：座位」）也會一併清掉。"""
    old_number, _ = split_other_info(old)
    new_number, new_notes = split_other_info(new)
    return join_other_info(old_number or new_number, new_notes)
