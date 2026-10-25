"""限流：每人每分鐘最多處理 N 張（docs/architecture.md 第 4 節第 3 點、12.3）。純計算，不碰網路。

- 滑動視窗：只計算「被接受」的張數，被擋掉的不占名額。
- 同一個人連續被擋時，一個視窗內只提醒一次，免得 bot 自己洗版。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    allowed: bool
    notify: bool  # 只有被擋、而且這個視窗內還沒提醒過，才是 True


class RateLimiter:
    def __init__(self, limit: int, window_s: float = 60.0, clock: Callable[[], float] = time.monotonic):
        if limit < 1 or window_s <= 0:
            raise ValueError("limit 與 window_s 必須大於 0")
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._accepted: dict[str, deque[float]] = {}
        self._last_notice: dict[str, float] = {}

    def check(self, user_id: str) -> Decision:
        now = self._clock()
        self._forget_stale(now)
        accepted = self._accepted.setdefault(user_id, deque())
        while accepted and now - accepted[0] >= self.window_s:
            accepted.popleft()
        if len(accepted) < self.limit:
            accepted.append(now)
            return Decision(allowed=True, notify=False)
        last = self._last_notice.get(user_id)
        if last is None or now - last >= self.window_s:
            self._last_notice[user_id] = now
            return Decision(allowed=False, notify=True)
        return Decision(allowed=False, notify=False)

    def _forget_stale(self, now: float) -> None:
        """清掉已經超過一個視窗沒動靜的使用者，避免記憶體越長越大。"""
        for user_id in [u for u, q in self._accepted.items() if not q or now - q[-1] >= self.window_s]:
            if now - self._last_notice.get(user_id, float("-inf")) >= self.window_s:
                self._accepted.pop(user_id, None)
                self._last_notice.pop(user_id, None)
