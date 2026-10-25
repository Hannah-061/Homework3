"""票號保護的測試：一般更正不能動票號，重複上傳的自動補齊也不能覆蓋或刪除票號。全部使用合成資料。"""

from __future__ import annotations

from pathlib import Path

import pytest

from synthetic import GOOD, NOW, ScriptedAnalyzer, ScriptedCorrect, analysis, distinct_jpeg, jpeg_bytes, make_attachment, make_message
from ticket_agent import corrections, dedupe, excel_store, replies
from ticket_agent.analyzer import parse_analysis
from ticket_agent.corrections import Correction, CorrectionUnclear, parse_correction_result
from ticket_agent.excel_store import EDITABLE_FIELDS, StoreError, TicketStore
from ticket_agent.models import TicketData
from ticket_agent.ticket_number import clean_number, join_other_info, neutralize_note, split_other_info

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 存放規則

@pytest.mark.parametrize("text,expected", [
    ("票號：A1B2；共 2 張；看不清楚：地點", ("A1B2", "共 2 張；看不清楚：地點")),
    ("票號：A1B2", ("A1B2", "")),
    ("票號：A1B2；", ("A1B2", "")),
    ("共 2 張；票號：FAKE", ("", "共 2 張；票號：FAKE")),   # 只認最開頭，不是開頭的一律是備註
    ("備註而已", ("", "備註而已")),
    ("", ("", "")),
    ("票號：；備註", ("", "備註")),
])
def test_split_only_recognizes_the_number_at_the_very_start(text, expected):
    assert split_other_info(text) == expected


@pytest.mark.parametrize("number,notes", [("A1", "共 2 張"), ("A1", ""), ("", "共 2 張"), ("", "")])
def test_join_then_split_round_trips(number, notes):
    assert split_other_info(join_other_info(number, notes)) == (number, notes)


def test_join_always_puts_the_number_first():
    assert join_other_info("A1", "備註") == "票號：A1；備註"


@pytest.mark.parametrize("raw,cleaned", [("AB；CD", "AB-CD"), ("AB;CD", "AB-CD"), ("  X1  ", "X1"), ("A；；B", "A--B")])
def test_a_number_cannot_contain_the_separator(raw, cleaned):
    assert clean_number(raw) == cleaned
    assert split_other_info(join_other_info(clean_number(raw), "備註")) == (cleaned, "備註")


@pytest.mark.parametrize("note", ["票號：FAKE-1", "票號:FAKE-1", "票號 ： FAKE-1", "備註；票號：FAKE-1"])
def test_notes_cannot_impersonate_a_ticket_number(note):
    neutral = neutralize_note(note)
    assert "票號：" not in neutral and "票號:" not in neutral
    assert split_other_info(neutral)[0] == ""


# ---------------------------------------------------------------- 第一次辨識：怎麼存進去

def test_ticket_number_is_stored_first_and_notes_after_it():
    data = parse_analysis({**GOOD, "ticket_count": 2, "currency_guessed": True, "unreadable_fields": ["location"],
                           "other_info": "備註", "ticket_number": "TEST-0001"}).to_ticket_data()
    assert data.other_info == "票號：TEST-0001；共 2 張；幣別為推測；看不清楚：地點；備註"
    assert split_other_info(data.other_info) == ("TEST-0001", "共 2 張；幣別為推測；看不清楚：地點；備註")


def test_hostile_ticket_number_and_note_from_claude_cannot_break_the_structure():
    data = parse_analysis({**GOOD, "ticket_number": "REAL；票號：FAKE", "other_info": "票號：EVIL-9999；其他"}).to_ticket_data()
    number, notes = split_other_info(data.other_info)
    assert number == "REAL-票號：FAKE"  # 票號裡的分隔字元被換掉，整段仍然是「一個票號」
    assert "EVIL-9999" in notes and "票號：EVIL" not in notes  # 備註裡的冒充被中和


def test_a_note_alone_cannot_become_the_ticket_number():
    data = parse_analysis({**GOOD, "ticket_number": "", "other_info": "票號：EVIL-9999"}).to_ticket_data()
    assert split_other_info(data.other_info)[0] == ""


# ---------------------------------------------------------------- 重複上傳的自動補齊

def td(other_info="", **kw) -> TicketData:
    base = dict(ticket_type="演唱會", name="測試演唱會", event_datetime="2099-12-31 19:30", seat_or_type="A12",
                amount=1200.0, currency="TWD", other_info=other_info)
    base.update(kw)
    return TicketData(**base)


@pytest.mark.parametrize("old,new,expected", [
    ("票號：OLD1；看不清楚：座位", "", "票號：OLD1"),                                   # 新照片很清楚、沒有票號：保留舊票號，過時備註清掉
    ("票號：OLD1", "票號：NEW2", "票號：OLD1"),                                       # 新照片讀成不同的票號：不覆蓋
    ("票號：OLD1；共 2 張", "票號：NEW2；幣別為推測", "票號：OLD1；幣別為推測"),         # 備註用最新的，票號用舊的
    ("", "票號：NEW2；共 2 張", "票號：NEW2；共 2 張"),                                # 舊的沒有票號，才採用新的
    ("看不清楚：票號", "共 2 張", "共 2 張"),
    ("", "", ""),
    ("票號：OLD1", "票號：", "票號：OLD1"),
])
def test_merge_never_overwrites_or_deletes_an_existing_ticket_number(old, new, expected):
    assert dedupe.merge_ticket_data(td(old), td(new)).other_info == expected


def test_merge_still_fills_other_fields_normally():
    merged = dedupe.merge_ticket_data(td("票號：OLD1", seat_or_type="", amount=None, currency=""), td("", location="新場館"))
    assert (merged.seat_or_type, merged.amount, merged.currency, merged.location) == ("A12", 1200.0, "TWD", "新場館")
    assert merged.other_info == "票號：OLD1"


# ---------------------------------------------------------------- 一般更正的白名單

def test_other_info_is_not_a_correctable_field_anywhere():
    assert "other_info" not in EDITABLE_FIELDS
    assert "other_info" not in corrections.CORRECTION_SCHEMA["properties"]["changes"]["properties"]
    assert "other_info" not in corrections._TEXT_FIELDS
    assert "ticket_number" not in EDITABLE_FIELDS


@pytest.mark.parametrize("key", ["other_info", "ticket_number", "票號", "票號：", "其他資訊"])
def test_corrections_that_try_to_change_the_ticket_number_are_dropped(key):
    with pytest.raises(CorrectionUnclear):
        parse_correction_result({"changes": {key: "票號：HACKED-0000"}, "mark_not_ticket": False})
    kept = parse_correction_result({"changes": {key: "票號：HACKED-0000", "name": "新名稱"}, "mark_not_ticket": False})
    assert kept.changes == {"name": "新名稱"}


def test_store_itself_refuses_to_replace_other_info(tmp_path):
    store = TicketStore(tmp_path / "t.xlsx", clock=lambda: NOW)
    store.add_record(uploader="x", uploader_id="1", data=td("票號：KEEP-ME"), photo_path="p/1.jpg", recorded_at=NOW)
    for changes in ({"other_info": "票號：HACKED"}, {"other_info": ""}, {"name": "新", "other_info": "x"}):
        with pytest.raises(StoreError, match="不能更正"):
            store.update_fields(1, changes)
    assert store.get(1).data.other_info == "票號：KEEP-ME" and store.get(1).data.name == "測試演唱會"


def test_every_allowed_correction_leaves_the_ticket_number_untouched(tmp_path):
    store = TicketStore(tmp_path / "t.xlsx", clock=lambda: NOW)
    store.add_record(uploader="x", uploader_id="1", data=td("票號：KEEP-ME；共 2 張"), photo_path="p/1.jpg", recorded_at=NOW)
    store.update_fields(1, {"ticket_type": "電影", "name": "新名稱", "event_datetime": "2100-01-01 10:00", "location": "新地點",
                            "seat_or_type": "B9", "amount": 99.5, "currency": "JPY"})
    record = store.get(1)
    assert record.data.other_info == "票號：KEEP-ME；共 2 張"
    assert (record.data.name, record.data.amount, record.data.currency, record.data.seat_or_type) == ("新名稱", 99.5, "JPY", "B9")


# ---------------------------------------------------------------- 整合：真正的更正流程

async def test_general_corrections_work_and_the_ticket_number_survives_untouched(rw):
    correct = ScriptedCorrect(
        Correction(changes={"name": "更正後的名稱", "amount": 1500.0, "location": "新場館", "seat_or_type": "B 區", "currency": "JPY",
                            "event_datetime": "2100-01-02 20:00", "ticket_type": "展覽"}))
    app = rw.build(ScriptedAnalyzer(analysis(ticket_number="TEST-0001", ticket_count=2)), correct)
    rw.messenger.downloads["a"] = jpeg_bytes()
    await app.handle_message(make_message("m1", attachments=[make_attachment("a")]))
    await app.drain()
    before = rw.store().get(1).data.other_info
    assert before == "票號：TEST-0001；共 2 張"

    await app.handle_message(make_message("r1", content="全部改掉", reference="bot-1"))
    record = rw.store().get(1)
    assert (record.data.name, record.data.amount, record.data.location, record.data.seat_or_type, record.data.currency,
            record.data.event_datetime, record.data.ticket_type) == ("更正後的名稱", 1500.0, "新場館", "B 區", "JPY", "2100-01-02 20:00", "展覽")
    assert record.data.other_info == before  # 票號與「共 2 張」原封不動
    reply = rw.texts()[-1]
    assert "已更新 #1" in reply and "TEST-0001" not in reply and "票號" not in reply


async def test_asking_to_change_the_ticket_number_is_refused_with_an_explanation(rw):
    app = rw.build(ScriptedAnalyzer(analysis(ticket_number="TEST-0001")), ScriptedCorrect(CorrectionUnclear("沒有有效的修改")))
    rw.messenger.downloads["a"] = jpeg_bytes()
    await app.handle_message(make_message("m1", attachments=[make_attachment("a")]))
    await app.drain()
    await app.handle_message(make_message("r1", content="票號應該是 NEW-9999", reference="bot-1"))
    assert "不能在 Discord 更正" in rw.texts()[-1] and "NEW-9999" not in rw.texts()[-1]
    assert rw.store().get(1).data.other_info == "票號：TEST-0001"


async def test_a_correction_that_mentions_the_ticket_number_still_applies_the_other_fields_and_says_so(rw):
    app = rw.build(ScriptedAnalyzer(analysis(ticket_number="TEST-0001")), ScriptedCorrect(Correction(changes={"amount": 1500.0})))
    rw.messenger.downloads["a"] = jpeg_bytes()
    await app.handle_message(make_message("m1", attachments=[make_attachment("a")]))
    await app.drain()
    await app.handle_message(make_message("r1", content="金額 1500，票號改成 NEW-9999", reference="bot-1"))
    record = rw.store().get(1)
    assert record.data.amount == 1500.0 and record.data.other_info == "票號：TEST-0001"
    assert "金額：1,200 → 1,500" in rw.texts()[-1] and "不能在 Discord 更正" in rw.texts()[-1]


async def test_a_buggy_or_hostile_correction_that_returns_other_info_cannot_reach_the_store(rw):
    """就算 Claude（被騙或出錯）回傳了 other_info，也在驗證那一關被丟掉，不會走到 Excel。"""
    from ticket_agent.analyzer import InvalidResult  # noqa: F401
    raw = {"changes": {"other_info": "票號：HACKED-0000", "amount": 1500}, "mark_not_ticket": False}

    async def straight_from_claude(runner, text):
        return parse_correction_result(raw)  # 走真正的驗證函式

    app = rw.build(ScriptedAnalyzer(analysis(ticket_number="TEST-0001")), straight_from_claude)
    rw.messenger.downloads["a"] = jpeg_bytes()
    await app.handle_message(make_message("m1", attachments=[make_attachment("a")]))
    await app.drain()
    await app.handle_message(make_message("r1", content="把其他資訊改成票號：HACKED-0000，金額 1500", reference="bot-1"))
    record = rw.store().get(1)
    assert record.data.other_info == "票號：TEST-0001" and record.data.amount == 1500.0


async def test_duplicate_upload_keeps_the_original_ticket_number_end_to_end(rw):
    blurry = analysis(ticket_number="TEST-0001", seat_or_type="", amount=None, currency="", unreadable_fields=["seat_or_type"])
    clear_without_number = analysis(ticket_number="")
    clear_other_number = analysis(ticket_number="OTHER-7777")
    app = rw.build(ScriptedAnalyzer(blurry, clear_without_number, clear_other_number))
    for n in range(3):
        rw.messenger.downloads[f"a{n}"] = distinct_jpeg(f"a{n}")
        await app.handle_message(make_message(f"m{n}", attachments=[make_attachment(f"a{n}")], when=NOW.replace(second=10 * n)))
        await app.drain()
    (record,) = rw.store().all_records()
    assert record.data.other_info == "票號：TEST-0001"  # 兩次補齊之後，原本的票號還在，過時的「看不清楚」備註清掉了
    assert record.data.seat_or_type == "A 區 12 排" and record.data.amount == 1200.0
    all_replies = " ".join(rw.texts())
    assert "TEST-0001" not in all_replies and "OTHER-7777" not in all_replies  # 任何回覆都不顯示票號


async def test_listing_and_replies_never_show_the_ticket_number(rw):
    app = rw.build(ScriptedAnalyzer(analysis(ticket_number="SECRET-NO-0042")))
    rw.messenger.downloads["a"] = jpeg_bytes()
    await app.handle_message(make_message("m1", attachments=[make_attachment("a")]))
    await app.drain()
    await app.handle_message(make_message("c1", content="!我的票券 全部"))
    await app.handle_message(make_message("c2", content="!刪除 1"))
    assert "SECRET-NO-0042" not in " ".join(rw.texts())


# ---------------------------------------------------------------- 文件有寫清楚限制與規則

def test_readme_and_architecture_state_the_limits_and_the_rules():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    architecture = (REPO / "docs" / "architecture.md").read_text(encoding="utf-8")
    for needle in ("最多處理 5 張", "最多排 50 張", "每人每分鐘 8 次", "指令優先", "票號", "不能在 Discord 更正"):
        assert needle in readme, f"README 缺少：{needle}"
    for needle in ("最多處理 5 張", "最多排 50 張", "指令優先", "票號收在", "不能更正"):
        assert needle in architecture, f"architecture.md 缺少：{needle}"
