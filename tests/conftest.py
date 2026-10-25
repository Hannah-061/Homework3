"""測試共用工具。

load_without_guard：把某個模組原始碼裡「某一段防護」整段拿掉，載入成另一個模組。
用來證明兩件事：（1）拿掉防護後程式仍然能跑；（2）沒有防護時，攻擊真的會成功，所以防護確實在發揮作用。
載入的是暫時的複本，不會改到真正的原始碼。
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "ticket_agent"


def _strip_guard(source: str, number: int) -> str:
    pattern = re.compile(rf"^# ==== 防護 {number}：.+?　開始 ====$.*?^# ==== 防護 {number} 結束 ====$", re.S | re.M)
    stripped, count = pattern.subn("", source)
    assert count == 1, f"防護 {number} 應該剛好有一組標記"
    return stripped


@pytest.fixture
def load_without_guard():
    loaded: list[str] = []

    def loader(module_file: str, number: int) -> types.ModuleType:
        source = _strip_guard((SRC / module_file).read_text(encoding="utf-8"), number)
        name = f"ticket_agent._noguard{number}_{module_file[:-3]}"
        module = types.ModuleType(name)
        module.__package__ = "ticket_agent"
        module.__file__ = str(SRC / module_file)
        sys.modules[name] = module
        loaded.append(name)
        exec(compile(source, module.__file__, "exec"), module.__dict__)
        return module

    yield loader
    for name in loaded:
        sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# 假的 claude：不呼叫任何服務、不花額度。它把「收到什麼」記錄下來，再依劇本輸出 stream-json。
# ---------------------------------------------------------------------------

import json  # noqa: E402
import os  # noqa: E402
import stat  # noqa: E402

FAKE_SCRIPT = r'''#!{python}
import json, os, subprocess, sys, time

REC = {rec!r}
SCEN = {scenario!r}
OPTS = json.loads({opts!r})

stdin = sys.stdin.buffer.read()
cwd = os.getcwd()
record = {{
    "argv": sys.argv[1:], "cwd": cwd, "cwd_listing": sorted(os.listdir(cwd)),
    "cwd_mode": os.stat(cwd).st_mode & 0o777, "env": dict(os.environ),
    "stdin": stdin.decode("utf-8", "replace"), "pid": os.getpid(),
}}

def save():
    with open(REC, "w") as handle:
        json.dump(record, handle)

save()

def emit(event):
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()

INIT = {{"type": "system", "subtype": "init", "tools": ["StructuredOutput"], "mcp_servers": [],
         "permissionMode": "dontAsk"}}
STRUCTURED = OPTS.get("structured", {{"is_ticket": False, "not_ticket_description": "假的測試物件"}})
RESULT = {{"type": "result", "subtype": "success", "is_error": False, "structured_output": STRUCTURED,
           "total_cost_usd": 0.01, "permission_denials": []}}

if SCEN == "ok":
    emit(INIT); emit(RESULT)
elif SCEN == "extra_tool":
    emit({{**INIT, "tools": ["StructuredOutput", "Read"]}}); emit(RESULT)
elif SCEN == "all_tools":
    emit({{**INIT, "tools": ["Read", "Bash", "Write"]}}); emit(RESULT)
elif SCEN == "mcp":
    emit({{**INIT, "mcp_servers": [{{"name": "claude.ai Docs", "status": "connected"}}]}}); emit(RESULT)
elif SCEN == "mode":
    emit({{**INIT, "permissionMode": "default"}}); emit(RESULT)
elif SCEN == "hook":
    emit({{"type": "system", "subtype": "hook_started"}}); emit(INIT); emit(RESULT)
elif SCEN == "no_init":
    emit(RESULT)
elif SCEN == "denials":
    emit(INIT); emit({{**RESULT, "permission_denials": [{{"tool_name": "Read"}}, {{"tool_name": "Bash"}}]}})
elif SCEN == "is_error":
    emit(INIT); emit({{**RESULT, "subtype": "error_during_execution", "is_error": True, "result": "SECRET-RESULT-TEXT"}})
elif SCEN == "no_structured":
    emit(INIT); emit({{k: v for k, v in RESULT.items() if k != "structured_output"}})
elif SCEN == "no_result":
    emit(INIT)
elif SCEN == "garbage":
    sys.stdout.write("not json at all SECRET-STDOUT\n")
elif SCEN == "exit1":
    sys.stderr.write("SECRET-STDERR-CONTENT\n"); sys.stdout.write("SECRET-STDOUT-CONTENT\n"); sys.exit(1)
elif SCEN == "exit1_result":
    emit(INIT); emit({{**RESULT, "subtype": "evil; rm -rf ~", "is_error": True}}); sys.exit(1)
elif SCEN == "exit1_result_ok_subtype":
    emit(INIT); emit({{**RESULT, "subtype": "error_max_turns", "is_error": True}}); sys.exit(1)
elif SCEN == "huge":
    emit(INIT); sys.stdout.write("x" * (5 * 1024 * 1024) + "\n"); emit(RESULT)
elif SCEN == "sleep":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    record["child_pid"] = child.pid
    save()
    time.sleep(120)
else:
    sys.exit(99)
'''


class FakeClaude:
    def __init__(self, path, record_path):
        self.path = str(path)
        self.record_path = record_path

    def record(self) -> dict:
        return json.loads(self.record_path.read_text())

    def has_run(self) -> bool:
        return self.record_path.exists()


@pytest.fixture
def make_fake_claude(tmp_path):
    counter = {"n": 0}

    def factory(scenario: str = "ok", **opts) -> FakeClaude:
        counter["n"] += 1
        script = tmp_path / f"fake-claude-{counter['n']}"
        record_path = tmp_path / f"record-{counter['n']}.json"
        script.write_text(FAKE_SCRIPT.format(python=sys.executable, rec=str(record_path), scenario=scenario,
                                             opts=json.dumps(opts, ensure_ascii=False)))
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return FakeClaude(script, record_path)

    return factory


def pytest_addoption(parser):
    parser.addoption("--run-live", action="store_true", default=False,
                     help="執行會呼叫真正 claude -p 的測試（會用掉 Claude 額度，只用合成資料）")


def pytest_configure(config):
    config.addinivalue_line("markers", "live: 會呼叫真正的 claude -p（用額度），預設跳過；加 --run-live 才執行")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live") or os.environ.get("RUN_LIVE_CLAUDE") == "1":
        return
    skip = pytest.mark.skip(reason="會呼叫真正的 claude -p；要執行請加 --run-live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# 整合測試用的完整 app（假的 Discord、假的辨識與更正、真的 Excel 與照片資料夾）
# ---------------------------------------------------------------------------

@pytest.fixture
def rw(tmp_path):
    from synthetic import FakeClock, FakeMessenger, ScriptedAnalyzer, ScriptedCorrect, Sleeper, analysis, make_config
    from ticket_agent.app import build_app
    from ticket_agent.excel_store import TicketStore

    class RW:
        def __init__(self):
            self.messenger = FakeMessenger()
            self.sleeper = Sleeper()
            self.clock = FakeClock()
            self.config = make_config(tmp_path)
            self.apps = []

        def build(self, analyzer=None, correct=None, **kwargs):
            app = build_app(self.config, self.messenger, analyze=analyzer or ScriptedAnalyzer(analysis()),
                            correct=correct or ScriptedCorrect(Exception("沒有設定更正劇本")),
                            sleep=self.sleeper, retry_delay_s=0.0, clock=self.clock, **kwargs)
            app.start()
            self.apps.append(app)
            return app

        def store(self):
            return TicketStore(self.config.excel_path)

        def photos(self):
            return sorted(p.name for p in (self.config.data_dir / "photos").iterdir())

        def texts(self):
            return self.messenger.visible_texts()

    world = RW()
    yield world
    for app in world.apps:
        app.lock.release()
