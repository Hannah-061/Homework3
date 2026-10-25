"""log 設定（docs/architecture.md 12.3：不記敏感資料到 log）。

程式裡本來就約定「log 只寫類型、不寫內容」；這裡是第二道保險：萬一哪裡不小心把機密寫進 log，
過濾器會在寫出去之前把 Discord token、票號、長串數字遮掉。log 檔只有本人可讀寫。
"""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}")
_TICKET_NUMBER = re.compile(r"(票號[:：]\s*)\S+")
_LONG_DIGITS = re.compile(r"\d{12,}")
LOG_NAME = "agent.log"


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: tuple[str, ...] = ()):
        super().__init__()
        self._secrets = tuple(s for s in secrets if s and len(s) >= 8)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[已遮蔽]")
        text = _TOKEN.sub("[已遮蔽的token]", text)
        text = _TICKET_NUMBER.sub(r"\1[已遮蔽]", text)
        return _LONG_DIGITS.sub("[已遮蔽的長數字]", text)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = None
        return True


def setup_logging(logs_dir: Path, *, secrets: tuple[str, ...] = (), level: int = logging.INFO) -> logging.Handler:
    """把 log 寫到 DATA_DIR/logs/agent.log（輪替，最多約 5 MB × 3 份）。回傳 handler，測試時可以拿來移除。"""
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / LOG_NAME
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    os.chmod(path, 0o600)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(secrets))
    root = logging.getLogger("ticket_agent")
    root.setLevel(level)
    root.addHandler(handler)
    return handler
