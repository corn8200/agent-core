"""Tests for MessageReader heartbeat + consecutive-timeout exit.

Verifies:
- poll_loop touches HEARTBEAT_PATH every outer iteration
- Two consecutive asyncio.TimeoutError raises trigger sys.exit(1)
- Two consecutive generic exceptions also trigger sys.exit(1)
- One timeout followed by success resets the counter
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "worktree_message_reader_hb", _HERE / "core" / "message_reader.py"
)
_mr = importlib.util.module_from_spec(_spec)
sys.modules["worktree_message_reader_hb"] = _mr
_spec.loader.exec_module(_mr)

MessageReader = _mr.MessageReader

# Save the real asyncio.sleep before any test patches it — used by the
# no-op sleep shim so we don't recursively call ourselves.
_real_sleep = asyncio.sleep


async def _noop_sleep(*_a, **_kw):
    """Drop-in for asyncio.sleep that yields control once, no delay.

    Using _real_sleep(0) rather than a bare pass keeps asyncio's task
    scheduling happy (poll_loop's outer while loop needs at least one
    suspension point per iteration).
    """
    await _real_sleep(0)


class _BreakLoop(BaseException):
    """Breakout signal — BaseException so poll_loop's `except Exception`
    doesn't swallow it."""


@pytest.fixture
def reader_with_tmp_heartbeat(tmp_path, monkeypatch):
    hb = tmp_path / "imessage-bus.heartbeat"
    monkeypatch.setattr(_mr, "HEARTBEAT_PATH", hb)
    reader = MessageReader(poll_interval=0)
    return reader, hb


def test_heartbeat_is_touched_on_each_poll(reader_with_tmp_heartbeat):
    reader, hb = reader_with_tmp_heartbeat

    iterations = [0]

    async def fake_poll():
        iterations[0] += 1
        if iterations[0] >= 2:
            raise _BreakLoop()
        return []

    reader._poll_once = fake_poll

    with pytest.raises(_BreakLoop):
        asyncio.run(reader.poll_loop())
    assert hb.exists(), "heartbeat file should exist after poll_loop ran"


def test_two_consecutive_timeouts_exits(reader_with_tmp_heartbeat, monkeypatch):
    reader, _ = reader_with_tmp_heartbeat

    async def always_timeout():
        raise asyncio.TimeoutError()

    # Bypass wait_for's timer so we don't depend on real time.
    async def no_wait(coro, timeout):
        return await coro

    monkeypatch.setattr(_mr.asyncio, "wait_for", no_wait)
    monkeypatch.setattr(_mr.asyncio, "sleep", _noop_sleep)

    reader._poll_once = always_timeout

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(reader.poll_loop())
    assert excinfo.value.code == 1


def test_two_consecutive_errors_exits(reader_with_tmp_heartbeat, monkeypatch):
    reader, _ = reader_with_tmp_heartbeat

    async def always_error():
        raise RuntimeError("relay down")

    reader._poll_once = always_error
    monkeypatch.setattr(_mr.asyncio, "sleep", _noop_sleep)

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(reader.poll_loop())
    assert excinfo.value.code == 1


def test_one_error_then_success_resets_counter(reader_with_tmp_heartbeat, monkeypatch):
    reader, _ = reader_with_tmp_heartbeat

    sequence = ["error", "ok", "stop"]

    async def mixed():
        step = sequence.pop(0)
        if step == "error":
            raise RuntimeError("transient")
        if step == "stop":
            raise _BreakLoop()
        return []

    reader._poll_once = mixed
    monkeypatch.setattr(_mr.asyncio, "sleep", _noop_sleep)

    # Must NOT SystemExit — one error then success resets counter, then _BreakLoop.
    with pytest.raises(_BreakLoop):
        asyncio.run(reader.poll_loop())
