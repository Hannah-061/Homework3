"""進入點。

- `ticket-agent --check`：只檢查設定，不連線。
- `ticket-agent --guards`：列出四段防護的位置。
- `ticket-agent`：檢查設定後連上 Discord，開始監看指定的頻道，直到按 Ctrl+C 結束。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import guards
from .config import ConfigError, check_report, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ticket-agent", description="Discord 票券記錄 agent")
    parser.add_argument("--check", action="store_true", help="只檢查 .env 設定，不連 Discord")
    parser.add_argument("--guards", action="store_true", help="列出四段防護的位置與行數，並檢查標記格式")
    args = parser.parse_args(argv)

    if args.guards:
        found, problems = guards.scan(Path(__file__).resolve().parent)
        print("\n".join(guards.format_report(found, problems)))
        return 1 if problems else 0

    try:
        config = load_config()
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 1

    for line in check_report(config):
        print(line)
    if args.check:
        return 0

    from .discord_adapter import run_bot  # 到這裡才載入 discord.py：--check / --guards 不需要它

    print("設定檢查通過，正在連上 Discord…（按 Ctrl+C 結束）")
    return run_bot(config)


def cli() -> None:
    sys.exit(main())


if __name__ == "__main__":
    cli()
