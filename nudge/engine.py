#!/usr/bin/env python3
"""Calendar nudge engine — proactive Pushover alerts for upcoming events.

Runs every 5 min via LaunchAgent. Pure Python — zero LLM tokens.
Nudge tiers: week_ahead, day_before, morning_preview, fifteen_min, five_min.
Dedup via SQLite unique index on (event_uid, nudge_tier).

Usage:
    python3 nudge/engine.py             # live mode
    python3 nudge/engine.py --dry-run   # log what would send, don't send
    python3 nudge/engine.py --status    # show recent nudge history
"""

import argparse
import asyncio
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.calendar_service import get_events, get_week_view, SKIP_CALENDARS
from core.constants import PERSONAL_EMAIL
from core.work_context import refresh as refresh_work_context
from home_ops.gather import gather_reminders
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
import agent_cp_client as cp  # noqa: E402
CP_AGENT = "nudge-engine"

from core.interactive_links import portal_url


@dataclass(frozen=True)
class NudgeProfile:
    title: str
    priority: int = 0
    sound: str = "pushover"


NUDGE_PROFILES = {
    "week_ahead": NudgeProfile("Week ahead", priority=0, sound="intermission"),
    "day_before": NudgeProfile("Tomorrow prep", priority=1, sound="vibrate"),
    "morning_preview": NudgeProfile("Today preview", priority=1, sound="vibrate"),
    "fifteen_min": NudgeProfile("Meeting soon", priority=1, sound="vibrate"),
    "five_min": NudgeProfile("Meeting now", priority=1, sound="persistent"),
}

STACK_FIRST_TIERS = {"week_ahead", "day_before", "morning_preview"}
PUSH_ONLY_TIERS = {"fifteen_min", "five_min"}


def _clip(value: str, limit: int) -> str:
    value = (value or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _profile(tier: str, *, title: str | None = None) -> NudgeProfile:
    base = NUDGE_PROFILES.get(tier, NudgeProfile("Nudge"))
    if title:
        return NudgeProfile(_clip(title, 80), priority=base.priority, sound=base.sound)
    return base


def _pushover_device() -> str | None:
    """Default nudges to the iPhone so they mirror to Watch instead of desktop-only."""
    device = os.environ.get("NUDGE_PUSHOVER_DEVICE", "iPhone").strip()
    if not device or device.casefold() in {"all", "*", "none"}:
        return None
    return device


def _reminder_lines(reminders: dict, include_week: bool = False) -> list[str]:
    """Format overdue/today (+optional this_week) reminder counts + sample names."""
    if not reminders:
        return []
    overdue = reminders.get("overdue", []) or []
    today = reminders.get("today", []) or []
    this_week = reminders.get("this_week", []) or [] if include_week else []
    if not (overdue or today or this_week):
        return []
    bits = []
    if overdue:
        bits.append(f"{len(overdue)} overdue")
    if today:
        bits.append(f"{len(today)} today")
    if include_week and this_week:
        bits.append(f"{len(this_week)} this week")
    out = ["Reminders: " + ", ".join(bits)]
    # Show up to 4 names, overdue first (most urgent)
    samples = (overdue + today + this_week)[:4]
    for r in samples:
        name = r.get("name") if isinstance(r, dict) else str(r)
        if name:
            out.append(f"  • {name[:60]}")
    return out


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except ValueError:
        return None


def _strip_work_prefix(name: str) -> str:
    cleaned = (name or "").strip()
    for prefix in ("📅", "🗓", "🔴", "🟠", "🟡"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    return cleaned.lstrip("-— ").strip() or name


def _work_item_due_dt(item: dict) -> datetime | None:
    return _parse_iso_dt(item.get("due") if isinstance(item, dict) else None)


def _work_items_for_date(items: list[dict], target_date, *, include_overdue: bool = False) -> list[dict]:
    out = []
    for item in items or []:
        due_dt = _work_item_due_dt(item)
        bucket = item.get("due_bucket")
        if include_overdue and bucket == "overdue":
            out.append(item)
        elif due_dt and due_dt.date() == target_date:
            out.append(item)
    out.sort(key=lambda x: (_work_item_due_dt(x) or datetime.max, x.get("name", "")))
    return out


def _work_context_lines(
    work_ctx: dict,
    *,
    target_date=None,
    include_week: bool = False,
    include_overdue: bool = False,
    limit: int = 6,
) -> list[str]:
    """Format concise Work calendar/reminder signal for previews."""
    if not work_ctx:
        return []

    lines: list[str] = []
    entries: list[str] = []
    calendar_ctx = work_ctx.get("calendar") or {}
    events = calendar_ctx.get("events") or calendar_ctx.get("upcoming") or []
    for event in events:
        if event.get("status") == "past":
            continue
        start = _parse_iso_dt(event.get("start"))
        if not start:
            continue
        if target_date and start.date() != target_date:
            continue
        if include_week and event.get("bucket") not in {"today", "tomorrow", "this_week"}:
            continue
        entries.append(f"{event.get('time') or start.strftime('%-I:%M %p')} - {event.get('summary')}")

    work = work_ctx.get("work") or {}
    meeting_items = work.get("meeting_reminders") or []
    priority_items = work.get("priority_reminders") or []
    if target_date:
        selected_meetings = _work_items_for_date(meeting_items, target_date, include_overdue=False)
        selected_priority = _work_items_for_date(priority_items, target_date, include_overdue=include_overdue)
    elif include_week:
        selected_meetings = [
            item for item in meeting_items
            if item.get("due_bucket") in {"overdue", "today", "tomorrow", "this_week"}
        ]
        selected_priority = [
            item for item in priority_items
            if item.get("due_bucket") in {"overdue", "today", "tomorrow", "this_week", "undated"}
        ]
    else:
        selected_meetings = []
        selected_priority = []

    for item in selected_meetings:
        due_dt = _work_item_due_dt(item)
        when = due_dt.strftime("%-I:%M %p") if due_dt else "Work"
        entries.append(f"{when} - {_strip_work_prefix(item.get('name', ''))}")
    for item in selected_priority:
        marker = item.get("priority_marker") or "P"
        entries.append(f"{marker} - {_strip_work_prefix(item.get('name', ''))}")

    deduped = []
    seen = set()
    for entry in entries:
        if not entry or entry in seen:
            continue
        seen.add(entry)
        deduped.append(entry)

    if deduped:
        lines.append("Work:")
        lines.extend(f"  {entry[:90]}" for entry in deduped[:limit])
    return lines


def _has_upcoming_work_meeting(work_ctx: dict, now: datetime, minutes: int = 60) -> bool:
    for item in ((work_ctx.get("work") or {}).get("meeting_reminders") or []):
        due_dt = _work_item_due_dt(item)
        if not due_dt:
            continue
        delta_min = (due_dt - now).total_seconds() / 60
        if 0 <= delta_min <= minutes:
            return True
    return False


def _nudge_uid(event) -> str:
    """Stable per occurrence. Calendar UIDs alone can collapse recurring events."""
    base = event.uid or event.summary
    return f"{base}|{event.start.isoformat()}"


def _meeting_title(prefix: str, summary: str) -> str:
    return _clip(f"{prefix}: {summary}", 80)


def _format_calendar_nudge(event, minutes_away: float) -> str:
    lines = [
        f"{event.start.strftime('%-I:%M %p')} - {event.summary}",
        f"Calendar: {event.calendar}",
    ]
    if event.location:
        lines.append(f"Where: {event.location}")
    if event.notes:
        lines.append(f"Notes: {event.notes[:240]}")
    hint = _nudge_recall(event.summary)
    if hint:
        lines.append(hint)
    if minutes_away <= 5:
        lines.append("Move now.")
    return "\n".join(lines)


def _format_work_reminder_nudge(item: dict, due_dt: datetime, minutes_away: float) -> str:
    title = _strip_work_prefix(item.get("name", "Work meeting"))
    lines = [
        f"{due_dt.strftime('%-I:%M %p')} - {title}",
        "Source: Work reminders",
    ]
    notes = (item.get("notes") or "").strip()
    if notes:
        lines.append(f"Notes: {notes[:240]}")
    if minutes_away <= 5:
        lines.append("Move now.")
    return "\n".join(lines)


async def _send_work_meeting_nudges(work_ctx: dict, now: datetime, dry_run: bool = False):
    for item in ((work_ctx.get("work") or {}).get("meeting_reminders") or []):
        due_dt = _work_item_due_dt(item)
        if not due_dt:
            continue
        minutes_away = (due_dt - now).total_seconds() / 60
        uid_base = item.get("fingerprint") or item.get("id") or item.get("name")
        uid = f"work-reminder|{uid_base}|{due_dt.isoformat()}"
        meeting_name = _strip_work_prefix(item.get("name", "Work meeting"))

        if 10 <= minutes_away <= 15 and not already_sent(uid, "fifteen_min"):
            msg = _format_work_reminder_nudge(item, due_dt, minutes_away)
            print(f"[nudge] work fifteen_min: {msg[:100]}")
            sent = await _send_nudge(
                msg,
                dry_run,
                tier="fifteen_min",
                title=_meeting_title(f"{int(minutes_away)} min", meeting_name),
                url=portal_url("/work"),
                url_title="Open work",
            )
            if sent:
                log_nudge(uid, meeting_name, due_dt.isoformat(), "fifteen_min", msg)
        elif 2 <= minutes_away <= 5 and not already_sent(uid, "five_min"):
            msg = _format_work_reminder_nudge(item, due_dt, minutes_away)
            print(f"[nudge] work five_min: {msg[:100]}")
            sent = await _send_nudge(
                msg,
                dry_run,
                tier="five_min",
                title=_meeting_title("5 min", meeting_name),
                url=portal_url("/work"),
                url_title="Open work",
            )
            if sent:
                log_nudge(uid, meeting_name, due_dt.isoformat(), "five_min", msg)

NUDGE_DB = Path.home() / "logs" / "nudge-state.db"

NUDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS nudge_log (
    id INTEGER PRIMARY KEY,
    event_uid TEXT NOT NULL,
    event_summary TEXT NOT NULL,
    event_start TEXT NOT NULL,
    nudge_tier TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    message TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_tier
    ON nudge_log(event_uid, nudge_tier);
"""


def _db() -> sqlite3.Connection:
    NUDGE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(NUDGE_DB), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_nudge_db():
    with _db() as conn:
        conn.executescript(NUDGE_SCHEMA)


def already_sent(event_uid: str, tier: str) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM nudge_log WHERE event_uid=? AND nudge_tier=?",
            (event_uid, tier),
        ).fetchone()
        return row is not None


def log_nudge(event_uid: str, summary: str, start: str, tier: str, message: str):
    try:
        with _db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO nudge_log "
                "(event_uid, event_summary, event_start, nudge_tier, sent_at, message) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (event_uid, summary, start, tier, datetime.now().isoformat(), message),
            )
    except sqlite3.Error:
        pass


def _should_skip(event) -> bool:
    """Skip all-day events and non-schedulable calendars."""
    return event.all_day or event.calendar in SKIP_CALENDARS


def _nudge_recall(event_title: str) -> str:
    """Return a one-line prior-context hint for an event, or "".

    Gated behind NUDGE_RECALL=1 — nudges are short alerts, so we only append
    the most-relevant single hit, trimmed to ~120 chars. Default OFF until
    we've validated the signal-to-noise in the wild.
    """
    if os.environ.get("NUDGE_RECALL") != "1":
        return ""
    if not event_title:
        return ""
    try:
        from core.recall import raw_search
        hits = raw_search(event_title, kind="nudge", limit=1)
    except Exception:
        return ""
    if not hits:
        return ""
    content = (hits[0].get("content") or "").strip().replace("\n", " ")
    if not content:
        return ""
    return f"Prior: {content[:120]}"


async def _send_pushover_notification(
    *,
    title: str,
    message: str,
    priority: int,
    sound: str,
    url: str | None = None,
    url_title: str | None = None,
):
    from core.pushover import send_pushover
    return await send_pushover(
        title=title,
        message=message,
        priority=priority,
        sound=sound,
        url=url,
        url_title=url_title,
        device=_pushover_device(),
        timestamp=int(datetime.now().timestamp()),
    )


async def _send_imessage_fallback(title: str, message: str):
    from core.message_bus import send_message
    return await send_message(f"{title}\n{message}", agent="nudge", tier="normal", attribution=True)


def _publish_stack_item(
    *,
    title: str,
    message: str,
    tier: str,
    priority: int,
    url: str | None = None,
    url_title: str | None = None,
) -> bool:
    payload = {
        "title": title,
        "message": message,
        "kind": "nudge",
        "tier": tier,
        "priority": priority,
        "sources": ("ui",),
    }
    if url:
        payload["url"] = url
    if url_title:
        payload["url_title"] = url_title
    try:
        return cp.event(CP_AGENT, "nudge", payload=payload) is not None
    except Exception as exc:
        print(f"[nudge] stack publish failed: {exc}", file=sys.stderr)
        return False


async def _send_nudge(
    message: str,
    dry_run: bool = False,
    *,
    tier: str = "normal",
    title: str | None = None,
    priority: int | None = None,
    sound: str | None = None,
    url: str | None = None,
    url_title: str | None = None,
):
    """Send nudge through the tier's preferred non-LLM delivery path."""
    prof = _profile(tier, title=title)
    push_title = prof.title
    push_priority = prof.priority if priority is None else priority
    push_sound = sound or prof.sound
    delivery = os.environ.get("NUDGE_DELIVERY", "").strip().casefold()
    effective_delivery = delivery or ("stack" if tier in STACK_FIRST_TIERS else "pushover")
    stack_first = tier in STACK_FIRST_TIERS and delivery not in {"pushover", "imessage", "both"}
    push_only = tier in PUSH_ONLY_TIERS

    if dry_run:
        print(
            "  [DRY RUN] Would send "
            f"delivery={effective_delivery} title={push_title!r} "
            f"priority={push_priority} sound={push_sound!r} "
            f"url={url or ''!r}: {message[:240]}"
        )
        return False

    if stack_first:
        if _publish_stack_item(
            title=push_title,
            message=message,
            tier=tier,
            priority=push_priority,
            url=url,
            url_title=url_title,
        ):
            print("[nudge] stack delivery: published")
            return True
        print("[nudge] stack delivery failed; trying pushover", file=sys.stderr)

    if delivery == "imessage" and not push_only:
        ok, result = await _send_imessage_fallback(push_title, message)
        print(f"[nudge] imessage delivery: {result}")
        return ok

    push = await _send_pushover_notification(
        title=push_title,
        message=message,
        priority=push_priority,
        sound=push_sound,
        url=url,
        url_title=url_title,
    )
    if push.ok:
        print(f"[nudge] pushover delivery: {push.detail}")
        if delivery == "both" and not push_only:
            ok, result = await _send_imessage_fallback(push_title, message)
            print(f"[nudge] imessage mirror: {result}")
            return True
        return True

    print(f"[nudge] pushover failed: {push.detail}", file=sys.stderr)
    if push_only:
        return False
    ok, result = await _send_imessage_fallback(push_title, message)
    print(f"[nudge] imessage fallback: {result}")
    return ok


def _in_any_active_window(now: datetime, *, upcoming_events: bool = False) -> bool:
    """Return True if `now` falls inside any tier's active send window.

    Tiers:
      - week_ahead:        Sunday 19:00-20:00
      - day_before:        any day 18:00-20:00
      - morning_preview:   any day 07:00-08:59
      - fifteen_min / five_min: gated on `upcoming_events` — only active when
        there is at least one event in the next hour. The caller fetches the
        20-minute window itself, but for cheap pre-gating we accept an
        explicit hint. When `upcoming_events=False` (default) these tiers
        do NOT open the window; callers that know events are imminent pass
        True to skip the early-return.

    Rationale: the engine was firing every 5 min (288 runs/day) with ~3800
    no-op hits queuing gather_reminders()/get_events(). Gating at the top
    drops ~85% of those runs to a fast "outside window, skip" log line.
    """
    hour = now.hour
    if now.weekday() == 6 and 19 <= hour <= 20:
        return True
    if 18 <= hour <= 20:
        return True
    if 7 <= hour <= 8:
        return True
    if upcoming_events:
        return True
    return False


async def _has_upcoming_events(now: datetime) -> bool:
    """Cheap probe: are there any schedulable events in the next hour?"""
    try:
        events = await get_events(now, now + timedelta(hours=1))
    except Exception:
        return False
    return any(not _should_skip(e) for e in events)


async def run_nudges(dry_run: bool = False, force_tier: str | None = None, bypass_window: bool = False):
    now = datetime.now()
    hour = now.hour
    work_ctx = {}

    try:
        work_ctx = await refresh_work_context()
        print(f"[nudge] work-context refreshed: {work_ctx.get('summary', {})}")
    except Exception as e:
        print(f"[nudge] work-context refresh failed: {e}", file=sys.stderr)

    # Top-of-loop gate: if no tier is in an active window AND no event is
    # imminent, skip all gather_* calls. This is the single biggest lever
    # for the 3800+ no-op fires/day.
    if not bypass_window and not _in_any_active_window(now):
        has_events = await _has_upcoming_events(now)
        has_work_meeting = _has_upcoming_work_meeting(work_ctx, now)
        if not has_events and not has_work_meeting:
            print(f"[nudge] outside active window, skipping")
            return

    # --- Week-ahead (Sunday 7-8 PM) ---
    if now.weekday() == 6 and 19 <= hour <= 20:
        week_key = f"week-{now.isocalendar()[1]}"
        if not already_sent(week_key, "week_ahead"):
            week = await get_week_view()
            if week.days:
                parts = [f"This week: {week.summary}"]
                if week.key_events:
                    parts.append("Key:")
                    for ke in week.key_events[:5]:
                        parts.append(f"  {ke['day']} {ke['time']} — {ke['summary']}")
                reminders = await gather_reminders()
                parts.extend(_reminder_lines(reminders, include_week=True))
                parts.extend(_work_context_lines(work_ctx, include_week=True))
                msg = "\n".join(parts)
                print(f"[nudge] week_ahead: {msg[:100]}")
                sent = await _send_nudge(
                    msg,
                    dry_run,
                    tier="week_ahead",
                    url=portal_url("/work"),
                    url_title="Open week",
                )
                if sent:
                    log_nudge(week_key, "week_ahead", now.isoformat(), "week_ahead", msg)

    # --- Day-before (6-8 PM) ---
    if 18 <= hour <= 20:
        tomorrow = now.date() + timedelta(days=1)
        tomorrow_start = datetime.combine(tomorrow, datetime.min.time()).replace(hour=0)
        tomorrow_end = datetime.combine(tomorrow, datetime.min.time()).replace(hour=23, minute=59)
        events = await get_events(tomorrow_start, tomorrow_end)
        schedulable = [e for e in events if not _should_skip(e)]

        day_key = f"day-{tomorrow.isoformat()}"
        reminders = await gather_reminders() if not already_sent(day_key, "day_before") else {}
        has_reminder_signal = bool(
            reminders and (reminders.get("overdue") or reminders.get("today"))
        )
        work_lines = _work_context_lines(
            work_ctx,
            target_date=tomorrow,
            include_overdue=True,
        )
        # Fire if there are events OR any overdue/today reminders to surface
        if (schedulable or has_reminder_signal or work_lines) and not already_sent(day_key, "day_before"):
            parts = [f"Tomorrow ({tomorrow.strftime('%A')}): {len(schedulable)} events"]
            for e in schedulable[:8]:
                parts.append(f"  {e.start.strftime('%-I:%M %p')} — {e.summary}")
            parts.extend(_reminder_lines(reminders, include_week=False))
            parts.extend(work_lines)
            msg = "\n".join(parts)
            print(f"[nudge] day_before: {msg[:100]}")
            sent = await _send_nudge(
                msg,
                dry_run,
                tier="day_before",
                url=portal_url("/work"),
                url_title="Open tomorrow",
            )
            if sent:
                log_nudge(day_key, "day_before", tomorrow.isoformat(), "day_before", msg)

    # --- Morning preview (7-8 AM, or forced) ---
    if (7 <= hour <= 8) or force_tier == "morning_preview":
        today = now.date()
        today_start = datetime.combine(today, datetime.min.time()).replace(hour=0)
        today_end = datetime.combine(today, datetime.min.time()).replace(hour=23, minute=59)
        events = await get_events(today_start, today_end)
        schedulable = [e for e in events if not _should_skip(e)]

        morning_key = f"morning-{today.isoformat()}"
        if force_tier == "morning_preview" or not already_sent(morning_key, "morning_preview"):
            from core.calendar_service import _compute_free_slots, get_schedule_view
            view = await get_schedule_view()

            parts = [f"Today: {len(schedulable)} events"]
            if schedulable:
                first = schedulable[0]
                parts.append(f"First: {first.start.strftime('%-I:%M %p')} — {first.summary}")
            if view.free_slots_today:
                free_strs = [
                    f"{s.start.strftime('%-I:%M')}-{s.end.strftime('%-I:%M %p')}"
                    for s in view.free_slots_today[:3]
                ]
                parts.append(f"Free: {', '.join(free_strs)}")
            if view.reminders:
                overdue = len(view.reminders.get("overdue", []))
                due_today = len(view.reminders.get("today", []))
                if overdue or due_today:
                    parts.append(f"Reminders: {overdue} overdue, {due_today} due today")
            parts.extend(_work_context_lines(
                work_ctx,
                target_date=today,
                include_overdue=True,
            ))

            msg = "\n".join(parts)
            print(f"[nudge] morning_preview: {msg[:100]}")
            sent = await _send_nudge(
                msg,
                dry_run,
                tier="morning_preview",
                url=portal_url("/work"),
                url_title="Open today",
            )
            if sent:
                log_nudge(morning_key, "morning_preview", today.isoformat(), "morning_preview", msg)

    # --- 15-min and 5-min event nudges ---
    window_start = now
    window_end = now + timedelta(minutes=20)
    events = await get_events(window_start, window_end)
    await _send_work_meeting_nudges(work_ctx, now, dry_run)

    for event in events:
        if _should_skip(event):
            continue

        minutes_away = (event.start - now).total_seconds() / 60

        # 15-min nudge (10-15 min before)
        if 10 <= minutes_away <= 15:
            uid = _nudge_uid(event)
            if not already_sent(uid, "fifteen_min"):
                msg = _format_calendar_nudge(event, minutes_away)
                print(f"[nudge] fifteen_min: {msg[:100]}")
                sent = await _send_nudge(
                    msg,
                    dry_run,
                    tier="fifteen_min",
                    title=_meeting_title(f"{int(minutes_away)} min", event.summary),
                    url=portal_url("/work"),
                    url_title="Open work",
                )
                if sent:
                    log_nudge(uid, event.summary, event.start.isoformat(), "fifteen_min", msg)

        # 5-min nudge (2-5 min before, only if event has location or notes)
        elif 2 <= minutes_away <= 5:
            if event.location or event.notes or event.calendar.casefold() == "work":
                uid = _nudge_uid(event)
                if not already_sent(uid, "five_min"):
                    msg = _format_calendar_nudge(event, minutes_away)
                    print(f"[nudge] five_min: {msg[:100]}")
                    sent = await _send_nudge(
                        msg,
                        dry_run,
                        tier="five_min",
                        title=_meeting_title("5 min", event.summary),
                        url=portal_url("/work"),
                        url_title="Open work",
                    )
                    if sent:
                        log_nudge(uid, event.summary, event.start.isoformat(), "five_min", msg)


async def show_status():
    """Show recent nudge history."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT nudge_tier, event_summary, sent_at, message "
            "FROM nudge_log ORDER BY sent_at DESC LIMIT 20"
        ).fetchall()
    if not rows:
        print("No nudges sent yet.")
        return
    for r in rows:
        print(f"  [{r['nudge_tier']}] {r['sent_at'][:16]} — {r['event_summary'][:60]}")


async def main():
    parser = argparse.ArgumentParser(description="Calendar nudge engine")
    parser.add_argument("--dry-run", action="store_true", help="Log without sending")
    parser.add_argument("--status", action="store_true", help="Show recent nudge history")
    parser.add_argument("--force-tier", metavar="TIER", help="Force a specific tier regardless of time window or dedup")
    parser.add_argument("--bypass-window", action="store_true", help="Skip the active-window gate")
    args = parser.parse_args()

    init_nudge_db()

    if cp.is_killed(CP_AGENT):
        print(f"[nudge] killed via agent-cp, exiting")
        return

    if args.status:
        await show_status()
        return

    print(f"[nudge] Running at {datetime.now():%Y-%m-%d %H:%M:%S} (dry_run={args.dry_run}, force_tier={args.force_tier}, bypass_window={args.bypass_window})")
    await run_nudges(dry_run=args.dry_run, force_tier=args.force_tier, bypass_window=args.bypass_window)
    print("[nudge] Done.")


if __name__ == "__main__":
    try: cp.event(CP_AGENT, "start")
    except Exception: pass
    try:
        asyncio.run(main())
    except BaseException as _e:
        import traceback as _tb
        _tbs = _tb.format_exc()
        try:
            cp.event(CP_AGENT, "error",
                     payload={"exc": type(_e).__name__, "msg": str(_e)[:500]})
        except Exception: pass
        import sys as _sys
        _sys.stderr.write(_tbs)
        raise
    try: cp.event(CP_AGENT, "complete")
    except Exception: pass
