#!/usr/bin/env python3
"""Backfill ALL iMessage threads into the vector DB, bucketed by ISO week.

Reads chat.db via the tmux relay (inherits FDA from Terminal.app), groups
messages by (chat_identifier, iso_week), and enqueues one vector row per
bucket. Content-hash dedup means re-running is cheap.

Usage:
    python bin/backfill_imessage_vector.py            # all time
    python bin/backfill_imessage_vector.py --since 2025-01-01
    python bin/backfill_imessage_vector.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from redis import Redis
from rq import Queue

from core.message_reader import (
    _FIELD_SEP,
    _RECORD_SEP,
    extract_text_from_attributed_body,
)
from core.tools import tmux_relay_shell

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_imessage")

REDIS_HOST = "100.118.21.64"
REDIS_PORT = 6379
_SHORT_CODE_RE = re.compile(r"^\d{5,6}$")
_BOT_ATTRIBUTION_RE = re.compile(r"^[^A-Za-z0-9]?\[[A-Z][A-Za-z0-9 _-]{1,30}\]")
MIN_TRANSCRIPT_CHARS = 50
MAX_CHUNK_CHARS = 2000  # keep under vector.py CHUNK_SIZE to avoid mid-thread splits


def _queue() -> Queue:
    return Queue("vps", connection=Redis(host=REDIS_HOST, port=REDIS_PORT, db=0))


async def _sqlite_b64(query: str, timeout: float = 30.0) -> str:
    """Run sqlite against chat.db through tmux relay, returning raw stdout."""
    b64_query = base64.b64encode(query.encode()).decode()
    cmd = (
        f"echo {b64_query} | base64 -d | "
        f"sqlite3 -separator $'\\x1f' -newline $'\\x1e' "
        f"~/Library/Messages/chat.db"
    )
    ok, out = await tmux_relay_shell(cmd, timeout=timeout)
    if not ok:
        raise RuntimeError(f"relay failed: {out[:200]}")
    return out


async def list_threads(since: str | None) -> list[dict]:
    clause = ""
    if since:
        # since is YYYY-MM-DD — convert to apple absolute time ns
        dt = datetime.fromisoformat(since)
        apple_ns = int((dt.timestamp() - 978307200) * 1_000_000_000)
        clause = f"AND m.date >= {apple_ns}"
    query = (
        f"SELECT c.ROWID, c.chat_identifier, c.display_name, COUNT(m.ROWID) "
        f"FROM chat c "
        f"JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id "
        f"JOIN message m ON cmj.message_id = m.ROWID "
        f"WHERE 1=1 {clause} "
        f"GROUP BY c.ROWID "
        f"HAVING COUNT(m.ROWID) > 0;"
    )
    raw = await _sqlite_b64(query)
    threads = []
    for record in raw.rstrip("\x1e\n").split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1f")
        if len(parts) < 4:
            continue
        try:
            threads.append({
                "chat_id": int(parts[0]),
                "chat_identifier": parts[1] or "",
                "display_name": parts[2] or "",
                "msg_count": int(parts[3]),
            })
        except ValueError:
            continue
    return threads


async def fetch_all_messages(chat_id: int, since: str | None) -> list[dict]:
    clause = ""
    if since:
        dt = datetime.fromisoformat(since)
        apple_ns = int((dt.timestamp() - 978307200) * 1_000_000_000)
        clause = f"AND m.date >= {apple_ns}"
    query = (
        f"SELECT m.ROWID, m.text, hex(m.attributedBody), m.is_from_me, "
        f"datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime'), "
        f"h.id "
        f"FROM message m "
        f"JOIN chat_message_join cmj ON m.ROWID = cmj.message_id "
        f"LEFT JOIN handle h ON m.handle_id = h.ROWID "
        f"WHERE cmj.chat_id = {chat_id} {clause} "
        f"ORDER BY m.date ASC;"
    )
    raw = await _sqlite_b64(query, timeout=60.0)
    out = []
    for record in raw.rstrip("\x1e\n").split("\x1e"):
        record = record.strip("\n")
        if not record or "\x1f" not in record:
            continue
        parts = record.split("\x1f")
        if len(parts) < 5:
            continue
        try:
            int(parts[0])
        except ValueError:
            continue
        text = parts[1] or ""
        hex_body = parts[2] or ""
        if not text and hex_body:
            extracted = extract_text_from_attributed_body(hex_body)
            if extracted:
                text = extracted
        if not text.strip():
            continue
        out.append({
            "text": text.strip(),
            "from_me": parts[3] == "1",
            "timestamp": parts[4] or "",
            "sender": parts[5] if len(parts) > 5 else "",
        })
    return out


def _is_short_code(ident: str) -> bool:
    return bool(_SHORT_CODE_RE.match((ident or "").lstrip("+")))


def _is_bot(text: str) -> bool:
    return bool(_BOT_ATTRIBUTION_RE.match((text or "").lstrip()))


def bucket_by_week(messages: list[dict]) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for m in messages:
        try:
            dt = datetime.fromisoformat(m["timestamp"])
        except Exception:
            continue
        y, w, _ = dt.isocalendar()
        buckets[f"{y}-W{w:02d}"].append(m)
    return buckets


def render_transcript(thread_name: str, week: str, messages: list[dict]) -> list[str]:
    """Return one or more text chunks (≤ MAX_CHUNK_CHARS each)."""
    header = f"Thread: {thread_name} | Week of {week}\n"
    lines = [header]
    for m in messages:
        who = "Me" if m["from_me"] else (m.get("sender") or "Them")
        ts = m["timestamp"]
        text = m["text"].replace("\n", " ")
        lines.append(f"[{ts}] {who}: {text}")

    chunks: list[str] = []
    buf = header
    for line in lines[1:]:
        candidate = buf + line + "\n"
        if len(candidate) > MAX_CHUNK_CHARS and len(buf) > len(header):
            chunks.append(buf.rstrip())
            buf = header + line + "\n"
        else:
            buf = candidate
    if buf.strip() and buf != header:
        chunks.append(buf.rstrip())
    return chunks


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--since", help="YYYY-MM-DD", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--only", help="chat_identifier substring filter", default=None)
    args = p.parse_args()

    threads = await list_threads(args.since)
    logger.info("found %d threads (since=%s)", len(threads), args.since or "all time")

    q = None if args.dry_run else _queue()

    total_enqueued = 0
    total_skipped = 0
    per_thread = []

    for t in threads:
        ident = t["chat_identifier"]
        if _is_short_code(ident):
            total_skipped += 1
            continue
        if args.only and args.only not in ident and args.only not in (t["display_name"] or ""):
            continue

        thread_name = t["display_name"] or ident
        messages = await fetch_all_messages(t["chat_id"], args.since)
        if not messages:
            continue

        # Drop bot-only buckets later per-week
        buckets = bucket_by_week(messages)

        enqueued_here = 0
        for week, week_msgs in sorted(buckets.items()):
            non_bot = [m for m in week_msgs if not _is_bot(m["text"])]
            if not non_bot:
                continue
            chunks = render_transcript(thread_name, week, week_msgs)
            for i, chunk in enumerate(chunks):
                if len(chunk) < MIN_TRANSCRIPT_CHARS:
                    continue
                part_suffix = f"_part{i+1}" if len(chunks) > 1 else ""
                source_id = f"{ident}::{week}{part_suffix}"
                metadata = {
                    "participants": [ident],
                    "chat_identifier": ident,
                    "thread_name": thread_name,
                    "iso_week": week,
                    "msg_count": len(week_msgs),
                    "part": i + 1,
                    "part_count": len(chunks),
                    "synced_at": datetime.now(timezone.utc).isoformat(),
                    "backfill": True,
                }
                if args.dry_run:
                    logger.info("DRY source=%s chars=%d", source_id, len(chunk))
                else:
                    q.enqueue(
                        "tasks.vector.embed_and_index",
                        "imessage_thread",
                        source_id,
                        chunk,
                        metadata,
                        job_timeout=120,
                    )
                enqueued_here += 1
                total_enqueued += 1

        per_thread.append({
            "thread": thread_name,
            "ident": ident,
            "msgs": len(messages),
            "week_chunks": enqueued_here,
        })

    logger.info("=== SUMMARY ===")
    logger.info("threads processed: %d", len(per_thread))
    logger.info("threads skipped (shortcode): %d", total_skipped)
    logger.info("chunks %s: %d", "planned" if args.dry_run else "enqueued", total_enqueued)
    for p in sorted(per_thread, key=lambda x: -x["week_chunks"])[:20]:
        logger.info("  %-30s %4d msgs → %3d week-chunks", p["thread"][:30], p["msgs"], p["week_chunks"])


if __name__ == "__main__":
    asyncio.run(main())
