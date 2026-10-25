"""把所有零件組起來（docs/architecture.md 第 2 節）。

build_app 之後得到一個 AgentApp：收到訊息就呼叫 handle_message，start/stop 控制背景的照片工作者。
這一層不知道 Discord 長什麼樣，只認 messaging.Messenger；真正的 discord.py 連線是下一個步驟的轉接層。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .analyzer import analyze_photo
from .claude_runner import RunnerSettings
from .config import Config
from .corrections import request_correction
from .excel_store import TicketStore
from .intake import MessageRouter, RequestCorrectionFn, RouterSettings
from .locking import InstanceLock
from .messaging import IncomingMessage, Messenger
from .queue_worker import AnalyzeFn, PhotoPipeline, PhotoWorker, PipelineSettings
from .ratelimit import RateLimiter
from .store_gateway import StoreGateway


@dataclass
class AgentApp:
    router: MessageRouter
    worker: PhotoWorker
    gateway: StoreGateway
    lock: InstanceLock

    async def handle_message(self, message: IncomingMessage) -> None:
        await self.router.handle(message)

    def start(self) -> None:
        self.worker.start()

    async def drain(self) -> None:
        """等到排隊中的照片都處理完。"""
        await self.worker.join()

    async def stop(self) -> None:
        await self.worker.stop()
        self.lock.release()


def build_app(
    config: Config,
    messenger: Messenger,
    *,
    analyze: AnalyzeFn = analyze_photo,
    correct: RequestCorrectionFn = request_correction,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    retry_delay_s: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    max_queue: int = 50,
) -> AgentApp:
    """建立資料夾、取得「同一時間只有一個 agent」的鎖、組好所有零件。鎖拿不到會丟 AlreadyRunning。"""
    config.ensure_dirs()
    lock = InstanceLock(config.data_dir)
    lock.acquire()
    try:
        runner = RunnerSettings.from_config(config)
        gateway = StoreGateway(TicketStore(config.excel_path), delay_s=retry_delay_s, sleep=sleep)
        pipeline_settings = PipelineSettings.from_config(config)
        pipeline = PhotoPipeline(
            PipelineSettings(**{**pipeline_settings.__dict__, "retry_delay_s": retry_delay_s}),
            runner, messenger, gateway, analyze=analyze, sleep=sleep)
        worker = PhotoWorker(pipeline, max_queue=max_queue)
        router_settings = RouterSettings.from_config(config)
        router = MessageRouter(
            RouterSettings(**{**router_settings.__dict__, "retry_delay_s": retry_delay_s}),
            runner, messenger, gateway, worker, RateLimiter(config.rate_limit_per_min),
            correct=correct, clock=clock, sleep=sleep)
    except BaseException:
        lock.release()
        raise
    return AgentApp(router=router, worker=worker, gateway=gateway, lock=lock)
