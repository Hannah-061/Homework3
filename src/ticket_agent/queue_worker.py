"""一張照片的完整處理流程，以及「排隊、一次處理一張」的工作者（docs/architecture.md 第 4、7 節）。

流程：下載 → 檢查與前處理 → 辨識（有上限的重試）→ 分流：
- 不是票券：存照片、回覆，不寫 Excel。
- 是票券：存照片 → 判斷是否重複 → 寫 Excel（新增，或補齊舊的一筆）→ 回覆。
- 失敗：存照片（檔名標「處理失敗」）、不寫 Excel、回覆「處理失敗」。
任何一張照片出錯，都只影響那一張，工作者會繼續處理下一張。log 只寫類型，不寫內容。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from . import replies
from .analyzer import AnalysisResult, InvalidResult, analyze_photo, clean_text
from .claude_runner import ClaudeFailed, IsolationError, RunnerSettings
from .config import Config
from .excel_store import StoreError, TicketStore
from .media import MediaError, prepare_image
from .messaging import IncomingMessage, Attachment, Messenger, safe_edit, safe_reply
from .photos import format_stamp, store_photo
from .retry import with_retries
from .store_gateway import StoreGateway

log = logging.getLogger(__name__)

AnalyzeFn = Callable[[RunnerSettings, bytes], Awaitable[AnalysisResult]]
MAX_UPLOADER_NAME = 80


@dataclass(frozen=True)
class PhotoJob:
    message: IncomingMessage
    attachment: Attachment
    status_message_id: str | None  # 先前回的「處理中」那則訊息，處理完會被編輯成結果


@dataclass(frozen=True)
class PipelineSettings:
    data_dir: Path
    max_attachment_bytes: int
    claude_retries: int
    retry_delay_s: float = 1.0

    @classmethod
    def from_config(cls, config: Config) -> PipelineSettings:
        return cls(data_dir=config.data_dir, max_attachment_bytes=config.max_attachment_bytes,
                   claude_retries=config.claude_retries)


class PhotoPipeline:
    def __init__(
        self,
        settings: PipelineSettings,
        runner: RunnerSettings,
        messenger: Messenger,
        gateway: StoreGateway,
        *,
        analyze: AnalyzeFn = analyze_photo,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._settings = settings
        self._runner = runner
        self._messenger = messenger
        self._gateway = gateway
        self._analyze = analyze
        self._sleep = sleep
        self._photos_dir = settings.data_dir / "photos"
        self._photos_dir.mkdir(parents=True, exist_ok=True)

    async def process(self, job: PhotoJob) -> None:
        """處理一張照片。除了被取消以外，不會丟出任何例外。"""
        try:
            await self._process(job)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 一張照片出錯不可以拖垮工作者
            log.error("處理照片時發生未預期的錯誤：%s", type(error).__name__)
            await self._finish(job, replies.format_failure())

    # ------------------------------------------------------------------

    async def _process(self, job: PhotoJob) -> None:
        try:
            raw = await self._messenger.download(job.attachment)
        except Exception as error:  # noqa: BLE001
            log.warning("下載附件失敗：%s", type(error).__name__)
            await self._finish(job, replies.format_failure())
            return
        try:
            jpeg = await asyncio.to_thread(prepare_image, raw, max_bytes=self._settings.max_attachment_bytes)
        except MediaError as error:
            log.info("附件無法處理：%s", error)
            await self._finish(job, replies.format_unsupported_attachment(self._settings.max_attachment_bytes // (1024 * 1024)))
            return

        try:
            analysis = await with_retries(
                lambda: self._analyze(self._runner, jpeg),
                retries=self._settings.claude_retries,
                retry_on=(ClaudeFailed, InvalidResult),
                no_retry=(IsolationError,),  # 隔離被破壞是安全問題，不重試
                what="辨識", delay_s=self._settings.retry_delay_s, sleep=self._sleep,
            )
        except (ClaudeFailed, InvalidResult) as error:
            log.warning("辨識失敗：%s", type(error).__name__)
            await self._fail(job, jpeg)
            return

        if not analysis.is_ticket:
            await self._save_photo(job, jpeg, f"非票券-{analysis.not_ticket_description}")
            await self._finish(job, replies.format_not_ticket(analysis.not_ticket_description))
            return
        await self._record_ticket(job, jpeg, analysis)

    async def _record_ticket(self, job: PhotoJob, jpeg: bytes, analysis: AnalysisResult) -> None:
        data = analysis.to_ticket_data()
        saved = await self._save_photo(job, jpeg, data.name or data.ticket_type or "票券")
        if saved is None:
            await self._finish(job, replies.format_failure())
            return
        photo_rel = str(saved.relative_to(self._settings.data_dir))
        message = job.message
        uploader = clean_text(message.author_name, MAX_UPLOADER_NAME, "uploader") or "（沒有名稱）"

        def write(store: TicketStore):
            """可以安全地重做：先用照片位置確認上一次是不是其實已經寫進去了。"""
            existing = store.find_by_photo_path(photo_rel)
            if existing is not None:
                record, updated = existing, len(existing.photo_paths) > 1
            else:
                duplicate = store.find_duplicate(message.author_id, data)
                if duplicate is not None:
                    record, updated = store.merge_into(duplicate.serial, data, photo_rel), True
                else:
                    record, updated = store.add_record(
                        uploader=uploader, uploader_id=message.author_id, data=data, photo_path=photo_rel,
                        recorded_at=message.created_at, reply_message_id=job.status_message_id), False
            if job.status_message_id and job.status_message_id not in record.reply_message_ids:
                record = store.add_reply_message_id(record.serial, job.status_message_id)
            return record, updated, store.summary(message.author_id)

        try:
            record, updated, summary = await self._gateway.run_with_retries(write)
        except StoreError:
            await self._finish(job, replies.format_failure())  # 照片已經存起來了，只是沒寫進 Excel
            return

        text = replies.format_ticket_reply(
            record, summary, updated=updated, ticket_count=analysis.ticket_count,
            currency_guessed=analysis.currency_guessed, unreadable=analysis.unreadable_fields)
        final_id = await self._finish(job, text)
        if final_id and final_id != job.status_message_id:  # 編輯失敗、改發新回覆：讓使用者回覆這一則也能更正
            try:
                await self._gateway.run_with_retries(lambda store: store.add_reply_message_id(record.serial, final_id))
            except StoreError:
                log.warning("無法記錄回覆訊息編號")

    async def _fail(self, job: PhotoJob, jpeg: bytes) -> None:
        await self._save_photo(job, jpeg, "處理失敗")
        await self._finish(job, replies.format_failure())

    async def _save_photo(self, job: PhotoJob, jpeg: bytes, label: str) -> Path | None:
        try:
            return await asyncio.to_thread(
                store_photo, self._photos_dir, format_stamp(job.message.created_at), label, jpeg)
        except Exception as error:  # noqa: BLE001 - 防護 3 的 UnsafePathError、磁碟錯誤等
            log.error("存照片失敗：%s", type(error).__name__)
            return None

    async def _finish(self, job: PhotoJob, text: str) -> str | None:
        """把結果送給使用者：優先編輯先前的「處理中」訊息；編輯不了就另外送一則新回覆。回傳帶有結果的訊息編號。"""
        if job.status_message_id and await safe_edit(self._messenger, job.status_message_id, text):
            return job.status_message_id
        return await safe_reply(self._messenger, job.message, text)


class PhotoWorker:
    """排隊、一次只處理一張（docs/architecture.md 第 7 節）。"""

    def __init__(self, pipeline: PhotoPipeline, max_queue: int = 50):
        self._pipeline = pipeline
        self._queue: asyncio.Queue[PhotoJob] = asyncio.Queue(maxsize=max_queue)
        self._busy = False
        self._task: asyncio.Task | None = None

    @property
    def pending(self) -> int:
        """目前在處理或排隊的張數（新來的照片「前面還有幾張」）。"""
        return self._queue.qsize() + (1 if self._busy else 0)

    @property
    def full(self) -> bool:
        return self._queue.full()

    def submit(self, job: PhotoJob) -> bool:
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            return False
        return True

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="photo-worker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def join(self) -> None:
        """等到佇列裡的工作都處理完（測試與正常關機時用）。"""
        await self._queue.join()

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            self._busy = True
            try:
                await self._pipeline.process(job)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - process 已經會處理，這是第二道保險
                log.error("工作者收到未預期的錯誤：%s", type(error).__name__)
            finally:
                self._busy = False
                self._queue.task_done()
