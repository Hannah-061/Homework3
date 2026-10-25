"""discord.py 轉接層（docs/architecture.md 第 2、3.2、3.3、4 節）。

這一層只做「翻譯」，不做任何業務判斷：
- 收：把 discord 的訊息轉成 messaging.IncomingMessage，交給 app.handle_message（核心流程）。
- 送：實作 messaging.Messenger，讓核心流程可以回覆、編輯訊息、下載附件。
所有票券邏輯、佇列、去重、更正、重試、Excel 鎖都在核心裡，這裡一律不碰。

安全規則：
- 只處理設定的那一個頻道（CHANNEL_ID）、不是私訊、不是 bot／webhook／系統訊息；判斷發生在轉換之前，
  其他頻道的訊息內容連看都不看。
- 附件網址不信任：只允許 Discord 官方檔案網址、不跟隨轉址、限制大小與時間。
- 回覆一律不觸發 @提及、不展開連結預覽，並限制在 Discord 的長度上限內。
- Discord Token 只會出現在 bot.run() 那一行；不寫進 log、錯誤訊息或任何字串。
- 使用者的訊息只當資料傳進核心，這裡沒有任何 shell 或命令列的使用。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
import discord

from .app import AgentApp, build_app
from .config import Config
from .locking import AlreadyRunning
from .log_setup import setup_logging
from .media import is_allowed_attachment_url
from .messaging import Attachment, IncomingMessage, Messenger

log = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT_S = 30
CHUNK_BYTES = 64 * 1024
DISCORD_MESSAGE_LIMIT = 2000
HANDLED_MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)
NO_MENTIONS = discord.AllowedMentions.none()

# 顯示在終端機的兩句話。固定文字，不含 Token、頻道編號、使用者訊息或票券資料。
CONNECTED_MESSAGE = "已連線，開始監看指定的頻道。"
CHANNEL_MISSING_MESSAGE = (
    "⚠️ 警告：已連上 Discord，但找不到可用的指定文字頻道，目前不會處理任何訊息。\n"
    "   請確認：bot 已加入該伺服器、有「檢視頻道」權限，而且 .env 的 CHANNEL_ID 是文字頻道的編號、沒有填錯。"
)


class DownloadError(Exception):
    """附件下載失敗（網址不允許、太大、逾時、對方回傳錯誤……）。訊息不含網址與內容。"""


# ---------------------------------------------------------------------------- 收：訊息轉換與篩選

def build_intents() -> discord.Intents:
    """只申請需要的：伺服器（為了找到頻道）、伺服器裡的訊息、訊息內容（要讀附件與指令文字，是需要在後台打開的特殊權限）。
    不要成員名單、不要上線狀態、不要私訊。"""
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.message_content = True
    return intents


def should_handle(message: Any, channel_id: int, own_user_id: int | None) -> bool:
    """最早的一道篩選，只看最基本的欄位。通過了才會轉換成 IncomingMessage。"""
    channel = getattr(message, "channel", None)
    if channel is None or channel.id != channel_id:  # 只處理指定的頻道（討論串有自己的編號，所以也不處理）
        return False
    if getattr(message, "guild", None) is None:  # 私訊
        return False
    author = message.author
    if getattr(author, "bot", False) or getattr(message, "webhook_id", None) is not None:
        return False
    if own_user_id is not None and author.id == own_user_id:
        return False
    return getattr(message, "type", None) in HANDLED_MESSAGE_TYPES  # 加入伺服器、釘選之類的系統訊息不處理


def to_incoming(message: Any) -> IncomingMessage:
    """把 discord 的訊息轉成核心使用的資料結構。編號一律轉成文字；時間轉成本機時區。"""
    reference = getattr(message, "reference", None)
    reference_id = getattr(reference, "message_id", None)
    return IncomingMessage(
        id=str(message.id),
        channel_id=message.channel.id,
        author_id=str(message.author.id),
        author_name=str(getattr(message.author, "display_name", None) or message.author.name),
        author_is_bot=bool(getattr(message.author, "bot", False)),
        content=message.content or "",
        attachments=tuple(
            Attachment(id=str(a.id), filename=a.filename or "", content_type=a.content_type, size=int(a.size or 0), url=a.url)
            for a in message.attachments
        ),
        created_at=message.created_at.astimezone().replace(tzinfo=None),
        reference_message_id=str(reference_id) if reference_id else None,
        is_dm=message.guild is None,
    )


# ---------------------------------------------------------------------------- 送：Messenger

async def fetch_limited(session: aiohttp.ClientSession, url: str, max_bytes: int) -> bytes:
    """下載一個網址：只接受 200、不跟隨轉址、宣告的大小與實際收到的大小都不能超過 max_bytes、整體有時間上限。
    （要不要允許這個網址，由呼叫端先用 media.is_allowed_attachment_url 決定。）"""
    try:
        timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_S)
        async with session.get(url, allow_redirects=False, timeout=timeout) as response:
            if response.status != 200:
                raise DownloadError(f"對方回應 {response.status}")
            if response.content_length is not None and response.content_length > max_bytes:
                raise DownloadError("檔案太大")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(CHUNK_BYTES):
                total += len(chunk)
                if total > max_bytes:  # 沒宣告大小、或宣告得比實際小，一樣擋得住
                    raise DownloadError("檔案太大")
                chunks.append(chunk)
            return b"".join(chunks)
    except DownloadError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as error:
        raise DownloadError(f"下載失敗（{type(error).__name__}）") from error


def clip_for_discord(text: str) -> str:
    return text if len(text) <= DISCORD_MESSAGE_LIMIT else text[: DISCORD_MESSAGE_LIMIT - 1] + "…"


class DiscordMessenger:
    """用 discord.py 實作 messaging.Messenger。只在指定的頻道裡回覆與編輯。"""

    def __init__(
        self,
        get_channel: Callable[[], Any],
        fetch_channel: Callable[[], Awaitable[Any]],
        max_download_bytes: int,
        *,
        fetch: Callable[[aiohttp.ClientSession, str, int], Awaitable[bytes]] = fetch_limited,
    ):
        self._get_channel = get_channel
        self._fetch_channel = fetch_channel
        self._max_bytes = max_download_bytes
        self._fetch = fetch
        self._session: aiohttp.ClientSession | None = None

    async def _channel(self) -> Any | None:
        channel = self._get_channel()
        if channel is not None:
            return channel
        try:
            return await self._fetch_channel()
        except discord.HTTPException as error:
            log.warning("找不到指定頻道：%s", type(error).__name__)
            return None

    async def reply(self, to: IncomingMessage, text: str) -> str | None:
        channel = await self._channel()
        if channel is None:
            return None
        text = clip_for_discord(text)
        try:
            sent = await channel.get_partial_message(int(to.id)).reply(
                text, mention_author=False, allowed_mentions=NO_MENTIONS, suppress_embeds=True)
            return str(sent.id)
        except (discord.HTTPException, ValueError) as error:
            if isinstance(error, discord.HTTPException) and error.status not in (400, 404):
                log.warning("回覆失敗：%s（%s）", type(error).__name__, error.status)
                return None
        # 被回覆的原訊息已經被刪除，無法引用：改成直接在頻道裡送出（內容一樣，只是不引用）
        try:
            sent = await channel.send(text, allowed_mentions=NO_MENTIONS, suppress_embeds=True)
            return str(sent.id)
        except discord.HTTPException as error:
            log.warning("送出訊息失敗：%s（%s）", type(error).__name__, error.status)
            return None

    async def edit(self, message_id: str, text: str) -> bool:
        channel = await self._channel()
        if channel is None:
            return False
        try:
            await channel.get_partial_message(int(message_id)).edit(content=clip_for_discord(text), allowed_mentions=NO_MENTIONS)
            return True
        except (discord.HTTPException, ValueError) as error:  # 訊息被刪、沒權限
            log.info("編輯訊息失敗：%s", type(error).__name__)
            return False

    async def download(self, attachment: Attachment) -> bytes:
        if not is_allowed_attachment_url(attachment.url):  # 不信任附件網址：不是 Discord 官方檔案網址就拒絕
            raise DownloadError("附件網址不被允許")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), trust_env=False)
        return await self._fetch(self._session, attachment.url, self._max_bytes)

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


# ---------------------------------------------------------------------------- 把 discord 事件接進核心

AppFactory = Callable[[Config, Messenger], AgentApp]


class TicketBot(discord.Client):
    def __init__(self, config: Config, *, app_factory: AppFactory = build_app, intents: discord.Intents | None = None):
        super().__init__(intents=intents or build_intents())
        self._config = config
        self._app_factory = app_factory
        self.app: AgentApp | None = None
        self.messenger = DiscordMessenger(
            get_channel=lambda: self.get_channel(config.channel_id),
            fetch_channel=lambda: self.fetch_channel(config.channel_id),
            max_download_bytes=config.max_attachment_bytes,
        )

    async def setup_hook(self) -> None:
        """登入之後、連線之前執行一次：這時候才組裝核心、取得「單一 agent」的鎖、啟動佇列工作者。"""
        self.app = self._app_factory(self._config, self.messenger)
        self.app.start()

    async def on_ready(self) -> None:
        """連線完成（斷線重連後也會再觸發一次）。確認找得到指定的文字頻道，並在終端機告訴使用者結果。"""
        channel = self.get_channel(self._config.channel_id)
        if channel is None or not hasattr(channel, "get_partial_message"):  # 找不到，或不是能收發訊息的文字頻道
            log.error("已連線，但找不到可用的指定頻道：請確認 bot 已加入伺服器、有「檢視頻道」權限，而且 CHANNEL_ID 沒填錯")
            print(CHANNEL_MISSING_MESSAGE, file=sys.stderr, flush=True)
        else:
            log.info("已連線，開始監看指定的頻道")
            print(CONNECTED_MESSAGE, flush=True)

    async def on_message(self, message: discord.Message) -> None:
        if self.app is None or not should_handle(message, self._config.channel_id, self.user.id if self.user else None):
            return
        try:
            incoming = to_incoming(message)
        except Exception as error:  # noqa: BLE001 - 轉換出錯只影響這一則
            log.error("轉換訊息失敗：%s", type(error).__name__)
            return
        await self.app.handle_message(incoming)  # 去重、限流、排隊、更正…都在核心裡

    async def close(self) -> None:
        try:
            if self.app is not None:
                await self.app.stop()  # 停掉工作者並放開單一 agent 的鎖
        finally:
            await self.messenger.aclose()
            await super().close()


def run_bot(config: Config, *, bot_factory: Callable[[Config], TicketBot] = TicketBot) -> int:
    """啟動並一直執行，直到被中斷。回傳結束代碼。錯誤訊息只說原因，不含 Token。"""
    config.ensure_dirs()
    handler = setup_logging(config.logs_dir, secrets=(config.discord_token,))
    logging.getLogger("discord").addHandler(handler)  # discord.py 自己的警告也寫進同一個（有遮蔽的）log
    logging.getLogger("discord").setLevel(logging.WARNING)
    bot = bot_factory(config)
    try:
        bot.run(config.discord_token, log_handler=None)  # Token 只在這裡使用
    except discord.LoginFailure:
        print("登入失敗：DISCORD_TOKEN 不正確，或已被重設。請到 Developer Portal 重新取得，更新 .env（見 docs/discord-bot-setup.md）。", file=sys.stderr)
        return 1
    except discord.PrivilegedIntentsRequired:
        print("連線被拒絕：Developer Portal 的 Bot 頁面還沒有開啟「Message Content Intent」並儲存（見 docs/discord-bot-setup.md 第 4 節）。", file=sys.stderr)
        return 1
    except AlreadyRunning as error:
        print(error, file=sys.stderr)
        return 1
    return 0
