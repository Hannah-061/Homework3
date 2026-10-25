"""重試、檔案鎖、Excel 閘門（鎖）、log 遮蔽的測試。全部使用合成資料。"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest
from openpyxl import load_workbook

from synthetic import NOW, Sleeper
from ticket_agent.excel_store import RECORD_SHEET, StoreError, TicketStore
from ticket_agent.locking import AlreadyRunning, InstanceLock
from ticket_agent.log_setup import RedactingFilter, setup_logging
from ticket_agent.models import TicketData
from ticket_agent.retry import MAX_RETRIES, with_retries
from ticket_agent.store_gateway import StoreGateway


class Boom(Exception):
    pass


class Fatal(Exception):
    pass


# ---------------------------------------------------------------- 重試

async def test_retry_succeeds_after_transient_failures_and_waits_between_attempts():
    sleeper, calls = Sleeper(), {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Boom
        return "ok"

    assert await with_retries(flaky, retries=2, retry_on=(Boom,), delay_s=1.5, sleep=sleeper) == "ok"
    assert calls["n"] == 3 and sleeper.delays == [1.5, 3.0]


async def test_retry_is_capped_and_raises_the_last_error():
    calls = {"n": 0}

    async def always_fails():
        calls["n"] += 1
        raise Boom(f"第{calls['n']}次")

    with pytest.raises(Boom, match="第3次"):
        await with_retries(always_fails, retries=2, retry_on=(Boom,), sleep=Sleeper())
    assert calls["n"] == 3  # 1 次 + 重試 2 次，不多不少


@pytest.mark.parametrize("retries,expected_calls", [(0, 1), (1, 2), (5, 6), (99, MAX_RETRIES + 1), (-3, 1)])
async def test_retry_count_never_exceeds_the_hard_cap(retries, expected_calls):
    calls = {"n": 0}

    async def always_fails():
        calls["n"] += 1
        raise Boom

    with pytest.raises(Boom):
        await with_retries(always_fails, retries=retries, retry_on=(Boom,), sleep=Sleeper())
    assert calls["n"] == expected_calls


async def test_no_retry_errors_are_raised_immediately_even_if_they_are_subclasses():
    class Special(Boom):
        pass

    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise Special

    with pytest.raises(Special):
        await with_retries(op, retries=5, retry_on=(Boom,), no_retry=(Special,), sleep=Sleeper())
    assert calls["n"] == 1


async def test_unlisted_errors_are_not_retried_and_cancellation_is_not_swallowed():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise Fatal

    with pytest.raises(Fatal):
        await with_retries(op, retries=5, retry_on=(Boom,), sleep=Sleeper())
    assert calls["n"] == 1

    async def cancelled():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await with_retries(cancelled, retries=5, retry_on=(Exception,), sleep=Sleeper())


async def test_retry_logs_contain_only_the_error_type(caplog):
    caplog.set_level(logging.DEBUG)

    async def op():
        raise Boom("SECRET-IN-ERROR-MESSAGE")

    with pytest.raises(Boom):
        await with_retries(op, retries=1, retry_on=(Boom,), sleep=Sleeper())
    assert "Boom" in caplog.text and "SECRET" not in caplog.text


# ---------------------------------------------------------------- 單一 agent 鎖

def test_second_instance_on_the_same_data_dir_is_refused(tmp_path):
    first = InstanceLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunning):
            InstanceLock(tmp_path).acquire()
    finally:
        first.release()
    with InstanceLock(tmp_path):  # 放開之後可以再拿
        pass


def test_different_data_dirs_do_not_conflict_and_release_is_idempotent(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with InstanceLock(tmp_path / "a"), InstanceLock(tmp_path / "b"):
        pass
    lock = InstanceLock(tmp_path / "a")
    lock.acquire()
    lock.acquire()
    lock.release()
    lock.release()


def test_lock_file_is_private_and_not_a_symlink_target(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("do not touch")
    (tmp_path / ".agent.lock").symlink_to(secret)
    with pytest.raises(OSError):  # O_NOFOLLOW：不跟著符號連結走
        InstanceLock(tmp_path).acquire()
    assert secret.read_text() == "do not touch"


# ---------------------------------------------------------------- Excel 閘門

def ticket(n: int) -> TicketData:
    return TicketData(ticket_type="演唱會", name=f"測試票 {n}", event_datetime="2099-12-31 19:30", amount=100.0, currency="TWD")


async def test_concurrent_writers_never_lose_records(tmp_path):
    store = TicketStore(tmp_path / "t.xlsx", clock=lambda: NOW)
    gateway = StoreGateway(store)
    count = 40

    def add(n):
        return lambda s: s.add_record(uploader=f"人{n % 5}", uploader_id=str(n % 5), data=ticket(n), photo_path=f"photos/{n}.jpg", recorded_at=NOW)

    records = await asyncio.gather(*(gateway.run(add(n)) for n in range(count)))
    assert sorted(r.serial for r in records) == list(range(1, count + 1))  # 流水號不重複、不跳號
    saved = store.all_records()
    assert len(saved) == count and {r.data.name for r in saved} == {f"測試票 {n}" for n in range(count)}
    stats = list(load_workbook(store.path)["統計"].iter_rows(min_row=2, values_only=True))
    assert sum(row[4] for row in stats) == count  # 統計表的歷史筆數合計也對
    assert sorted(p.name for p in tmp_path.iterdir()) == ["t.xlsx", "t.xlsx.bak"]  # 沒有殘留的暫存檔


async def test_gateway_runs_one_operation_at_a_time():
    class Spy:
        active = 0
        peak = 0

    def op(_store):
        Spy.active += 1
        Spy.peak = max(Spy.peak, Spy.active)
        time.sleep(0.01)
        Spy.active -= 1

    gateway = StoreGateway(store=object())  # type: ignore[arg-type]
    await asyncio.gather(*(gateway.run(op) for _ in range(20)))
    assert Spy.peak == 1


async def test_gateway_does_not_block_the_event_loop_while_writing():
    ticks = []

    def slow(_store):
        time.sleep(0.3)

    async def ticker():
        for _ in range(5):
            await asyncio.sleep(0.03)
            ticks.append(time.monotonic())

    gateway = StoreGateway(store=object())  # type: ignore[arg-type]
    await asyncio.gather(gateway.run(slow), ticker())
    assert len(ticks) == 5 and ticks[-1] - ticks[0] < 0.25  # 寫檔期間事件迴圈照常運作


async def test_gateway_retries_store_errors_up_to_the_limit_and_then_gives_up():
    sleeper, calls = Sleeper(), {"n": 0}

    def failing(_store):
        calls["n"] += 1
        raise StoreError("寫不進去")

    gateway = StoreGateway(store=object(), attempts=3, delay_s=0.5, sleep=sleeper)  # type: ignore[arg-type]
    with pytest.raises(StoreError):
        await gateway.run_with_retries(failing)
    assert calls["n"] == 3 and sleeper.delays == [0.5, 1.0]


async def test_gateway_does_not_retry_other_errors():
    calls = {"n": 0}

    def bug(_store):
        calls["n"] += 1
        raise ValueError("程式錯誤不該重試")

    gateway = StoreGateway(store=object(), attempts=3, sleep=Sleeper())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await gateway.run_with_retries(bug)
    assert calls["n"] == 1


async def test_lock_is_released_after_a_failure():
    gateway = StoreGateway(store=object(), attempts=1, sleep=Sleeper())  # type: ignore[arg-type]

    def fail(_store):
        raise StoreError("x")

    with pytest.raises(StoreError):
        await gateway.run_with_retries(fail)
    assert await asyncio.wait_for(gateway.run(lambda _s: "still works"), 2) == "still works"


# ---------------------------------------------------------------- log 遮蔽

def test_redacting_filter_masks_tokens_ticket_numbers_and_long_digits():
    filt = RedactingFilter(secrets=("MySuperSecretValue123",))
    sample = ("token MySuperSecretValue123 and abcdefghijklmnopqrstuvwxyz.ABCDEFG.abcdefghijklmnopqrstuvwxyz0123 "
              "票號：SECRET-NO-777 帳號 123456789012345678 一般文字 2099-12-31")
    cleaned = filt.redact(sample)
    for leaked in ("MySuperSecretValue123", "abcdefghijklmnopqrstuvwxyz.ABCDEFG", "SECRET-NO-777", "123456789012345678"):
        assert leaked not in cleaned
    assert "一般文字 2099-12-31" in cleaned


def test_log_file_is_private_and_redacted(tmp_path):
    handler = setup_logging(tmp_path / "logs", secrets=("FAKE-DISCORD-TOKEN-FOR-TESTS",))
    try:
        logging.getLogger("ticket_agent.test").error("出錯了 FAKE-DISCORD-TOKEN-FOR-TESTS 票號：A1B2C3D4E5 %s", "額外")
        handler.flush()
    finally:
        logging.getLogger("ticket_agent").removeHandler(handler)
        handler.close()
    path = tmp_path / "logs" / "agent.log"
    text = path.read_text(encoding="utf-8")
    assert "FAKE-DISCORD" not in text and "A1B2C3D4E5" not in text and "額外" in text
    assert (path.stat().st_mode & 0o777) == 0o600


def test_threads_cannot_corrupt_the_store_without_the_gateway_but_serial_numbers_are_unique_with_it(tmp_path):
    """用多個執行緒透過同一個鎖寫入（模擬 asyncio.to_thread 的情況），結果仍然完整。"""
    store = TicketStore(tmp_path / "t.xlsx", clock=lambda: NOW)
    lock = threading.Lock()

    def work(n):
        with lock:
            store.add_record(uploader="x", uploader_id="1", data=ticket(n), photo_path=f"p/{n}.jpg", recorded_at=NOW)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(15)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(r.serial for r in store.all_records()) == list(range(1, 16))
