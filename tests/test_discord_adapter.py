"""Discord 轉接層的測試。

全部不連線到真實的 Discord：用假的 discord 訊息／頻道物件，以及（只有下載功能）本機 127.0.0.1 上的小型 HTTP 伺服器。
完整流程的測試把假的 discord 事件送進真正的核心（build_app），確認 Discord 訊息確實走完「接收 → 核心流程 → 回覆」。
沒有使用任何真實 Token、真實頻道或真實票券。
"""

from __future__ import annotations

import ast
import asyncio
import logging
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from synthetic import (
    CHANNEL, FAKE_TOKEN, FakeClock, ScriptedAnalyzer, ScriptedCorrect, Sleeper, analysis, distinct_jpeg, jpeg_bytes, make_config,
)
from ticket_agent import discord_adapter, main as main_module
from ticket_agent.app import build_app
from ticket_agent.corrections import Correction, CorrectionUnclear
from ticket_agent.discord_adapter import (
    DownloadError, DiscordMessenger, TicketBot, build_intents, clip_for_discord, fetch_limited, run_bot, should_handle,
    to_incoming,
)
from ticket_agent.locking import AlreadyRunning
from ticket_agent.messaging import Attachment
from ticket_agent.models import STATUS_ACTIVE

CDN = "https://cdn.discordapp.com/attachments/1/2/ticket.jpg"
SRC = Path(discord_adapter.__file__).resolve().parent
_ids = count(5000)


# ================================================================== 假的 discord 物件

def http_error(status: int, cls=discord.HTTPException):
    return cls(SimpleNamespace(status=status, reason="fake"), "fake error")


def fake_user(user_id=100, name="xiaoming", display="小明", bot=False):
    return SimpleNamespace(id=user_id, name=name, display_name=display, bot=bot)


def fake_attachment(att_id=1, filename="ticket.jpg", content_type="image/jpeg", size=5000, url=CDN):
    return SimpleNamespace(id=att_id, filename=filename, content_type=content_type, size=size, url=url)


def fake_message(*, msg_id=None, channel_id=CHANNEL, in_guild=True, author=None, content="", attachments=(), reference_id=None,
                 reference=True, mtype=discord.MessageType.default, webhook_id=None,
                 created=datetime(2026, 10, 24, 7, 30, 12, tzinfo=timezone.utc)):
    return SimpleNamespace(
        id=msg_id if msg_id is not None else next(_ids), channel=SimpleNamespace(id=channel_id),
        guild=SimpleNamespace(id=1) if in_guild else None, author=author or fake_user(), content=content,
        attachments=list(attachments), reference=SimpleNamespace(message_id=reference_id) if reference else None,
        type=mtype, webhook_id=webhook_id, created_at=created,
    )


class FakePartial:
    def __init__(self, channel, message_id):
        self.channel, self.id = channel, message_id

    async def reply(self, content=None, **kwargs):
        self.channel.calls.append(("reply", self.id, content, kwargs))
        if self.channel.fail_reply:
            raise self.channel.fail_reply
        return SimpleNamespace(id=next(_ids))

    async def edit(self, **kwargs):
        self.channel.calls.append(("edit", self.id, kwargs.get("content"), kwargs))
        if self.channel.fail_edit:
            raise self.channel.fail_edit
        self.channel.edited[self.id] = kwargs.get("content")


class FakeChannel:
    def __init__(self, channel_id=CHANNEL):
        self.id = channel_id
        self.calls: list[tuple] = []
        self.edited: dict[int, str] = {}
        self.fail_reply: Exception | None = None
        self.fail_send: Exception | None = None
        self.fail_edit: Exception | None = None
        self.replied: dict[int, str] = {}

    def get_partial_message(self, message_id):
        return FakePartial(self, message_id)

    async def send(self, content=None, **kwargs):
        self.calls.append(("send", None, content, kwargs))
        if self.fail_send:
            raise self.fail_send
        return SimpleNamespace(id=next(_ids))

    def of(self, kind):
        return [call for call in self.calls if call[0] == kind]


def messenger_for(channel, *, fetch_returns=None, max_bytes=10 * 1024 * 1024, cached=True):
    fetch_calls = []

    async def fake_fetch(session, url, limit):
        fetch_calls.append((url, limit))
        if isinstance(fetch_returns, BaseException):
            raise fetch_returns
        return fetch_returns if fetch_returns is not None else jpeg_bytes()

    async def fetch_channel():
        if channel is None:
            raise http_error(404, discord.NotFound)
        return channel

    messenger = DiscordMessenger(lambda: channel if cached else None, fetch_channel, max_bytes, fetch=fake_fetch)
    messenger.fetch_calls = fetch_calls
    return messenger


# ================================================================== 訊息轉換

def test_to_incoming_converts_every_field():
    message = fake_message(msg_id=123456789012345678, author=fake_user(987654321098765432, display="小明"), content="你好",
                           attachments=[fake_attachment(77, "a.png", "image/png", 1234)], reference_id=555)
    incoming = to_incoming(message)
    assert incoming.id == "123456789012345678" and incoming.author_id == "987654321098765432"
    assert incoming.author_name == "小明" and incoming.content == "你好" and incoming.channel_id == CHANNEL
    assert incoming.reference_message_id == "555" and incoming.is_dm is False and incoming.author_is_bot is False
    (att,) = incoming.attachments
    assert att == Attachment(id="77", filename="a.png", content_type="image/png", size=1234, url=CDN)


def test_created_time_becomes_naive_local_time():
    message = fake_message(created=datetime(2026, 10, 24, 7, 30, 12, tzinfo=timezone.utc))
    incoming = to_incoming(message)
    assert incoming.created_at.tzinfo is None
    assert incoming.created_at == datetime(2026, 10, 24, 7, 30, 12, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def test_missing_reference_or_message_id_gives_none_and_odd_values_do_not_crash():
    assert to_incoming(fake_message(reference=False)).reference_message_id is None
    assert to_incoming(fake_message(reference_id=None)).reference_message_id is None
    odd = fake_message(content=None, attachments=[fake_attachment(filename=None, content_type=None, size=None)])
    incoming = to_incoming(odd)
    assert incoming.content == "" and incoming.attachments[0].filename == "" and incoming.attachments[0].size == 0


def test_display_name_falls_back_to_user_name():
    assert to_incoming(fake_message(author=fake_user(display=None, name="fallback"))).author_name == "fallback"


# ================================================================== 只處理指定頻道

OWN = 999


def test_only_the_configured_guild_channel_is_handled():
    assert should_handle(fake_message(), CHANNEL, OWN)


@pytest.mark.parametrize("message", [
    fake_message(channel_id=CHANNEL + 1),                                     # 別的頻道
    fake_message(channel_id=0),
    fake_message(in_guild=False),                                              # 私訊
    fake_message(author=fake_user(bot=True)),                                  # 別的 bot
    fake_message(author=fake_user(OWN)),                                       # 自己
    fake_message(webhook_id=123),                                              # webhook
    fake_message(mtype=discord.MessageType.new_member),                        # 系統訊息
    fake_message(mtype=discord.MessageType.pins_add),
    fake_message(mtype=discord.MessageType.thread_created),
    fake_message(mtype=discord.MessageType.thread_starter_message),
])
def test_everything_else_is_ignored(message):
    assert not should_handle(message, CHANNEL, OWN)


def test_replies_are_handled_and_unknown_own_user_id_is_tolerated():
    assert should_handle(fake_message(mtype=discord.MessageType.reply, reference_id=5), CHANNEL, OWN)
    assert should_handle(fake_message(), CHANNEL, None)


def test_a_thread_inside_the_channel_has_its_own_id_and_is_not_handled():
    assert not should_handle(fake_message(channel_id=CHANNEL + 10), CHANNEL, OWN)


def test_intents_are_the_minimum_needed():
    intents = build_intents()
    assert (intents.guilds, intents.guild_messages, intents.message_content) == (True, True, True)
    for risky in ("members", "presences", "dm_messages", "guild_reactions", "typing", "voice_states", "guild_typing",
                  "dm_reactions", "invites", "webhooks", "integrations", "emojis", "moderation", "message_content_dummy"):
        assert not getattr(intents, risky, False), risky
    assert intents.value == 33281  # guilds + guild_messages + message_content，沒有別的


# ================================================================== 送出：回覆與編輯

async def test_reply_targets_the_original_message_without_mentions_or_embeds():
    channel = FakeChannel()
    messenger = messenger_for(channel)
    new_id = await messenger.reply(to_incoming(fake_message(msg_id=4242)), "你好 @everyone <@123>")
    assert new_id is not None and new_id.isdigit()
    ((kind, target, text, kwargs),) = channel.calls
    assert (kind, target, text) == ("reply", 4242, "你好 @everyone <@123>")
    assert kwargs["mention_author"] is False and kwargs["suppress_embeds"] is True
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


async def test_long_text_is_clipped_to_discords_limit():
    channel = FakeChannel()
    await messenger_for(channel).reply(to_incoming(fake_message()), "字" * 5000)
    assert len(channel.calls[0][2]) == 2000 and clip_for_discord("短") == "短"


async def test_when_the_original_message_was_deleted_the_reply_falls_back_to_a_plain_message():
    channel = FakeChannel()
    channel.fail_reply = http_error(400)
    new_id = await messenger_for(channel).reply(to_incoming(fake_message()), "結果")
    assert new_id is not None
    assert [c[0] for c in channel.calls] == ["reply", "send"]
    _, _, text, kwargs = channel.calls[1]
    assert text == "結果" and kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    assert kwargs["suppress_embeds"] is True


@pytest.mark.parametrize("status", [400, 404])
async def test_fallback_that_also_fails_returns_none_instead_of_raising(status):
    channel = FakeChannel()
    channel.fail_reply, channel.fail_send = http_error(status), http_error(403, discord.Forbidden)
    assert await messenger_for(channel).reply(to_incoming(fake_message()), "x") is None


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_other_http_errors_are_not_retried_as_plain_messages(status):
    channel = FakeChannel()
    channel.fail_reply = http_error(status)
    assert await messenger_for(channel).reply(to_incoming(fake_message()), "x") is None
    assert [c[0] for c in channel.calls] == ["reply"]


async def test_channel_that_cannot_be_found_gives_none_and_false():
    messenger = messenger_for(None, cached=False)
    assert await messenger.reply(to_incoming(fake_message()), "x") is None
    assert await messenger.edit("1", "x") is False


async def test_channel_is_fetched_when_not_cached():
    channel = FakeChannel()
    messenger = messenger_for(channel, cached=False)
    assert await messenger.reply(to_incoming(fake_message()), "x") is not None


async def test_edit_replaces_the_content_without_mentions():
    channel = FakeChannel()
    assert await messenger_for(channel).edit("4242", "完成 @here") is True
    assert channel.edited == {4242: "完成 @here"}
    assert channel.calls[0][3]["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


@pytest.mark.parametrize("error", [http_error(404, discord.NotFound), http_error(403, discord.Forbidden), http_error(500)])
async def test_edit_failures_return_false(error):
    channel = FakeChannel()
    channel.fail_edit = error
    assert await messenger_for(channel).edit("4242", "x") is False


@pytest.mark.parametrize("bad_id", ["", "abc", "12.5", "../x"])
async def test_edit_with_a_non_numeric_id_returns_false_without_calling_discord(bad_id):
    channel = FakeChannel()
    assert await messenger_for(channel).edit(bad_id, "x") is False
    assert channel.calls == []


# ================================================================== 下載附件

async def test_download_refuses_any_url_that_is_not_the_discord_cdn_and_never_fetches_it():
    messenger = messenger_for(FakeChannel())
    for url in ("https://evil.example.com/a.jpg", "http://cdn.discordapp.com/a.jpg", "https://127.0.0.1/a.jpg",
                "file:///etc/passwd", "https://cdn.discordapp.com.evil.example.com/a.jpg", "https://user:pw@cdn.discordapp.com/a.jpg"):
        with pytest.raises(DownloadError, match="不被允許"):
            await messenger.download(Attachment("1", "a.jpg", "image/jpeg", 10, url))
    assert messenger.fetch_calls == []


async def test_download_passes_the_size_limit_and_reuses_one_session():
    messenger = messenger_for(FakeChannel(), max_bytes=1234)
    attachment = Attachment("1", "a.jpg", "image/jpeg", 10, CDN)
    assert (await messenger.download(attachment))[:3] == b"\xff\xd8\xff"
    await messenger.download(attachment)
    assert [limit for _, limit in messenger.fetch_calls] == [1234, 1234]
    session = messenger._session
    assert session is not None and not session.closed
    await messenger.aclose()
    assert session.closed
    await messenger.aclose()  # 重複關閉不會出錯


async def test_the_download_session_carries_no_cookies_and_ignores_proxy_environment():
    messenger = messenger_for(FakeChannel())
    await messenger.download(Attachment("1", "a.jpg", "image/jpeg", 10, CDN))
    assert isinstance(messenger._session.cookie_jar, aiohttp.DummyCookieJar) and messenger._session.trust_env is False
    await messenger.aclose()


@pytest.fixture
async def http_server():
    state = {"hits": {}, "servers": []}

    def counted(name, handler):
        async def wrapper(request):
            state["hits"][name] = state["hits"].get(name, 0) + 1
            return await handler(request)
        return wrapper

    async def ok(_r):
        return web.Response(body=b"X" * 1000)

    async def big_declared(_r):
        return web.Response(body=b"X" * 5000)

    async def chunked_big(_r):
        response = web.StreamResponse()
        response.enable_chunked_encoding()
        await response.prepare(_r)
        for _ in range(6):
            await response.write(b"Y" * 600)
        await response.write_eof()
        return response

    async def redirect(_r):
        raise web.HTTPFound("/target")

    async def target(_r):
        return web.Response(body=b"SECRET-TARGET")

    async def missing(_r):
        raise web.HTTPNotFound()

    async def broken(_r):
        raise web.HTTPInternalServerError()

    async def slow(_r):
        await asyncio.sleep(5)
        return web.Response(body=b"late")

    app = web.Application()
    for name, handler in (("ok", ok), ("big", big_declared), ("chunked", chunked_big), ("redirect", redirect), ("target", target),
                          ("missing", missing), ("broken", broken), ("slow", slow)):
        app.router.add_get(f"/{name}", counted(name, handler))
    server = TestServer(app)
    await server.start_server()
    state["url"] = lambda name: str(server.make_url(f"/{name}"))
    async with aiohttp.ClientSession() as session:
        state["session"] = session
        yield state
    await server.close()


async def test_fetch_limited_returns_the_body_when_within_the_limit(http_server):
    assert await fetch_limited(http_server["session"], http_server["url"]("ok"), 1000) == b"X" * 1000  # 剛好等於上限也可以


async def test_fetch_limited_rejects_a_declared_size_over_the_limit(http_server):
    with pytest.raises(DownloadError, match="太大"):
        await fetch_limited(http_server["session"], http_server["url"]("big"), 1000)


async def test_fetch_limited_rejects_an_undeclared_size_that_turns_out_too_big(http_server):
    with pytest.raises(DownloadError, match="太大"):
        await fetch_limited(http_server["session"], http_server["url"]("chunked"), 2000)


async def test_fetch_limited_never_follows_redirects(http_server):
    with pytest.raises(DownloadError, match="302"):
        await fetch_limited(http_server["session"], http_server["url"]("redirect"), 10_000)
    assert http_server["hits"].get("target", 0) == 0  # 轉址的目標根本沒被請求


@pytest.mark.parametrize("name,status", [("missing", "404"), ("broken", "500")])
async def test_fetch_limited_rejects_error_statuses(http_server, name, status):
    with pytest.raises(DownloadError, match=status):
        await fetch_limited(http_server["session"], http_server["url"](name), 1000)


async def test_fetch_limited_times_out_on_slow_servers(http_server, monkeypatch):
    monkeypatch.setattr(discord_adapter, "DOWNLOAD_TIMEOUT_S", 0.2)
    with pytest.raises(DownloadError, match="下載失敗"):
        await fetch_limited(http_server["session"], http_server["url"]("slow"), 1000)


async def test_fetch_limited_turns_connection_errors_into_download_errors_without_leaking_the_url():
    secret_url = "http://127.0.0.1:1/SECRET-PATH-12345"
    async with aiohttp.ClientSession() as session:
        with pytest.raises(DownloadError) as info:
            await fetch_limited(session, secret_url, 1000)
    assert "SECRET-PATH" not in str(info.value) and "127.0.0.1" not in str(info.value)


# ================================================================== TicketBot：事件接進核心

class RecordingApp:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.handled = []

    def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def handle_message(self, message):
        self.handled.append(message)


def make_bot(tmp_path, app=None, **config_overrides):
    config = make_config(tmp_path, **config_overrides)
    app = app or RecordingApp()
    calls = []

    def factory(cfg, messenger):
        calls.append((cfg, messenger))
        return app

    bot = TicketBot(config, app_factory=factory)
    bot.factory_calls = calls
    return bot, app


async def test_setup_hook_builds_and_starts_the_core(tmp_path):
    bot, app = make_bot(tmp_path)
    assert bot.app is None
    await bot.setup_hook()
    assert bot.app is app and app.started and bot.factory_calls[0][1] is bot.messenger
    await bot.close()


async def test_messages_in_the_right_channel_reach_the_core(tmp_path):
    bot, app = make_bot(tmp_path)
    await bot.setup_hook()
    await bot.on_message(fake_message(msg_id=1, content="!我的票券"))
    (incoming,) = app.handled
    assert incoming.id == "1" and incoming.content == "!我的票券" and incoming.channel_id == CHANNEL
    await bot.close()


async def test_other_channels_dms_bots_and_the_bot_itself_never_reach_the_core_or_even_get_converted(tmp_path, monkeypatch):
    bot, app = make_bot(tmp_path)
    await bot.setup_hook()
    bot._connection.user = SimpleNamespace(id=OWN)
    converted = []
    real = discord_adapter.to_incoming
    monkeypatch.setattr(discord_adapter, "to_incoming", lambda m: converted.append(m) or real(m))
    for message in (fake_message(channel_id=CHANNEL + 1, content="另一個頻道的私人聊天"), fake_message(in_guild=False),
                    fake_message(author=fake_user(bot=True)), fake_message(author=fake_user(OWN)), fake_message(webhook_id=9),
                    fake_message(mtype=discord.MessageType.new_member)):
        await bot.on_message(message)
    assert app.handled == [] and converted == []  # 其他頻道的內容連轉換都沒做
    await bot.close()


async def test_a_conversion_error_affects_only_that_message(tmp_path, monkeypatch, caplog):
    bot, app = make_bot(tmp_path)
    await bot.setup_hook()
    broken = fake_message(content="SECRET-MESSAGE-TEXT")
    broken.created_at = None  # 轉換會出錯
    caplog.set_level(logging.DEBUG)
    await bot.on_message(broken)
    await bot.on_message(fake_message(content="正常的訊息"))
    assert [m.content for m in app.handled] == ["正常的訊息"]
    assert "SECRET-MESSAGE-TEXT" not in caplog.text
    await bot.close()


async def test_messages_before_the_core_exists_are_ignored(tmp_path):
    bot, app = make_bot(tmp_path)
    await bot.on_message(fake_message())
    assert app.handled == []
    await bot.close()


async def test_close_stops_the_core_and_closes_the_download_session_even_if_stopping_fails(tmp_path):
    class FailingStop(RecordingApp):
        async def stop(self):
            raise RuntimeError("stop failed")

    bot, app = make_bot(tmp_path, app=FailingStop())
    await bot.setup_hook()
    bot.messenger._session = aiohttp.ClientSession()
    session = bot.messenger._session
    with pytest.raises(RuntimeError):
        await bot.close()
    assert session.closed


async def test_on_ready_complains_clearly_if_the_channel_is_not_visible_and_never_logs_the_token(tmp_path, caplog):
    bot, _ = make_bot(tmp_path)
    caplog.set_level(logging.INFO)
    await bot.on_ready()
    assert "找不到可用的指定頻道" in caplog.text and FAKE_TOKEN not in caplog.text
    await bot.close()


# ---------------------------------------------------------------- 終端機上的連線提示

async def _ready_output(tmp_path, capsys, monkeypatch, channel):
    bot, _ = make_bot(tmp_path)
    monkeypatch.setattr(bot, "get_channel", lambda _id: channel)
    await bot.on_ready()
    await bot.close()
    return capsys.readouterr()


async def test_when_the_channel_is_found_the_terminal_says_it_is_watching(tmp_path, capsys, monkeypatch):
    captured = await _ready_output(tmp_path, capsys, monkeypatch, FakeChannel())
    assert captured.out == "已連線，開始監看指定的頻道。\n" and captured.err == ""


async def test_when_the_channel_is_not_found_the_terminal_shows_a_clear_warning_and_no_success_message(tmp_path, capsys, monkeypatch):
    captured = await _ready_output(tmp_path, capsys, monkeypatch, None)
    assert "已連線，開始監看指定的頻道" not in captured.out + captured.err  # 不能讓人誤以為可以用了
    assert captured.out == "" and "警告" in captured.err and "不會處理任何訊息" in captured.err
    assert "CHANNEL_ID" in captured.err and "檢視頻道" in captured.err


async def test_an_id_that_is_not_a_text_channel_is_also_a_warning(tmp_path, capsys, monkeypatch):
    voice_like = SimpleNamespace(id=CHANNEL)  # 沒有 get_partial_message：不是能收發訊息的文字頻道
    captured = await _ready_output(tmp_path, capsys, monkeypatch, voice_like)
    assert "警告" in captured.err and "已連線，開始監看" not in captured.out


async def test_terminal_output_never_contains_the_token_channel_id_or_message_content(tmp_path, capsys, monkeypatch, caplog):
    bot, app = make_bot(tmp_path)
    await bot.setup_hook()
    secret_text = "SECRET-USER-MESSAGE-TEXT 票號：SECRET-TICKET-NO-1"
    for channel in (FakeChannel(), None):
        monkeypatch.setattr(bot, "get_channel", lambda _id, c=channel: c)
        await bot.on_ready()
    await bot.on_message(fake_message(content=secret_text, attachments=[fake_attachment(1)]))
    await bot.close()
    captured = capsys.readouterr()
    everything = captured.out + captured.err
    for forbidden in (FAKE_TOKEN, str(CHANNEL), secret_text, "SECRET-TICKET-NO-1"):
        assert forbidden not in everything


async def test_a_reconnect_announces_again_so_the_user_can_see_the_bot_is_back(tmp_path, capsys, monkeypatch):
    bot, _ = make_bot(tmp_path)
    monkeypatch.setattr(bot, "get_channel", lambda _id: FakeChannel())
    await bot.on_ready()
    await bot.on_ready()
    await bot.close()
    assert capsys.readouterr().out.count("已連線，開始監看指定的頻道。") == 2


# ================================================================== 完整流程：discord 事件 → 真正的核心 → discord 回覆

@pytest.fixture
async def stack(tmp_path):
    """假的 discord 頻道 + 真正的核心（build_app）。辨識與更正用劇本，其餘（佇列、去重、Excel、鎖、照片）都是真的。"""

    class Stack:
        channel = FakeChannel()
        config = make_config(tmp_path)
        analyzer = ScriptedAnalyzer(analysis())
        corrector = ScriptedCorrect(Correction(changes={"amount": 1500.0}))
        clock = FakeClock()
        downloads: dict[str, object] = {}

        async def start(self):
            def factory(config, messenger):
                return build_app(config, messenger, analyze=self.analyzer, correct=self.corrector, sleep=Sleeper(),
                                 retry_delay_s=0.0, clock=self.clock)

            self.bot = TicketBot(self.config, app_factory=factory)
            fetch_urls = []

            async def fake_fetch(session, url, limit):
                fetch_urls.append(url)
                item = self.downloads.get(url, distinct_jpeg(str(len(fetch_urls))))  # 每次下載一張不同的圖（相同位元組會被當成重複上傳）
                if isinstance(item, BaseException):
                    raise item
                return item

            self.fetch_urls = fetch_urls
            self.bot.messenger = DiscordMessenger(lambda: self.channel, lambda: self._fetch(), self.config.max_attachment_bytes, fetch=fake_fetch)
            await self.bot.setup_hook()
            return self

        async def _fetch(self):
            return self.channel

        async def send(self, message):
            await self.bot.on_message(message)
            await self.bot.app.drain()

        def records(self):
            from ticket_agent.excel_store import TicketStore
            return TicketStore(self.config.excel_path).all_records()

    s = Stack()
    s.channel = FakeChannel()
    await s.start()
    yield s
    await s.bot.close()


def last_status(stack_):
    """目前使用者在頻道裡看到的最後一則 bot 結果：最後一次編輯，或最後一次回覆／新訊息的內容。"""
    texts = [c[2] for c in stack_.channel.calls if c[0] in ("edit", "reply", "send")]
    return texts[-1]


async def test_a_ticket_photo_goes_from_discord_through_the_core_and_back(stack):
    await stack.send(fake_message(msg_id=7001, attachments=[fake_attachment(1, "ticket.jpg", url=CDN)]))
    kinds = [c[0] for c in stack.channel.calls]
    assert kinds == ["reply", "edit"]  # 先回「處理中」，再把同一則編輯成結果
    reply_call, edit_call = stack.channel.calls
    assert reply_call[1] == 7001 and reply_call[2] == "⏳ 處理中…"
    assert "🎫 已記錄 #1" in edit_call[2] and "本月（2026-10）：1 筆，TWD 1,200" in edit_call[2]
    assert stack.fetch_urls == [CDN]
    (record,) = stack.records()
    assert record.uploader == "小明" and record.uploader_id == "100" and record.status == STATUS_ACTIVE
    assert record.data.other_info == "票號：TEST-0001" and "TEST-0001" not in edit_call[2]
    assert len(list((stack.config.data_dir / "photos").iterdir())) == 1


async def test_the_same_discord_event_delivered_twice_creates_only_one_record(stack):
    message = fake_message(msg_id=7002, attachments=[fake_attachment(1)])
    await stack.send(message)
    await stack.send(message)  # Discord 重連時可能重送
    assert len(stack.records()) == 1 and stack.analyzer.calls == 1 and len(stack.channel.of("reply")) == 1


async def test_replying_to_the_result_message_corrects_the_record(stack):
    await stack.send(fake_message(msg_id=7003, attachments=[fake_attachment(1)]))
    status_id = stack.channel.of("edit")[0][1]  # 被編輯成結果的那則 bot 訊息
    await stack.send(fake_message(msg_id=7004, content="金額應該是 1500", reference_id=status_id, mtype=discord.MessageType.reply))
    assert stack.records()[0].data.amount == 1500.0 and "金額：1,200 → 1,500" in last_status(stack)
    assert stack.corrector.texts == ["金額應該是 1500"]


async def test_commands_and_ticket_number_rule_work_through_discord(stack):
    await stack.send(fake_message(msg_id=7005, attachments=[fake_attachment(1)]))
    await stack.send(fake_message(msg_id=7006, content="!我的票券"))
    assert "你的票券" in last_status(stack) and "TEST-0001" not in last_status(stack)
    status_id = stack.channel.of("edit")[0][1]
    stack.corrector.outcomes = [CorrectionUnclear("無")]
    await stack.send(fake_message(msg_id=7007, content="票號改成 HACKED-1", reference_id=status_id, mtype=discord.MessageType.reply))
    assert "不能在 Discord 更正" in last_status(stack)
    assert stack.records()[0].data.other_info == "票號：TEST-0001"


async def test_all_core_limits_still_apply_through_discord(stack):
    await stack.send(fake_message(msg_id=7010, attachments=[fake_attachment(i, url=CDN) for i in range(7)]))  # 7 張，只處理 5 張
    assert stack.analyzer.calls == 5
    assert any("最多處理 5 張" in c[2] for c in stack.channel.calls)


async def test_unsupported_and_foreign_attachments_are_refused_before_any_download(stack):
    await stack.send(fake_message(msg_id=7011, attachments=[fake_attachment(1, "notes.txt", "text/plain")]))
    await stack.send(fake_message(msg_id=7012, attachments=[fake_attachment(2, url="https://evil.example.com/a.jpg")]))
    await stack.send(fake_message(msg_id=7013, attachments=[fake_attachment(3, size=11 * 1024 * 1024)]))
    assert stack.fetch_urls == [] and stack.records() == []
    assert [c[2] for c in stack.channel.of("reply")].count("只支援 JPG、PNG、WebP、PDF，且每個檔案要在 10 MB 以內") == 3


async def test_messages_without_attachments_or_commands_are_ignored(stack):
    await stack.send(fake_message(msg_id=7014, content="大家好"))
    assert stack.channel.calls == [] and stack.fetch_urls == []


async def test_download_failure_gets_a_failure_reply_and_no_record(stack):
    stack.downloads[CDN] = DownloadError("檔案太大")
    await stack.send(fake_message(msg_id=7015, attachments=[fake_attachment(1)]))
    assert last_status(stack) == "處理失敗，請稍後再試" and stack.records() == []


async def test_a_non_image_that_passes_the_name_check_is_rejected_after_download(stack):
    stack.downloads[CDN] = b"this is not an image at all"
    await stack.send(fake_message(msg_id=7016, attachments=[fake_attachment(1)]))
    assert "只支援 JPG、PNG、WebP、PDF" in last_status(stack) and stack.records() == []


async def test_if_the_users_message_was_deleted_the_result_still_reaches_the_channel(stack):
    stack.channel.fail_reply = http_error(400)  # 回覆原訊息失敗：原訊息已被刪除
    await stack.send(fake_message(msg_id=7017, attachments=[fake_attachment(1)]))
    assert len(stack.records()) == 1
    sends = stack.channel.of("send")
    assert sends and any("🎫 已記錄 #1" in c[2] for c in stack.channel.calls)  # 結果用一般訊息送出
    assert stack.records()[0].reply_message_ids  # 之後回覆它仍然可以更正


async def test_if_the_status_message_cannot_be_edited_a_new_reply_carries_the_result(stack):
    stack.channel.fail_edit = http_error(404, discord.NotFound)
    await stack.send(fake_message(msg_id=7018, attachments=[fake_attachment(1)]))
    replies_ = [c[2] for c in stack.channel.of("reply")]
    assert replies_[0] == "⏳ 處理中…" and "🎫 已記錄 #1" in replies_[1]


async def test_discord_being_down_for_replies_does_not_lose_the_record_or_crash(stack):
    stack.channel.fail_reply = http_error(503)
    stack.channel.fail_send = http_error(503)
    stack.channel.fail_edit = http_error(503)
    await stack.send(fake_message(msg_id=7019, attachments=[fake_attachment(1)]))
    assert len(stack.records()) == 1  # 資料照常寫入
    stack.channel.fail_reply = stack.channel.fail_send = stack.channel.fail_edit = None
    stack.analyzer.outcomes = [analysis(), analysis(name="第二張")]
    await stack.send(fake_message(msg_id=7020, attachments=[fake_attachment(2)]))  # 之後恢復正常，是另一張票
    assert "🎫 已記錄 #2" in last_status(stack) and len(stack.records()) == 2


async def test_many_users_at_once_through_discord_lose_nothing(stack):
    stack.analyzer.outcomes = [lambda _j: analysis(name=f"票{stack.analyzer.calls}", amount=100)]
    stack.bot.app.router._limiter.limit = 1000  # 這個測試要驗證的是鎖與佇列，不是限流
    messages = [fake_message(msg_id=8000 + u * 10 + k, author=fake_user(100 + u, display=f"人{u}"), attachments=[fake_attachment(k)])
                for u in range(5) for k in range(4)]
    await asyncio.gather(*(stack.bot.on_message(m) for m in messages))
    await stack.bot.app.drain()
    records = stack.records()
    assert len(records) == 20 and sorted(r.serial for r in records) == list(range(1, 21))


# ================================================================== 啟動、錯誤處理與 Token

@pytest.fixture
def clean_loggers():
    names = ("ticket_agent", "discord")
    before = {n: list(logging.getLogger(n).handlers) for n in names}
    levels = {n: logging.getLogger(n).level for n in names}
    yield
    for name in names:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if handler not in before[name]:
                logger.removeHandler(handler)
                handler.close()
        logger.setLevel(levels[name])


class FakeBot:
    def __init__(self, raises=None):
        self.raises = raises
        self.run_args = None

    def run(self, token, **kwargs):
        self.run_args = (token, kwargs)
        if self.raises:
            raise self.raises


def test_run_bot_passes_the_token_only_to_run_and_disables_discords_own_log_handler(tmp_path, clean_loggers):
    fake = FakeBot()
    assert run_bot(make_config(tmp_path), bot_factory=lambda cfg: fake) == 0
    token, kwargs = fake.run_args
    assert token == FAKE_TOKEN and kwargs == {"log_handler": None}
    assert (tmp_path / "data" / "logs" / "agent.log").exists()


@pytest.mark.parametrize("error,expected", [
    (discord.LoginFailure("Improper token has been passed."), "DISCORD_TOKEN 不正確"),
    (discord.PrivilegedIntentsRequired(None), "Message Content Intent"),
    (AlreadyRunning("已經有另一個 agent 正在使用這個資料目錄，請先關掉它"), "另一個 agent"),
])
def test_startup_failures_give_a_clear_message_and_never_print_the_token(tmp_path, clean_loggers, capsys, error, expected):
    assert run_bot(make_config(tmp_path), bot_factory=lambda cfg: FakeBot(error)) == 1
    captured = capsys.readouterr()
    assert expected in captured.err and FAKE_TOKEN not in captured.err + captured.out


def test_log_file_never_contains_the_token_even_if_something_logs_it(tmp_path, clean_loggers):
    run_bot(make_config(tmp_path), bot_factory=lambda cfg: FakeBot())
    logging.getLogger("ticket_agent.test").error("不小心把 %s 寫進 log", FAKE_TOKEN)
    logging.getLogger("discord.client").warning("也來自 discord：%s", FAKE_TOKEN)
    for handler in logging.getLogger("ticket_agent").handlers:
        handler.flush()
    text = (tmp_path / "data" / "logs" / "agent.log").read_text(encoding="utf-8")
    assert FAKE_TOKEN not in text and "[已遮蔽]" in text


def test_main_check_never_starts_the_bot(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(main_module, "load_config", lambda: make_config(tmp_path))  # 測試絕不讀真正的 .env

    def must_not_run(*_a, **_k):
        raise AssertionError("--check 不可以連線")

    monkeypatch.setattr(discord_adapter, "run_bot", must_not_run)
    assert main_module.main(["--check"]) == 0
    assert FAKE_TOKEN not in capsys.readouterr().out


def test_main_without_flags_runs_the_bot_and_returns_its_exit_code(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    seen = []
    monkeypatch.setattr(discord_adapter, "run_bot", lambda cfg: seen.append(cfg) or 7)
    assert main_module.main([]) == 7
    assert seen == [config] and FAKE_TOKEN not in capsys.readouterr().out


def test_main_with_a_broken_config_never_reaches_discord(monkeypatch, capsys):
    from ticket_agent.config import ConfigError

    def broken():
        raise ConfigError(["DISCORD_TOKEN 沒有設定"])

    monkeypatch.setattr(main_module, "load_config", broken)
    monkeypatch.setattr(discord_adapter, "run_bot", lambda cfg: (_ for _ in ()).throw(AssertionError("不該連線")))
    assert main_module.main([]) == 1
    assert "DISCORD_TOKEN 沒有設定" in capsys.readouterr().err


def test_the_token_is_only_ever_used_by_config_main_and_the_one_run_call():
    """靜態檢查（語法樹）：整個 src/ 裡，讀取 discord_token 的地方只能是 config.py、discord_adapter.py（bot.run 與遮蔽 log）。"""
    users = {}
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "discord_token"]
        if hits:
            users[path.name] = len(hits)
    assert set(users) <= {"config.py", "discord_adapter.py"}, users
    assert users.get("discord_adapter.py") == 2  # setup_logging 的遮蔽清單、bot.run，僅此兩處


def test_the_token_never_appears_in_any_f_string_or_format_call_or_log_call():
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                assert "discord_token" not in ast.dump(node), f"{path.name} 的字串模板含有 token"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("info", "warning", "error", "debug", "exception", "print"):
                assert "discord_token" not in ast.dump(node), f"{path.name} 的 log／輸出含有 token"


async def test_the_help_command_works_through_discord_with_no_pings_and_no_side_effects(stack):
    from ticket_agent import replies
    await stack.send(fake_message(msg_id=9001, content="!說明"))
    ((kind, target, text, kwargs),) = stack.channel.calls
    assert kind == "reply" and target == 9001
    assert text == replies.format_help(max_mb=10, max_attachments=5, confirm_timeout_s=60, rate_limit_per_min=8)
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict() and kwargs["mention_author"] is False
    assert stack.records() == [] and stack.fetch_urls == [] and stack.analyzer.calls == 0 and stack.corrector.calls == 0


async def test_help_from_another_channel_is_ignored_through_discord(stack):
    await stack.send(fake_message(msg_id=9002, content="!說明", channel_id=CHANNEL + 1))
    assert stack.channel.calls == []
