"""Tests for imessage_inbound.poll."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_poll():
    import importlib
    import imessage_inbound.poll as p
    return p


# Sample chat.db row output (pipe-separated, ROWID|text|mac_ts|is_from_me|sender|chat_id)
# mac_ts = (unix_ts - 978307200) * 1e9
# unix_ts for "2026-05-05 10:00 UTC" = 1746439200
# mac_ts = (1746439200 - 978307200) * 1e9 = 768132000000000000
SAMPLE_CHAT_OUTPUT = """
PRAGMA query_only=1;
100|Hello from Ashley|768132000000000000|0|cornash89@gmail.com|cornash89@gmail.com
101|How are you?|768132100000000000|0|cornash89@gmail.com|cornash89@gmail.com
102|Call me|768132200000000000|0|+13042684985|+13042684985
103|Sent by John|768132300000000000|1|corn82@icloud.com|cornash89@gmail.com
""".strip()


def test_parse_chat_rows_filters_from_me():
    p = _load_poll()
    rows = p._parse_chat_rows(SAMPLE_CHAT_OUTPUT)
    # row 103 is from_me=1, should be excluded
    row_ids = [r["rowid"] for r in rows]
    assert 103 not in row_ids


def test_parse_chat_rows_filters_johns_handle():
    p = _load_poll()
    rows = p._parse_chat_rows(SAMPLE_CHAT_OUTPUT)
    # row 102 has John's own number +13042684985 as sender
    row_ids = [r["rowid"] for r in rows]
    assert 102 not in row_ids


def test_parse_chat_rows_includes_inbound():
    p = _load_poll()
    rows = p._parse_chat_rows(SAMPLE_CHAT_OUTPUT)
    row_ids = [r["rowid"] for r in rows]
    assert 100 in row_ids
    assert 101 in row_ids


def test_group_by_thread_groups_correctly():
    p = _load_poll()
    rows = p._parse_chat_rows(SAMPLE_CHAT_OUTPUT)
    threads = p._group_by_thread(rows)
    # rows 100 and 101 should group under cornash89@gmail.com
    assert "cornash89@gmail.com" in threads
    assert len(threads["cornash89@gmail.com"]) == 2


def test_dedup_same_thread_no_new_rowid():
    """Same thread re-polled with no new ROWID → 0 events."""
    p = _load_poll()
    events_fired = []

    async def fake_relay(cmd, timeout=15.0):
        return True, SAMPLE_CHAT_OUTPUT

    def fake_event(agent, kind, payload=None, **kwargs):
        events_fired.append((agent, kind, payload))
        return 42

    async def run():
        with patch("imessage_inbound.poll.tmux_relay_shell", side_effect=fake_relay):
            with patch.object(p.cp, "event", side_effect=fake_event):
                # First run — no seen state
                with patch("imessage_inbound.poll._load_seen", return_value={}):
                    with patch("imessage_inbound.poll._save_seen"):
                        await p._run()
                count_first = len(events_fired)

                # Second run with same ROWID in seen (simulating full dedup)
                seen = {"cornash89@gmail.com": 101}
                with patch("imessage_inbound.poll._load_seen", return_value=seen):
                    with patch("imessage_inbound.poll._save_seen"):
                        await p._run()

        return count_first

    count = asyncio.run(run())
    # First run should fire events; second run with seen rowid=101 should fire 0
    assert count >= 1
    # After second run, no additional events beyond first batch
    assert len(events_fired) == count


def test_thread_grouping_three_messages_one_event():
    """3 messages from same chat → 1 event with message_count_24h=3."""
    p = _load_poll()
    events_fired = []

    three_msg_output = """
100|Msg 1|768132000000000000|0|cornash89@gmail.com|cornash89@gmail.com
101|Msg 2|768132100000000000|0|cornash89@gmail.com|cornash89@gmail.com
102|Msg 3|768132200000000000|0|cornash89@gmail.com|cornash89@gmail.com
""".strip()

    async def fake_relay(cmd, timeout=15.0):
        return True, three_msg_output

    def fake_event(agent, kind, payload=None, **kwargs):
        events_fired.append((agent, kind, payload))
        return 42

    async def run():
        with patch("imessage_inbound.poll.tmux_relay_shell", side_effect=fake_relay):
            with patch.object(p.cp, "event", side_effect=fake_event):
                with patch("imessage_inbound.poll._load_seen", return_value={}):
                    with patch("imessage_inbound.poll._save_seen"):
                        await p._run()

    asyncio.run(run())
    # Should be exactly 1 event for 1 thread
    assert len(events_fired) == 1
    _, _, payload = events_fired[0]
    assert payload["metadata"]["message_count_24h"] == 3


def test_lock_contention_exits_clean(tmp_path):
    """Relay raises → tick logs and exits clean (no crash)."""
    p = _load_poll()

    async def fake_relay_error(cmd, timeout=15.0):
        return False, "database is locked"

    async def fake_relay_retry(cmd, timeout=15.0):
        return False, "still locked"

    async def run():
        call_count = [0]

        async def fake_relay_seq(cmd, timeout=15.0):
            call_count[0] += 1
            if call_count[0] == 1:
                return False, "database is locked"
            return False, "still locked"

        with patch("imessage_inbound.poll.tmux_relay_shell", side_effect=fake_relay_seq):
            with patch("imessage_inbound.poll._load_seen", return_value={}):
                with patch("imessage_inbound.poll._save_seen"):
                    # Should complete without raising
                    await p._run()

    asyncio.run(run())  # Must not raise
