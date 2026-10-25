"""累計怎麼算（docs/architecture.md 第 8.6 節）。純計算，不碰檔案。

- 只算「有效」的紀錄、只算同一位上傳者自己的（呼叫端先篩好上傳者）。
- 當月 = 「記錄時間」落在目前這個月（不是票面上的活動日期）。
- 一筆 = 一張照片：一張照片裡有多張票，也只算一筆（金額是總價）。
- 金額依幣別分開加總，不換算匯率。金額是空的紀錄算進筆數，不算進金額。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from .models import STATUS_ACTIVE, TicketRecord

UNKNOWN_CURRENCY = "幣別不明"


def month_key(moment: datetime) -> str:
    return f"{moment.year:04d}-{moment.month:02d}"


@dataclass(frozen=True)
class PeriodTotals:
    count: int = 0
    amounts: dict[str, float] = field(default_factory=dict)  # 幣別 -> 金額合計
    unknown_amount_count: int = 0  # 有幾筆沒有金額


@dataclass(frozen=True)
class Summary:
    month_label: str  # 'YYYY-MM'
    month: PeriodTotals
    history: PeriodTotals


def compute_totals(records: Iterable[TicketRecord]) -> PeriodTotals:
    count = 0
    unknown = 0
    amounts: dict[str, float] = {}
    for record in records:
        if record.status != STATUS_ACTIVE:
            continue
        count += 1
        if record.data.amount is None:
            unknown += 1
            continue
        currency = record.data.currency or UNKNOWN_CURRENCY
        amounts[currency] = round(amounts.get(currency, 0.0) + record.data.amount, 2)
    return PeriodTotals(count=count, amounts=amounts, unknown_amount_count=unknown)


def summarize(records: Iterable[TicketRecord], now: datetime) -> Summary:
    """records 必須都是同一位上傳者的。"""
    records = list(records)
    label = month_key(now)
    this_month = [r for r in records if month_key(r.recorded_at) == label]
    return Summary(month_label=label, month=compute_totals(this_month), history=compute_totals(records))


def format_amount(value: float) -> str:
    text = f"{value:,.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_amounts(amounts: dict[str, float]) -> str:
    """例如 'JPY 15,000；TWD 3,200'。沒有任何金額時回傳空字串。"""
    order = sorted(amounts, key=lambda currency: (currency == UNKNOWN_CURRENCY, currency))
    return "；".join(f"{currency} {format_amount(amounts[currency])}" for currency in order)
