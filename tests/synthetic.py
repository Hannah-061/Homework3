"""測試用的合成資料與假零件：假的 Discord（FakeMessenger）、假的辨識、合成的假票券圖。
全部都是假的，沒有任何真實票券、帳號或機密。"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

from ticket_agent.analyzer import AnalysisResult, parse_analysis
from ticket_agent.config import Config
from ticket_agent.messaging import Attachment, IncomingMessage

CHANNEL = 4242424242424242
OK_URL = "https://cdn.discordapp.com/attachments/1/2/ticket.jpg"
FAKE_TOKEN = "FAKE-DISCORD-TOKEN-FOR-TESTS"
NOW = datetime(2026, 10, 24, 15, 30, 12)

GOOD = {
    "is_ticket": True, "not_ticket_description": "", "ticket_type": "演唱會", "name": "測試演唱會 2099",
    "event_datetime": "2099-12-31 19:30", "location": "假的場館", "seat_or_type": "A 區 12 排", "amount": 1200,
    "currency": "TWD", "currency_guessed": False, "ticket_count": 1, "ticket_number": "TEST-0001",
    "other_info": "", "unreadable_fields": [],
}


def analysis(**overrides) -> AnalysisResult:
    return parse_analysis({**GOOD, **overrides})


def not_ticket(description: str = "一杯咖啡") -> AnalysisResult:
    return parse_analysis({"is_ticket": False, "not_ticket_description": description})


# ------------------------------------------------------------------ 合成圖片

def _draw_ticket(lines: list[str], size=(900, 500)) -> Image.Image:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=30)
    for i, line in enumerate(lines):
        draw.text((30, 30 + i * 60), line, fill="black", font=font)
    return image


def jpeg_bytes(lines=("TEST CONCERT 2099 (SYNTHETIC)", "Seat A12  TWD 1200"), size=(900, 500)) -> bytes:
    buffer = io.BytesIO()
    _draw_ticket(list(lines), size).save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


def png_bytes(size=(600, 300), alpha=False) -> bytes:
    buffer = io.BytesIO()
    image = _draw_ticket(["PNG TEST TICKET"], size)
    if alpha:
        image = image.convert("RGBA")
        image.putpixel((0, 0), (0, 0, 0, 0))
    image.save(buffer, "PNG")
    return buffer.getvalue()


def webp_bytes(size=(600, 300)) -> bytes:
    buffer = io.BytesIO()
    _draw_ticket(["WEBP TEST TICKET"], size).save(buffer, "WEBP")
    return buffer.getvalue()


def pdf_bytes(pages=1, size=(300, 200), encrypted=False) -> bytes:
    doc = pymupdf.open()
    for number in range(pages):
        page = doc.new_page(width=size[0], height=size[1])
        page.insert_text((20, 50), f"SYNTHETIC TEST PDF PAGE {number + 1}")
    if encrypted:
        return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="user-pw", owner_pw="owner-pw")
    return doc.tobytes()


# ------------------------------------------------------------------ 假的 Discord

def make_attachment(att_id="a1", filename="ticket.jpg", size=5000, content_type="image/jpeg", url=OK_URL) -> Attachment:
    return Attachment(id=att_id, filename=filename, content_type=content_type, size=size, url=url)


def make_message(msg_id="m1", *, author_id="100", name="小明", content="", attachments=(), channel=CHANNEL,
                 reference=None, bot=False, dm=False, when=NOW) -> IncomingMessage:
    return IncomingMessage(id=msg_id, channel_id=channel, author_id=author_id, author_name=name, author_is_bot=bot,
                           content=content, attachments=tuple(attachments), created_at=when,
                           reference_message_id=reference, is_dm=dm)


class FakeMessenger:
    def __init__(self):
        self.sent: list[dict] = []  # {"id", "to", "text"}
        self.edits: list[tuple[str, str]] = []
        self.downloads: dict[str, bytes | Exception] = {}
        self.edit_ok = True
        self.reply_fails = False
        self.download_calls: list[str] = []
        self._counter = 0

    async def reply(self, to, text):
        if self.reply_fails:
            raise RuntimeError("discord down")
        self._counter += 1
        message_id = f"bot-{self._counter}"
        self.sent.append({"id": message_id, "to": to.id, "text": text})
        return message_id

    async def edit(self, message_id, text):
        if not self.edit_ok:
            return False
        self.edits.append((message_id, text))
        return True

    async def download(self, attachment):
        self.download_calls.append(attachment.id)
        item = self.downloads.get(attachment.id)
        if isinstance(item, Exception):
            raise item
        if item is None:
            raise FileNotFoundError("沒有這個附件")
        return item

    def text_of(self, message_id: str) -> str:
        """某則 bot 訊息「現在」使用者看到的內容：最後一次編輯，沒有編輯過就是原本送出的。"""
        for mid, text in reversed(self.edits):
            if mid == message_id:
                return text
        return next(item["text"] for item in self.sent if item["id"] == message_id)

    def visible_texts(self) -> list[str]:
        return [self.text_of(item["id"]) for item in self.sent]


class ScriptedAnalyzer:
    """假的辨識：依劇本依序回傳結果或丟例外，劇本用完就重複最後一項。記錄被呼叫幾次。"""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or [analysis()]
        self.calls = 0
        self.images: list[bytes] = []

    async def __call__(self, runner, jpeg):
        index = min(self.calls, len(self.outcomes) - 1)
        self.calls += 1
        self.images.append(jpeg)
        outcome = self.outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome(jpeg) if callable(outcome) else outcome


class Sleeper:
    """假的 sleep：不真的等，只記錄被要求等多久。"""

    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, seconds):
        self.delays.append(seconds)


def make_config(tmp: Path, **overrides) -> Config:
    values = dict(discord_token=FAKE_TOKEN, channel_id=CHANNEL, claude_model="sonnet",
                  data_dir=tmp / "data", work_dir=tmp / "work", claude_retries=2, rate_limit_per_min=8)
    values.update(overrides)
    return Config(**values)


class ScriptedCorrect:
    """假的「解析更正」：依劇本回傳 Correction 或丟例外。記錄收到的文字。"""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.texts: list[str] = []

    async def __call__(self, runner, text):
        index = min(self.calls, len(self.outcomes) - 1)
        self.calls += 1
        self.texts.append(text)
        outcome = self.outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome(text) if callable(outcome) else outcome


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now
