"""收到訊息之後怎麼分流（docs/architecture.md 第 4、7、11 節）。

只處理指定頻道、不是 bot、不是私訊的訊息。依序判斷：
1. 是「回覆」某則訊息：可能是刪除確認（回覆「確定」），或是更正某筆紀錄。
2. 是 `!` 指令：`!我的票券`、`!刪除`。
3. 附有檔案：逐一檢查、限流、回「處理中」、排進佇列。
4. 其他（純聊天文字）：不理。

安全規則：誰可以改、改哪一筆、要不要刪，都由這裡用「上傳者ID」決定，從不交給 Claude 判斷。
log 只寫類型，不寫訊息內容、名稱或票號。
"""

from __future__ import annotations

import asyncio
import logging
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from . import replies
from .analyzer import InvalidResult
from .claude_runner import ClaudeFailed, IsolationError, RunnerSettings
from .commands import BadCommand, DeleteCommand, HelpCommand, ListCommand, parse_command
from .config import Config
from .corrections import Correction, CorrectionUnclear, request_correction
from .excel_store import StoreError, TicketStore
from .media import attachment_looks_supported
from .messaging import IncomingMessage, Messenger, safe_edit, safe_reply
from .models import STATUS_ACTIVE, STATUS_DELETED, STATUS_REMOVED
from .queue_worker import PhotoJob, PhotoWorker
from .ratelimit import RateLimiter
from .retry import with_retries
from .store_gateway import StoreGateway
from .totals import month_key

log = logging.getLogger(__name__)

CONFIRM_WORDS = ("確定",)
SEEN_LIMIT = 2000
PENDING_LIMIT = 200

RequestCorrectionFn = Callable[[RunnerSettings, str], Awaitable[Correction]]


@dataclass(frozen=True)
class RouterSettings:
    channel_id: int
    max_attachment_bytes: int
    claude_retries: int
    max_attachments_per_message: int = 5
    confirm_timeout_s: int = 60
    retry_delay_s: float = 1.0

    @classmethod
    def from_config(cls, config: Config) -> RouterSettings:
        return cls(channel_id=config.channel_id, max_attachment_bytes=config.max_attachment_bytes,
                   claude_retries=config.claude_retries)


@dataclass(frozen=True)
class PendingDelete:
    serial: int
    user_id: str
    expires_at: float


class MessageRouter:
    def __init__(
        self,
        settings: RouterSettings,
        runner: RunnerSettings,
        messenger: Messenger,
        gateway: StoreGateway,
        worker: PhotoWorker,
        limiter: RateLimiter,
        *,
        correct: RequestCorrectionFn = request_correction,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._s = settings
        self._runner = runner
        self._messenger = messenger
        self._gateway = gateway
        self._worker = worker
        self._limiter = limiter
        self._correct = correct
        self._clock = clock
        self._sleep = sleep
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._pending_deletes: OrderedDict[str, PendingDelete] = OrderedDict()

    async def handle(self, message: IncomingMessage) -> None:
        """處理一則收到的訊息。除了被取消以外，不會丟出例外。"""
        if message.is_dm or message.channel_id != self._s.channel_id or message.author_is_bot:
            return
        if self._already_seen(message.id):  # Discord 重連時可能重送同一則，不可以處理兩次
            return
        try:
            command = parse_command(message.content)
            if command is None and message.reference_message_id and not message.attachments and message.content.strip():
                if await self._handle_reply(message):
                    return
            if command is not None:
                await self._handle_command(message, command)
            elif message.attachments:
                await self._handle_attachments(message)
        except Exception as error:  # noqa: BLE001 - 一則訊息出錯不可以影響其他訊息
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            log.error("處理訊息時發生未預期的錯誤：%s", type(error).__name__)
            await safe_reply(self._messenger, message, replies.format_failure())

    # ---------------------------------------------------------------- 附件

    async def _handle_attachments(self, message: IncomingMessage) -> None:
        attachments = message.attachments[: self._s.max_attachments_per_message]
        if len(message.attachments) > len(attachments):
            await safe_reply(self._messenger, message, replies.format_too_many_attachments(self._s.max_attachments_per_message))
        unsupported = False
        for attachment in attachments:
            if not attachment_looks_supported(attachment, self._s.max_attachment_bytes):
                unsupported = True
                continue
            decision = self._limiter.check(message.author_id)
            if not decision.allowed:
                if decision.notify:
                    await safe_reply(self._messenger, message, replies.format_rate_limited())
                break
            if self._worker.full:
                await safe_reply(self._messenger, message, replies.format_busy())
                break
            status_id = await safe_reply(self._messenger, message, replies.format_processing(self._worker.pending))
            if not self._worker.submit(PhotoJob(message=message, attachment=attachment, status_message_id=status_id)):
                text = replies.format_busy()
                if not (status_id and await safe_edit(self._messenger, status_id, text)):
                    await safe_reply(self._messenger, message, text)
                break
        if unsupported:
            await safe_reply(self._messenger, message, replies.format_unsupported_attachment(self._s.max_attachment_bytes // (1024 * 1024)))

    # ---------------------------------------------------------------- 指令

    async def _handle_command(self, message: IncomingMessage, command: ListCommand | DeleteCommand | HelpCommand | BadCommand) -> None:
        if isinstance(command, BadCommand):
            await safe_reply(self._messenger, message, command.usage)
            return
        if not await self._within_rate_limit(message):  # 說明也一樣要過限流（不繞過任何保護）
            return
        if isinstance(command, HelpCommand):
            await safe_reply(self._messenger, message, replies.format_help(  # 固定文字：不讀 Excel、不呼叫 Claude
                max_mb=self._s.max_attachment_bytes // (1024 * 1024), max_attachments=self._s.max_attachments_per_message,
                confirm_timeout_s=self._s.confirm_timeout_s, rate_limit_per_min=self._limiter.limit))
        elif isinstance(command, ListCommand):
            await self._list(message, command)
        else:
            await self._ask_delete_confirmation(message, command)

    async def _within_rate_limit(self, message: IncomingMessage) -> bool:
        """指令與更正和照片共用同一個限流（每人每分鐘 N 次）。被擋時，同一個視窗只提醒一次。"""
        decision = self._limiter.check(message.author_id)
        if not decision.allowed and decision.notify:
            await safe_reply(self._messenger, message, replies.format_rate_limited())
        return decision.allowed

    async def _list(self, message: IncomingMessage, command: ListCommand) -> None:
        uid = message.author_id
        now_month = month_key(message.created_at)
        month = {"month": now_month, "all": None}.get(command.period, command.period)
        label = {"month": now_month, "all": "全部"}.get(command.period, command.period)

        def read(store: TicketStore):
            return store.list_records(uid, month=month), store.summary(uid)

        records, summary = await self._gateway.run(read)
        for chunk in replies.split_message(replies.format_user_list(records, summary, label)):
            await safe_reply(self._messenger, message, chunk)

    async def _ask_delete_confirmation(self, message: IncomingMessage, command: DeleteCommand) -> None:
        uid = message.author_id
        record = await self._gateway.run(lambda store: store.get(command.serial))
        if record is None or record.uploader_id != uid or record.status != STATUS_ACTIVE:
            await safe_reply(self._messenger, message, replies.format_not_found())  # 別人的、已刪除的、不存在的，一律同樣回覆
            return
        confirm_id = await safe_reply(
            self._messenger, message, replies.format_delete_confirm(record, self._s.confirm_timeout_s))
        if confirm_id:
            self._purge_expired_confirmations()
            self._pending_deletes[confirm_id] = PendingDelete(record.serial, uid, self._clock() + self._s.confirm_timeout_s)
            while len(self._pending_deletes) > PENDING_LIMIT:
                self._pending_deletes.popitem(last=False)

    # ---------------------------------------------------------------- 回覆（刪除確認、更正）

    async def _handle_reply(self, message: IncomingMessage) -> bool:
        """回傳 True 代表這則訊息已經處理完了（或該被忽略）；False 代表它不是對我們的回覆，繼續往下判斷。"""
        reference = message.reference_message_id
        pending = self._pending_deletes.get(reference)
        if pending is not None:
            return await self._handle_delete_confirmation(message, pending)
        record = await self._gateway.run(lambda store: store.find_by_reply_message_id(reference))
        if record is None:
            return False
        await self._handle_correction(message, record.serial)
        return True

    async def _handle_delete_confirmation(self, message: IncomingMessage, pending: PendingDelete) -> bool:
        if message.author_id != pending.user_id:
            return True  # 別人回覆別人的確認訊息：無視
        if self._clock() > pending.expires_at:
            self._pending_deletes.pop(message.reference_message_id, None)
            await safe_reply(self._messenger, message, replies.format_confirm_expired())
            return True
        if unicodedata.normalize("NFKC", message.content).strip() not in CONFIRM_WORDS:
            return True  # 不是「確定」：維持等待，直到逾時
        self._pending_deletes.pop(message.reference_message_id, None)

        def delete(store: TicketStore):
            record = store.get(pending.serial)
            if record is None or record.uploader_id != message.author_id or record.status != STATUS_ACTIVE:
                return None, None  # 在這段時間裡被刪掉或改變了：什麼都不做
            return store.set_status(pending.serial, STATUS_DELETED), store.summary(message.author_id)

        try:
            record, summary = await self._gateway.run_with_retries(delete)
        except StoreError:
            await safe_reply(self._messenger, message, replies.format_failure())
            return True
        text = replies.format_not_found() if record is None else replies.format_deleted(record, summary)
        await safe_reply(self._messenger, message, text)
        return True

    async def _handle_correction(self, message: IncomingMessage, serial: int) -> None:
        uid = message.author_id
        record = await self._gateway.run(lambda store: store.get(serial))
        if record is None or record.status != STATUS_ACTIVE:
            await safe_reply(self._messenger, message, replies.format_not_found())
            return
        if record.uploader_id != uid:  # 權限由程式用上傳者ID判斷
            await safe_reply(self._messenger, message, replies.format_not_owner())
            return
        if not await self._within_rate_limit(message):
            return
        try:
            correction = await with_retries(
                lambda: self._correct(self._runner, message.content),
                retries=self._s.claude_retries, retry_on=(ClaudeFailed, InvalidResult), no_retry=(IsolationError,),
                what="解析更正", delay_s=self._s.retry_delay_s, sleep=self._sleep,
            )
        except CorrectionUnclear:
            asks_for_ticket_number = "票號" in message.content
            await safe_reply(self._messenger, message,
                             replies.format_ticket_number_notice() if asks_for_ticket_number else replies.format_correction_unclear())
            return
        except (ClaudeFailed, InvalidResult) as error:
            log.warning("解析更正失敗：%s", type(error).__name__)
            await safe_reply(self._messenger, message, replies.format_failure())
            return

        def apply(store: TicketStore):
            """在鎖裡面再檢查一次擁有者與狀態，避免剛才到現在之間有變動。"""
            current = store.get(serial)
            if current is None or current.uploader_id != uid or current.status != STATUS_ACTIVE:
                return None
            if correction.mark_not_ticket:
                after = store.set_status(serial, STATUS_REMOVED)
            elif replies.changed_fields(current.data, replace(current.data, **correction.changes)):
                after = store.update_fields(serial, correction.changes)
            else:
                return current, current, store.summary(uid)
            return current, after, store.summary(uid)

        try:
            result = await self._gateway.run_with_retries(apply)
        except StoreError:
            await safe_reply(self._messenger, message, replies.format_failure())
            return
        if result is None:
            await safe_reply(self._messenger, message, replies.format_not_found())
            return
        before, after, summary = result
        if correction.mark_not_ticket:
            text = replies.format_marked_not_ticket(after, summary)
        else:
            changed = replies.changed_fields(before.data, after.data)
            text = replies.format_correction(before, after, changed, summary) if changed else replies.format_no_change()
            if "票號" in message.content:  # 其他欄位照常更正，但要讓使用者知道票號沒有被改
                text += "\n\n" + replies.format_ticket_number_notice()
        new_id = await safe_reply(self._messenger, message, text)
        if new_id and not correction.mark_not_ticket:  # 之後回覆這則也能繼續更正
            try:
                await self._gateway.run_with_retries(lambda store: store.add_reply_message_id(serial, new_id))
            except StoreError:
                log.warning("無法記錄回覆訊息編號")

    # ---------------------------------------------------------------- 小工具

    def _already_seen(self, message_id: str) -> bool:
        if message_id in self._seen:
            return True
        self._seen[message_id] = None
        while len(self._seen) > SEEN_LIMIT:
            self._seen.popitem(last=False)
        return False

    def _purge_expired_confirmations(self) -> None:
        now = self._clock()
        for key in [k for k, v in self._pending_deletes.items() if v.expires_at < now]:
            del self._pending_deletes[key]
