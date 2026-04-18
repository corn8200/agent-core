"""Dispatch-control shortcode coverage (backlog #91).

Covers K<n>, S, ?<n>, R<n>, M<n> and the supporting DB plumbing
(short_id allocation, session lookup by short_id, tmux session stamping).

All tests avoid network + subprocess: send_message, _post_approval, and
subprocess.run/Popen are monkeypatched. DB is routed to a temp path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import core.message_db as _mdb  # noqa: E402

# Route DB to an isolated temp path BEFORE anything that might query it.
_TMP_DB = Path("/tmp") / f"shortcode-test-{id(object())}.db"
_mdb.DB_PATH = _TMP_DB
_mdb.init_db()


def _run(coro):
    return asyncio.run(coro)


def _make_msg(text: str, rowid: int = 1, chat: str = "corn82@icloud.com"):
    from core.message_reader import InboundMessage
    return InboundMessage(
        rowid=rowid,
        chat_identifier=chat,
        text=text,
        timestamp="2026-04-18T10:00:00",
        is_from_me=False,
    )


# --- short_id allocation ---------------------------------------------------


def test_short_id_is_monotonic_and_unique():
    from core import message_db as db

    sid1, short1 = db.create_session(
        chat_identifier="alloc1@test", agent_name="scout",
        sdk_session_id="sdk-a", initial_prompt="p1",
    )
    sid2, short2 = db.create_session(
        chat_identifier="alloc2@test", agent_name="wrench",
        sdk_session_id="sdk-b", initial_prompt="p2",
    )
    sid3, short3 = db.create_session(
        chat_identifier="alloc1@test", agent_name="forge",
        sdk_session_id="sdk-c", initial_prompt="p3",
    )
    assert short2 == short1 + 1
    assert short3 == short2 + 1
    assert sid1 != sid2 != sid3


def test_short_id_not_reused_after_close():
    from core import message_db as db

    _, short_a = db.create_session(
        chat_identifier="reuse@test", agent_name="scout",
        sdk_session_id="sdk-x", initial_prompt="x",
    )
    # Close existing sessions
    for s in db.list_sessions(chat_identifier="reuse@test"):
        db.close_session(s["session_id"])
    _, short_b = db.create_session(
        chat_identifier="reuse@test", agent_name="scout",
        sdk_session_id="sdk-y", initial_prompt="y",
    )
    assert short_b > short_a


def test_get_session_by_short_id_roundtrip():
    from core import message_db as db

    sid, short_id = db.create_session(
        chat_identifier="lookup@test", agent_name="anvil",
        sdk_session_id="sdk-lookup", initial_prompt="build thing",
    )
    found = db.get_session_by_short_id(short_id)
    assert found is not None
    assert found["session_id"] == sid
    assert found["initial_prompt"] == "build thing"
    assert db.get_session_by_short_id(999_999) is None


# --- regex + _looks_like_shortcode -----------------------------------------


def test_looks_like_shortcode_matches_new_codes():
    from core.message_router import _looks_like_shortcode

    for text in ("K1", "k42", "S", " s ", "?7", "R3", "m99", "A1", "D2", "E1 {}"):
        assert _looks_like_shortcode(text), f"should match: {text!r}"


def test_looks_like_shortcode_rejects_prose():
    from core.message_router import _looks_like_shortcode

    for text in ("kill it", "status update", "retry the job please", "more detail",
                 "? what is this", "R2D2 is here"):
        assert not _looks_like_shortcode(text), f"should not match: {text!r}"


# --- K<n> kill -------------------------------------------------------------


def test_kill_code_happy_path(monkeypatch):
    from core import message_db as db
    from core import message_router as mr

    sid, short_id = db.create_session(
        chat_identifier="kill@test", agent_name="scout",
        sdk_session_id="sdk-kill", initial_prompt="do stuff",
    )
    db.set_session_tmux_name(sid, "agent-scout-kill-abc")

    sent = []

    async def fake_send(message, agent, recipient="", **kwargs):
        sent.append((agent, message, recipient))
        return True, "ok"

    tmux_calls = []

    def fake_run(cmd, check=False, capture_output=False, timeout=None):
        tmux_calls.append(cmd)
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(mr, "send_message", fake_send)
    monkeypatch.setattr(mr.subprocess, "run", fake_run)

    handler = _run(mr._handle_short_code(_make_msg(f"K{short_id}")))
    assert handler == "dispatch-kill"
    assert tmux_calls and tmux_calls[0][:2] == ["tmux", "kill-session"]
    assert "agent-scout-kill-abc" in tmux_calls[0]
    assert any(f"dispatch {short_id} killed" in m for _, m, _ in sent)

    # Session is now closed.
    reloaded = db.get_session_by_short_id(short_id)
    assert reloaded["status"] == "closed"


def test_kill_code_missing_session(monkeypatch):
    from core import message_router as mr

    sent = []

    async def fake_send(message, agent, recipient="", **kwargs):
        sent.append((agent, message, recipient))
        return True, "ok"

    def no_run(*args, **kwargs):
        raise AssertionError("should not call tmux for missing session")

    monkeypatch.setattr(mr, "send_message", fake_send)
    monkeypatch.setattr(mr.subprocess, "run", no_run)

    handler = _run(mr._handle_short_code(_make_msg("K999999")))
    assert handler == "dispatch-kill"
    assert any("no such dispatch 999999" in m for _, m, _ in sent)


# --- S status --------------------------------------------------------------


def test_status_code_lists_recent(monkeypatch):
    from core import message_db as db
    from core import message_router as mr

    # Seed 3 sessions.
    for i, agent in enumerate(("scout", "wrench", "forge")):
        db.create_session(
            chat_identifier=f"status{i}@test", agent_name=agent,
            sdk_session_id=f"sdk-st-{i}", initial_prompt=f"do job {i}",
        )

    sent = []

    async def fake_send(message, agent, recipient="", **kwargs):
        sent.append((agent, message, recipient))
        return True, "ok"

    monkeypatch.setattr(mr, "send_message", fake_send)

    handler = _run(mr._handle_short_code(_make_msg("S")))
    assert handler == "dispatch-status"
    assert sent
    body = sent[0][1]
    # Most recent listed first. Three lines, each with [short_id] agent status.
    assert "scout" in body or "wrench" in body or "forge" in body
    assert body.count("[") >= 1


def test_status_code_empty(monkeypatch):
    """S on a DB with no sessions should reply [no recent dispatches]."""
    from core import message_router as mr
    from core import message_db as db

    sent = []

    async def fake_send(message, agent, recipient="", **kwargs):
        sent.append((agent, message, recipient))
        return True, "ok"

    def fake_recent(limit=5):
        return []

    monkeypatch.setattr(mr, "send_message", fake_send)
    monkeypatch.setattr(mr, "get_recent_sessions_summary", fake_recent)

    handler = _run(mr._handle_short_code(_make_msg("S")))
    assert handler == "dispatch-status"
    assert any("no recent dispatches" in m for _, m, _ in sent)


# --- ?<n> explain ----------------------------------------------------------


def test_explain_code_reports_session(monkeypatch):
    from core import message_db as db
    from core import message_router as mr

    sid, short_id = db.create_session(
        chat_identifier="explain@test", agent_name="critic",
        sdk_session_id="sdk-ex", initial_prompt="review the PR for logic errors",
    )
    db.touch_session(sid, status="awaiting_reply", last_question="which file?")

    sent = []

    async def fake_send(message, agent, recipient="", **kwargs):
        sent.append((agent, message, recipient))
        return True, "ok"

    monkeypatch.setattr(mr, "send_message", fake_send)

    handler = _run(mr._handle_short_code(_make_msg(f"?{short_id}")))
    assert handler == "dispatch-explain"
    body = sent[0][1]
    assert f"[{short_id}]" in body
    assert "critic" in body
    assert "awaiting_reply" in body
    assert "which file?" in body


def test_explain_code_missing(monkeypatch):
    from core import message_router as mr

    sent = []

    async def fake_send(message, agent, recipient="", **kwargs):
        sent.append((agent, message, recipient))
        return True, "ok"

    monkeypatch.setattr(mr, "send_message", fake_send)

    handler = _run(mr._handle_short_code(_make_msg("?424242")))
    assert handler == "dispatch-explain"
    assert any("no such dispatch 424242" in m for _, m, _ in sent)


# --- R<n> retry ------------------------------------------------------------


def test_retry_code_creates_fresh_session(monkeypatch):
    from core import message_db as db
    from core import message_router as mr

    sid, short_id = db.create_session(
        chat_identifier="retry@test", agent_name="scout",
        sdk_session_id="sdk-orig", initial_prompt="research drone companies",
    )

    sent = []
    spawn_calls = []

    async def fake_send(message, agent, recipient="", **kwargs):
        sent.append((agent, message, recipient))
        return True, "ok"

    async def fake_spawn(**kwargs):
        spawn_calls.append(kwargs)

    monkeypatch.setattr(mr, "send_message", fake_send)
    monkeypatch.setattr(mr, "_spawn_dispatch", fake_spawn)

    handler = _run(mr._handle_short_code(_make_msg(f"R{short_id}")))
    assert handler.startswith("dispatch-retry:")

    assert len(spawn_calls) == 1
    assert spawn_calls[0]["agent_name"] == "scout"
    assert spawn_calls[0]["prompt"] == "research drone companies"
    assert spawn_calls[0]["resume"] is False

    new_short = spawn_calls[0]["short_id"]
    assert new_short > short_id
    assert any(f"retry {short_id} -> new dispatch {new_short}" in m for _, m, _ in sent)


def test_retry_code_missing(monkeypatch):
    from core import message_router as mr

    sent = []

    async def fake_send(message, agent, recipient="", **kwargs):
        sent.append((agent, message, recipient))
        return True, "ok"

    async def no_spawn(**kwargs):
        raise AssertionError("no spawn on missing session")

    monkeypatch.setattr(mr, "send_message", fake_send)
    monkeypatch.setattr(mr, "_spawn_dispatch", no_spawn)

    handler = _run(mr._handle_short_code(_make_msg("R888888")))
    assert handler == "dispatch-retry:miss"
    assert any("no such dispatch 888888" in m for _, m, _ in sent)


# --- M<n> more -------------------------------------------------------------


def test_more_code_resumes_session(monkeypatch):
    from core import message_db as db
    from core import message_router as mr

    sid, short_id = db.create_session(
        chat_identifier="more@test", agent_name="forge",
        sdk_session_id="sdk-more", initial_prompt="write cover letter",
    )

    sent = []
    spawn_calls = []

    async def fake_send(message, agent, recipient="", **kwargs):
        sent.append((agent, message, recipient))
        return True, "ok"

    async def fake_spawn(**kwargs):
        spawn_calls.append(kwargs)

    monkeypatch.setattr(mr, "send_message", fake_send)
    monkeypatch.setattr(mr, "_spawn_dispatch", fake_spawn)

    handler = _run(mr._handle_short_code(_make_msg(f"M{short_id}")))
    assert handler.startswith("resume:") or handler.startswith("dispatch-more")

    assert len(spawn_calls) == 1
    assert spawn_calls[0]["resume"] is True
    assert spawn_calls[0]["agent_name"] == "forge"
    assert "Expand your last response" in spawn_calls[0]["prompt"]
    assert spawn_calls[0]["short_id"] == short_id


def test_more_code_missing(monkeypatch):
    from core import message_router as mr

    sent = []

    async def fake_send(message, agent, recipient="", **kwargs):
        sent.append((agent, message, recipient))
        return True, "ok"

    monkeypatch.setattr(mr, "send_message", fake_send)

    handler = _run(mr._handle_short_code(_make_msg("M777777")))
    assert handler == "dispatch-more:miss"
    assert any("no such dispatch 777777" in m for _, m, _ in sent)


# --- Outbound prefix stamping ---------------------------------------------


def test_dispatch_script_prefixes_output():
    from core import message_router as mr

    script = mr._build_dispatch_script(
        prompt_path="/tmp/fake-prompt.txt",
        agent_name="scout",
        model="opus",
        chat_identifier="prefix@test",
        session_id="sid-abc",
        sdk_session_id="sdk-abc",
        resume=False,
        short_id=42,
    )
    # The emitted dispatcher body composes f"[42] {result}" for normal returns
    # and f"[42] {question}" for CLARIFY.
    assert 'f"[42] {result}"' in script
    assert 'f"[42] {question}"' in script
    # Attribution must be suppressed on dispatch output — the [N] is the identity.
    assert "attribution=False" in script


# --- Routing integration ---------------------------------------------------


def test_route_integration_kill_code(monkeypatch):
    """End-to-end: K<n> on a message goes through the shortcode layer."""
    from core import message_db as db
    from core import message_router as mr

    sid, short_id = db.create_session(
        chat_identifier="route@test", agent_name="scout",
        sdk_session_id="sdk-route", initial_prompt="task",
    )
    db.set_session_tmux_name(sid, "agent-scout-route-xyz")

    sent = []

    async def fake_send(message, agent, recipient="", **kwargs):
        sent.append((agent, message, recipient))
        return True, "ok"

    def fake_run(cmd, check=False, capture_output=False, timeout=None):
        class R:
            returncode = 0
        return R()

    async def no_classify(msg):
        raise AssertionError("LLM classification should not run for shortcode")

    monkeypatch.setattr(mr, "send_message", fake_send)
    monkeypatch.setattr(mr.subprocess, "run", fake_run)
    monkeypatch.setattr(mr, "_classify_with_opus", no_classify)

    handler = _run(mr.route(_make_msg(f"K{short_id}", rowid=7777)))
    assert handler == "dispatch-kill"


# --- Cleanup ---------------------------------------------------------------


def teardown_module(module):
    try:
        _TMP_DB.unlink(missing_ok=True)
        _TMP_DB.with_suffix(_TMP_DB.suffix + "-shm").unlink(missing_ok=True)
        _TMP_DB.with_suffix(_TMP_DB.suffix + "-wal").unlink(missing_ok=True)
    except Exception:
        pass
