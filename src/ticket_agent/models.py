"""各模組共用的資料結構（沒有任何邏輯）。欄位對應 docs/architecture.md 第 8.2 節。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

STATUS_ACTIVE = "有效"
STATUS_DELETED = "已刪除"
STATUS_REMOVED = "已更正移除"  # 上傳者更正成「這不是票券」


@dataclass(frozen=True)
class TicketData:
    """票券本身的內容（Claude 辨識出來的，或上傳者更正後的）。看不到的欄位是空字串或 None。"""

    ticket_type: str = ""
    name: str = ""
    event_datetime: str = ""  # 'YYYY-MM-DD HH:MM' 或 'YYYY-MM-DD'，看不到就空字串
    location: str = ""
    seat_or_type: str = ""
    amount: float | None = None
    currency: str = ""  # 三個大寫字母，例如 TWD；看不到就空字串
    other_info: str = ""  # 票號、備註、「共 N 張」等。可能含票號，回覆與查詢絕對不顯示


@dataclass(frozen=True)
class TicketRecord:
    """Excel「紀錄」工作表裡的一列。"""

    serial: int
    recorded_at: datetime  # 收到照片的時間（本機時區，沒有時區資訊）
    uploader: str  # Discord 顯示名稱（外部輸入，寫進 Excel 前要過防護 4）
    uploader_id: str  # Discord 帳號編號：身分判斷一律用這個
    data: TicketData
    photo_paths: tuple[str, ...] = ()  # 相對於 DATA_DIR；重複上傳時新的在前
    status: str = STATUS_ACTIVE
    reply_message_ids: tuple[str, ...] = ()
    photo_sha256: tuple[str, ...] = ()  # 這一筆對應的照片雜湊（原始位元組的 SHA-256）；補齊過的紀錄有多個，新的在前。舊紀錄是空的
