#!/usr/bin/env python3
"""notes-triage producer — 15 min LaunchAgent.

Calls apple-bridge /notes/inventory, applies hygiene heuristics, publishes
note_triage stack items to cp-api via agent_cp_client.

PID-file guard ensures only one instance runs at a time.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

import httpx
import agent_cp_client as cp

AGENT = "notes-triage"
BRIDGE_BASE = "http://100.122.35.56:8765"
CACHE_DIR = Path.home() / ".cache" / "notes-triage"
STATE_FILE = CACHE_DIR / "state.json"
LOCK_FILE = CACHE_DIR / "lock.pid"
STALE_INBOX_DAYS = 14
TINY_CHAR_COUNT = 50


def _load_token() -> str:
    token = os.environ.get("BRIDGE_READ_TOKEN", "")
    if token:
        return token
    secrets = Path.home() / ".config" / "secrets.env"
    if secrets.exists():
        for line in secrets.read_text().splitlines():
            if line.startswith("BRIDGE_READ_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def _acquire_lock() -> bool:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            # Check if PID is still alive
            os.kill(pid, 0)
            print(f"[notes-triage] lock held by pid {pid}, exiting", file=sys.stderr)
            return False
        except (ProcessLookupError, OSError):
            LOCK_FILE.unlink(missing_ok=True)
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def _release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _note_hash(note: dict) -> str:
    raw = f"{note.get('id', '')}:{note.get('modified_at', '')}:{note.get('char_count', '')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _fetch_inventory(token: str) -> list[dict]:
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(
            f"{BRIDGE_BASE}/notes/inventory",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("notes", [])


def _apply_heuristics(notes: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    flagged: list[dict] = []

    # Build dedup-hash → [notes] for duplicate detection
    hash_groups: dict[str, list[dict]] = {}
    for note in notes:
        dh = note.get("dedup_hash", "")
        if dh:
            hash_groups.setdefault(dh, []).append(note)

    for note in notes:
        reasons: list[str] = []
        folder = note.get("folder", "")
        modified_raw = note.get("modified_at", "")
        char_count = note.get("char_count", 999)

        # Stale inbox
        if folder.lower() in ("inbox", "notes") and modified_raw:
            try:
                mod = datetime.fromisoformat(modified_raw.replace("Z", "+00:00"))
                if (now - mod).days >= STALE_INBOX_DAYS:
                    reasons.append(f"stale-inbox ({(now - mod).days}d)")
            except ValueError:
                pass

        # Tiny note
        if isinstance(char_count, int) and char_count < TINY_CHAR_COUNT:
            reasons.append(f"tiny ({char_count} chars)")

        # Duplicate pair
        dh = note.get("dedup_hash", "")
        if dh and len(hash_groups.get(dh, [])) > 1:
            others = [n.get("title", "") for n in hash_groups[dh] if n is not note]
            reasons.append(f"dup-pair with: {', '.join(others[:2])}")

        if reasons:
            note = dict(note)
            note["_reasons"] = reasons
            flagged.append(note)

    return flagged


def _publish(flagged: list[dict], state: dict, token: str) -> dict:
    updated_state = dict(state)
    published = 0

    for note in flagged:
        note_id = str(note.get("id", note.get("title", "")))
        nh = _note_hash(note)
        if updated_state.get(note_id) == nh:
            continue  # unchanged

        reasons = note.get("_reasons", [])
        title = note.get("title") or "Untitled note"
        body_parts = [f"[{', '.join(reasons)}]"]
        body_parts.append(f"Folder: {note.get('folder', 'unknown')}")
        if note.get("char_count"):
            body_parts.append(f"Size: {note['char_count']} chars")

        payload = {
            "title": f"Note: {title}",
            "body": " | ".join(body_parts),
            "kind": "note_triage",
            "verbs": ["KEEP", "MERGE", "FILE", "KILL", "PROMOTE_REMINDER"],
            "priority": 1,
            "dedup_key": f"notes-triage:{note_id}",
            "sources": ["apple-notes"],
            "metadata": {
                "note_id": note_id,
                "folder": note.get("folder", ""),
                "modified_at": note.get("modified_at", ""),
                "char_count": note.get("char_count", 0),
                "reasons": reasons,
            },
        }

        result = cp.event(AGENT, "note_triage", payload=payload)
        if result is not None:
            updated_state[note_id] = nh
            published += 1
        else:
            print(f"[notes-triage] cp.event failed for note {note_id}", file=sys.stderr)

    return updated_state


def main() -> None:
    if not _acquire_lock():
        sys.exit(0)
    try:
        token = _load_token()
        if not token:
            print("[notes-triage] BRIDGE_READ_TOKEN not found", file=sys.stderr)
            sys.exit(1)

        state = _load_state()

        try:
            notes = _fetch_inventory(token)
        except Exception as exc:
            print(f"[notes-triage] fetch failed: {exc}", file=sys.stderr)
            sys.exit(1)

        flagged = _apply_heuristics(notes)
        print(f"[notes-triage] {len(notes)} notes, {len(flagged)} flagged", flush=True)

        new_state = _publish(flagged, state, token)
        _save_state(new_state)

        published = sum(1 for k, v in new_state.items() if state.get(k) != v)
        print(f"[notes-triage] published {published} new events", flush=True)
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
