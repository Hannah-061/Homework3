"""重複判斷、累計、限流、指令解析、回覆組版的測試。全部使用假資料。"""

from __future__ import annotations

from datetime import datetime

import pytest

from ticket_agent import commands, dedupe, replies
from ticket_agent.commands import BadCommand, DeleteCommand, ListCommand, parse_command
from ticket_agent.models import STATUS_ACTIVE, STATUS_DELETED, TicketData, TicketRecord
from ticket_agent.ratelimit import RateLimiter
from ticket_agent.totals import Summary, compute_totals, format_amount, format_amounts, summarize

NOW = datetime(2026, 10, 24, 12, 0, 0)


def data(**overrides) -> TicketData:
    base = dict(ticket_type="演唱會", name="測試演唱會", event_datetime="2099-12-31 19:30", location="假的場館",
                seat_or_type="A 區 12 排", amount=1200.0, currency="TWD", other_info="票號：SECRET-TICKET-9981")
    base.update(overrides)
    return TicketData(**base)


def record(serial=1, uid="1", when=NOW, status=STATUS_ACTIVE, uploader="小明", **overrides) -> TicketRecord:
    return TicketRecord(serial=serial, recorded_at=when, uploader=uploader, uploader_id=uid, data=data(**overrides), status=status)


# ---------- 重複判斷 ----------

def test_identical_tickets_are_duplicates():
    assert dedupe.match_score(data(), data()) == 4


def test_normalization_ignores_case_width_and_spaces():
    assert dedupe.match_score(data(name="Test  Concert"), data(name="ＴＥＳＴ concert")) == 4


def test_two_matching_fields_are_enough_when_others_are_blank():
    blurry = data(seat_or_type="", event_datetime="")
    assert dedupe.match_score(blurry, data()) == 2


def test_one_matching_field_is_not_enough():
    assert dedupe.find_duplicate([record(name="電影", ticket_type="", event_datetime="", seat_or_type="")], "1",
                                 data(ticket_type="", event_datetime="", seat_or_type="")) is None


def test_different_seat_is_a_different_ticket():
    assert dedupe.match_score(data(), data(seat_or_type="B 區 1 排")) is None


def test_different_datetime_is_a_different_ticket():
    assert dedupe.match_score(data(), data(event_datetime="2099-12-31 21:00")) is None


def test_date_only_matches_same_day_with_time():
    assert dedupe.match_score(data(event_datetime="2099-12-31"), data()) == 4
    assert dedupe.match_score(data(event_datetime="2099-12-30"), data()) is None


def test_find_duplicate_ignores_other_users_and_inactive_records():
    records = [record(1, uid="A"), record(2, uid="B", status=STATUS_DELETED)]
    assert dedupe.find_duplicate(records, "B", data()) is None
    assert dedupe.find_duplicate(records, "A", data()).serial == 1


def test_find_duplicate_prefers_most_matches_then_latest():
    weak = record(1, seat_or_type="", event_datetime="")
    strong = record(2)
    newest_strong = record(3)
    assert dedupe.find_duplicate([weak, strong, newest_strong], "1", data()).serial == 3


def test_merge_keeps_old_values_where_new_is_empty():
    old = data(location="", amount=None, currency="")
    new = data(location="新場館", other_info="", seat_or_type="")
    merged = dedupe.merge_ticket_data(old, new)
    assert merged.location == "新場館" and merged.amount == 1200.0 and merged.seat_or_type == "A 區 12 排"


# ---------- 累計 ----------

def test_totals_group_by_currency_and_skip_inactive():
    records = [record(1, amount=3800.0), record(2, amount=15000.0, currency="JPY"), record(3, amount=100.0, status=STATUS_DELETED),
               record(4, amount=None, currency=""), record(5, amount=0.1), record(6, amount=0.2)]
    totals = compute_totals(records)
    assert totals.count == 5 and totals.unknown_amount_count == 1
    assert totals.amounts == {"TWD": 3800.3, "JPY": 15000.0}


def test_amounts_without_currency_are_listed_separately():
    totals = compute_totals([record(amount=50.0, currency="")])
    assert format_amounts(totals.amounts) == "幣別不明 50"


def test_summary_splits_month_and_history_by_recorded_time():
    records = [record(1, when=datetime(2026, 10, 1)), record(2, when=datetime(2026, 9, 30)), record(3, when=datetime(2025, 10, 5))]
    summary = summarize(records, NOW)
    assert summary.month_label == "2026-10" and summary.month.count == 1 and summary.history.count == 3


@pytest.mark.parametrize("value,text", [(3200, "3,200"), (12.5, "12.5"), (0, "0"), (1234567.891, "1,234,567.89"), (10.0, "10")])
def test_format_amount(value, text):
    assert format_amount(value) == text


def test_format_amounts_is_sorted_and_empty_is_blank():
    assert format_amounts({"TWD": 3200.0, "JPY": 15000.0}) == "JPY 15,000；TWD 3,200"
    assert format_amounts({}) == ""


# ---------- 限流 ----------

class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_rate_limiter_allows_n_then_blocks_then_recovers():
    clock = FakeClock()
    limiter = RateLimiter(8, 60, clock)
    assert all(limiter.check("u").allowed for _ in range(8))
    ninth = limiter.check("u")
    assert not ninth.allowed and ninth.notify
    clock.now += 59.9
    assert not limiter.check("u").allowed
    clock.now += 0.2  # 第一批的時間點都滿 60 秒了
    assert limiter.check("u").allowed


def test_rate_limiter_notifies_only_once_per_window():
    clock = FakeClock()
    limiter = RateLimiter(1, 60, clock)
    limiter.check("u")
    assert limiter.check("u").notify is True
    assert limiter.check("u").notify is False
    assert limiter.check("u").notify is False
    clock.now += 61
    limiter.check("u")  # 恢復後又用掉名額
    assert limiter.check("u").notify is True


def test_rate_limiter_is_per_user_and_blocked_attempts_do_not_extend_the_block():
    clock = FakeClock()
    limiter = RateLimiter(2, 60, clock)
    limiter.check("a"); limiter.check("a")
    for _ in range(50):
        assert not limiter.check("a").allowed  # 一直嘗試，也不會讓封鎖延長
    assert limiter.check("b").allowed
    clock.now += 60.1
    assert limiter.check("a").allowed


def test_rate_limiter_forgets_idle_users():
    clock = FakeClock()
    limiter = RateLimiter(1, 60, clock)
    for i in range(100):
        limiter.check(f"user{i}")
    clock.now += 500
    limiter.check("fresh")
    assert len(limiter._accepted) == 1


@pytest.mark.parametrize("limit,window", [(0, 60), (-1, 60), (1, 0), (1, -5)])
def test_rate_limiter_rejects_bad_settings(limit, window):
    with pytest.raises(ValueError):
        RateLimiter(limit, window)


# ---------- 指令解析 ----------

@pytest.mark.parametrize("text,expected", [
    ("!我的票券", ListCommand("month")),
    ("  !我的票券  ", ListCommand("month")),
    ("！我的票券", ListCommand("month")),
    ("!我的票券 全部", ListCommand("all")),
    ("!我的票券 2026-09", ListCommand("2026-09")),
    ("!我的票券 ２０２６-０９", ListCommand("2026-09")),
    ("!刪除 12", DeleteCommand(12)),
    ("!刪除 #12", DeleteCommand(12)),
    ("！刪除　１２", DeleteCommand(12)),
])
def test_valid_commands(text, expected):
    assert parse_command(text) == expected


@pytest.mark.parametrize("text", ["!我的票券 2026-13", "!我的票券 2026-9", "!我的票券 全部 全部", "!我的票券 abc", "!我的票券 ; rm -rf ~"])
def test_bad_list_arguments(text):
    assert parse_command(text) == BadCommand(commands.LIST_USAGE)


@pytest.mark.parametrize("text", ["!刪除", "!刪除 abc", "!刪除 0", "!刪除 -1", "!刪除 1 2", "!刪除 12.5", "!刪除 1234567890", "!刪除 $(whoami)"])
def test_bad_delete_arguments(text):
    assert parse_command(text) == BadCommand(commands.DELETE_USAGE)


@pytest.mark.parametrize("text", ["", "你好", "我的票券", "!help", "!play 歌", "/刪除 1", "請問!刪除 1", None])
def test_other_messages_are_ignored(text):
    assert parse_command(text) is None


# ---------- 回覆 ----------

def summary_for(records):
    return summarize(records, NOW)


def test_ticket_reply_has_fields_and_totals_but_never_the_ticket_number():
    rec = record(12)
    text = replies.format_ticket_reply(rec, summary_for([rec]), ticket_count=2, currency_guessed=True)
    for expected in ("🎫 已記錄 #12", "種類：演唱會", "名稱：測試演唱會", "2099-12-31 19:30", "假的場館", "A 區 12 排",
                     "金額：TWD 1,200（幣別為推測）", "其他資訊：共 2 張", "你的累計", "本月（2026-10）：1 筆，TWD 1,200", "歷史：1 筆"):
        assert expected in text
    assert "SECRET-TICKET-9981" not in text and "票號" not in text


def test_updated_reply_and_unreadable_warning():
    rec = record(3)
    text = replies.format_ticket_reply(rec, summary_for([rec]), updated=True, unreadable=["地點"])
    assert text.startswith("🔄 已更新先前那筆 #3") and "以下欄位看不清楚，已留空：地點" in text


def test_missing_fields_show_dash():
    rec = record(1, location="", amount=None, currency="")
    text = replies.format_ticket_reply(rec, summary_for([rec]))
    assert "地點：—" in text and "金額：—" in text and "金額未知" in text


def test_list_reply_hides_ticket_numbers_and_limits_rows():
    recs = [record(i, name=f"票{i}") for i in range(1, 31)]
    text = replies.format_user_list(recs, summary_for(recs), "2026-10")
    assert "SECRET-TICKET-9981" not in text
    assert "#30 " in text and "#11 " in text and "#10 " not in text
    assert "另有 10 筆較早的沒有列出" in text
    assert "你的累計" in text


def test_empty_list_reply():
    assert "這個期間沒有紀錄" in replies.format_user_list([], summary_for([]), "2026-10")


def test_correction_reply_shows_old_and_new_values_but_not_other_info_content():
    before = record(5)
    after = TicketRecord(**{**before.__dict__, "data": data(amount=1500.0, other_info="票號：NEW-SECRET-0002")})
    text = replies.format_correction(before, after, replies.changed_fields(before.data, after.data), summary_for([after]))
    assert "已更新 #5：" in text and "金額：1,200 → 1,500" in text and "其他資訊：已更新" in text
    assert "SECRET" not in text


def test_changed_fields_lists_only_differences_in_display_order():
    assert replies.changed_fields(data(), data(name="新名稱", amount=1.0)) == ["name", "amount"]


def test_mentions_are_neutralized():
    for hostile in ("@everyone", "@here", "<@123456789012345678>", "<#123456789012345678>", "<@&123456789012345678>"):
        out = replies.neutralize_mentions(hostile)
        assert "@everyone" not in out and "@here" not in out and "<@1" not in out and "<@&1" not in out and "<#1" not in out
    rec = record(1, name="@everyone 快來", uploader="@here")
    text = replies.format_ticket_reply(rec, summary_for([rec]))
    assert "@everyone" not in text and "@here" not in text
    assert "@everyone" not in replies.format_not_ticket("@everyone")


def test_safe_truncates_and_collapses_whitespace():
    assert replies.safe("a\n\n b\t c", 100) == "a b c"
    assert len(replies.safe("x" * 500, 100)) == 100


def test_split_message_respects_limit():
    text = "\n".join(f"行{i} " + "字" * 50 for i in range(200))
    chunks = replies.split_message(text, 500)
    assert all(len(c) <= 500 for c in chunks) and "\n".join(chunks) == text
    assert replies.split_message("短", 500) == ["短"]
    assert [len(c) for c in replies.split_message("x" * 1200, 500)] == [500, 500, 200]
    assert replies.split_message("") == [""]


def test_every_reply_fits_discord_limit_even_with_hostile_input():
    rec = record(1, name="名" * 5000, location="地" * 5000, seat_or_type="座" * 5000, uploader="人" * 5000)
    assert len(replies.format_ticket_reply(rec, summary_for([rec]))) < replies.DISCORD_LIMIT
    assert len(replies.format_not_ticket("貓" * 5000)) < replies.DISCORD_LIMIT


def test_simple_messages():
    assert replies.format_processing(0) == "⏳ 處理中…"
    assert replies.format_processing(2) == "⏳ 處理中…（前面還有 2 張）"
    assert replies.format_not_ticket("一杯咖啡") == "你傳的不是票券，是一杯咖啡"
    assert replies.format_rate_limited() == "傳太快了，請稍後再試"
    assert replies.format_failure() == "處理失敗，請稍後再試"
    assert "10 MB" in replies.format_unsupported_attachment(10)
    assert "只有原上傳者" in replies.format_not_owner() and replies.format_not_found() == "找不到這一筆"
    assert "60 秒" in replies.format_delete_confirm(record(7)) and "#7" in replies.format_delete_confirm(record(7))
    assert "SECRET" not in replies.format_delete_confirm(record(7))
    rec = record(7)
    assert "已刪除 #7" in replies.format_deleted(rec, summary_for([]))
    assert "不是票券" in replies.format_marked_not_ticket(rec, summary_for([]))
    assert "金額應該是 1200" in replies.format_correction_unclear()


# ================================================================== !說明 指令

from ticket_agent.commands import COMMAND_WORDS, HELP_USAGE, HelpCommand  # noqa: E402
from ticket_agent.excel_store import EDITABLE_FIELDS  # noqa: E402


@pytest.mark.parametrize("text", ["!說明", "  !說明  ", "！說明", "!說明\n", "\t!說明"])
def test_help_command_is_recognized(text):
    assert parse_command(text) == HelpCommand()


@pytest.mark.parametrize("text", ["!說明 abc", "!說明 刪除", "!說明 1", "!說明 ; rm -rf ~", "!說明 !我的票券", "！說明　全部"])
def test_help_with_extra_arguments_gets_the_usage_hint_like_the_other_commands(text):
    assert parse_command(text) == BadCommand(HELP_USAGE)
    assert "!說明" in HELP_USAGE


@pytest.mark.parametrize("text", ["!help", "!HELP", "!幫助", "!說明書", "!說 明", "說明", "!!說明", "請問 !說明", "/說明"])
def test_no_aliases_and_nothing_else_is_mistaken_for_help(text):
    assert parse_command(text) is None  # 只支援 !說明，不新增 !help 之類的別名


def test_existing_commands_parse_exactly_as_before():
    assert parse_command("!我的票券") == ListCommand("month")
    assert parse_command("!我的票券 全部") == ListCommand("all")
    assert parse_command("!我的票券 2026-09") == ListCommand("2026-09")
    assert parse_command("!刪除 12") == DeleteCommand(12)
    assert parse_command("!刪除 #12") == DeleteCommand(12)
    assert parse_command("!刪除") == BadCommand(commands.DELETE_USAGE)
    assert parse_command("!我的票券 2026-13") == BadCommand(commands.LIST_USAGE)


# ---------- 說明文字的內容

import re as _re  # noqa: E402

HELP_DEFAULTS = dict(max_mb=10, max_attachments=5, confirm_timeout_s=60, rate_limit_per_min=8)


def help_text(**overrides) -> str:
    return replies.format_help(**{**HELP_DEFAULTS, **overrides})


def test_help_mentions_every_supported_feature_the_user_asked_for():
    text = help_text()
    for needle in ("!我的票券", "!我的票券 2026-09", "!我的票券 全部", "!刪除 編號", "!說明", "確定", "圖片或 PDF", "回覆", "辨識結果", "這不是票券"):
        assert needle in text, needle


def test_help_warns_clearly_that_delete_only_marks_the_record():
    text = help_text()
    assert "標記為「已刪除」" in text
    assert "不會刪除 Excel 的資料列" in text and "不會刪除本機的照片" in text


def test_help_correctable_fields_are_exactly_the_real_whitelist():
    text = help_text()
    section = text.split("【更正】")[1].split("【刪除】")[0]
    for name in EDITABLE_FIELDS:
        assert replies.FIELD_LABELS[name] in section, name
    assert "其他資訊" not in text  # 其他資訊不能更正，不可以寫成可以更正
    assert "票號不能在 Discord 更正" in section


def test_help_does_not_claim_features_that_do_not_exist():
    text = help_text()
    for word in ("復原", "還原", "匯出", "下載紀錄", "修改票號", "改票號", "重新辨識", "管理員", "所有人的紀錄", "別人的紀錄可以", "!help", "網頁", "http"):
        assert word not in text, word


def test_help_numbers_come_from_the_settings_not_from_constants():
    text = help_text(max_mb=20, max_attachments=3, confirm_timeout_s=90, rate_limit_per_min=3)
    assert "20 MB" in text and "3 張" in text and "90 秒" in text and "每分鐘最多 3 次" in text
    for stale in ("10 MB", "5 張", "60 秒", "8 次"):
        assert stale not in text, stale


def test_help_fits_in_one_discord_message_and_cannot_ping_anyone():
    text = help_text()
    assert len(text) < 1900 < replies.DISCORD_LIMIT
    assert "@" not in text and "<#" not in text


def test_every_command_in_the_help_text_is_really_recognized_by_the_parser():
    """說明裡寫的每個 `!指令` 都必須是真的；反過來，解析器認得的每個指令也都必須出現在說明裡。"""
    text = help_text()
    examples = _re.findall(r"`(![^`]+)`", text)
    assert examples, "說明裡應該有指令範例"
    for example in examples:
        if "編號" in example:  # 用法範例裡的占位文字，不是真正可以輸入的指令
            continue
        parsed = parse_command(example)
        assert parsed is not None and not isinstance(parsed, BadCommand), f"說明裡的範例不能用：{example}"
    mentioned = {_re.match(r"!(\S+)", example).group(1) for example in examples}
    assert mentioned == set(COMMAND_WORDS)


def test_the_help_text_agrees_with_the_real_defaults():
    from ticket_agent.config import OPTIONAL_INT_KEYS
    from ticket_agent.intake import RouterSettings
    settings = RouterSettings(channel_id=1, max_attachment_bytes=OPTIONAL_INT_KEYS["MAX_ATTACHMENT_MB"][0] * 1024 * 1024, claude_retries=2)
    text = replies.format_help(max_mb=settings.max_attachment_bytes // (1024 * 1024), max_attachments=settings.max_attachments_per_message,
                               confirm_timeout_s=settings.confirm_timeout_s, rate_limit_per_min=OPTIONAL_INT_KEYS["RATE_LIMIT_PER_MIN"][0])
    assert "10 MB" in text and "5 張" in text and "60 秒" in text and "每分鐘最多 8 次" in text
