"""config.py 的測試。全部用假的值，不會讀到真正的 .env。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ticket_agent.config import Config, ConfigError, check_report, load_config

FAKE_TOKEN = "FAKE-TOKEN-for-tests_only"
FAKE_CHANNEL = "123456789012345678"


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    """假的專案根目錄與專案外的資料目錄。"""
    project = tmp_path / "project"
    project.mkdir()
    return {"project": project, "data": tmp_path / "ticket-data"}


def valid_env(layout: dict[str, Path], **overrides: str) -> dict[str, str]:
    env = {
        "DISCORD_TOKEN": FAKE_TOKEN,
        "CHANNEL_ID": FAKE_CHANNEL,
        "CLAUDE_MODEL": "sonnet",
        "DATA_DIR": str(layout["data"]),
    }
    env.update(overrides)
    return env


def load(layout: dict[str, Path], env: dict[str, str]) -> Config:
    return load_config(env_path=None, environ=env, project_root=layout["project"])


def problems_of(layout: dict[str, Path], env: dict[str, str]) -> list[str]:
    with pytest.raises(ConfigError) as info:
        load(layout, env)
    return info.value.problems


# ---------- 正常情況 ----------

def test_valid_config_loads_with_defaults(layout):
    config = load(layout, valid_env(layout))
    assert config.channel_id == int(FAKE_CHANNEL)
    assert config.claude_model == "sonnet"
    assert config.data_dir == layout["data"].resolve()
    assert config.rate_limit_per_min == 8
    assert config.max_attachment_mb == 10
    assert config.max_attachment_bytes == 10 * 1024 * 1024
    assert config.claude_timeout_s == 120
    assert config.claude_retries == 2
    assert config.excel_path == config.data_dir / "tickets.xlsx"
    assert config.photos_dir == config.data_dir / "photos"
    assert config.logs_dir == config.data_dir / "logs"


def test_optional_numbers_can_be_overridden(layout):
    config = load(layout, valid_env(layout, RATE_LIMIT_PER_MIN="3", CLAUDE_RETRIES="0"))
    assert config.rate_limit_per_min == 3
    assert config.claude_retries == 0


def test_reads_dotenv_file_and_environment_overrides_it(layout, tmp_path):
    env_file = tmp_path / "dotenv-for-test"
    env_file.write_text(
        f"DISCORD_TOKEN={FAKE_TOKEN}\nCHANNEL_ID={FAKE_CHANNEL}\n"
        f"CLAUDE_MODEL=from-file\nDATA_DIR={layout['data']}\n"
    )
    config = load_config(
        env_path=env_file, environ={"CLAUDE_MODEL": "from-env"}, project_root=layout["project"]
    )
    assert config.claude_model == "from-env"
    assert config.discord_token == FAKE_TOKEN


def test_quotes_in_dotenv_file_are_stripped_by_parser(layout, tmp_path):
    env_file = tmp_path / "dotenv-for-test"
    env_file.write_text(
        f'DISCORD_TOKEN="{FAKE_TOKEN}"\nCHANNEL_ID={FAKE_CHANNEL}\n'
        f"CLAUDE_MODEL=sonnet\nDATA_DIR={layout['data']}\n"
    )
    config = load_config(env_path=env_file, environ={}, project_root=layout["project"])
    assert config.discord_token == FAKE_TOKEN


# ---------- 缺少項目 ----------

def test_empty_environment_reports_all_four_missing_at_once(layout):
    problems = problems_of(layout, {})
    for key in ("DISCORD_TOKEN", "CHANNEL_ID", "CLAUDE_MODEL", "DATA_DIR"):
        assert any(p.startswith(key) for p in problems), key


def test_blank_value_counts_as_missing(layout):
    problems = problems_of(layout, valid_env(layout, CLAUDE_MODEL="   "))
    assert any(p.startswith("CLAUDE_MODEL") for p in problems)


def test_missing_dotenv_file_is_not_a_crash(layout, tmp_path):
    with pytest.raises(ConfigError):
        load_config(env_path=tmp_path / "nope", environ={}, project_root=layout["project"])


# ---------- 各欄位格式 ----------

@pytest.mark.parametrize("bad", ["abc", "12345", "1234567890123456789012", "-123456789012345678", "123 456789012345678"])
def test_channel_id_must_be_snowflake_digits(layout, bad):
    problems = problems_of(layout, valid_env(layout, CHANNEL_ID=bad))
    assert any(p.startswith("CHANNEL_ID") for p in problems)


@pytest.mark.parametrize("bad", ["has space", 'quoted"token', "it's", "貼上剛剛複製的 Token", "<your token>"])
def test_token_rejects_whitespace_quotes_and_placeholders(layout, bad):
    problems = problems_of(layout, valid_env(layout, DISCORD_TOKEN=bad))
    assert any(p.startswith("DISCORD_TOKEN") for p in problems)


def test_model_rejects_whitespace(layout):
    problems = problems_of(layout, valid_env(layout, CLAUDE_MODEL="claude sonnet"))
    assert any(p.startswith("CLAUDE_MODEL") for p in problems)


@pytest.mark.parametrize("key,bad", [
    ("RATE_LIMIT_PER_MIN", "0"),
    ("RATE_LIMIT_PER_MIN", "abc"),
    ("MAX_ATTACHMENT_MB", "-5"),
    ("CLAUDE_TIMEOUT_S", "5"),
    ("CLAUDE_RETRIES", "99"),
])
def test_optional_numbers_are_range_checked(layout, key, bad):
    problems = problems_of(layout, valid_env(layout, **{key: bad}))
    assert any(p.startswith(key) for p in problems)


# ---------- DATA_DIR 的位置 ----------

def test_data_dir_must_be_absolute(layout):
    problems = problems_of(layout, valid_env(layout, DATA_DIR="ticket-data"))
    assert any(p.startswith("DATA_DIR") for p in problems)


def test_data_dir_inside_project_is_rejected(layout):
    problems = problems_of(layout, valid_env(layout, DATA_DIR=str(layout["project"] / "data")))
    assert any("專案資料夾裡面" in p for p in problems)


def test_data_dir_equal_to_project_is_rejected(layout):
    problems = problems_of(layout, valid_env(layout, DATA_DIR=str(layout["project"])))
    assert any(p.startswith("DATA_DIR") for p in problems)


def test_data_dir_that_contains_project_is_rejected(layout):
    problems = problems_of(layout, valid_env(layout, DATA_DIR=str(layout["project"].parent)))
    assert any("上層資料夾" in p for p in problems)


def test_data_dir_dotdot_trick_into_project_is_rejected(layout):
    sneaky = str(layout["data"].parent / "ticket-data" / ".." / "project" / "data")
    problems = problems_of(layout, valid_env(layout, DATA_DIR=sneaky))
    assert any(p.startswith("DATA_DIR") for p in problems)


def test_data_dir_symlink_into_project_is_rejected(layout):
    link = layout["data"].parent / "link"
    link.symlink_to(layout["project"], target_is_directory=True)
    problems = problems_of(layout, valid_env(layout, DATA_DIR=str(link / "data")))
    assert any(p.startswith("DATA_DIR") for p in problems)


def test_data_dir_pointing_at_a_file_is_rejected(layout):
    layout["data"].write_text("not a directory")
    problems = problems_of(layout, valid_env(layout))
    assert any("檔案" in p for p in problems)


def test_tilde_is_expanded(layout, monkeypatch):
    monkeypatch.setenv("HOME", str(layout["data"].parent))
    config = load(layout, valid_env(layout, DATA_DIR="~/ticket-data"))
    assert config.data_dir == layout["data"].resolve()


# ---------- 不洩漏機密 ----------

def test_repr_and_str_never_contain_the_token(layout):
    config = load(layout, valid_env(layout))
    assert FAKE_TOKEN not in repr(config)
    assert FAKE_TOKEN not in str(config)


def test_error_messages_never_contain_values(layout):
    secret_like = "SECRET-VALUE-with space"
    with pytest.raises(ConfigError) as info:
        load(layout, valid_env(layout, DISCORD_TOKEN=secret_like, CHANNEL_ID="notdigits"))
    text = str(info.value)
    assert secret_like not in text
    assert "notdigits" not in text


def test_check_report_never_contains_values(layout):
    config = load(layout, valid_env(layout))
    report = "\n".join(check_report(config))
    assert FAKE_TOKEN not in report
    assert FAKE_CHANNEL not in report
    assert str(layout["data"]) not in report


# ---------- 建立資料夾 ----------

def test_ensure_dirs_creates_private_directories(layout, tmp_path):
    config = load_config(
        env_path=None,
        environ=valid_env(layout),
        project_root=layout["project"],
        work_dir=tmp_path / "work",
    )
    config.ensure_dirs()
    for path in (config.data_dir, config.photos_dir, config.logs_dir, config.work_dir):
        assert path.is_dir()
        assert (path.stat().st_mode & 0o777) == 0o700


def test_ensure_dirs_is_idempotent_and_keeps_existing_permissions(layout, tmp_path):
    layout["data"].mkdir(mode=0o755)
    layout["data"].chmod(0o755)
    config = load_config(
        env_path=None,
        environ=valid_env(layout),
        project_root=layout["project"],
        work_dir=tmp_path / "work",
    )
    config.ensure_dirs()
    config.ensure_dirs()
    assert (layout["data"].stat().st_mode & 0o777) == 0o755  # 使用者原本就有的資料夾，不去改權限


# ---------- 暫存資料夾（防護 1 的前提：不能在專案裡）----------

def test_default_work_dir_is_outside_project_and_per_user(layout):
    import os
    import tempfile

    config = load(layout, valid_env(layout))
    assert not config.work_dir.is_relative_to(layout["project"].resolve())
    assert config.work_dir.is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert config.work_dir.name == f"ticket-agent-work-{os.getuid()}"


def test_real_default_is_outside_the_real_project():
    from ticket_agent.config import PROJECT_ROOT, default_work_dir

    assert not default_work_dir().is_relative_to(PROJECT_ROOT)


def test_work_dir_inside_project_is_rejected(layout):
    with pytest.raises(ConfigError) as info:
        load_config(
            env_path=None,
            environ=valid_env(layout),
            project_root=layout["project"],
            work_dir=layout["project"] / ".work",
        )
    assert any("暫存資料夾" in p and "專案" in p for p in info.value.problems)


def test_work_dir_inside_data_dir_is_rejected(layout):
    with pytest.raises(ConfigError) as info:
        load_config(
            env_path=None,
            environ=valid_env(layout),
            project_root=layout["project"],
            work_dir=layout["data"] / "tmp",
        )
    assert any("互相包含" in p for p in info.value.problems)


def test_ensure_dirs_refuses_symlinked_work_dir(layout, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "work-link"
    link.symlink_to(target, target_is_directory=True)
    config = Config(
        discord_token=FAKE_TOKEN, channel_id=1, claude_model="sonnet",
        data_dir=layout["data"], work_dir=link,
    )
    with pytest.raises(ConfigError):
        config.ensure_dirs()


def test_ensure_dirs_tightens_loose_permissions_on_own_work_dir(layout, tmp_path):
    work = tmp_path / "work"
    work.mkdir(mode=0o755)
    work.chmod(0o755)
    config = Config(
        discord_token=FAKE_TOKEN, channel_id=1, claude_model="sonnet",
        data_dir=layout["data"], work_dir=work,
    )
    config.ensure_dirs()
    assert (work.stat().st_mode & 0o777) == 0o700


def test_ensure_dirs_refuses_work_dir_owned_by_someone_else(layout, tmp_path, monkeypatch):
    import os

    work = tmp_path / "work"
    work.mkdir()
    config = Config(
        discord_token=FAKE_TOKEN, channel_id=1, claude_model="sonnet",
        data_dir=layout["data"], work_dir=work,
    )
    monkeypatch.setattr(os, "getuid", lambda: os.stat(work).st_uid + 1)
    with pytest.raises(ConfigError):
        config.ensure_dirs()


# ---------- 模型名稱（不能被 claude 當成旗標）----------

@pytest.mark.parametrize("bad", ["-p", "--dangerously-skip-permissions", "--model", "-", "a b", "x;y", "$(id)", "a\nb", "x" * 150])
def test_model_name_that_could_be_a_flag_or_shell_text_is_rejected(layout, bad):
    problems = problems_of(layout, valid_env(layout, CLAUDE_MODEL=bad))
    assert any(p.startswith("CLAUDE_MODEL") for p in problems)


@pytest.mark.parametrize("good", ["sonnet", "opus", "claude-sonnet-5-5", "claude-haiku-4-5-20251001", "sonnet[1m]"])
def test_normal_model_names_are_accepted(layout, good):
    assert load(layout, valid_env(layout, CLAUDE_MODEL=good)).claude_model == good
