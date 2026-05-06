"""Apple Reminders service backed by EventKit.

This is the shared reader for briefs, nudges, and work-context export. It
regenerates from the live Reminders store each call, so completed/deleted/edited
items naturally disappear or update on the next refresh.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


SECTION_DIVIDER_PREFIXES = "📋🔖📌🔹▪•🔮⭐✨"


def _parse_due(due_iso: str) -> datetime | None:
    if not due_iso:
        return None
    try:
        return (
            datetime.fromisoformat(due_iso.replace("Z", "+00:00"))
            .astimezone()
            .replace(tzinfo=None)
        )
    except ValueError:
        return None


def _as_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_section_divider(name: str, due_iso: str) -> bool:
    """Skip pure section headers, but never drop Work meeting markers.

    John's Work list uses 📅/🗓 as meeting prefixes, so those are intentionally
    not in SECTION_DIVIDER_PREFIXES.
    """
    return bool(name and name[0] in SECTION_DIVIDER_PREFIXES and not due_iso and len(name) < 30)


def parse_reminders_output(
    raw: str,
    *,
    limit_later: int | None = 10,
    limit_undated: int | None = 15,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Parse reminders_fetch.swift output into urgency buckets."""
    current = now or datetime.now()
    today_date = current.date()
    week_end = today_date + timedelta(days=7)
    buckets: dict[str, list[dict[str, Any]]] = {
        "overdue": [],
        "today": [],
        "this_week": [],
        "later": [],
        "undated": [],
    }
    total = 0

    for rec in raw.split("\x1e"):
        rec = rec.strip(" \t\n\r")
        if not rec:
            continue
        parts = rec.split("\x1f")
        if len(parts) < 2:
            continue

        name = parts[0].strip()
        list_name = parts[1].strip() if len(parts) > 1 else ""
        due_iso = parts[2].strip() if len(parts) > 2 else ""
        reminder_id = parts[3].strip() if len(parts) > 3 else ""
        notes = parts[4].strip() if len(parts) > 4 else ""
        priority = _as_int(parts[5].strip()) if len(parts) > 5 else None

        if not name or _is_section_divider(name, due_iso):
            continue

        total += 1
        due_dt = _parse_due(due_iso)
        entry: dict[str, Any] = {
            "name": name,
            "list": list_name,
            "due": due_iso or None,
        }
        if reminder_id:
            entry["id"] = reminder_id
        if notes:
            entry["notes"] = notes[:500]
        if priority is not None and priority != 0:
            entry["priority"] = priority

        if due_dt is None:
            buckets["undated"].append(entry)
        elif due_dt.date() < today_date:
            buckets["overdue"].append(entry)
        elif due_dt.date() == today_date:
            buckets["today"].append(entry)
        elif due_dt.date() <= week_end:
            buckets["this_week"].append(entry)
        else:
            buckets["later"].append(entry)

    full_later = len(buckets["later"])
    full_undated = len(buckets["undated"])
    if limit_later is not None:
        buckets["later"] = buckets["later"][:limit_later]
    if limit_undated is not None:
        buckets["undated"] = buckets["undated"][:limit_undated]

    return {
        "count": total,
        "overdue": buckets["overdue"],
        "today": buckets["today"],
        "this_week": buckets["this_week"],
        "later": buckets["later"],
        "undated": buckets["undated"],
        "truncated": {
            "later": max(0, full_later - len(buckets["later"])),
            "undated": max(0, full_undated - len(buckets["undated"])),
        },
    }


async def get_reminders(
    *,
    limit_later: int | None = 10,
    limit_undated: int | None = 15,
) -> dict[str, Any]:
    """Get incomplete Apple Reminders bucketed by urgency."""
    empty: dict[str, Any] = {
        "count": 0,
        "overdue": [],
        "today": [],
        "this_week": [],
        "later": [],
        "undated": [],
    }
    script_path = Path(__file__).resolve().parent / "reminders_fetch.swift"
    if not script_path.exists():
        return {**empty, "error": f"missing {script_path}"}

    try:
        proc = await asyncio.create_subprocess_exec(
            "swift",
            str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            return {**empty, "error": "swift reminders_fetch timeout"}
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()[:200]
            return {**empty, "error": f"swift rc={proc.returncode}: {err}"}

        return parse_reminders_output(
            stdout.decode(errors="replace"),
            limit_later=limit_later,
            limit_undated=limit_undated,
        )
    except Exception as e:
        return {**empty, "error": str(e)}
