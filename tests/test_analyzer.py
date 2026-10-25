"""辨識結果的解析與驗證測試：壞資料都不可以讓程式當掉，只能丟 InvalidResult。全部使用假資料。"""

from __future__ import annotations

import json

import pytest

from ticket_agent.analyzer import JSON_SCHEMA, InvalidResult, extract_json_object, parse_analysis


def good(**overrides) -> dict:
    data = {
        "is_ticket": True, "not_ticket_description": "", "ticket_type": "演唱會", "name": "測試演唱會 2099",
        "event_datetime": "2099-12-31 19:30", "location": "假的場館", "seat_or_type": "A 區 12 排", "amount": 1200,
        "currency": "TWD", "currency_guessed": False, "ticket_count": 1, "ticket_number": "TEST-0001",
        "other_info": "", "unreadable_fields": [],
    }
    data.update(overrides)
    return data


# ---------- 取出 JSON ----------

@pytest.mark.parametrize("text", [
    '{"a": 1}',
    '  {"a": 1}\n',
    '```json\n{"a": 1}\n```',
    '```\n{"a": 1}\n```',
    '好的，結果如下：\n{"a": 1}\n以上。',
    'prefix {"a": 1} suffix',
])
def test_extract_json_object_handles_common_wrappers(text):
    assert extract_json_object(text) == {"a": 1}


@pytest.mark.parametrize("text", ["", "沒有 JSON", "{壞掉的", "[1, 2, 3]", '"字串"', "null", "{", "}{", "[" * 100000, "{" * 100000])
def test_extract_json_object_returns_none_for_garbage(text):
    assert extract_json_object(text) is None


# ---------- 正常情況 ----------

def test_valid_ticket_from_dict_and_from_text():
    from_dict = parse_analysis(good())
    from_text = parse_analysis("```json\n" + json.dumps(good(), ensure_ascii=False) + "\n```")
    assert from_dict == from_text
    assert from_dict.is_ticket and from_dict.name == "測試演唱會 2099" and from_dict.amount == 1200.0
    assert from_dict.currency == "TWD" and from_dict.ticket_count == 1


def test_non_ticket_needs_only_two_fields():
    result = parse_analysis({"is_ticket": False, "not_ticket_description": "一杯咖啡"})
    assert not result.is_ticket and result.not_ticket_description == "一杯咖啡"


def test_is_ticket_accepts_true_false_strings():
    assert parse_analysis(good(is_ticket="true")).is_ticket
    assert not parse_analysis({"is_ticket": "FALSE", "not_ticket_description": "貓"}).is_ticket


def test_unknown_extra_fields_are_ignored():
    result = parse_analysis(good(extra="惡意", __proto__={"x": 1}))
    assert not hasattr(result, "extra")


def test_schema_requires_every_field_and_forbids_extras():
    assert set(JSON_SCHEMA["required"]) == set(JSON_SCHEMA["properties"])
    assert JSON_SCHEMA["additionalProperties"] is False


# ---------- 壞資料：一律 InvalidResult，不當掉 ----------

@pytest.mark.parametrize("raw", [
    None, "", "不是 JSON", "[1]", 42, [], {}, {"name": "沒有 is_ticket"},
    {"is_ticket": "也許"}, {"is_ticket": None}, {"is_ticket": 1}, {"is_ticket": []},
    {"is_ticket": False},  # 缺 not_ticket_description
    {"is_ticket": False, "not_ticket_description": ""},
    {"is_ticket": False, "not_ticket_description": "   "},
    {"is_ticket": False, "not_ticket_description": ["x"]},
])
def test_garbage_raises_invalid_result(raw):
    with pytest.raises(InvalidResult):
        parse_analysis(raw)


def test_missing_ticket_fields_are_rejected():
    for key in list(good()):
        if key in ("is_ticket",):
            continue
        broken = good()
        del broken[key]
        with pytest.raises(InvalidResult, match=key):
            parse_analysis(broken)


def test_ticket_needs_a_name_or_a_type():
    with pytest.raises(InvalidResult):
        parse_analysis(good(name="", ticket_type=""))
    assert parse_analysis(good(name="", ticket_type="電影")).ticket_type == "電影"
    assert parse_analysis(good(name="某場次", ticket_type="")).name == "某場次"


@pytest.mark.parametrize("key", ["name", "ticket_type", "location", "seat_or_type", "other_info", "ticket_number", "currency"])
@pytest.mark.parametrize("bad", [["a"], {"a": 1}, True])
def test_non_text_in_text_fields_is_rejected(key, bad):
    with pytest.raises(InvalidResult):
        parse_analysis(good(**{key: bad}))


def test_numbers_in_text_fields_become_text():
    assert parse_analysis(good(name=2099)).name == "2099"


# ---------- 金額 ----------

@pytest.mark.parametrize("value,expected", [
    (1200, 1200.0), (0, 0.0), (12.5, 12.5), ("1200", 1200.0), ("1,200", 1200.0), (" 3,800.50 ", 3800.5), (None, None),
])
def test_amount_accepts_reasonable_values(value, expected):
    result = parse_analysis(good(amount=value))
    assert result.amount == expected
    assert "金額" not in result.unreadable_fields


@pytest.mark.parametrize("value", ["一千二", "NT$ 一千二", "NT$1200", "12.3.4", "", "abc", -5, "-5", float("nan"),
                                    float("inf"), 10**12, True, False, [1], {"a": 1}])
def test_unreasonable_amount_becomes_none_and_is_flagged(value):
    result = parse_analysis(good(amount=value))
    assert result.amount is None
    assert "金額" in result.unreadable_fields


# ---------- 幣別、日期、張數、看不清楚 ----------

@pytest.mark.parametrize("value,expected", [("twd", "TWD"), (" jpy ", "JPY"), ("TWD", "TWD"), ("NT$", ""), ("US", ""),
                                             ("USDX", ""), ("台幣", ""), ("12A", ""), ("", "")])
def test_currency_must_be_three_letters(value, expected):
    assert parse_analysis(good(currency=value)).currency == expected


@pytest.mark.parametrize("value,kept", [
    ("2099-12-31 19:30", True), ("2099-12-31", True), ("2099-02-30", False), ("2099-13-01", False),
    ("2099-12-31 25:61", False), ("明天晚上", False), ("12/31", False), ("2099-12-31T19:30", False),
])
def test_event_datetime_must_be_a_real_date(value, kept):
    result = parse_analysis(good(event_datetime=value))
    assert (result.event_datetime == value) is kept
    assert ("日期時間" in result.unreadable_fields) is (not kept)


def test_empty_event_datetime_is_not_flagged():
    result = parse_analysis(good(event_datetime=""))
    assert result.event_datetime == "" and "日期時間" not in result.unreadable_fields


@pytest.mark.parametrize("value,expected", [(2, 2), ("3", 3), (2.0, 2), (0, 1), (-1, 1), (100, 1), ("abc", 1), (None, 1), (True, 1), (2.5, 1)])
def test_ticket_count_falls_back_to_one(value, expected):
    assert parse_analysis(good(ticket_count=value)).ticket_count == expected


def test_unreadable_fields_accept_keys_and_chinese_names_and_drop_unknown():
    result = parse_analysis(good(unreadable_fields=["location", "座位或票種", "location", "秘密欄位", 5, None]))
    assert result.unreadable_fields == ("地點", "座位或票種")
    assert parse_analysis(good(unreadable_fields="location")).unreadable_fields == ()


# ---------- 文字清理 ----------

def test_control_and_invisible_characters_are_removed():
    result = parse_analysis(good(name="演\x00唱‮會​\n\t二\r號"))
    assert result.name == "演唱會 二 號"


def test_long_text_is_truncated():
    result = parse_analysis(good(name="長" * 1000, other_info="x" * 5000, location="y" * 500))
    assert len(result.name) == 100 and len(result.other_info) == 500 and len(result.location) == 100


def test_text_is_not_executed_or_interpreted():
    payload = "=HYPERLINK(\"http://example.com\",\"x\")"
    assert parse_analysis(good(name=payload)).name == payload  # 解析階段不改內容，公式處理是防護 4 的工作


# ---------- 轉成要存的內容 ----------

def test_to_ticket_data_collects_notes_into_other_info():
    result = parse_analysis(good(ticket_count=2, currency_guessed=True, unreadable_fields=["location"], other_info="備註"))
    data = result.to_ticket_data()
    assert data.other_info == "票號：TEST-0001；共 2 張；幣別為推測；看不清楚：地點；備註"
    assert data.amount == 1200.0 and data.currency == "TWD" and data.name == "測試演唱會 2099"


def test_to_ticket_data_without_notes_has_empty_other_info():
    assert parse_analysis(good(ticket_number="")).to_ticket_data().other_info == ""


def test_guessed_currency_note_only_when_currency_exists():
    assert "幣別為推測" not in parse_analysis(good(currency="", currency_guessed=True)).to_ticket_data().other_info
