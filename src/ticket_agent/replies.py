"""回覆文字的組版（docs/architecture.md 第 10 節）。純字串處理，不碰 Discord。

安全規則：
- 回覆裡所有來自外部的文字（名稱、地點、上傳者…）都先壓掉 @提及，並限制長度。
- 回覆與查詢**不顯示票號**，也不顯示自由文字的「其他資訊」（可能含票號），
  只顯示程式自己產生的註記：共 N 張、幣別為推測、看不清楚的欄位。
- 單則訊息不超過 Discord 的 2000 字上限。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .commands import DELETE_COMMAND, HELP_COMMAND, LIST_COMMAND
from .excel_store import EDITABLE_FIELDS
from .models import TicketData, TicketRecord
from .totals import PeriodTotals, Summary, format_amount, format_amounts

DISCORD_LIMIT = 2000
SAFE_LIMIT = 1900
LIST_MAX_ROWS = 20
FIELD_LABELS = {
    "ticket_type": "票券種類", "name": "名稱", "event_datetime": "日期時間", "location": "地點",
    "seat_or_type": "座位或票種", "amount": "金額", "currency": "幣別", "other_info": "其他資訊",
}


def neutralize_mentions(text: str) -> str:
    """讓 @everyone、@某人、<@123>、<#123> 都不會真的標記到人。"""
    return text.replace("@", "@​").replace("<#", "<​#")


def safe(text: str, max_len: int = 100) -> str:
    text = " ".join(text.split())
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return neutralize_mentions(text)


def split_message(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """把太長的訊息在換行處切成多則，每則不超過 limit。"""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current or not chunks:
        chunks.append(current)
    return chunks


def _money(data: TicketData) -> str:
    if data.amount is None:
        return "—"
    return f"{data.currency + ' ' if data.currency else ''}{format_amount(data.amount)}"


def _period_line(label: str, totals: PeriodTotals) -> str:
    amounts = format_amounts(totals.amounts) or "金額未知"
    line = f" {label}：{totals.count} 筆，{amounts}"
    if totals.unknown_amount_count and totals.amounts:
        line += f"（另有 {totals.unknown_amount_count} 筆金額未知）"
    return line


def format_totals(summary: Summary) -> str:
    return "\n".join([
        "你的累計",
        _period_line(f"本月（{summary.month_label}）", summary.month),
        _period_line("歷史", summary.history),
    ])


def format_processing(ahead: int) -> str:
    return "⏳ 處理中…" if ahead <= 0 else f"⏳ 處理中…（前面還有 {ahead} 張）"


def format_failure() -> str:
    return "處理失敗，請稍後再試"


def format_rate_limited() -> str:
    return "傳太快了，請稍後再試"


def format_unsupported_attachment(max_mb: int) -> str:
    return f"只支援 JPG、PNG、WebP、PDF，且每個檔案要在 {max_mb} MB 以內"


def format_not_ticket(description: str) -> str:
    return f"你傳的不是票券，是{safe(description)}"


def format_ticket_reply(
    record: TicketRecord,
    summary: Summary,
    *,
    updated: bool = False,
    ticket_count: int = 1,
    currency_guessed: bool = False,
    unreadable: Sequence[str] = (),
) -> str:
    """辨識成功（新增或補齊舊的一筆）後的回覆。"""
    d = record.data
    header = f"🔄 已更新先前那筆 #{record.serial}" if updated else f"🎫 已記錄 #{record.serial}"
    money = _money(d) + ("（幣別為推測）" if currency_guessed and d.currency and d.amount is not None else "")
    lines = [
        header,
        f"種類：{safe(d.ticket_type) or '—'}",
        f"名稱：{safe(d.name) or '—'}",
        f"日期時間：{safe(d.event_datetime) or '—'}",
        f"地點：{safe(d.location) or '—'}",
        f"座位／票種：{safe(d.seat_or_type) or '—'}",
        f"金額：{money}",
    ]
    if ticket_count > 1:
        lines.append(f"其他資訊：共 {ticket_count} 張")
    if unreadable:
        names = "、".join(safe(name, 20) for name in unreadable)
        lines.append(f"⚠️ 以下欄位看不清楚，已留空：{names}（可以重傳清楚的照片，我會補齊）")
    lines += ["", format_totals(summary)]
    return "\n".join(lines)


def _record_line(record: TicketRecord) -> str:
    d = record.data
    parts = [safe(d.name or d.ticket_type or "（沒有名稱）", 40), safe(d.event_datetime, 16), _money(d)]
    return f"#{record.serial} " + "｜".join(p for p in parts if p)


def format_user_list(records: Sequence[TicketRecord], summary: Summary, period_label: str) -> str:
    """`!我的票券` 的回覆。不含票號。"""
    lines = [f"你的票券（{period_label}）"]
    if not records:
        lines.append("這個期間沒有紀錄")
    for record in records[-LIST_MAX_ROWS:]:
        lines.append(_record_line(record))
    if len(records) > LIST_MAX_ROWS:
        lines.append(f"…另有 {len(records) - LIST_MAX_ROWS} 筆較早的沒有列出")
    lines += ["", format_totals(summary)]
    return "\n".join(lines)


def format_delete_confirm(record: TicketRecord, timeout_s: int = 60) -> str:
    name = safe(record.data.name or record.data.ticket_type or "（沒有名稱）", 40)
    return f"確定要刪除 #{record.serial}「{name}」嗎？請在 {timeout_s} 秒內回覆這則訊息並輸入「確定」，或直接在這個頻道傳送「確定」。"


def format_deleted(record: TicketRecord, summary: Summary) -> str:
    return f"已刪除 #{record.serial}（照片保留在本機）\n\n{format_totals(summary)}"


def format_help(*, max_mb: int, max_attachments: int, confirm_timeout_s: int, rate_limit_per_min: int) -> str:
    """`!說明` 的回覆。只描述目前真的有的功能；數字（大小、張數、秒數、次數）由呼叫端從實際設定帶入，不寫死。
    可更正的欄位名稱直接取自真正的白名單（EDITABLE_FIELDS），不會多寫或少寫。"""
    correctable = "、".join(FIELD_LABELS[name] for name in EDITABLE_FIELDS)
    return "\n".join([
        "📖 票券紀錄 Bot 說明",
        "",
        "【記錄票券】",
        f"直接在這個頻道傳票券的圖片或 PDF（JPG、PNG、WebP、PDF，每個檔案 {max_mb} MB 以內，"
        f"一則訊息最多 {max_attachments} 張；PDF 只處理第一頁）。我會辨識並記錄，回覆結果和你自己的累計。",
        "",
        "【查詢】",
        f"`!{LIST_COMMAND}`　查看本月的紀錄",
        f"`!{LIST_COMMAND} 2026-09`　查看指定月份的紀錄（格式 YYYY-MM）",
        f"`!{LIST_COMMAND} 全部`　查看所有紀錄",
        "",
        "【更正】",
        f"用 Discord 的「回覆」功能回覆我的辨識結果訊息，用文字說要改什麼，例如「金額應該是 1500」。"
        f"可以更正：{correctable}；也可以說「這不是票券」把這筆撤回。票號不能在 Discord 更正。",
        "",
        "【刪除】",
        f"`!{DELETE_COMMAND} 編號`　例如 `!{DELETE_COMMAND} 12`。我會先請你確認，請在 {confirm_timeout_s} 秒內完成，有兩種方式：",
        "① 用「回覆」功能回覆我的確認訊息並輸入「確定」；",
        "② 直接在這個頻道傳送「確定」（只有一筆等待確認時才可以）。",
        "如果你同時有好幾筆等待確認的刪除，我不會猜是哪一筆，請用①回覆你要確認的那則訊息。",
        "⚠️ 刪除只會把這筆紀錄標記為「已刪除」（不再計入累計），不會刪除 Excel 的資料列，也不會刪除本機的照片。",
        "",
        "【其他】",
        f"`!{HELP_COMMAND}`　顯示這份說明",
        f"只能查看與修改自己的紀錄。每人每分鐘最多 {rate_limit_per_min} 次操作（每張照片、每次更正、每次查詢或刪除指令各算一次）。",
    ])


def format_busy() -> str:
    return "目前排隊的照片太多，請稍後再傳"


def format_too_many_attachments(limit: int) -> str:
    return f"一則訊息最多處理 {limit} 張，多的沒有處理"


def format_no_change() -> str:
    return "你提供的內容和目前的紀錄一樣，沒有更新"


def format_ticket_number_notice() -> str:
    return "ℹ️ 票號為了安全，不能在 Discord 更正；需要的話請直接編輯 Excel 檔。"


def format_confirm_ambiguous() -> str:
    return "你有多筆等待確認的刪除，沒辦法判斷是哪一筆。請回覆你要確認的那則確認訊息，並輸入「確定」。"


def format_confirm_expired() -> str:
    return "確認已逾時，沒有刪除。要刪除請重新輸入 `!刪除 編號`"


def format_not_found() -> str:
    return "找不到這一筆"


def format_not_owner() -> str:
    return "只有原上傳者能更正這一筆"


def format_correction_unclear() -> str:
    return "我看不懂要改什麼，請再說一次，例如：金額應該是 1200"


def _change_value(field_name: str, data: TicketData) -> str:
    if field_name == "amount":
        return "—" if data.amount is None else format_amount(data.amount)
    return safe(str(getattr(data, field_name)), 50) or "—"


def format_correction(before: TicketRecord, after: TicketRecord, changed: Sequence[str], summary: Summary) -> str:
    """更正成功：列出「舊值 → 新值」，不含票號（其他資訊的內容不顯示）。"""
    lines = [f"已更新 #{after.serial}："]
    for name in changed:
        label = FIELD_LABELS[name]
        if name == "other_info":
            lines.append(f" {label}：已更新")
        else:
            lines.append(f" {label}：{_change_value(name, before.data)} → {_change_value(name, after.data)}")
    lines += ["", format_totals(summary)]
    return "\n".join(lines)


def format_marked_not_ticket(record: TicketRecord, summary: Summary) -> str:
    return f"已將 #{record.serial} 更正為「不是票券」並移除（照片保留在本機）\n\n{format_totals(summary)}"


def changed_fields(before: TicketData, after: TicketData) -> list[str]:
    return [name for name in FIELD_LABELS if getattr(before, name) != getattr(after, name)]


def describe_changes(changes: Mapping[str, object]) -> list[str]:
    return [name for name in FIELD_LABELS if name in changes]
