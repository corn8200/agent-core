"""Tests for core/imessage_triage: classify, publish, poll."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.imessage_triage.classify import (
    VALID_CATEGORIES,
    _parse_response,
    classify_thread,
)
from core.imessage_triage.publish import should_publish
from core.imessage_triage.poll import _group_by_thread


# ---------------------------------------------------------------------------
# classify.py
# ---------------------------------------------------------------------------


def test_valid_categories_covers_spec():
    required = {"action_me", "action_them", "action_us", "scheduling",
                "social", "status_update", "marketing", "noise"}
    assert required == VALID_CATEGORIES


def test_should_publish_action_me():
    assert should_publish("action_me") is True


def test_should_publish_scheduling():
    assert should_publish("scheduling") is True


def test_should_publish_noise_false():
    assert should_publish("noise") is False


def test_should_publish_social_false():
    assert should_publish("social") is False


def test_should_publish_all_non_publish_categories():
    for cat in ("action_them", "action_us", "social", "status_update", "marketing", "noise"):
        assert should_publish(cat) is False, f"{cat} should not publish"


def test_parse_response_valid_json():
    raw = '{"category": "action_me", "urgency": 8}'
    result = _parse_response(raw)
    assert result["category"] == "action_me"
    assert result["urgency"] == 8


def test_parse_response_code_fence():
    raw = '```json\n{"category": "scheduling", "urgency": 5}\n```'
    result = _parse_response(raw)
    assert result["category"] == "scheduling"
    assert result["urgency"] == 5


def test_parse_response_unknown_category_becomes_noise():
    raw = '{"category": "totally_unknown", "urgency": 3}'
    result = _parse_response(raw)
    assert result["category"] == "noise"


def test_parse_response_urgency_clamped_to_range():
    low = _parse_response('{"category": "noise", "urgency": -5}')
    assert low["urgency"] == 1
    high = _parse_response('{"category": "noise", "urgency": 99}')
    assert high["urgency"] == 10


def test_classify_thread_uses_haiku_call(monkeypatch):
    calls = []

    def fake_call_haiku(prompt, *, api_key):
        calls.append(prompt)
        return '{"category": "action_me", "urgency": 8}'

    monkeypatch.setattr("core.imessage_triage.classify._call_haiku", fake_call_haiku)
    # Pass the key directly so _api_key() vault lookup is bypassed
    result = classify_thread(
        "+15555550100", ["[Them] Need you to sign the document"],
        api_key="sk-test-key",
    )
    assert result["category"] == "action_me"
    assert result["urgency"] == 8
    assert calls, "expected haiku to be called"
    assert "+15555550100" in calls[0]


def test_classify_thread_no_key_returns_noise():
    with patch("core.imessage_triage.classify._api_key", return_value=""):
        result = classify_thread("+15555550100", ["[Them] Test"])
    assert result["category"] == "noise"
    assert result["urgency"] == 1


def test_classify_thread_api_error_calls_doctor_and_returns_noise(monkeypatch):
    def boom(prompt, *, api_key):
        raise Exception("API 400")

    monkeypatch.setattr("core.imessage_triage.classify._call_haiku", boom)
    doctor_calls = []
    monkeypatch.setattr(
        "core.imessage_triage.classify.doctor_escalate",
        lambda **kw: doctor_calls.append(kw),
    )
    result = classify_thread("+15555550100", ["[Them] Test"], api_key="dummy-key")
    assert result["category"] == "noise"
    assert result["urgency"] == 1
    assert doctor_calls, "expected doctor_escalate to be called on API failure"
    assert doctor_calls[0]["watcher"] == "imessage-triage"


# ---------------------------------------------------------------------------
# poll.py — group_by_thread
# ---------------------------------------------------------------------------


def _make_rows(*specs):
    """specs: (rowid, chat_id, text, is_from_me)"""
    return [
        {"rowid": rowid, "chat_identifier": cid, "text": t,
         "is_from_me": isme, "ts_str": "2026-01-01 00:00:00"}
        for rowid, cid, t, isme in specs
    ]


def test_group_by_thread_separates_chats():
    rows = _make_rows(
        (1, "abc@icloud.com", "hi", False),
        (2, "abc@icloud.com", "hey", False),
        (3, "+15551234567", "test", False),
    )
    groups = _group_by_thread(rows)
    assert len(groups) == 2
    assert len(groups["abc@icloud.com"]) == 2
    assert len(groups["+15551234567"]) == 1


def test_group_by_thread_empty_input():
    assert _group_by_thread([]) == {}
