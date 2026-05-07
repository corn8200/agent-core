"""Tests for core/imessage_triage: classify, publish, poll."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.imessage_triage.classify import (
    VALID_CATEGORIES,
    _parse_response,
    classify_thread,
)
from core.imessage_triage.publish import should_publish, is_test_handle
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


def _make_fake_query(response_json: str):
    """Build an async generator that yields a fake AssistantMessage."""
    from core.mac_sdk import AssistantMessage, TextBlock

    async def _fake_query(prompt, options=None, **kwargs):
        msg = MagicMock(spec=AssistantMessage)
        block = MagicMock(spec=TextBlock)
        block.text = response_json
        msg.content = [block]
        yield msg

    return _fake_query


def test_classify_thread_uses_mac_sdk(monkeypatch):
    fake = _make_fake_query('{"category": "action_me", "urgency": 8}')
    monkeypatch.setattr("core.imessage_triage.classify.query", fake)

    result = asyncio.run(classify_thread("+15551234567", ["[Them] Need you to sign the document"]))
    assert result["category"] == "action_me"
    assert result["urgency"] == 8


def test_classify_thread_api_error_calls_doctor_after_repeated_failures(monkeypatch, tmp_path):
    from core.imessage_triage import classify as classify_mod

    async def boom(prompt, options=None, **kwargs):
        raise Exception("SDK 500")
        yield  # make it an async generator

    monkeypatch.setattr("core.imessage_triage.classify.query", boom)
    monkeypatch.setattr(classify_mod, "CLASSIFY_FAILURE_STATE", tmp_path / "failures.json")
    doctor_calls = []
    monkeypatch.setattr(
        "core.imessage_triage.classify.doctor_escalate",
        lambda **kw: doctor_calls.append(kw),
    )

    result = asyncio.run(classify_thread("+15551234567", ["[Them] Test"]))
    assert result["category"] == "noise"
    assert result["urgency"] == 1
    assert result["deferred"] is True
    assert not doctor_calls

    asyncio.run(classify_thread("+15551234567", ["[Them] Test"]))
    result = asyncio.run(classify_thread("+15551234567", ["[Them] Test"]))

    assert result["category"] == "noise"
    assert result["urgency"] == 1
    assert result["deferred"] is True
    assert doctor_calls, "expected doctor_escalate after repeated SDK failures"
    assert doctor_calls[0]["watcher"] == "imessage-triage"
    assert doctor_calls[0]["context"]["consecutive_failures"] == 3


def test_classify_thread_quota_exceeded_returns_noise(monkeypatch):
    from core.mac_sdk import SDKQuotaExceeded

    async def quota_boom(prompt, options=None, **kwargs):
        raise SDKQuotaExceeded("50/hr hit")
        yield

    monkeypatch.setattr("core.imessage_triage.classify.query", quota_boom)
    result = asyncio.run(classify_thread("+15551234567", ["[Them] Test"]))
    assert result["category"] == "noise"
    assert result["urgency"] == 1
    assert result["deferred"] is True


def test_classify_thread_usage_gate_defers_without_doctor(monkeypatch):
    from core.claude_usage_guard import ClaudeUsageGateError

    async def usage_boom(prompt, options=None, **kwargs):
        raise ClaudeUsageGateError("icloud-20x is above Claude automation gate")
        yield

    monkeypatch.setattr("core.imessage_triage.classify.query", usage_boom)
    doctor_calls = []
    monkeypatch.setattr(
        "core.imessage_triage.classify.doctor_escalate",
        lambda **kw: doctor_calls.append(kw),
    )

    result = asyncio.run(classify_thread("+15551234567", ["[Them] Test"]))

    assert result["category"] == "noise"
    assert result["urgency"] == 1
    assert result["deferred"] is True
    assert not doctor_calls


def test_poll_usage_gate_defers_before_query(monkeypatch):
    from core.imessage_triage import poll as poll_mod

    queried = False

    async def fake_query_new_messages(last_rowid):
        nonlocal queried
        queried = True
        return []

    monkeypatch.setattr(
        poll_mod,
        "claude_usage_block_reason",
        lambda phase: "icloud-20x is above Claude automation gate",
    )
    monkeypatch.setattr(poll_mod, "_query_new_messages", fake_query_new_messages)

    asyncio.run(poll_mod._run())

    assert queried is False


def test_poll_deferred_classification_keeps_rowid_for_retry(monkeypatch):
    from core.imessage_triage import poll as poll_mod

    saved_state = {}

    async def fake_query_new_messages(last_rowid):
        return _make_rows((101, "+13045551234", "need help", False))

    async def fake_snippet(chat_identifier, limit=15):
        return ["[Them] need help"]

    async def fake_classify(from_handle, messages):
        return {
            "category": "noise",
            "urgency": 1,
            "deferred": True,
            "reason": "usage gate",
        }

    monkeypatch.setattr(poll_mod, "claude_usage_block_reason", lambda phase: None)
    monkeypatch.setattr(poll_mod, "_load_state", lambda: {"last_rowid": 100, "thread_rowids": {}})
    monkeypatch.setattr(poll_mod, "_query_new_messages", fake_query_new_messages)
    monkeypatch.setattr(poll_mod, "_get_thread_snippet", fake_snippet)
    monkeypatch.setattr(poll_mod, "classify_thread", fake_classify)
    monkeypatch.setattr(poll_mod, "_save_state", lambda state: saved_state.update(state))
    monkeypatch.setattr(poll_mod, "publish_imessage_triage", lambda **kw: pytest.fail("should not publish"))

    asyncio.run(poll_mod._run())

    assert saved_state["last_rowid"] == 100
    assert saved_state["thread_rowids"] == {}


# ---------------------------------------------------------------------------
# publish.py — test-data filter
# ---------------------------------------------------------------------------


def test_is_test_handle_positive():
    assert is_test_handle("+15555550100") is True
    assert is_test_handle("+15559990100") is True


def test_is_test_handle_negative():
    assert is_test_handle("+13045551234") is False
    assert is_test_handle("user@icloud.com") is False
    assert is_test_handle("+44555123456") is False


def test_publish_rejects_test_handle():
    from core.imessage_triage.publish import publish_imessage_triage
    result = publish_imessage_triage(
        chat_db_msg_id=999,
        category="action_me",
        urgency=8,
        from_handle="+15555550100",
        preview="test",
    )
    assert result is False


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
