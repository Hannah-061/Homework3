"""讀取並檢查設定（.env）。

規則（見 docs/architecture.md 第 11 項與第 12 節）：
- 四個必填項目缺任何一個，啟動時一次列出所有問題並停止，不進入一半的狀態。
- 錯誤訊息、repr、log 都不會出現 DISCORD_TOKEN 或其他設定值的內容，只說「哪一項有什麼問題」。
- DATA_DIR 必須在專案資料夾之外，也不能把專案資料夾或暫存資料夾包在裡面
  （避免被 commit，也讓 claude -p 看不到 DATA_DIR，見防護 1）。
- 暫存資料夾（給 claude -p 當工作目錄）放在系統暫存區，不在專案裡：專案根目錄有 .env，
  工作目錄的上層若剛好是專案，風險比較大。它只給本人讀寫（權限 700），並檢查沒有被別人搶先佔用。
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


def default_work_dir() -> Path:
    """系統暫存區底下、每個使用者各自一個的資料夾（macOS 的暫存區本來就是每人獨立的）。"""
    return Path(tempfile.gettempdir()).resolve() / f"ticket-agent-work-{os.getuid()}"

REQUIRED_KEYS = ("DISCORD_TOKEN", "CHANNEL_ID", "CLAUDE_MODEL", "DATA_DIR")

# 選填項目：沒設就用預設值（architecture.md 第 4、5、7 節）。
OPTIONAL_INT_KEYS = {
    # key: (預設值, 最小值, 最大值)
    "RATE_LIMIT_PER_MIN": (8, 1, 100),
    "MAX_ATTACHMENT_MB": (10, 1, 25),
    "CLAUDE_TIMEOUT_S": (120, 10, 600),
    "CLAUDE_RETRIES": (2, 0, 5),
}

_PLACEHOLDER_HINTS = ("貼上", "你的", "your_", "<", ">")
_SNOWFLAKE = re.compile(r"\d{15,20}")
# 模型名稱或別名（sonnet、claude-sonnet-5-5、sonnet[1m]…）。第一個字元不能是 -，否則會被 claude 當成旗標。
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-\[\]]{0,99}")


class ConfigError(Exception):
    """設定有問題。problems 裡每一項都是一句給人看的說明，不含任何設定值。"""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("設定有問題：\n" + "\n".join(f"  - {p}" for p in self.problems))


@dataclass(frozen=True)
class Config:
    discord_token: str = field(repr=False)  # repr=False：避免被印進 log 或錯誤訊息
    channel_id: int
    claude_model: str
    data_dir: Path
    work_dir: Path
    rate_limit_per_min: int = 8
    max_attachment_mb: int = 10
    claude_timeout_s: int = 120
    claude_retries: int = 2

    @property
    def excel_path(self) -> Path:
        return self.data_dir / "tickets.xlsx"

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def max_attachment_bytes(self) -> int:
        return self.max_attachment_mb * 1024 * 1024

    def ensure_dirs(self) -> None:
        """建立 DATA_DIR、photos、logs 和暫存資料夾。新建的資料夾權限設為只有本人可讀寫。

        DATA_DIR 若是使用者原本就有的資料夾，不去改它的權限；暫存資料夾則一定要是本人專用。
        """
        for path in (self.data_dir, self.photos_dir, self.logs_dir):
            created = not path.exists()
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if created:
                path.chmod(0o700)
        ensure_private_dir(self.work_dir)


def ensure_private_dir(path: Path) -> None:
    """確保 path 是只有本人能進入的資料夾。

    已經存在時要檢查：不是符號連結、是本人的、沒有開放給群組或其他人。
    （在共用的 /tmp 上，別人可能先建一個同名資料夾或連結來搶位置。）
    """
    if path.is_symlink():
        raise ConfigError(["暫存資料夾是符號連結，為安全起見不使用；請刪除後重新啟動"])
    if not path.exists():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    info = path.stat()
    if not path.is_dir() or info.st_uid != os.getuid():
        raise ConfigError(["暫存資料夾不是本人擁有的資料夾，為安全起見不使用"])
    if info.st_mode & 0o077:
        path.chmod(0o700)  # 是本人的，只是權限太寬鬆：收緊


def load_config(
    *,
    env_path: Path | None = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
    project_root: Path = PROJECT_ROOT,
    work_dir: Path | None = None,
) -> Config:
    """讀 .env，再讓同名的環境變數覆蓋它；檢查後回傳 Config。有任何問題就丟 ConfigError。

    測試時可傳 env_path=None 與自訂的 environ，就不會碰到真正的 .env。
    """
    raw = _collect(env_path, os.environ if environ is None else environ)
    problems: list[str] = []

    for key in REQUIRED_KEYS:
        if not raw.get(key):
            problems.append(f"{key} 沒有設定（.env 裡缺少這一項，或值是空的）")

    token = _check_token(raw.get("DISCORD_TOKEN"), problems)
    channel_id = _check_channel_id(raw.get("CHANNEL_ID"), problems)
    model = _check_model(raw.get("CLAUDE_MODEL"), problems)
    work = (work_dir if work_dir is not None else default_work_dir()).resolve()
    if work == project_root.resolve() or work.is_relative_to(project_root.resolve()):
        problems.append("暫存資料夾不能放在專案資料夾裡面（專案根目錄有 .env，會被 claude -p 的工作目錄包到）")
    data_dir = _check_data_dir(raw.get("DATA_DIR"), project_root.resolve(), work, problems)
    numbers = {key: _check_int(key, raw.get(key), spec, problems) for key, spec in OPTIONAL_INT_KEYS.items()}

    if problems:
        raise ConfigError(problems)

    return Config(
        discord_token=token,
        channel_id=channel_id,
        claude_model=model,
        data_dir=data_dir,
        work_dir=work,
        rate_limit_per_min=numbers["RATE_LIMIT_PER_MIN"],
        max_attachment_mb=numbers["MAX_ATTACHMENT_MB"],
        claude_timeout_s=numbers["CLAUDE_TIMEOUT_S"],
        claude_retries=numbers["CLAUDE_RETRIES"],
    )


def _collect(env_path: Path | None, environ: Mapping[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    if env_path is not None and env_path.is_file():
        for key, value in dotenv_values(env_path).items():
            if value is not None:
                values[key] = value.strip()
    for key in (*REQUIRED_KEYS, *OPTIONAL_INT_KEYS):
        if environ.get(key, "").strip():
            values[key] = environ[key].strip()
    return values


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(hint in lowered for hint in _PLACEHOLDER_HINTS)


def _check_token(value: str | None, problems: list[str]) -> str:
    if not value:
        return ""
    if re.search(r"\s", value) or '"' in value or "'" in value:
        problems.append("DISCORD_TOKEN 裡有空白或引號（等號後面直接貼 Token，不要加引號、不要有空格）")
    elif _looks_like_placeholder(value):
        problems.append("DISCORD_TOKEN 看起來還是範例提示文字，不是真的 Token")
    return value


def _check_channel_id(value: str | None, problems: list[str]) -> int:
    if not value:
        return 0
    if not _SNOWFLAKE.fullmatch(value):
        problems.append("CHANNEL_ID 必須是 15～20 位的純數字（請用 Discord 右鍵「複製頻道 ID」，不是頻道名稱）")
        return 0
    return int(value)


def _check_model(value: str | None, problems: list[str]) -> str:
    if not value:
        return ""
    if not MODEL_PATTERN.fullmatch(value):
        problems.append("CLAUDE_MODEL 格式不對（只能用英數字與 . _ : - [ ]，而且不能以 - 開頭或含空白）")
    return value


def _check_data_dir(value: str | None, project_root: Path, work_dir: Path, problems: list[str]) -> Path:
    if not value:
        return Path()
    path = Path(value).expanduser()
    if not path.is_absolute():
        problems.append("DATA_DIR 必須是完整路徑（以 / 或 ~ 開頭）")
        return Path()
    path = path.resolve()
    if path == project_root or path.is_relative_to(project_root):
        problems.append("DATA_DIR 不能放在專案資料夾裡面（會有被 commit 的風險）；請放在專案外面")
    elif project_root.is_relative_to(path):
        problems.append("DATA_DIR 不能是專案資料夾的上層資料夾（claude -p 會看得到專案；請用專屬的資料夾）")
    elif path == work_dir or path.is_relative_to(work_dir) or work_dir.is_relative_to(path):
        problems.append("DATA_DIR 和暫存資料夾不能互相包含")
    elif path.exists() and not path.is_dir():
        problems.append("DATA_DIR 指到的是一個檔案，不是資料夾")
    return path


def _check_int(key: str, value: str | None, spec: tuple[int, int, int], problems: list[str]) -> int:
    default, low, high = spec
    if not value:
        return default
    if not re.fullmatch(r"\d+", value) or not low <= int(value) <= high:
        problems.append(f"{key} 必須是 {low}～{high} 的整數（不設定的話預設是 {default}）")
        return default
    return int(value)


def check_report(config: Config) -> list[str]:
    """設定通過後的狀態摘要。只回報「是否設定、是否可用」，不印出任何設定值。"""
    nearest = config.data_dir
    while not nearest.exists() and nearest != nearest.parent:
        nearest = nearest.parent
    writable = os.access(nearest, os.W_OK)
    exists = config.data_dir.is_dir()
    return [
        "DISCORD_TOKEN   ✅ 已設定（內容不顯示）",
        "CHANNEL_ID      ✅ 格式正確",
        "CLAUDE_MODEL    ✅ 已設定",
        "DATA_DIR        ✅ 在專案資料夾之外",
        "暫存資料夾      ✅ 在系統暫存區，不在專案裡，也和 DATA_DIR 互不包含",
        f"DATA_DIR 狀態   {'✅ 已存在' if exists else 'ℹ️ 尚未建立（正式啟動時會自動建立，權限 700）'}",
        f"DATA_DIR 可寫入 {'✅' if writable else '❌ 沒有寫入權限（檢查上層資料夾權限）'}",
        f"限流 {config.rate_limit_per_min} 次/分鐘、附件上限 {config.max_attachment_mb} MB、"
        f"claude 逾時 {config.claude_timeout_s} 秒、重試 {config.claude_retries} 次",
    ]
