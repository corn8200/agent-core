"""Tests for voice_memo_intake.watch."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_watch():
    import voice_memo_intake.watch as w
    return w


SAMPLE_LS_OUTPUT = """
total 10
-rw-r--r--  1 john staff  1024000 May  5 14:23:01 2026 Recording001.m4a
-rw-r--r--  1 john staff   512000 May  5 15:10:00 2026 Recording002.m4a
-rw-r--r--  1 john staff    10000 May  5 15:11:00 2026 not_a_memo.txt
""".strip()


def test_parse_ls_listing_finds_m4a():
    w = _load_watch()
    files = w._parse_ls_listing(SAMPLE_LS_OUTPUT)
    names = [f["name"] for f in files]
    assert "Recording001.m4a" in names
    assert "Recording002.m4a" in names
    assert "not_a_memo.txt" not in names


def test_parse_ls_listing_skips_total():
    w = _load_watch()
    files = w._parse_ls_listing(SAMPLE_LS_OUTPUT)
    assert all(f["name"].endswith(".m4a") for f in files)


def test_new_file_triggers_pipeline():
    """New file → transcribe → upload → cp.event → seen updated."""
    w = _load_watch()
    events_fired = []

    async def fake_relay(cmd, timeout=15.0):
        return True, SAMPLE_LS_OUTPUT

    async def fake_transcribe(filename, token):
        return "This is the transcript text."

    def fake_upload(local_path, r2_key, r2_token):
        return "https://r2.example.com/voice-memos/2026-05-05/Recording001.m4a"

    def fake_event(agent, kind, payload=None, **kwargs):
        events_fired.append((agent, kind, payload))
        return 42

    seen_saved = {}

    async def run():
        with patch("voice_memo_intake.watch.tmux_relay_shell", side_effect=fake_relay):
            with patch("voice_memo_intake.watch._transcribe", side_effect=fake_transcribe):
                with patch("voice_memo_intake.watch._upload_r2", side_effect=fake_upload):
                    with patch.object(w.cp, "event", side_effect=fake_event):
                        with patch("voice_memo_intake.watch._load_seen", return_value={}):
                            with patch("voice_memo_intake.watch._save_seen") as mock_save:
                                with patch("voice_memo_intake.watch._load_read_token", return_value="test-token"):
                                    with patch("voice_memo_intake.watch._load_r2_token", return_value="test-r2"):
                                        await w._run()
                                        if mock_save.call_args:
                                            seen_saved.update(mock_save.call_args[0][0])

    asyncio.run(run())

    assert len(events_fired) == 2  # 2 new .m4a files
    for agent, kind, payload in events_fired:
        assert agent == "voice-memo-intake"
        assert kind == "voice_memo_intake"
        assert "audio_url" in payload["metadata"]
        assert payload["metadata"]["audio_url"] != ""
        assert "full_transcript" in payload["metadata"]


def test_already_seen_file_no_event():
    """File in seen.json → no cp.event fired."""
    w = _load_watch()
    events_fired = []

    async def fake_relay(cmd, timeout=15.0):
        return True, SAMPLE_LS_OUTPUT

    def fake_event(agent, kind, payload=None, **kwargs):
        events_fired.append((agent, kind, payload))
        return 42

    # Both files already seen
    already_seen = {
        "Recording001.m4a": "2026-05-05T14:23:00Z",
        "Recording002.m4a": "2026-05-05T15:10:00Z",
    }

    async def run():
        with patch("voice_memo_intake.watch.tmux_relay_shell", side_effect=fake_relay):
            with patch.object(w.cp, "event", side_effect=fake_event):
                with patch("voice_memo_intake.watch._load_seen", return_value=already_seen):
                    with patch("voice_memo_intake.watch._save_seen"):
                        with patch("voice_memo_intake.watch._load_read_token", return_value="test-token"):
                            with patch("voice_memo_intake.watch._load_r2_token", return_value=""):
                                await w._run()

    asyncio.run(run())
    assert len(events_fired) == 0


def test_seen_updated_only_after_cp_event_ok():
    """seen.json is updated only after cp.event returns non-None."""
    w = _load_watch()

    async def fake_relay(cmd, timeout=15.0):
        # Single file listing
        return True, "-rw-r--r-- 1 j s 1000 May 5 10:00:00 2026 Recording_test.m4a"

    async def fake_transcribe(filename, token):
        return "transcript"

    def fake_upload(local_path, r2_key, r2_token):
        return "https://r2.example.com/test.m4a"

    saved_states = []

    def fake_save(state):
        saved_states.append(dict(state))

    # cp.event returns None (failure)
    async def run_fail():
        with patch("voice_memo_intake.watch.tmux_relay_shell", side_effect=fake_relay):
            with patch("voice_memo_intake.watch._transcribe", side_effect=fake_transcribe):
                with patch("voice_memo_intake.watch._upload_r2", side_effect=fake_upload):
                    with patch.object(w.cp, "event", return_value=None):
                        with patch("voice_memo_intake.watch._load_seen", return_value={}):
                            with patch("voice_memo_intake.watch._save_seen", side_effect=fake_save):
                                with patch("voice_memo_intake.watch._load_read_token", return_value="t"):
                                    with patch("voice_memo_intake.watch._load_r2_token", return_value=""):
                                        await w._run()

    asyncio.run(run_fail())
    # When cp.event fails, file should NOT be in saved state
    assert all("Recording_test.m4a" not in s for s in saved_states)
