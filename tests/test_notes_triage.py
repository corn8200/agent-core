"""Tests for notes_triage.monitor."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_monitor():
    import importlib
    import notes_triage.monitor as m
    return m


SAMPLE_NOTES = [
    {
        "id": "note-1",
        "title": "Old inbox note",
        "folder": "Inbox",
        "char_count": 200,
        "modified_at": "2026-03-01T10:00:00Z",
        "dedup_hash": "hash-unique-1",
    },
    {
        "id": "note-2",
        "title": "Tiny note",
        "folder": "Notes",
        "char_count": 20,
        "modified_at": "2026-05-01T10:00:00Z",
        "dedup_hash": "hash-unique-2",
    },
    {
        "id": "note-dup-a",
        "title": "Dup note",
        "folder": "Notes",
        "char_count": 300,
        "modified_at": "2026-04-01T10:00:00Z",
        "dedup_hash": "hash-dup",
    },
    {
        "id": "note-dup-b",
        "title": "Dup note",
        "folder": "Notes",
        "char_count": 300,
        "modified_at": "2026-04-01T10:05:00Z",
        "dedup_hash": "hash-dup",
    },
]


def test_heuristic_stale_inbox():
    m = _load_monitor()
    notes = [SAMPLE_NOTES[0]]
    flagged = m._apply_heuristics(notes)
    assert len(flagged) == 1
    assert any("stale-inbox" in r for r in flagged[0]["_reasons"])


def test_heuristic_tiny():
    m = _load_monitor()
    notes = [SAMPLE_NOTES[1]]
    flagged = m._apply_heuristics(notes)
    assert len(flagged) == 1
    assert any("tiny" in r for r in flagged[0]["_reasons"])


def test_heuristic_dup_pair():
    m = _load_monitor()
    notes = [SAMPLE_NOTES[2], SAMPLE_NOTES[3]]
    flagged = m._apply_heuristics(notes)
    assert len(flagged) == 2
    for f in flagged:
        assert any("dup-pair" in r for r in f["_reasons"])


def test_publish_fires_cp_event_for_each_flagged(tmp_path):
    m = _load_monitor()
    events_fired = []

    def fake_event(agent, kind, payload=None, **kwargs):
        events_fired.append((agent, kind, payload))
        return 42

    with patch.object(m.cp, "event", side_effect=fake_event):
        # 4 notes: stale-inbox, tiny, 2 dup-pair = 4 flagged
        flagged = m._apply_heuristics(SAMPLE_NOTES)
        new_state = m._publish(flagged, {}, "fake-token")

    assert len(events_fired) == 4
    for agent, kind, payload in events_fired:
        assert agent == "notes-triage"
        assert kind == "note_triage"
        assert "verbs" in payload
        assert "KEEP" in payload["verbs"]
        assert "KILL" in payload["verbs"]
        assert "dedup_key" in payload


def test_publish_skips_unchanged_state(tmp_path):
    m = _load_monitor()
    events_fired = []

    def fake_event(agent, kind, payload=None, **kwargs):
        events_fired.append((agent, kind, payload))
        return 42

    with patch.object(m.cp, "event", side_effect=fake_event):
        flagged = m._apply_heuristics([SAMPLE_NOTES[1]])  # tiny note
        # First publish
        state = m._publish(flagged, {}, "fake-token")
        assert len(events_fired) == 1
        # Second publish with same state — should skip
        m._publish(flagged, state, "fake-token")
        assert len(events_fired) == 1  # still 1, not 2


def test_apply_heuristics_clean_note_not_flagged():
    m = _load_monitor()
    clean = [{
        "id": "note-clean",
        "title": "Clean note with enough content",
        "folder": "Notes",
        "char_count": 500,
        "modified_at": "2026-05-05T10:00:00Z",
        "dedup_hash": "hash-clean-unique",
    }]
    flagged = m._apply_heuristics(clean)
    assert len(flagged) == 0
