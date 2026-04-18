"""Universal Outbox tests.

Covers:
- self-recipient -> direct send, no pending file
- third-party -> queued, pending file exists, correct preview format
- APPROVE promotes to sent/, send_fn called once with _approved=True
- DENY moves to denied/, send_fn NOT called
- APPROVE on non-existent uuid -> graceful
- _is_self normalization across phone formats

All tests stub out the preview-delivery call so no real iMessage fires.
Outbox storage is redirected to a per-test temp dir.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import pytest

import core.outbox as ob


# --- Fixtures --------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_outbox(monkeypatch, tmp_path):
    """Redirect outbox storage to a temp dir and stub the preview delivery."""
    tmp_root = tmp_path / "outbox"
    pending = tmp_root / "pending"
    sent = tmp_root / "sent"
    denied = tmp_root / "denied"

    monkeypatch.setattr(ob, "OUTBOX_ROOT", tmp_root)
    monkeypatch.setattr(ob, "PENDING_DIR", pending)
    monkeypatch.setattr(ob, "SENT_DIR", sent)
    monkeypatch.setattr(ob, "DENIED_DIR", denied)

    sent_preview = {"count": 0, "last": None}

    async def _fake_preview(text: str):
        sent_preview["count"] += 1
        sent_preview["last"] = text

    monkeypatch.setattr(ob, "_send_preview_imessage", _fake_preview)
    yield {"pending": pending, "sent": sent, "denied": denied, "preview": sent_preview}
    shutil.rmtree(tmp_root, ignore_errors=True)


def _run(coro):
    return asyncio.run(coro)


# --- _is_self normalization ------------------------------------------------

def test_is_self_matches_icloud():
    assert ob._is_self("corn82@icloud.com")
    assert ob._is_self("CORN82@icloud.com")
    assert ob._is_self("  corn82@icloud.com  ")


def test_is_self_matches_phone_formats():
    assert ob._is_self("+13042684985")
    assert ob._is_self("304-268-4985")
    assert ob._is_self("304 268 4985")
    assert ob._is_self("(304) 268-4985")
    assert ob._is_self("3042684985")


def test_is_self_rejects_strangers():
    assert not ob._is_self("principal@school.edu")
    assert not ob._is_self("+18005551212")
    assert not ob._is_self("")
    assert not ob._is_self(None)  # type: ignore[arg-type]


# --- queue_or_send: self path ----------------------------------------------

def test_self_recipient_sends_direct_and_leaves_no_pending(_isolated_outbox):
    calls = {"n": 0, "kwargs": None}

    async def fake_send(**kwargs):
        calls["n"] += 1
        calls["kwargs"] = kwargs
        return True, "sent"

    async def run():
        result = await ob.queue_or_send(
            channel="imessage",
            recipient="corn82@icloud.com",
            subject=None,
            body="hello me",
            source="test",
            send_fn=fake_send,
            send_fn_module="core.tools",
            send_fn_name="send_imessage_reliable",
            send_kwargs={"buddy": "corn82@icloud.com", "message": "hello me"},
        )
        return result

    result = _run(run())
    assert result["status"] == "sent_direct"
    assert calls["n"] == 1
    assert calls["kwargs"]["buddy"] == "corn82@icloud.com"
    # No pending record created
    assert not any(_isolated_outbox["pending"].glob("*.json")) if _isolated_outbox["pending"].exists() else True
    # No preview iMessage sent (self path bypasses outbox entirely)
    assert _isolated_outbox["preview"]["count"] == 0


# --- queue_or_send: third-party path --------------------------------------

def test_third_party_queues_and_writes_pending(_isolated_outbox):
    async def fake_send(**kwargs):  # pragma: no cover - must not be called
        raise AssertionError("send_fn should NOT fire for queued third-party")

    async def run():
        return await ob.queue_or_send(
            channel="email_personal",
            recipient="principal@school.edu",
            subject="DLE follow-up",
            body="Good morning, checking in about next week's meeting.",
            source="test-third-party",
            send_fn=fake_send,
            send_fn_module="core.tools",
            send_fn_name="send_personal_email",
            send_kwargs={"args": {"to": "principal@school.edu",
                                   "subject": "DLE follow-up",
                                   "body": "Good morning..."}},
        )

    result = _run(run())
    assert result["status"] == "queued"
    uid = result["uuid"]
    assert uid and len(uid) == 12

    # Pending record written
    pending_file = _isolated_outbox["pending"] / f"{uid}.json"
    assert pending_file.exists()
    record = json.loads(pending_file.read_text())
    assert record["recipient"] == "principal@school.edu"
    assert record["channel"] == "email_personal"
    assert record["source"] == "test-third-party"
    assert record["send_fn_module"] == "core.tools"
    assert record["send_fn_name"] == "send_personal_email"

    # Preview was sent to John with APPROVE/DENY tokens
    assert _isolated_outbox["preview"]["count"] == 1
    preview = _isolated_outbox["preview"]["last"]
    assert "[OUTBOX]" in preview
    assert "email_personal" in preview
    assert "principal@school.edu" in preview
    assert f"APPROVE:{uid}" in preview
    assert f"DENY:{uid}" in preview


# --- promote (APPROVE) -----------------------------------------------------

def test_approve_promotes_and_calls_send_fn(_isolated_outbox, monkeypatch):
    calls = {"n": 0, "kwargs": None}

    async def fake_send_fn(**kwargs):
        calls["n"] += 1
        calls["kwargs"] = kwargs
        return {"content": [{"type": "text", "text": "Personal email sent to them"}]}

    # Inject a fake module exposing our fake send fn under a known attr.
    import types
    fake_mod = types.ModuleType("tests._fake_send_mod")
    fake_mod.fake_send = fake_send_fn  # type: ignore[attr-defined]
    import sys
    sys.modules["tests._fake_send_mod"] = fake_mod

    # Queue a third-party record via queue_or_send so the pending file uses
    # the real write path.
    async def seed():
        async def unused(**_k):
            raise AssertionError("unused in queue path")
        return await ob.queue_or_send(
            channel="email_personal",
            recipient="teacher@school.edu",
            subject="checking in",
            body="hi, just following up.",
            source="test-approve",
            send_fn=unused,
            send_fn_module="tests._fake_send_mod",
            send_fn_name="fake_send",
            send_kwargs={"args": {"to": "teacher@school.edu",
                                   "subject": "checking in",
                                   "body": "hi, just following up."}},
        )

    seed_result = _run(seed())
    uid = seed_result["uuid"]
    assert (_isolated_outbox["pending"] / f"{uid}.json").exists()

    # Now approve.
    result = _run(ob.promote(uid))
    assert result["status"] == "sent"
    assert calls["n"] == 1
    # _approved=True must have been injected into the args dict
    assert calls["kwargs"]["args"]["_approved"] is True
    assert calls["kwargs"]["args"]["to"] == "teacher@school.edu"

    # Pending moved to sent/
    assert not (_isolated_outbox["pending"] / f"{uid}.json").exists()
    sent_file = _isolated_outbox["sent"] / f"{uid}.json"
    assert sent_file.exists()
    sent_record = json.loads(sent_file.read_text())
    assert sent_record["decision"] == "approve"
    assert "decided_at" in sent_record


def test_approve_missing_uuid_is_graceful(_isolated_outbox):
    result = _run(ob.promote("deadbeef0000"))
    assert result["status"] == "missing"
    assert result["uuid"] == "deadbeef0000"


def test_approve_injects_bare_approved_for_non_args_shape(_isolated_outbox):
    """Native async fns (not @tool decorated) get _approved=True at top level,
    not inside an args dict."""
    calls = {"n": 0, "kwargs": None}

    async def fake_native(**kwargs):
        calls["n"] += 1
        calls["kwargs"] = kwargs
        return True, "ok"

    import types, sys
    fake_mod = types.ModuleType("tests._fake_native_mod")
    fake_mod.fake_native = fake_native  # type: ignore[attr-defined]
    sys.modules["tests._fake_native_mod"] = fake_mod

    async def seed():
        return await ob.queue_or_send(
            channel="imessage",
            recipient="stranger@example.com",
            subject=None,
            body="hi",
            source="test",
            send_fn=fake_native,
            send_fn_module="tests._fake_native_mod",
            send_fn_name="fake_native",
            send_kwargs={"buddy": "stranger@example.com", "message": "hi"},
        )

    uid = _run(seed())["uuid"]
    _run(ob.promote(uid))
    assert calls["n"] == 1
    assert calls["kwargs"]["_approved"] is True
    assert calls["kwargs"]["buddy"] == "stranger@example.com"


# --- deny (DENY) -----------------------------------------------------------

def test_deny_moves_to_denied_and_never_calls_send(_isolated_outbox):
    async def fake_send(**_k):  # pragma: no cover
        raise AssertionError("send must NOT fire on deny")

    async def seed():
        return await ob.queue_or_send(
            channel="imessage",
            recipient="stranger@example.com",
            subject=None,
            body="nope",
            source="test-deny",
            send_fn=fake_send,
            send_fn_module="core.tools",
            send_fn_name="send_imessage_reliable",
            send_kwargs={"buddy": "stranger@example.com", "message": "nope"},
        )

    uid = _run(seed())["uuid"]
    assert (_isolated_outbox["pending"] / f"{uid}.json").exists()

    result = _run(ob.deny(uid))
    assert result["status"] == "denied"
    assert result["uuid"] == uid
    assert not (_isolated_outbox["pending"] / f"{uid}.json").exists()
    denied_file = _isolated_outbox["denied"] / f"{uid}.json"
    assert denied_file.exists()
    denied_record = json.loads(denied_file.read_text())
    assert denied_record["decision"] == "deny"


def test_deny_missing_uuid_is_graceful(_isolated_outbox):
    result = _run(ob.deny("deadbeef1111"))
    assert result["status"] == "missing"


# --- SdkMcpTool unwrapping in promote() ------------------------------------

def test_promote_unwraps_sdk_mcp_tool(_isolated_outbox):
    """@tool-decorated fns become SdkMcpTool NamedTuples. promote() must
    follow .handler to the real coroutine."""
    from claude_agent_sdk import tool as sdk_tool  # allow-direct-sdk

    calls = {"n": 0}

    @sdk_tool("fake_tool", "test tool", {"to": str})
    async def fake_tool_handler(args):
        calls["n"] += 1
        return {"content": [{"type": "text", "text": f"to={args.get('to')} _approved={args.get('_approved')}"}]}

    import types, sys
    fake_mod = types.ModuleType("tests._fake_tool_mod")
    fake_mod.fake_tool_handler = fake_tool_handler  # type: ignore[attr-defined]
    sys.modules["tests._fake_tool_mod"] = fake_mod

    async def seed():
        async def unused(**_k):
            raise AssertionError("unused")
        return await ob.queue_or_send(
            channel="email_personal",
            recipient="strange@example.com",
            subject="hi",
            body="hi there",
            source="test-tool-unwrap",
            send_fn=unused,
            send_fn_module="tests._fake_tool_mod",
            send_fn_name="fake_tool_handler",
            send_kwargs={"args": {"to": "strange@example.com"}},
        )

    uid = _run(seed())["uuid"]
    result = _run(ob.promote(uid))
    assert result["status"] == "sent", f"expected sent, got {result}"
    assert calls["n"] == 1
