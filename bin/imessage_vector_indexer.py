#!/usr/bin/env python3
"""Scheduled iMessage vector indexer.

Runs as a LaunchAgent every 30 minutes. Reads Messages' chat.db through the
tmux FDA relay, groups transcripts by thread/week, and enqueues idempotent
pgvector upserts on the VPS RQ worker.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from redis import Redis
from rq import Queue

from core.message_reader import (
    _FIELD_SEP,
    _RECORD_SEP,
    extract_text_from_attributed_body,
)
from core.tools import tmux_relay_shell
from core.vector import PG_DSN as VECTOR_DEFAULT_PG_DSN

LOGGER = logging.getLogger("imessage_vector_indexer")

REDIS_HOST = os.environ.get("RQ_REDIS_HOST", "100.118.21.64")
REDIS_PORT = int(os.environ.get("RQ_REDIS_PORT", "6379"))
REDIS_DB = int(os.environ.get("RQ_REDIS_DB", "0"))
QUEUE_NAME = os.environ.get("RQ_VECTOR_QUEUE", "vps")

STATE_PATH = Path.home() / "logs" / "imessage-vector-indexer-state.json"
CHAT_DB = "~/Library/Messages/chat.db"
TESTED_MACOS_PRODUCT_VERSION = "26.4.1"
DEFAULT_LOOKBACK_DAYS = 30
OVERLAP_DAYS = 2
MIN_TRANSCRIPT_CHARS = 50
MAX_TRANSCRIPT_CHARS = 80_000

SHORT_CODE_RE = re.compile(r"^\+?\d{5,6}(?:\D.*)?$")
BOT_ATTRIBUTION_RE = re.compile(r"^[^A-Za-z0-9]?\[[A-Z][A-Za-z0-9 _-]{1,30}\]")
EXCLUDED_THREAD_RE = re.compile(r"\b(?:SENTINEL|OVERWATCH|Jannson|Mercury)\b", re.I)
APPLE_EPOCH_OFFSET = 978_307_200

EXPECTED_MESSAGE_COLUMNS = {
    "date",
    "text",
    "attributedBody",
    "is_from_me",
    "guid",
    "handle_id",
}

SKIPPED_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS imessage_indexer_skipped (
    id BIGSERIAL PRIMARY KEY,
    skipped_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id TEXT NOT NULL,
    chat_id BIGINT,
    chat_identifier TEXT,
    message_rowid TEXT,
    message_guid TEXT,
    reason TEXT NOT NULL,
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT
);
"""


@dataclass(frozen=True)
class ChatMessage:
    chat_id: int
    chat_identifier: str
    thread_name: str
    rowid: int
    guid: str
    text: str
    from_me: bool
    timestamp: str
    sender: str


def _configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _pg_dsn() -> str:
    return os.environ.get("VEC_PG_DSN") or os.environ.get("PG_DSN") or VECTOR_DEFAULT_PG_DSN


def _pg_connect():
    import psycopg2

    delays = (0, 5, 30, 120)
    last_error: Exception | None = None
    for attempt, delay in enumerate(delays, start=1):
        if delay:
            time.sleep(delay)
        try:
            return psycopg2.connect(_pg_dsn())
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            LOGGER.warning("postgres connect failed attempt=%d/%d: %s", attempt, len(delays), exc)
    raise RuntimeError(f"postgres connection failed after retries: {last_error}")


def _ensure_skipped_table() -> None:
    with _pg_connect() as conn, conn.cursor() as cur:
        cur.execute(SKIPPED_TABLE_SQL)
        conn.commit()


def _record_skipped(
    run_id: str,
    reason: str,
    raw: dict[str, Any],
    error: str | None = None,
    chat_id: int | None = None,
    chat_identifier: str | None = None,
    message_rowid: str | None = None,
    message_guid: str | None = None,
) -> None:
    try:
        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO imessage_indexer_skipped
                  (run_id, chat_id, chat_identifier, message_rowid, message_guid, reason, raw, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    run_id,
                    chat_id,
                    chat_identifier,
                    message_rowid,
                    message_guid,
                    reason,
                    json.dumps(raw, ensure_ascii=False),
                    (error or "")[:1000] if error else None,
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("failed to record skipped message reason=%s: %s", reason, exc)


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(STATE_PATH)


def _parse_pg_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _last_success_from_db() -> datetime | None:
    with _pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              MAX((metadata->>'synced_at')::timestamptz)
                FILTER (WHERE metadata->>'syncer' = 'imessage_vector_indexer') AS indexer_synced_at,
              MAX((metadata->>'synced_at')::timestamptz)
                FILTER (WHERE metadata ? 'backfill') AS legacy_backfill_synced_at
            FROM vec_embeddings
            WHERE source_type = 'imessage_thread'
              AND metadata ? 'synced_at'
            """
        )
        row = cur.fetchone()
    if not row:
        return None
    return row[0] or row[1]


def _determine_since(explicit_since: str | None, lookback_days: int) -> datetime:
    if explicit_since:
        dt = datetime.fromisoformat(explicit_since)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    state_ts = _parse_pg_ts(str(_load_state().get("last_success_at") or ""))
    db_ts = _last_success_from_db()
    anchor = state_ts or db_ts
    if anchor is None:
        anchor = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return anchor.astimezone(timezone.utc) - timedelta(days=OVERLAP_DAYS)


def _week_start(dt: datetime) -> datetime:
    local_date = dt.date()
    monday = local_date - timedelta(days=local_date.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)


def _apple_ns(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((dt.timestamp() - APPLE_EPOCH_OFFSET) * 1_000_000_000)


def _is_short_code(chat_identifier: str) -> bool:
    return bool(SHORT_CODE_RE.match(chat_identifier or ""))


def _is_bot(text: str) -> bool:
    return bool(BOT_ATTRIBUTION_RE.match((text or "").lstrip()))


def _excluded_thread(chat_identifier: str, thread_name: str) -> bool:
    target = f"{chat_identifier} {thread_name}"
    return bool(EXCLUDED_THREAD_RE.search(target))


def _sw_vers() -> dict[str, str]:
    try:
        out = subprocess.check_output(["sw_vers"], text=True, timeout=5)
    except Exception:
        return {}
    result: dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip()
    return result


async def _sqlite(sql: str, timeout: float = 60.0) -> str:
    encoded = base64.b64encode(sql.encode("utf-8")).decode("ascii")
    cmd = (
        f"echo {encoded} | base64 -d | "
        f"sqlite3 -separator $'\\x1f' -newline $'\\x1e' {CHAT_DB}"
    )
    ok, output = await tmux_relay_shell(cmd, timeout=timeout)
    if not ok:
        raise RuntimeError(f"tmux relay failed: {output[:500]}")
    if output.startswith("Error:") or "\nError:" in output:
        raise RuntimeError(f"sqlite error: {output[:500]}")
    return output


async def _message_columns() -> set[str]:
    raw = await _sqlite("PRAGMA table_info(message);", timeout=20.0)
    cols: set[str] = set()
    for record in raw.rstrip(_RECORD_SEP).rstrip("\n").split(_RECORD_SEP):
        parts = record.strip("\n").split(_FIELD_SEP)
        if len(parts) >= 2 and parts[1]:
            cols.add(parts[1])
    missing = sorted(EXPECTED_MESSAGE_COLUMNS - cols)
    extra = sorted(cols - EXPECTED_MESSAGE_COLUMNS)
    if missing:
        LOGGER.warning(
            "message schema differs from tested macOS %s missing=%s extra_count=%d",
            TESTED_MACOS_PRODUCT_VERSION,
            missing,
            len(extra),
        )
    return cols


def _select_expr(cols: set[str], name: str, fallback: str = "NULL") -> str:
    return f"m.{name}" if name in cols else fallback


async def _fetch_raw_messages(since: datetime) -> tuple[str, set[str]]:
    cols = await _message_columns()
    date_expr = _select_expr(cols, "date", "0")
    handle_select = "h.id" if "handle_id" in cols else "NULL"
    handle_join = "LEFT JOIN handle h ON m.handle_id = h.ROWID " if "handle_id" in cols else ""
    where = f"WHERE m.date >= {_apple_ns(since)} " if "date" in cols else "WHERE 1=1 "
    query = (
        "SELECT c.ROWID, c.chat_identifier, c.display_name, "
        "m.ROWID, "
        f"{_select_expr(cols, 'guid')}, "
        f"{_select_expr(cols, 'text')}, "
        f"hex({_select_expr(cols, 'attributedBody')}) AS attributed_hex, "
        f"{_select_expr(cols, 'is_from_me', '0')}, "
        f"datetime({date_expr}/1000000000 + 978307200, 'unixepoch', 'localtime'), "
        f"{handle_select} "
        "FROM message m "
        "JOIN chat_message_join cmj ON m.ROWID = cmj.message_id "
        "JOIN chat c ON cmj.chat_id = c.ROWID "
        f"{handle_join}"
        f"{where}"
        f"ORDER BY c.ROWID ASC, {date_expr} ASC, m.ROWID ASC;"
    )
    return await _sqlite(query, timeout=120.0), cols


def _parse_record(
    record: str,
    run_id: str,
    skip_writer: Callable[..., None],
) -> ChatMessage | None:
    parts = record.strip("\n").split(_FIELD_SEP)
    if len(parts) < 9:
        skip_writer(run_id, "malformed_record", {"parts": parts[:12]}, error=f"field_count={len(parts)}")
        return None

    chat_identifier = parts[1] or ""
    thread_name = parts[2] or chat_identifier
    message_rowid = parts[3] if len(parts) > 3 else None
    message_guid = parts[4] if len(parts) > 4 else None

    try:
        chat_id = int(parts[0])
        rowid = int(parts[3])
    except (TypeError, ValueError) as exc:
        skip_writer(
            run_id,
            "bad_integer",
            {"parts": parts[:12]},
            error=str(exc),
            chat_identifier=chat_identifier,
            message_rowid=message_rowid,
            message_guid=message_guid,
        )
        return None

    text = parts[5] or ""
    hex_body = parts[6] or ""
    if not text and hex_body:
        try:
            text = extract_text_from_attributed_body(hex_body) or ""
        except Exception as exc:  # noqa: BLE001
            skip_writer(
                run_id,
                "bad_attributed_body",
                {"chat_identifier": chat_identifier, "rowid": rowid, "guid": message_guid},
                error=str(exc),
                chat_id=chat_id,
                chat_identifier=chat_identifier,
                message_rowid=str(rowid),
                message_guid=message_guid,
            )
            return None

    timestamp = parts[8] or ""
    if timestamp:
        try:
            datetime.fromisoformat(timestamp)
        except ValueError as exc:
            skip_writer(
                run_id,
                "bad_timestamp",
                {"chat_identifier": chat_identifier, "rowid": rowid, "guid": message_guid, "timestamp": timestamp},
                error=str(exc),
                chat_id=chat_id,
                chat_identifier=chat_identifier,
                message_rowid=str(rowid),
                message_guid=message_guid,
            )
            return None

    if not text.strip():
        return None

    return ChatMessage(
        chat_id=chat_id,
        chat_identifier=chat_identifier,
        thread_name=thread_name,
        rowid=rowid,
        guid=message_guid or "",
        text=text.strip(),
        from_me=parts[7] == "1",
        timestamp=timestamp,
        sender=parts[9] if len(parts) > 9 else "",
    )


def parse_messages(
    raw: str,
    run_id: str,
    skip_writer: Callable[..., None] = _record_skipped,
    inject_bad_row: bool = False,
) -> list[ChatMessage]:
    records = [
        r for r in raw.rstrip(_RECORD_SEP).rstrip("\n").split(_RECORD_SEP)
        if r.strip("\n")
    ]
    if inject_bad_row:
        records.append("bad\x1frecord")
    messages: list[ChatMessage] = []
    for record in records:
        try:
            msg = _parse_record(record, run_id, skip_writer)
        except Exception as exc:  # noqa: BLE001
            skip_writer(run_id, "parse_exception", {"record": record[:500]}, error=str(exc))
            continue
        if msg is not None:
            messages.append(msg)
    return messages


def _bucket_messages(messages: Iterable[ChatMessage]) -> dict[tuple[str, str], list[ChatMessage]]:
    buckets: dict[tuple[str, str], list[ChatMessage]] = defaultdict(list)
    for msg in messages:
        if _is_short_code(msg.chat_identifier):
            continue
        if _excluded_thread(msg.chat_identifier, msg.thread_name):
            continue
        try:
            dt = datetime.fromisoformat(msg.timestamp)
        except ValueError:
            continue
        iso_year, iso_week, _ = dt.isocalendar()
        buckets[(msg.chat_identifier, f"{iso_year}-W{iso_week:02d}")].append(msg)
    return buckets


def _render_transcript(messages: list[ChatMessage]) -> str:
    if not messages:
        return ""
    thread_name = messages[0].thread_name or messages[0].chat_identifier
    iso_year, iso_week, _ = datetime.fromisoformat(messages[0].timestamp).isocalendar()
    header = f"Thread: {thread_name} | Week: {iso_year}-W{iso_week:02d}"
    lines = [header]
    for msg in messages:
        who = "Me" if msg.from_me else (msg.sender or msg.chat_identifier or "Them")
        text = msg.text.replace("\n", " ").strip()
        lines.append(f"[{msg.timestamp}] {who}: {text}")
    rendered = "\n".join(lines)
    if len(rendered) > MAX_TRANSCRIPT_CHARS:
        rendered = rendered[-MAX_TRANSCRIPT_CHARS:]
        rendered = header + "\n[trimmed to latest messages]\n" + rendered
    return rendered


def _queue() -> Queue:
    return Queue(
        QUEUE_NAME,
        connection=Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB),
    )


def _enqueue_bucket(q: Queue, source_id: str, content: str, metadata: dict[str, Any]) -> str:
    job = q.enqueue(
        "tasks.vector.embed_and_index",
        "imessage_thread",
        source_id,
        content,
        metadata,
        job_timeout=180,
    )
    return job.id


async def run_indexer(args: argparse.Namespace) -> dict[str, Any]:
    run_started = datetime.now(timezone.utc)
    run_id = run_started.strftime("%Y%m%dT%H%M%SZ")
    _ensure_skipped_table()

    since_anchor = _determine_since(args.since, args.lookback_days)
    since = _week_start(since_anchor)
    sw = _sw_vers()
    LOGGER.info(
        "run_id=%s since=%s tested_macos=%s actual_macos=%s",
        run_id,
        since.isoformat(),
        TESTED_MACOS_PRODUCT_VERSION,
        sw.get("ProductVersion", "unknown"),
    )

    raw, cols = await _fetch_raw_messages(since)
    messages = parse_messages(raw, run_id, inject_bad_row=args.inject_bad_row)
    buckets = _bucket_messages(messages)
    q = None if args.dry_run else _queue()

    enqueued = 0
    too_short = 0
    job_ids: list[str] = []
    sync_time = datetime.now(timezone.utc).isoformat()

    for (chat_identifier, iso_week), week_messages in sorted(buckets.items()):
        thread_name = week_messages[0].thread_name or chat_identifier
        if not any(not _is_bot(m.text) for m in week_messages):
            continue
        content = _render_transcript(week_messages)
        if len(content) < MIN_TRANSCRIPT_CHARS:
            too_short += 1
            continue
        source_id = f"{chat_identifier}::{iso_week}"
        metadata = {
            "participants": [chat_identifier],
            "chat_identifier": chat_identifier,
            "thread_name": thread_name,
            "iso_week": iso_week,
            "msg_count": len(week_messages),
            "message_rowids": [m.rowid for m in week_messages],
            "message_guids": [m.guid for m in week_messages if m.guid],
            "first_msg_at": week_messages[0].timestamp,
            "last_msg_at": week_messages[-1].timestamp,
            "synced_at": sync_time,
            "syncer": "imessage_vector_indexer",
            "macos_product_version": sw.get("ProductVersion"),
            "macos_build_version": sw.get("BuildVersion"),
            "query_tested_macos_product_version": TESTED_MACOS_PRODUCT_VERSION,
            "schema_missing_columns": sorted(EXPECTED_MESSAGE_COLUMNS - cols),
            "refresh_metadata": True,
        }
        if args.dry_run:
            LOGGER.info("DRY source_id=%s chars=%d messages=%d", source_id, len(content), len(week_messages))
        else:
            assert q is not None
            job_ids.append(_enqueue_bucket(q, source_id, content, metadata))
        enqueued += 1

    result = {
        "run_id": run_id,
        "since": since.isoformat(),
        "messages": len(messages),
        "buckets": len(buckets),
        "enqueued": enqueued,
        "too_short": too_short,
        "dry_run": args.dry_run,
        "job_ids": job_ids[:20],
        "job_count": len(job_ids),
    }
    if not args.dry_run:
        _save_state({
            "last_success_at": datetime.now(timezone.utc).isoformat(),
            "last_run_id": run_id,
            "last_result": result,
        })
    LOGGER.info("summary %s", json.dumps(result, sort_keys=True))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Index iMessage threads into vector memory")
    parser.add_argument("--since", help="YYYY-MM-DD or ISO datetime; overrides catch-up state")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--inject-bad-row", action="store_true", help="Append one malformed synthetic row to prove skip handling")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    try:
        asyncio.run(run_indexer(args))
        return 0
    except Exception:
        LOGGER.exception("imessage vector indexer failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
