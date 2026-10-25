"""Excel 的「唯一入口」：所有非同步工作都透過這裡讀寫，同一時間只有一個在碰 Excel。

- asyncio.Lock：同一時間只有一個工作在讀寫 Excel 檔，不會有兩個工作同時「讀進整個檔 → 加一列 → 存檔」
  而互相蓋掉對方（紀錄遺失）。（docs/architecture.md 第 7 節）
- asyncio.to_thread：openpyxl 讀寫是會卡住的同步動作，丟到另一個執行緒，才不會卡住整個事件迴圈
  （收訊息、回「處理中」）。鎖保證同一時間只有一個執行緒在碰檔案。
- run_with_retries：Excel 暫時寫不進去（例如你正開著檔案）時，有上限地重試。
  重試的函式必須是「重做一次也不會重複寫入」的（呼叫端用照片位置當作識別，先檢查是否已經寫進去）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .excel_store import StoreError, TicketStore
from .retry import with_retries

T = TypeVar("T")


class StoreGateway:
    def __init__(
        self,
        store: TicketStore,
        *,
        attempts: int = 3,
        delay_s: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._store = store
        self._lock = asyncio.Lock()
        self._attempts = attempts
        self._delay_s = delay_s
        self._sleep = sleep

    async def run(self, operation: Callable[[TicketStore], T]) -> T:
        """拿到鎖之後，在另一個執行緒執行 operation(store)。只執行一次。"""
        async with self._lock:
            return await asyncio.to_thread(operation, self._store)

    async def run_with_retries(self, operation: Callable[[TicketStore], T]) -> T:
        """同 run，但 Excel 寫入失敗（StoreError）時最多共嘗試 attempts 次。每次嘗試才拿鎖，等待的時候不占著鎖。"""
        return await with_retries(
            lambda: self.run(operation), retries=self._attempts - 1, retry_on=(StoreError,),
            what="寫入 Excel", delay_s=self._delay_s, sleep=self._sleep,
        )
