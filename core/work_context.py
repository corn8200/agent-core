"""Focused work context exporter.

Writes a live snapshot of near-term calendar events and active reminders to:
- ~/logs/work-context.json
- ~/logs/work-context.md

The Work source of truth is Apple Calendar named "Work" plus Reminders list
named "Work". The files are regenerated from EventKit, not incrementally
patched, so canceled events and completed/deleted reminders disappear on the
next refresh.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.calendar_service import CalendarEvent, get_events  # noqa: E402
from core.reminders_service import get_reminders  # noqa: E402


WORK_CONTEXT_JSON = Path.home() / "logs" / "work-context.json"
WORK_CONTEXT_MD = Path.home() / "logs" / "work-context.md"
DEFAULT_LOOKAHEAD_DAYS = 14
VERSION = 1

WORK_CALENDAR_NAMES = {"work"}
WORK_RELATED_LISTS = {"work"}
SKIP_CALENDARS = {"Siri Suggestions", "US Holidays", "Birthdays", "Scheduled Reminders"}
MEETING_PREFIXES = ("📅", "🗓")
PRIORITY_MARKERS = {"🔴": "P1", "🟠": "P2", "🟡": "P3"}


def _fingerprint(parts: Iterable[Any]) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except ValueError:
        return None


def _day_bucket(dt: datetime | None, today) -> str:
    if dt is None:
        return "undated"
    delta = (dt.date() - today).days
    if delta < 0:
        return "past"
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta <= 7:
        return "this_week"
    return "later"


def _event_to_dict(event: CalendarEvent | dict[str, Any], *, now: datetime) -> dict[str, Any] | None:
    if isinstance(event, CalendarEvent):
        summary = event.summary
        start = event.start
        end = event.end
        calendar = event.calendar
        location = event.location or ""
        notes = event.notes or ""
        all_day = event.all_day
        uid = event.uid or ""
    else:
        summary = (event.get("summary") or event.get("title") or "").strip()
        start = _parse_dt(event.get("start"))
        end = _parse_dt(event.get("end"))
        calendar = (event.get("calendar") or "").strip()
        location = (event.get("location") or "").strip()
        notes = (event.get("notes") or "").strip()
        all_day = bool(event.get("all_day"))
        uid = (event.get("uid") or event.get("id") or "").strip()

    if not summary or not start:
        return None
    if calendar in SKIP_CALENDARS:
        return None
    if calendar.casefold() not in WORK_CALENDAR_NAMES:
        return None
    if end is None:
        end = start

    status = "upcoming"
    if end < now:
        status = "past"
    elif start <= now <= end:
        status = "current"

    fp = _fingerprint([uid, calendar, summary, start.isoformat(), end.isoformat()])
    return {
        "id": uid or fp,
        "fingerprint": fp,
        "summary": summary,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "day": start.strftime("%a %b %-d"),
        "time": "all-day" if all_day else start.strftime("%-I:%M %p"),
        "calendar": calendar,
        "location": location or None,
        "notes": notes[:500] or None,
        "all_day": all_day,
        "bucket": _day_bucket(start, now.date()),
        "status": status,
        "_sort": start.isoformat(),
    }


def _priority_marker(name: str) -> str | None:
    if not name:
        return None
    return PRIORITY_MARKERS.get(name[0])


def _reminder_to_dict(item: dict[str, Any], bucket: str, *, now: datetime) -> dict[str, Any] | None:
    name = (item.get("name") or item.get("title") or "").strip()
    if not name:
        return None
    list_name = (item.get("list") or item.get("calendar") or "").strip()
    due = item.get("due")
    due_dt = _parse_dt(due)
    reminder_id = (item.get("id") or "").strip()
    fp = _fingerprint([reminder_id, list_name, name, due or ""])
    kind = "meeting" if name.startswith(MEETING_PREFIXES) else "task"
    priority_marker = _priority_marker(name)
    normalized = {
        "id": reminder_id or fp,
        "fingerprint": fp,
        "name": name,
        "list": list_name,
        "due": due or None,
        "due_bucket": bucket,
        "day_bucket": _day_bucket(due_dt, now.date()),
        "kind": kind,
        "priority_marker": priority_marker,
        "priority": item.get("priority"),
        "notes": (item.get("notes") or "")[:500] or None,
        "is_work_related": list_name.casefold() in WORK_RELATED_LISTS,
        "_sort": due_dt.isoformat() if due_dt else f"z-{list_name}-{name.lower()}",
    }
    return normalized


def _flatten_reminders(reminders: dict[str, Any], *, now: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for bucket in ("overdue", "today", "this_week", "later", "undated"):
        for item in reminders.get(bucket, []) or []:
            if isinstance(item, dict):
                normalized = _reminder_to_dict(item, bucket, now=now)
                if normalized:
                    out.append(normalized)
    out.sort(key=lambda r: (r["due_bucket"] not in {"overdue", "today"}, r["_sort"]))
    return out


def build_context(
    events: Iterable[CalendarEvent | dict[str, Any]],
    reminders: dict[str, Any],
    *,
    generated_at: datetime | None = None,
    now: datetime | None = None,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    source: str = "eventkit",
) -> dict[str, Any]:
    """Build a serializable work context from normalized calendar/reminder data."""
    current = now or datetime.now()
    generated = generated_at or datetime.now(timezone.utc)
    normalized_events = [
        ev for ev in (_event_to_dict(e, now=current) for e in events) if ev is not None
    ]
    normalized_events.sort(key=lambda e: e["_sort"])
    all_reminders = _flatten_reminders(reminders or {}, now=current)
    work_reminders = [r for r in all_reminders if r["is_work_related"]]
    normalized_reminders = work_reminders
    meeting_reminders = [r for r in work_reminders if r["kind"] == "meeting"]
    priority_reminders = [r for r in work_reminders if r.get("priority_marker")]
    upcoming_events = [e for e in normalized_events if e["status"] != "past"]

    for item in normalized_events:
        item.pop("_sort", None)
    for item in normalized_reminders:
        item.pop("_sort", None)

    context = {
        "version": VERSION,
        "generated_at": generated.isoformat(),
        "generated_at_local": current.isoformat(),
        "source": source,
        "window": {
            "start": datetime.combine(current.date(), datetime.min.time()).isoformat(),
            "end": (
                datetime.combine(current.date(), datetime.min.time())
                + timedelta(days=lookahead_days)
            ).isoformat(),
            "lookahead_days": lookahead_days,
        },
        "files": {
            "json": str(WORK_CONTEXT_JSON),
            "markdown": str(WORK_CONTEXT_MD),
        },
        "summary": {
            "events": len(normalized_events),
            "upcoming_events": len(upcoming_events),
            "reminders": len(normalized_reminders),
            "work_reminders": len(work_reminders),
            "meeting_reminders": len(meeting_reminders),
            "priority_reminders": len(priority_reminders),
            "overdue_reminders": len([r for r in normalized_reminders if r["due_bucket"] == "overdue"]),
            "today_reminders": len([r for r in normalized_reminders if r["due_bucket"] == "today"]),
        },
        "calendar": {
            "events": normalized_events,
            "upcoming": upcoming_events[:25],
        },
        "reminders": {
            "all": normalized_reminders,
            "by_bucket": {
                bucket: [r for r in normalized_reminders if r["due_bucket"] == bucket]
                for bucket in ("overdue", "today", "this_week", "later", "undated")
            },
        },
        "work": {
            "reminders": work_reminders,
            "meeting_reminders": meeting_reminders,
            "priority_reminders": priority_reminders,
            "next_actions": [
                r for r in work_reminders
                if r["due_bucket"] in {"overdue", "today", "this_week"} or r["kind"] == "meeting"
            ][:40],
        },
        "notes": [
            "Work source of truth: Apple Calendar named Work plus Reminders list named Work.",
            "Regenerated from live EventKit each refresh; canceled Work calendar events and completed/deleted Work reminders are removed by absence.",
            "Work Reminders prefixes are preserved: calendar emoji means meeting, red/orange/yellow mean P1/P2/P3.",
        ],
    }
    if reminders and reminders.get("error"):
        context["reminders"]["error"] = reminders.get("error")
    if reminders and reminders.get("truncated"):
        context["reminders"]["source_truncated"] = reminders.get("truncated")
    return context


def _format_event(event: dict[str, Any]) -> str:
    loc = f" @ {event['location']}" if event.get("location") else ""
    return f"- {event['day']} {event['time']} - {event['summary']}{loc}"


def _format_reminder(reminder: dict[str, Any]) -> str:
    due = f" due {reminder['due'][:10]}" if reminder.get("due") else ""
    marker = f" [{reminder['priority_marker']}]" if reminder.get("priority_marker") else ""
    return f"- [{reminder.get('list') or 'No list'}]{marker} {reminder['name']}{due}"


def render_markdown(context: dict[str, Any]) -> str:
    lines = [
        "# Work context",
        "",
        f"Generated: {context.get('generated_at_local')}",
        f"Window: {context.get('window', {}).get('lookahead_days')} days",
        "",
        "## Counts",
    ]
    summary = context.get("summary", {})
    for key in (
        "upcoming_events",
        "reminders",
        "work_reminders",
        "meeting_reminders",
        "priority_reminders",
        "overdue_reminders",
        "today_reminders",
    ):
        lines.append(f"- {key}: {summary.get(key, 0)}")

    lines.extend(["", "## Upcoming calendar"])
    upcoming = context.get("calendar", {}).get("upcoming", []) or []
    lines.extend(_format_event(e) for e in upcoming[:40])
    if not upcoming:
        lines.append("- None")

    lines.extend(["", "## Work reminders"])
    work_reminders = context.get("work", {}).get("reminders", []) or []
    lines.extend(_format_reminder(r) for r in work_reminders)
    if not work_reminders:
        lines.append("- None")

    lines.extend(["", "## All reminders"])
    by_bucket = context.get("reminders", {}).get("by_bucket", {}) or {}
    for bucket in ("overdue", "today", "this_week", "later", "undated"):
        items = by_bucket.get(bucket, []) or []
        lines.extend(["", f"### {bucket.replace('_', ' ').title()}"])
        lines.extend(_format_reminder(r) for r in items)
        if not items:
            lines.append("- None")

    lines.append("")
    return "\n".join(lines)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def write_context(context: dict[str, Any]) -> None:
    _atomic_write(WORK_CONTEXT_JSON, json.dumps(context, indent=2, default=str))
    _atomic_write(WORK_CONTEXT_MD, render_markdown(context))


async def refresh(
    *,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    write: bool = True,
) -> dict[str, Any]:
    """Refresh the work context from live Calendar + Reminders."""
    now = datetime.now()
    start = datetime.combine(now.date(), datetime.min.time())
    end = start + timedelta(days=lookahead_days)
    events, reminders = await asyncio.gather(
        get_events(start, end),
        get_reminders(limit_later=None, limit_undated=None),
    )
    context = build_context(
        events,
        reminders,
        now=now,
        lookahead_days=lookahead_days,
        source="eventkit-live",
    )
    if write:
        write_context(context)
    return context


def load() -> dict[str, Any] | None:
    if not WORK_CONTEXT_JSON.exists():
        return None
    try:
        return json.loads(WORK_CONTEXT_JSON.read_text())
    except Exception:
        return None


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Refresh or print the work context snapshot")
    parser.add_argument("--days", type=int, default=DEFAULT_LOOKAHEAD_DAYS)
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print JSON to stdout")
    args = parser.parse_args()

    context = await refresh(lookahead_days=args.days, write=not args.no_write)
    if args.json:
        print(json.dumps(context, indent=2, default=str))
    else:
        print(f"work-context json: {WORK_CONTEXT_JSON}")
        print(f"work-context md: {WORK_CONTEXT_MD}")
        print(f"summary: {context['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
