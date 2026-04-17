"""Enqueue iMessage threads for vector embedding on the VPS RQ worker.

One chunk per thread (last 10 messages of the last 24h), not per message.
Debounced to at most one emit per minute per chat_identifier. Skips
transactional short-code senders, bot-attribution-only threads, and
tiny combined transcripts.

Mac syncer pattern — mirrors tasks/mac_vector.py reminders/notes/calendar.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable

from redis import Redis
from rq import Queue

from core.message_reader import (
    _FIELD_SEP,
    _RECORD_SEP,
    _sqlite_via_relay_cmd,
    extract_text_from_attributed_body,
)
from core.tools import tmux_relay_shell

logger = logging.getLogger(__name__)

REDIS_HOST = "100.118.21.64"
REDIS_PORT = 6379
REDIS_DB = 0

THREAD_MSG_WINDOW = 10
THREAD_HOURS_WINDOW = 24
DEBOUNCE_SECONDS = 60
MIN_TRANSCRIPT_CHARS = 50

# US carrier short codes are 5-6 digits. Treat pure-digit chat identifiers
# of that length as transactional (Amazon, DoorDash, verification codes).
_SHORT_CODE_RE = re.compile(r"^\d{5,6}$")
_BOT_ATTRIBUTION_RE = re.compile(r"^[^A-Za-z0-9]?\[[A-Z][A-Za-z0-9 _-]{1,30}\]")


_last_emit: dict[str, float] = {}
_queue: Queue | None = None


def _get_queue() -> Queue:
    global _queue
    if _queue is None:
        _queue = Queue(
            "vps",
            connection=Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB),
        )
    return _queue


def _is_short_code(chat_identifier: str) -> bool:
    ident = (chat_identifier or "").lstrip("+")
    return bool(_SHORT_CODE_RE.match(ident))


def _is_bot_attribution(text: str) -> bool:
    if not text:
        return False
    return bool(_BOT_ATTRIBUTION_RE.match(text.lstrip()))


def _format_participant(chat_identifier: str, from_me: bool) -> str:
    if from_me:
        return "Me"
    return chat_identifier or "Them"


async def _fetch_thread_messages(chat_identifier: str, limit: int) -> list[dict]:
    """Fetch last N messages for a thread, extracting attributedBody when
    the plain text column is empty (which is most modern messages).
    """
    safe = chat_identifier.replace("'", "''")
    query = (
        f"SELECT m.ROWID, m.text, hex(m.attributedBody), m.is_from_me, "
        f"datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime') "
        f"FROM message m "
        f"JOIN chat_message_join cmj ON m.ROWID = cmj.message_id "
        f"JOIN chat c ON cmj.chat_id = c.ROWID "
        f"WHERE c.chat_identifier = '{safe}' "
        f"ORDER BY m.ROWID DESC LIMIT {limit};"
    )
    ok, output = await tmux_relay_shell(
        _sqlite_via_relay_cmd(query),
        timeout=10.0,
    )
    if not ok:
        return []
    raw = output.rstrip(_RECORD_SEP).rstrip("\n")
    if not raw:
        return []

    results: list[dict] = []
    for record in raw.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record or _FIELD_SEP not in record:
            continue
        parts = record.split(_FIELD_SEP)
        if len(parts) < 4:
            continue
        try:
            int(parts[0])
        except (ValueError, TypeError):
            continue
        text = parts[1] or ""
        hex_body = parts[2] if len(parts) > 2 else ""
        if not text and hex_body:
            extracted = extract_text_from_attributed_body(hex_body)
            if extracted:
                text = extracted
        if not text:
            continue
        results.append({
            "text": text,
            "from_me": parts[3] == "1",
            "timestamp": parts[4] if len(parts) > 4 else "",
        })
    results.reverse()
    return results


async def maybe_enqueue_thread(chat_identifier: str) -> str:
    """Enqueue embed_and_index for this thread's last 10 msgs / 24h.

    Returns a status string for logging: "enqueued", "debounced",
    "short_code", "too_short", "bot_only", "no_messages", "error".
    """
    if not chat_identifier:
        return "no_messages"

    if _is_short_code(chat_identifier):
        return "short_code"

    now = time.monotonic()
    last = _last_emit.get(chat_identifier, 0.0)
    if now - last < DEBOUNCE_SECONDS:
        return "debounced"

    try:
        messages = await _fetch_thread_messages(chat_identifier, limit=THREAD_MSG_WINDOW)
    except Exception as e:
        logger.warning("vector: fetch_thread_messages failed for %s: %s", chat_identifier, e)
        return "error"

    if not messages:
        return "no_messages"

    # chat.db emits timestamps as local-time naive strings ("2026-04-16 19:17:35").
    # Compare against naive local-now.
    cutoff_local = datetime.now() - timedelta(hours=THREAD_HOURS_WINDOW)
    filtered: list[dict] = []
    for m in messages:
        ts = m.get("timestamp") or ""
        try:
            parsed = datetime.fromisoformat(ts)
            if parsed < cutoff_local:
                continue
        except Exception:
            pass
        filtered.append(m)

    if not filtered:
        return "no_messages"

    non_bot = [m for m in filtered if not _is_bot_attribution(m.get("text", ""))]
    if not non_bot:
        return "bot_only"

    transcript_lines = []
    for m in filtered:
        text = (m.get("text") or "").strip()
        if not text:
            continue
        who = _format_participant(chat_identifier, m.get("from_me", False))
        transcript_lines.append(f"{who}: {text}")
    transcript = "\n".join(transcript_lines)

    if len(transcript) < MIN_TRANSCRIPT_CHARS:
        return "too_short"

    last_msg_ts = filtered[-1].get("timestamp") or datetime.now(timezone.utc).isoformat()
    # Bucket by ISO year-week so each thread accumulates one row per week
    # instead of overwriting a single chat_identifier row on every enqueue.
    try:
        bucket_date = datetime.fromisoformat(last_msg_ts)
    except Exception:
        bucket_date = datetime.now()
    iso_year, iso_week, _ = bucket_date.isocalendar()
    source_id = f"{chat_identifier}::{iso_year}-W{iso_week:02d}"
    metadata = {
        "participants": [chat_identifier],
        "chat_identifier": chat_identifier,
        "iso_week": f"{iso_year}-W{iso_week:02d}",
        "last_msg_at": last_msg_ts,
        "msg_count": len(filtered),
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        q = _get_queue()
        q.enqueue(
            "tasks.vector.embed_and_index",
            "imessage_thread",
            source_id,
            transcript,
            metadata,
            job_timeout=120,
        )
    except Exception as e:
        logger.warning("vector: enqueue failed for %s: %s", chat_identifier, e)
        return "error"

    _last_emit[chat_identifier] = now
    return "enqueued"
