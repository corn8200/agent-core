"""Parse-side tests for MessageReader.

These don't touch chat.db or the tmux relay — they exercise the pure
parsing logic that turns sqlite3 output into InboundMessage records.

Focus: body content must not break row parsing. Specifically:
- embedded newlines (multi-line iMessage bodies)
- parens / "(shared from GROUNDTRUTH)" substrings
- literal pipes
- Unicode
"""

import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

# Load the worktree's message_reader.py directly, bypassing the editable
# install that points at the main repo.
_HERE = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "worktree_message_reader", _HERE / "core" / "message_reader.py"
)
_mr = importlib.util.module_from_spec(_spec)
sys.modules["worktree_message_reader"] = _mr
_spec.loader.exec_module(_mr)

MessageReader = _mr.MessageReader
_FIELD_SEP = _mr._FIELD_SEP
_RECORD_SEP = _mr._RECORD_SEP
_sqlite_via_relay_cmd = _mr._sqlite_via_relay_cmd


def _build_row(rowid, text, hex_body, is_from_me, chat_id, ts):
    return _FIELD_SEP.join([str(rowid), text, hex_body, is_from_me, chat_id, ts])


def _build_output(rows):
    return _RECORD_SEP.join(rows) + _RECORD_SEP


async def _run_poll_with_fake_output(output):
    reader = MessageReader(poll_interval=1)
    reader._get_last_rowid = lambda: 0

    async def fake_relay(cmd, timeout=10.0):
        return True, output

    with patch.object(_mr, "tmux_relay_shell", fake_relay):
        return await reader._poll_once()


def test_simple_row():
    output = _build_output([
        _build_row(100, "hello", "", "0", "corn82@icloud.com", "2026-04-14 10:00:00"),
    ])
    msgs = asyncio.run(_run_poll_with_fake_output(output))
    assert len(msgs) == 1
    assert msgs[0].rowid == 100
    assert msgs[0].text == "hello"


def test_body_with_pipes_and_parens():
    body = "check this | foo (shared from GROUNDTRUTH) | bar"
    output = _build_output([
        _build_row(200, body, "", "0", "corn82@icloud.com", "2026-04-14 10:00:00"),
    ])
    msgs = asyncio.run(_run_poll_with_fake_output(output))
    assert len(msgs) == 1
    assert msgs[0].rowid == 200
    assert msgs[0].text == body


def test_body_with_embedded_newlines():
    body = "[Nudge] Tomorrow (Wednesday): 0 events\nReminders: 1 today\n(shared from GROUNDTRUTH)"
    output = _build_output([
        _build_row(300, body, "", "0", "corn82@icloud.com", "2026-04-14 10:00:00"),
    ])
    msgs = asyncio.run(_run_poll_with_fake_output(output))
    assert len(msgs) == 1
    assert msgs[0].rowid == 300
    assert msgs[0].text == body
    assert "\n" in msgs[0].text


def test_multiple_rows_with_hostile_bodies():
    rows = [
        _build_row(1, "first | line\nwith newline", "", "0", "chat1", "ts1"),
        _build_row(2, "(shared from GROUNDTRUTH)\nnext", "", "1", "chat1", "ts2"),
        _build_row(3, "plain", "", "0", "chat1", "ts3"),
    ]
    msgs = asyncio.run(_run_poll_with_fake_output(_build_output(rows)))
    # row 2 is from_me without a bot-attribution prefix so it should survive
    assert [m.rowid for m in msgs] == [1, 2, 3]


def test_malformed_row_is_skipped_not_fatal():
    good = _build_row(10, "ok", "", "0", "chat1", "ts")
    bad = _FIELD_SEP.join(["not-an-int", "junk", "", "0", "chat1", "ts"])
    output = _build_output([good, bad])
    msgs = asyncio.run(_run_poll_with_fake_output(output))
    assert [m.rowid for m in msgs] == [10]


def test_empty_output():
    msgs = asyncio.run(_run_poll_with_fake_output(""))
    assert msgs == []


def test_sqlite_cmd_uses_control_separators():
    cmd = _sqlite_via_relay_cmd("SELECT 1;")
    assert "\\x1f" in cmd
    assert "\\x1e" in cmd
    assert "|||" not in cmd


if __name__ == "__main__":
    test_simple_row()
    test_body_with_pipes_and_parens()
    test_body_with_embedded_newlines()
    test_multiple_rows_with_hostile_bodies()
    test_malformed_row_is_skipped_not_fatal()
    test_empty_output()
    test_sqlite_cmd_uses_control_separators()
    print("All tests passed.")
