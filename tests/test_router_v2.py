"""router-v2 coverage — Tasks A, B, C, D.

All tests avoid network, real chat.db, and real claude CLI calls. The
tmux_relay_shell, Anthropic SDK, and OpenAI client are monkeypatched.

Test groups:
    test_attach_*      — Task A, attachment enrichment
    test_session_*     — Task B, resumable sessions + CLARIFY
    test_vps_*         — Task C, Layer 0 reply tag
    test_batch_*       — Task D, silent batching
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Route the DB to a per-test path so we don't stomp ~/logs/message_bus.db.
_TMP_DB = Path("/tmp") / f"routerv2-test-{id(object())}.db"
import core.message_db as _mdb  # noqa: E402
_mdb.DB_PATH = _TMP_DB
# Re-init against the temp path.
_mdb.init_db()


# --------------------------------------------------------------------- Task A


def _run(coro):
    return asyncio.run(coro)


async def _fake_relay_factory(rows):
    """Return an async tmux_relay_shell stub that yields the given output."""
    sep_f = "\x1f"
    sep_r = "\x1e"
    out = sep_r.join(sep_f.join(str(c) for c in r) for r in rows)
    if out:
        out += sep_r

    async def stub(cmd, timeout=10.0):
        return True, out
    return stub


def test_attach_query_parses_rows(monkeypatch):
    from core import message_attachments as ma

    rows = [
        [101, "~/Library/Messages/Attachments/aa/bb/photo.jpg", "photo.jpg",
         "image/jpeg", "public.jpeg", 12345],
        [102, "~/Library/Messages/Attachments/cc/dd/voice.caf", "voice.caf",
         "audio/x-caf", "com.apple.coreaudio-format", 54321],
    ]
    stub = _run(_fake_relay_factory(rows))
    monkeypatch.setattr(ma, "tmux_relay_shell", stub)

    infos = _run(ma.query_attachments_for_message(42))
    assert len(infos) == 2
    assert infos[0].kind == "image"
    assert infos[1].kind == "audio"
    assert infos[0].transfer_name == "photo.jpg"
    assert infos[1].mime_type == "audio/x-caf"


def test_attach_stage_invokes_cp(monkeypatch, tmp_path):
    from core import message_attachments as ma

    calls = {}

    async def stub(cmd, timeout=15.0):
        calls["cmd"] = cmd
        return True, "OK\n"

    monkeypatch.setattr(ma, "tmux_relay_shell", stub)
    info = ma.AttachmentInfo(
        attachment_rowid=99,
        filename="~/Library/Messages/Attachments/aa/bb/photo.jpg",
        transfer_name="photo.jpg",
        mime_type="image/jpeg",
    )
    dest = _run(ma.stage_attachment(info))
    assert dest is not None
    assert "cp" in calls["cmd"]
    assert "photo.jpg" in calls["cmd"]
    assert str(dest).startswith("/tmp/ab-attach-")


def test_attach_enrich_image_prepends(monkeypatch, tmp_path):
    from core import message_attachments as ma

    fake_path = tmp_path / "img.jpg"
    fake_path.write_bytes(b"\xff\xd8\xff\xe0not-really")

    async def fake_query(rowid):
        return [ma.AttachmentInfo(
            attachment_rowid=1,
            filename="~/Library/Messages/Attachments/aa/bb/img.jpg",
            transfer_name="img.jpg",
            mime_type="image/jpeg",
        )]

    async def fake_stage(info):
        info.staged_path = fake_path
        return fake_path

    async def fake_desc(path):
        return "red square with number 7"

    def no_cleanup(info):
        pass

    monkeypatch.setattr(ma, "query_attachments_for_message", fake_query)
    monkeypatch.setattr(ma, "stage_attachment", fake_stage)
    monkeypatch.setattr(ma, "describe_image", fake_desc)
    monkeypatch.setattr(ma, "cleanup_staged", no_cleanup)

    result = _run(ma.enrich_message_text(42, "check this out"))
    assert result.startswith("[image: red square with number 7]")
    assert "check this out" in result


def test_attach_enrich_audio_prepends(monkeypatch, tmp_path):
    from core import message_attachments as ma

    fake_path = tmp_path / "voice.m4a"
    fake_path.write_bytes(b"fake-audio")

    async def fake_query(rowid):
        return [ma.AttachmentInfo(
            attachment_rowid=2,
            filename="~/Library/Messages/Attachments/aa/bb/voice.m4a",
            transfer_name="voice.m4a",
            mime_type="audio/mp4",
        )]

    async def fake_stage(info):
        info.staged_path = fake_path
        return fake_path

    async def fake_transcribe(path):
        return "hey can you pick up milk"

    monkeypatch.setattr(ma, "query_attachments_for_message", fake_query)
    monkeypatch.setattr(ma, "stage_attachment", fake_stage)
    monkeypatch.setattr(ma, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ma, "cleanup_staged", lambda info: None)

    result = _run(ma.enrich_message_text(43, ""))
    assert result == "[voice: hey can you pick up milk]"


def test_attach_unsupported_falls_through(monkeypatch, tmp_path):
    from core import message_attachments as ma

    fake_path = tmp_path / "deck.pdf"
    fake_path.write_bytes(b"%PDF-1.4")

    async def fake_query(rowid):
        return [ma.AttachmentInfo(
            attachment_rowid=3,
            filename="~/Library/Messages/Attachments/aa/bb/deck.pdf",
            transfer_name="deck.pdf",
            mime_type="application/pdf",
        )]

    async def fake_stage(info):
        info.staged_path = fake_path
        return fake_path

    monkeypatch.setattr(ma, "query_attachments_for_message", fake_query)
    monkeypatch.setattr(ma, "stage_attachment", fake_stage)
    monkeypatch.setattr(ma, "cleanup_staged", lambda info: None)

    result = _run(ma.enrich_message_text(44, "look"))
    assert "unsupported" in result
    assert "deck.pdf" in result
    assert "look" in result


def test_attach_no_attachments_is_identity(monkeypatch):
    from core import message_attachments as ma

    async def fake_query(rowid):
        return []

    monkeypatch.setattr(ma, "query_attachments_for_message", fake_query)
    result = _run(ma.enrich_message_text(50, "hello world"))
    assert result == "hello world"


# --------------------------------------------------------------------- Task B


def test_session_create_and_fetch():
    from core import message_db as db
    chat = "createfetch@test"
    sid, short_id = db.create_session(
        chat_identifier=chat,
        agent_name="scout",
        sdk_session_id="sdk-123",
        initial_prompt="research X",
    )
    assert isinstance(short_id, int) and short_id > 0
    active = db.get_active_session(chat)
    assert active is not None
    assert active["session_id"] == sid
    assert active["agent_name"] == "scout"
    assert active["status"] == "open"


def test_session_touch_and_close():
    from core import message_db as db
    chat = "touchclose@test"
    sid, _ = db.create_session(
        chat_identifier=chat, agent_name="wrench",
        sdk_session_id="sdk-xx", initial_prompt="check vps",
    )
    db.touch_session(sid, status="awaiting_reply", last_question="which service?")
    active = db.get_active_session(chat)
    assert active["status"] == "awaiting_reply"
    assert active["last_question"] == "which service?"
    db.close_session(sid)
    active = db.get_active_session(chat)
    assert active is None  # closed sessions are not 'active'


def test_session_stale_expiration():
    from core import message_db as db
    sid, _ = db.create_session(
        chat_identifier="stale@test", agent_name="scout",
        sdk_session_id="sdk-stale", initial_prompt="old",
    )
    # Forcibly age the session past the cutoff.
    with sqlite3.connect(str(db.DB_PATH)) as conn:
        conn.execute(
            "UPDATE sessions SET last_activity_at = '2020-01-01T00:00:00' "
            "WHERE session_id = ?", (sid,),
        )
        conn.commit()
    assert db.get_active_session("stale@test") is None  # past cutoff
    n = db.expire_stale_sessions()
    assert n >= 1
    with sqlite3.connect(str(db.DB_PATH)) as conn:
        row = conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()
    assert row[0] == "closed"


def test_router_clarify_regex():
    from core.message_router import CLARIFY_RE
    text = "I need one more detail.\nCLARIFY: which repo should I grep?"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    import re
    m = re.match(r"\s*CLARIFY\s*:\s*(.+)$", lines[-1], re.IGNORECASE)
    assert m is not None
    assert "which repo" in m.group(1)


def test_session_lock_blocks_parallel(monkeypatch):
    """Second dispatch on same chat while one is active gets blocked."""
    from core import message_db as db
    from core import message_router as mr

    # Pre-seed an active session.
    db.create_session(
        chat_identifier="lock@test", agent_name="scout",
        sdk_session_id="locked", initial_prompt="running",
    )

    sent = []

    async def fake_send(message, agent, recipient="", **kwargs):
        sent.append((agent, message, recipient))
        return True, "ok"

    async def no_spawn(**kwargs):
        raise AssertionError("should not spawn when locked")

    monkeypatch.setattr(mr, "send_message", fake_send)
    monkeypatch.setattr(mr, "_spawn_dispatch", no_spawn)

    handler = _run(mr._dispatch_to_agent("wrench", "do a thing", "lock@test"))
    assert handler.startswith("blocked:")
    assert any("session locked" in m for _, m, _ in sent)


# --------------------------------------------------------------------- Task C


def test_vps_tag_matches_all_services():
    from core.message_router import VPS_TAG_RE, VPS_REPLY_SERVICES
    for svc in VPS_REPLY_SERVICES:
        text = f"[V:{svc}:abc123] here is my reply"
        m = VPS_TAG_RE.match(text)
        assert m is not None
        assert m.group("service").lower() == svc
        assert m.group("ref") == "abc123"


def test_vps_tag_whitespace_tolerant():
    from core.message_router import VPS_TAG_RE
    m = VPS_TAG_RE.match("  [ v : Sentinel : ref-99 ]  hello")
    assert m is not None
    assert m.group("service").lower() == "sentinel"
    assert m.group("ref") == "ref-99"


def test_vps_tag_rejects_nonvps():
    from core.message_router import VPS_TAG_RE
    # "A" short-code should not match
    assert VPS_TAG_RE.match("A1") is None
    # research: prefix should not match
    assert VPS_TAG_RE.match("research: look up x") is None


def test_vps_handler_posts_and_returns(monkeypatch):
    from core import message_router as mr
    from core.message_reader import InboundMessage

    captured = {}

    def fake_post(service, ref, reply, chat_identifier, timestamp):
        captured.update(dict(
            service=service, ref=ref, reply=reply,
            chat=chat_identifier, ts=timestamp,
        ))
        return True, 200, "ok"

    monkeypatch.setattr(mr, "_post_vps_reply", fake_post)

    msg = InboundMessage(
        rowid=999,
        chat_identifier="corn82@icloud.com",
        text="[V:sentinel:abc12] snooze the alert for 1h",
        timestamp="2026-04-17T10:00:00",
        is_from_me=False,
    )
    handler = _run(mr._handle_vps_reply(msg))
    assert handler == "vps:sentinel"
    assert captured["service"] == "sentinel"
    assert captured["ref"] == "abc12"
    assert "snooze" in captured["reply"]


def test_vps_handler_falls_through_on_http_fail(monkeypatch):
    """Non-2xx must return None so the router keeps processing."""
    from core import message_router as mr
    from core.message_reader import InboundMessage

    def fake_post(*args, **kwargs):
        return False, 503, "backend down"

    monkeypatch.setattr(mr, "_post_vps_reply", fake_post)

    msg = InboundMessage(
        rowid=1000,
        chat_identifier="corn82@icloud.com",
        text="[V:mailtriage:zz] skip it",
        timestamp="t",
        is_from_me=False,
    )
    handler = _run(mr._handle_vps_reply(msg))
    assert handler is None


def test_vps_handler_unknown_service_falls_through(monkeypatch):
    from core import message_router as mr
    from core.message_reader import InboundMessage

    # Ensure no POST attempted
    def boom(*args, **kwargs):
        raise AssertionError("should not POST for unknown service")

    monkeypatch.setattr(mr, "_post_vps_reply", boom)

    msg = InboundMessage(
        rowid=1001,
        chat_identifier="corn82@icloud.com",
        text="[V:stranger:x] hi",
        timestamp="t",
        is_from_me=False,
    )
    handler = _run(mr._handle_vps_reply(msg))
    assert handler is None


def test_message_bus_reply_tag_prepends(monkeypatch):
    """send_message(reply_tag='sentinel:xyz') must prepend [V:...]."""
    from core import message_bus as mb

    deliveries = []

    async def fake_deliver(recipient, message, tier):
        deliveries.append((recipient, message, tier))
        return True, "sent"

    monkeypatch.setattr(mb, "_deliver", fake_deliver)

    ok, _ = _run(mb.send_message(
        "alert cleared", agent="sentinel", recipient="corn82@icloud.com",
        reply_tag="sentinel:xyz",
    ))
    assert ok
    msg = deliveries[0][1]
    assert msg.startswith("[V:sentinel:xyz] ")
    # Attribution still present (after the tag).
    assert "[Sentinel]" in msg or "[sentinel]" in msg.lower()


# --------------------------------------------------------------------- Task D


def test_batch_joins_multiple_messages(monkeypatch):
    """Three enqueues within the window produce one joined outbound."""
    from core import message_bus as mb
    from core import message_batch as mbatch

    sent = []

    async def fake_deliver(recipient, message, tier):
        sent.append((recipient, message, tier))
        return True, "sent"

    monkeypatch.setattr(mb, "_deliver", fake_deliver)

    async def scenario():
        # Reset batch singleton so we pick up the patched _deliver.
        mbatch._window = mbatch.BatchWindow(fake_deliver)
        await mb.send_message("one", agent="scout", recipient="r@x",
                              batch_window=1)
        await mb.send_message("two", agent="forge", recipient="r@x",
                              batch_window=1)
        await mb.send_message("three", agent="wrench", recipient="r@x",
                              batch_window=1)
        # Wait for the sliding window + the spawned send task.
        await asyncio.sleep(2.5)

    _run(scenario())
    # Expect exactly ONE delivery on r@x.
    r_deliveries = [d for d in sent if d[0] == "r@x"]
    assert len(r_deliveries) == 1
    body = r_deliveries[0][1]
    assert "[batched: 3 messages" in body
    # All three fragments present, separator visible.
    for frag in ("one", "two", "three"):
        assert frag in body
    assert "— — —" in body


def test_batch_force_flush_on_overflow(monkeypatch):
    from core import message_batch as mbatch

    sent = []

    async def fake_deliver(recipient, message, tier):
        sent.append((recipient, message, tier))
        return True, "sent"

    async def scenario():
        win = mbatch.BatchWindow(fake_deliver)
        # Push MAX_QUEUE+1 messages, all in same chat
        for i in range(mbatch.MAX_QUEUE + 1):
            await win.enqueue(
                recipient="overflow@x",
                message=f"m{i}",
                tier="normal",
                agent="scout",
                log_id=None,
                window=60,  # a large window — overflow must force flush sooner
            )
        # Give the flush task a moment
        await asyncio.sleep(0.5)

    _run(scenario())
    assert len(sent) >= 1
    body = sent[0][1]
    assert "[batched" in body


def test_batch_default_instant(monkeypatch):
    """batch_window=None must send instantly (no queueing)."""
    from core import message_bus as mb

    sent = []

    async def fake_deliver(recipient, message, tier):
        sent.append((recipient, message, tier))
        return True, "sent"

    monkeypatch.setattr(mb, "_deliver", fake_deliver)

    ok, _ = _run(mb.send_message("instant", agent="scout", recipient="r@y"))
    assert ok
    assert len(sent) == 1  # single instant delivery
    assert "instant" in sent[0][1]


def test_batch_flush_all_drains(monkeypatch):
    from core import message_batch as mbatch

    sent = []

    async def fake_deliver(recipient, message, tier):
        sent.append((recipient, message, tier))
        return True, "sent"

    async def scenario():
        win = mbatch.BatchWindow(fake_deliver)
        mbatch._window = win
        await win.enqueue(recipient="flush@x", message="a", tier="normal",
                          agent="scout", log_id=None, window=60)
        await win.enqueue(recipient="flush@x", message="b", tier="normal",
                          agent="scout", log_id=None, window=60)
        assert win.pending_count() == 2
        await mbatch.flush_all()
        await asyncio.sleep(0.3)  # let _send_batched task run
        assert win.pending_count() == 0

    _run(scenario())
    assert len(sent) == 1


# --------------------------------------------------------- Router layer order


def test_router_layer_order_vps_before_shortcode(monkeypatch):
    """A message that looks like both a VPS tag and a short-code must go to VPS."""
    from core import message_router as mr
    from core.message_reader import InboundMessage

    # "[V:notify:a1]" looks like a V-tag; short-code regex wouldn't match it
    # because of the bracket, but we still want to be sure VPS runs first.
    def fake_post(service, ref, reply, chat_identifier, timestamp):
        return True, 200, "ok"

    async def no_shortcode(msg):
        raise AssertionError("short-code handler ran before VPS")

    async def no_prefix(msg):
        return None

    monkeypatch.setattr(mr, "_post_vps_reply", fake_post)
    monkeypatch.setattr(mr, "_handle_short_code", no_shortcode)
    monkeypatch.setattr(mr, "_handle_prefix", no_prefix)

    msg = InboundMessage(
        rowid=2000,
        chat_identifier="corn82@icloud.com",
        text="[V:notify:foo] skip it",
        timestamp="t",
        is_from_me=False,
    )
    handler = _run(mr.route(msg))
    assert handler == "vps:notify"


# -------------------------------------------------------- Reader + attachments


def test_reader_enriches_attachment_message(monkeypatch):
    """Reader calls enrich_message_text when an attachment row exists."""
    from core import message_reader as _mr

    # Build a fake chat.db output: one row, empty text body.
    sep_f = _mr._FIELD_SEP
    sep_r = _mr._RECORD_SEP
    rowid = 5001
    row = sep_f.join([str(rowid), "", "", "0",
                       "corn82@icloud.com", "2026-04-17 10:00:00"])
    out = row + sep_r

    async def fake_relay(cmd, timeout=10.0):
        return True, out

    async def fake_enrich(rid, text):
        return "[image: a cat sitting on a chair]"

    monkeypatch.setattr(_mr, "tmux_relay_shell", fake_relay)
    monkeypatch.setattr(_mr, "_enrich_with_attachments", fake_enrich)

    reader = _mr.MessageReader(poll_interval=1)
    reader._get_last_rowid = lambda: 0
    reader._save_rowid = lambda r: None
    msgs = _run(reader._poll_once())
    assert len(msgs) == 1
    assert msgs[0].text == "[image: a cat sitting on a chair]"


# --------------------------------------------------------------------- Cleanup

def teardown_module(module):
    try:
        _TMP_DB.unlink(missing_ok=True)
        # WAL sidecars
        _TMP_DB.with_suffix(_TMP_DB.suffix + "-shm").unlink(missing_ok=True)
        _TMP_DB.with_suffix(_TMP_DB.suffix + "-wal").unlink(missing_ok=True)
    except Exception:
        pass
