"""同一時間只能有一個 agent 在用同一個 DATA_DIR（docs/architecture.md 第 7、8.4 節）。

非同步工作之間的互斥由 store_gateway.StoreGateway 的 asyncio.Lock 負責；
這裡防的是另一種情況：你不小心開了兩個 agent，兩個程序同時改同一個 Excel 檔。
用作業系統的檔案鎖（flock），程序結束或當掉時作業系統會自動釋放。
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

LOCK_NAME = ".agent.lock"


class AlreadyRunning(Exception):
    """另一個 agent 正在使用這個資料目錄。"""


class InstanceLock:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / LOCK_NAME
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            raise AlreadyRunning("已經有另一個 agent 正在使用這個資料目錄，請先關掉它") from error
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()
