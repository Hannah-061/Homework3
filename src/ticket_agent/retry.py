"""有上限的重試（docs/architecture.md 第 4 節第 10 點）。

重試次數一定有上限（最多 MAX_RETRIES 次），不會無限重試。取消（CancelledError）不會被吞掉。
log 只記「第幾次、什麼類型的錯誤」，不記錯誤裡的內容。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")
MAX_RETRIES = 5  # 不管設定檔怎麼寫，都不會超過


async def with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    retries: int,
    retry_on: tuple[type[BaseException], ...],
    no_retry: tuple[type[BaseException], ...] = (),
    what: str = "操作",
    delay_s: float = 1.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """執行 operation。遇到 retry_on 的錯誤最多再試 retries 次（總共 retries + 1 次），仍失敗就把最後一個錯誤丟出去。
    no_retry 裡的錯誤（例如隔離被破壞）一次都不重試，直接丟出去。每次重試前等 delay_s * 第幾次 秒。"""
    retries = max(0, min(int(retries), MAX_RETRIES))
    attempt = 0
    while True:
        attempt += 1
        try:
            return await operation()
        except no_retry:
            raise
        except retry_on as error:
            if attempt > retries:
                log.warning("%s失敗，共嘗試 %d 次仍失敗：%s", what, attempt, type(error).__name__)
                raise
            log.info("%s第 %d 次失敗（%s），稍後重試", what, attempt, type(error).__name__)
            await sleep(delay_s * attempt)
