#!/usr/bin/env python3
"""Calendar nudge engine — proactive iMessage alerts for upcoming events.

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
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.calendar import get_events, get_week_view, SKIP_CALENDARS
from core.constants import PERSONAL_EMAIL
from core.gather import gather_reminders
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
import agent_cp_client as cp  # noqa: E402
CP_AGENT = "nudge-engine"


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


async def _send_nudge(message: str, dry_run: bool = False):
    """Send via message bus or just print in dry-run mode."""
    if dry_run:
        print(f"  [DRY RUN] Would send: {message[:200]}")
        return
    from core.message_bus import send_message
    await send_message(message, agent="nudge", tier="normal", attribution=True)


async def run_nudges(dry_run: bool = False):
    now = datetime.now()
    hour = now.hour

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
                msg = "\n".join(parts)
                print(f"[nudge] week_ahead: {msg[:100]}")
                await _send_nudge(msg, dry_run)
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
        # Fire if there are events OR any overdue/today reminders to surface
        if (schedulable or has_reminder_signal) and not already_sent(day_key, "day_before"):
            parts = [f"Tomorrow ({tomorrow.strftime('%A')}): {len(schedulable)} events"]
            for e in schedulable[:8]:
                parts.append(f"  {e.start.strftime('%-I:%M %p')} — {e.summary}")
            parts.extend(_reminder_lines(reminders, include_week=False))
            msg = "\n".join(parts)
            print(f"[nudge] day_before: {msg[:100]}")
            await _send_nudge(msg, dry_run)
            log_nudge(day_key, "day_before", tomorrow.isoformat(), "day_before", msg)

    # --- Morning preview (7-8 AM) ---
    if 7 <= hour <= 8:
        today = now.date()
        today_start = datetime.combine(today, datetime.min.time()).replace(hour=0)
        today_end = datetime.combine(today, datetime.min.time()).replace(hour=23, minute=59)
        events = await get_events(today_start, today_end)
        schedulable = [e for e in events if not _should_skip(e)]

        morning_key = f"morning-{today.isoformat()}"
        if not already_sent(morning_key, "morning_preview"):
            from core.calendar import _compute_free_slots, get_schedule_view
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

            msg = "\n".join(parts)
            print(f"[nudge] morning_preview: {msg[:100]}")
            await _send_nudge(msg, dry_run)
            log_nudge(morning_key, "morning_preview", today.isoformat(), "morning_preview", msg)

    # --- 15-min and 5-min event nudges ---
    window_start = now
    window_end = now + timedelta(minutes=20)
    events = await get_events(window_start, window_end)

    for event in events:
        if _should_skip(event):
            continue

        minutes_away = (event.start - now).total_seconds() / 60

        # 15-min nudge (10-15 min before)
        if 10 <= minutes_away <= 15:
            uid = event.uid or f"{event.summary}-{event.start.isoformat()}"
            if not already_sent(uid, "fifteen_min"):
                msg = f"Meeting in {int(minutes_away)} min: {event.summary} ({event.start.strftime('%-I:%M %p')})"
                if event.location:
                    msg += f"\nLocation: {event.location}"
                print(f"[nudge] fifteen_min: {msg[:100]}")
                await _send_nudge(msg, dry_run)
                log_nudge(uid, event.summary, event.start.isoformat(), "fifteen_min", msg)

        # 5-min nudge (2-5 min before, only if event has location or notes)
        elif 2 <= minutes_away <= 5:
            if event.location or event.notes:
                uid = event.uid or f"{event.summary}-{event.start.isoformat()}"
                if not already_sent(uid, "five_min"):
                    msg = f"5 min: {event.summary} ({event.start.strftime('%-I:%M %p')})"
                    if event.location:
                        msg += f"\n{event.location}"
                    if event.notes:
                        msg += f"\nNotes: {event.notes[:200]}"
                    print(f"[nudge] five_min: {msg[:100]}")
                    await _send_nudge(msg, dry_run)
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
    args = parser.parse_args()

    init_nudge_db()

    if cp.is_killed(CP_AGENT):
        print(f"[nudge] killed via agent-cp, exiting")
        return

    if args.status:
        await show_status()
        return

    print(f"[nudge] Running at {datetime.now():%Y-%m-%d %H:%M:%S} (dry_run={args.dry_run})")
    await run_nudges(dry_run=args.dry_run)
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
