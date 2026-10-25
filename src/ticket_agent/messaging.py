"""和 Discord 之間的「抽象介面」（docs/architecture.md 第 4 節）。

整個 agent 的邏輯只認這裡定義的資料結構與 Messenger 介面，完全不 import discord。
真正的 discord.py 連線（把 discord 的訊息轉成 IncomingMessage、實作 Messenger）是另外一層薄薄的轉接，
所以所有流程都可以用假的 Discord 事件測試。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Attachment:
    id: str
    filename: str
    content_type: str | None
    size: int
    url: str


@dataclass(frozen=True)
class IncomingMessage:
    id: str
    channel_id: int
    author_id: str  # 身分判斷一律用這個，不用顯示名稱
    author_name: str
    author_is_bot: bool
    content: str
    attachments: tuple[Attachment, ...] = ()
    created_at: datetime = field(default_factory=datetime.now)  # 本機時區、沒有時區資訊
    reference_message_id: str | None = None  # 這則訊息是在「回覆」哪一則
    is_dm: bool = False


class Messenger(Protocol):
    async def reply(self, to: IncomingMessage, text: str) -> str | None:
        """回覆一則訊息，回傳新訊息的編號；送不出去回傳 None。送出的內容不可以觸發 @提及。"""

    async def edit(self, message_id: str, text: str) -> bool:
        """編輯 bot 自己先前送出的訊息。成功回傳 True，失敗（被刪、沒權限）回傳 False。"""

    async def download(self, attachment: Attachment) -> bytes:
        """下載附件。轉接層只能從 Discord 官方檔案網址下載，而且要限制大小。失敗丟例外。"""


async def safe_reply(messenger: Messenger, to: IncomingMessage, text: str) -> str | None:
    """回覆失敗不可以讓整個流程當掉：只記錄「失敗了」，不記錄內容。"""
    try:
        return await messenger.reply(to, text)
    except Exception as error:  # noqa: BLE001 - 外部服務可能丟任何例外
        log.warning("送出回覆失敗：%s", type(error).__name__)
        return None


async def safe_edit(messenger: Messenger, message_id: str, text: str) -> bool:
    try:
        return bool(await messenger.edit(message_id, text))
    except Exception as error:  # noqa: BLE001
        log.warning("編輯訊息失敗：%s", type(error).__name__)
        return False
