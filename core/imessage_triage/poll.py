"""iMessage triage producer — Mac LaunchAgent, 60s interval.

Reads chat.db via tmux_relay_shell (TCC-protected). Groups new inbound
messages by thread (chat_identifier), classifies each thread with Haiku 4.5,
and publishes action_me / scheduling threads to cockpit /inbox via cp-api.

Does NOT conflict with imessage-inbound (different purpose: inbound publishes
all threads as imessage_thread items; triage classifies them and publishes only
action-requiring items as imessage_triage items with ACTION/KILL verbs).

State: ~/.cache/imessage-triage/state.json
  {
    "last_rowid": int,          -- highest ROWID processed across all threads
    "thread_rowids": {          -- per-thread high-water-mark
      "<chat_identifier>": int
    }
  }
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.tools import tmux_relay_shell  # noqa: E402
from core.imessage_triage.classify import classify_thread  # noqa: E402
from core.imessage_triage.publish import publish_imessage_triage  # noqa: E402

AGENT = "imessage-triage"
CACHE_DIR = Path.home() / ".cache" / "imessage-triage"
STATE_FILE = CACHE_DIR / "state.json"
LOCK_FILE = CACHE_DIR / "lock.pid"
APPLE_EPOCH_OFFSET = 978307200  # seconds from Unix epoch to 2001-01-01

# Skip obviously irrelevant short-lived / one-off handles
SKIP_HANDLE_PATTERNS = (
    "chat",  # group chat identifiers that are UUID-based often start with "chat"
)

# NANPA 555-01xx numbers are reserved for fictional/test use (TV, movies, smoke tests).
# Never a real iMessage sender — skip before classification to avoid false doctor escalations.
_TEST_HANDLE_PREFIXES = ("+1555",)


def _is_test_handle(handle: str) -> bool:
    return any(handle.startswith(p) for p in _TEST_HANDLE_PREFIXES)

DRY_RUN = os.environ.get("IMESSAGE_TRIAGE_DRY_RUN", "").lower() in {"1", "true", "yes"}


def _load_state() -> dict:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"last_rowid": 0, "thread_rowids": {}}


def _save_state(state: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _acquire_lock() -> bool:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
            # Check if process is still alive
            os.kill(old_pid, 0)
            print(f"[{AGENT}] another instance running (pid={old_pid}), exiting", flush=True)
            return False
        except (ProcessLookupError, ValueError):
            pass  # stale lock
    LOCK_FILE.write_text(str(pid))
    return True


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _sqlite_relay_cmd(sql: str) -> str:
    """Wrap SQL for tmux_relay_shell. Single-quoted strings are bash-escaped."""
    safe = sql.replace("'", "'\\''")
    return f"sqlite3 -readonly -separator '|' ~/Library/Messages/chat.db '{safe}'"


async def _query_new_messages(last_rowid: int) -> list[dict]:
    """Return rows newer than last_rowid, all inbound non-system messages."""
    sql = (
        f"SELECT m.ROWID, m.text, m.is_from_me, c.chat_identifier, "
        f"m.handle_id, "
        f"datetime(m.date/1000000000 + {APPLE_EPOCH_OFFSET}, 'unixepoch') "
        f"FROM message m "
        f"JOIN chat_message_join cmj ON m.ROWID = cmj.message_id "
        f"JOIN chat c ON cmj.chat_id = c.ROWID "
        f"WHERE m.ROWID > {last_rowid} "
        f"AND (m.text IS NOT NULL AND length(trim(m.text)) > 0) "
        f"ORDER BY m.ROWID ASC "
        f"LIMIT 500;"
    )
    ok, output = await tmux_relay_shell(_sqlite_relay_cmd(sql), timeout=15.0)
    if not ok:
        print(f"[{AGENT}] relay failed: {output[:200]}", flush=True)
        return []
    if output.startswith("Error:") or "\nError:" in output:
        print(f"[{AGENT}] sqlite error: {output[:200]}", flush=True)
        return []
    rows = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 6:
            continue
        try:
            rowid = int(parts[0])
            text = parts[1]
            is_from_me = parts[2] == "1"
            chat_identifier = parts[3]
            ts_str = parts[5]
            rows.append({
                "rowid": rowid,
                "text": text,
                "is_from_me": is_from_me,
                "chat_identifier": chat_identifier,
                "ts_str": ts_str,
            })
        except (ValueError, IndexError):
            continue
    return rows


async def _get_thread_snippet(chat_identifier: str, limit: int = 15) -> list[str]:
    """Return last N messages of a thread for classification context."""
    # Quoting: _sqlite_relay_cmd handles shell-level single-quote escaping.
    # We only need to escape the SQL string literal itself here.
    sql_safe_id = chat_identifier.replace("'", "''")
    sql = (
        f"SELECT m.text, m.is_from_me "
        f"FROM message m "
        f"JOIN chat_message_join cmj ON m.ROWID = cmj.message_id "
        f"JOIN chat c ON cmj.chat_id = c.ROWID "
        f"WHERE c.chat_identifier = '{sql_safe_id}' "
        f"AND (m.text IS NOT NULL AND length(trim(m.text)) > 0) "
        f"ORDER BY m.ROWID DESC LIMIT {limit};"
    )
    ok, output = await tmux_relay_shell(_sqlite_relay_cmd(sql), timeout=10.0)
    if not ok or not output.strip():
        return []
    lines = []
    for line in output.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        text = parts[0].strip()
        speaker = "Me" if parts[1] == "1" else "Them"
        if text:
            lines.append(f"[{speaker}] {text[:400]}")
    lines.reverse()  # oldest first
    return lines


def _group_by_thread(rows: list[dict]) -> dict[str, list[dict]]:
    threads: dict[str, list[dict]] = {}
    for row in rows:
        cid = row["chat_identifier"]
        threads.setdefault(cid, []).append(row)
    return threads


async def _run() -> None:
    state = _load_state()
    last_rowid = int(state.get("last_rowid") or 0)
    thread_rowids: dict[str, int] = dict(state.get("thread_rowids") or {})

    rows = await _query_new_messages(last_rowid)
    if not rows:
        print(f"[{AGENT}] no new messages since rowid={last_rowid}", flush=True)
        return

    new_max_rowid = max(r["rowid"] for r in rows)
    threads = _group_by_thread(rows)

    published = 0
    classified = 0
    errors = 0

    for chat_id, thread_rows in threads.items():
        if any(chat_id.startswith(p) for p in SKIP_HANDLE_PATTERNS) or _is_test_handle(chat_id):
            print(f"[{AGENT}] skip test/placeholder handle {chat_id[:40]}", flush=True)
            continue

        thread_last = thread_rowids.get(chat_id, 0)
        new_rows = [r for r in thread_rows if r["rowid"] > thread_last]
        if not new_rows:
            continue

        # Use the newest inbound message as the anchor
        inbound = [r for r in new_rows if not r["is_from_me"]]
        if not inbound:
            # Only outbound new messages — still update rowid but skip classify
            thread_rowids[chat_id] = max(r["rowid"] for r in new_rows)
            continue

        anchor = inbound[-1]
        from_handle = chat_id  # chat_identifier is the best proxy for sender
        preview = anchor["text"][:300]
        received_at = anchor.get("ts_str")

        snippet = await _get_thread_snippet(chat_id)
        if not snippet:
            snippet = [f"[Them] {preview}"]

        result = classify_thread(from_handle, snippet)
        classified += 1
        category = result["category"]
        urgency = result["urgency"]

        print(
            f"[{AGENT}] thread={chat_id[:40]} category={category} urgency={urgency} "
            f"new_msgs={len(new_rows)}",
            flush=True,
        )

        ok = publish_imessage_triage(
            chat_db_msg_id=anchor["rowid"],
            category=category,
            urgency=urgency,
            from_handle=from_handle,
            preview=preview,
            received_at=received_at,
            dry_run=DRY_RUN,
        )
        if ok:
            published += 1
        elif category in {"action_me", "scheduling"}:
            errors += 1
            print(f"[{AGENT}] publish failed for {chat_id[:40]}", flush=True)

        thread_rowids[chat_id] = max(r["rowid"] for r in new_rows)

    _save_state({
        "last_rowid": new_max_rowid,
        "thread_rowids": thread_rowids,
    })
    print(
        f"[{AGENT}] threads={len(threads)} classified={classified} "
        f"published={published} errors={errors}",
        flush=True,
    )


def main() -> None:
    if not _acquire_lock():
        sys.exit(0)
    try:
        asyncio.run(_run())
    except Exception as exc:
        print(f"[{AGENT}] fatal: {exc}", flush=True)
        sys.exit(1)
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
