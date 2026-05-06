#!/usr/bin/env python3
"""imessage-inbound producer — 5 min LaunchAgent.

Reads chat.db via tmux_relay_shell (TCC-protected), groups by thread,
publishes imessage_thread stack items to cp-api via agent_cp_client.

Rules:
- Read-only: sqlite3 -readonly + PRAGMA query_only=1
- Only inbound messages (is_from_me=0)
- Last 24h only (Apple epoch: mac_ts/1e9 + 978307200 = unix_ts)
- Exclude John's own handles
- Dedup by chat_identifier + last seen ROWID
- PID-lock guard
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

import agent_cp_client as cp
from core.tools import tmux_relay_shell

AGENT = "imessage-inbound"
CACHE_DIR = Path.home() / ".cache" / "imessage-inbound"
SEEN_FILE = CACHE_DIR / "seen.json"
LOCK_FILE = CACHE_DIR / "lock.pid"

APPLE_EPOCH_OFFSET = 978307200  # seconds from unix epoch to 2001-01-01
HOURS_WINDOW = 24

# John's own handles — exclude these from inbound
JOHNS_HANDLES = {
    "corn82@icloud.com",
    "corn82@gmail.com",
    "corn82@outlook.com",
    "+13042684985",
    "3042684985",
}

# SQL query — read chat.db with readonly flags
CHAT_QUERY = """
PRAGMA query_only=1;
SELECT
    m.ROWID,
    m.text,
    m.date AS mac_ts,
    m.is_from_me,
    h.id AS sender_handle,
    c.chat_identifier
FROM message m
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
JOIN chat c ON c.ROWID = cmj.chat_id
LEFT JOIN handle h ON h.ROWID = m.handle_id
WHERE m.is_from_me = 0
  AND m.text IS NOT NULL
  AND m.text != ''
  AND (m.date / 1000000000 + {offset}) >= strftime('%s','now','-{hours} hours')
ORDER BY c.chat_identifier, m.date ASC;
""".strip()


def _acquire_lock() -> bool:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)
            print(f"[imessage-inbound] lock held by pid {pid}, exiting", file=sys.stderr)
            return False
        except (ProcessLookupError, OSError):
            LOCK_FILE.unlink(missing_ok=True)
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def _release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def _load_seen() -> dict[str, int]:
    try:
        return json.loads(SEEN_FILE.read_text())
    except Exception:
        return {}


def _save_seen(seen: dict[str, int]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=2))


def _parse_chat_rows(output: str) -> list[dict]:
    rows = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("PRAGMA"):
            continue
        parts = line.split("|")
        if len(parts) < 6:
            continue
        try:
            row_id = int(parts[0].strip())
            text = parts[1].strip()
            mac_ts = int(parts[2].strip()) if parts[2].strip() else 0
            is_from_me = int(parts[3].strip()) if parts[3].strip() else 0
            sender_handle = parts[4].strip()
            chat_identifier = parts[5].strip()

            if is_from_me:
                continue
            if sender_handle.lower() in {h.lower() for h in JOHNS_HANDLES}:
                continue

            unix_ts = mac_ts / 1_000_000_000 + APPLE_EPOCH_OFFSET
            rows.append({
                "rowid": row_id,
                "text": text,
                "unix_ts": unix_ts,
                "sender_handle": sender_handle,
                "chat_identifier": chat_identifier,
            })
        except (ValueError, IndexError):
            continue
    return rows


def _group_by_thread(rows: list[dict]) -> dict[str, list[dict]]:
    threads: dict[str, list[dict]] = {}
    for row in rows:
        cid = row["chat_identifier"]
        threads.setdefault(cid, []).append(row)
    return threads


async def _run() -> None:
    seen = _load_seen()

    query = CHAT_QUERY.format(offset=APPLE_EPOCH_OFFSET, hours=HOURS_WINDOW)
    cmd = f"sqlite3 -readonly -separator '|' ~/Library/Messages/chat.db \"{query}\""

    ok, output = await tmux_relay_shell(cmd, timeout=20.0)
    if not ok:
        if "lock" in output.lower() or "busy" in output.lower():
            print("[imessage-inbound] chat.db lock contention, backoff 5s", file=sys.stderr)
            await asyncio.sleep(5)
            ok, output = await tmux_relay_shell(cmd, timeout=20.0)
        if not ok:
            print(f"[imessage-inbound] relay failed: {output[:200]}", file=sys.stderr)
            return

    rows = _parse_chat_rows(output)
    if not rows:
        print("[imessage-inbound] no new inbound messages in window", flush=True)
        return

    threads = _group_by_thread(rows)
    published = 0
    new_seen = dict(seen)

    for chat_id, messages in threads.items():
        # Filter to only messages newer than last seen ROWID
        last_rowid = seen.get(chat_id, 0)
        new_msgs = [m for m in messages if m["rowid"] > last_rowid]
        if not new_msgs:
            continue

        last_msg = new_msgs[-1]
        max_rowid = max(m["rowid"] for m in new_msgs)

        payload = {
            "title": f"iMessage from {last_msg['sender_handle']}",
            "body": last_msg["text"][:500],
            "kind": "imessage_thread",
            "verbs": ["REPLY", "ARCHIVE", "KILL", "SNOOZE"],
            "priority": 1,
            "dedup_key": f"imessage-inbound:{chat_id}",
            "sources": ["imessage"],
            "metadata": {
                "thread_id": chat_id,
                "sender_handle": last_msg["sender_handle"],
                "message_count_24h": len(messages),
                "new_message_count": len(new_msgs),
                "last_rowid": max_rowid,
                "last_ts": datetime.fromtimestamp(last_msg["unix_ts"], tz=timezone.utc).isoformat(),
            },
        }

        result = cp.event(AGENT, "imessage_thread", payload=payload)
        if result is not None:
            new_seen[chat_id] = max_rowid
            published += 1
        else:
            print(f"[imessage-inbound] cp.event failed for thread {chat_id}", file=sys.stderr)

    _save_seen(new_seen)
    print(f"[imessage-inbound] {len(threads)} threads, {published} new events", flush=True)


def main() -> None:
    if not _acquire_lock():
        sys.exit(0)
    try:
        asyncio.run(_run())
    except Exception as exc:
        print(f"[imessage-inbound] fatal: {exc}", file=sys.stderr)
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
