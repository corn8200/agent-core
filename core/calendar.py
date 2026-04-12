"""Calendar service — read/write/availability/conflict detection.

Primary source: Apple Calendar via osascript (includes Google via CalDAV).
Writes target the Google CalDAV calendar so events sync back to Google.
"""

import asyncio
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, date
from typing import Optional


# Calendars to skip in scheduling logic (still shown in display output)
SKIP_CALENDARS = {"Siri Suggestions", "US Holidays", "Birthdays"}


@dataclass
class CalendarEvent:
    summary: str
    start: datetime
    end: datetime
    calendar: str
    location: str = ""
    notes: str = ""
    all_day: bool = False
    uid: str = ""

    @property
    def duration_min(self) -> int:
        return int((self.end - self.start).total_seconds() / 60)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat()
        d["duration_min"] = self.duration_min
        return d


@dataclass
class FreeSlot:
    start: datetime
    end: datetime

    @property
    def duration_min(self) -> int:
        return int((self.end - self.start).total_seconds() / 60)

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration_min": self.duration_min,
        }


@dataclass
class DayView:
    date: date
    day_name: str
    events: list[CalendarEvent]
    event_count: int = 0
    free_hours: float = 0.0
    busiest_block: str = ""

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "day_name": self.day_name,
            "events": [e.to_dict() for e in self.events],
            "event_count": self.event_count,
            "free_hours": round(self.free_hours, 1),
            "busiest_block": self.busiest_block,
        }


@dataclass
class ScheduleView:
    events_today: list[CalendarEvent] = field(default_factory=list)
    events_tomorrow: list[CalendarEvent] = field(default_factory=list)
    reminders: dict = field(default_factory=dict)
    free_slots_today: list[FreeSlot] = field(default_factory=list)
    next_event: Optional[CalendarEvent] = None
    minutes_until_next: Optional[int] = None
    current_status: str = ""

    def to_dict(self) -> dict:
        return {
            "events_today": [e.to_dict() for e in self.events_today],
            "events_tomorrow": [e.to_dict() for e in self.events_tomorrow],
            "reminders": self.reminders,
            "free_slots_today": [s.to_dict() for s in self.free_slots_today],
            "next_event": self.next_event.to_dict() if self.next_event else None,
            "minutes_until_next": self.minutes_until_next,
            "current_status": self.current_status,
        }


@dataclass
class WeekView:
    days: list[DayView] = field(default_factory=list)
    summary: str = ""
    key_events: list[dict] = field(default_factory=list)
    total_free_hours: float = 0.0
    busiest_day: str = ""
    lightest_day: str = ""

    def to_dict(self) -> dict:
        return {
            "days": [d.to_dict() for d in self.days],
            "summary": self.summary,
            "key_events": self.key_events,
            "total_free_hours": round(self.total_free_hours, 1),
            "busiest_day": self.busiest_day,
            "lightest_day": self.lightest_day,
        }


# --- osascript helpers ---

async def _run_osascript(script: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode().strip()


def _parse_apple_date(s: str) -> Optional[datetime]:
    """Parse AppleScript date string like 'Sunday, April 5, 2026 at 18:00:00'."""
    if not s:
        return None
    s2 = re.sub(r"^\w+,\s*", "", s)
    for fmt in (
        "%B %d, %Y at %H:%M:%S",
        "%B %-d, %Y at %H:%M:%S",
        "%B %d, %Y at %I:%M:%S %p",
        "%B %d, %Y",
    ):
        try:
            return datetime.strptime(s2, fmt)
        except ValueError:
            continue
    return None


# --- Core API ---

async def get_events(start: datetime, end: datetime) -> list[CalendarEvent]:
    """Get calendar events in a date range from Apple Calendar."""
    # AppleScript date format for setting dates
    start_str = start.strftime("%B %d, %Y")
    end_str = end.strftime("%B %d, %Y")

    script = f'''
    set FS to (ASCII character 31)
    set RS to (ASCII character 30)
    tell application "Calendar"
        set startDate to date "{start_str}"
        set time of startDate to {start.hour * 3600 + start.minute * 60}
        set endDate to date "{end_str}"
        set time of endDate to {end.hour * 3600 + end.minute * 60 + 86399}
        set output to ""
        repeat with cal in calendars
            try
                set evts to (every event of cal whose start date >= startDate and start date < endDate)
                repeat with e in evts
                    set sd to start date of e
                    set ed to end date of e
                    set loc to ""
                    try
                        set loc to location of e
                        if loc is missing value then set loc to ""
                    end try
                    set nt to ""
                    try
                        set nt to description of e
                        if nt is missing value then set nt to ""
                    end try
                    set eid to ""
                    try
                        set eid to uid of e
                    end try
                    set output to output & (summary of e) & FS & (sd as string) & FS & (ed as string) & FS & (name of cal) & FS & loc & FS & nt & FS & eid & RS
                end repeat
            end try
        end repeat
        return output
    end tell
    '''
    try:
        result = await _run_osascript(script)
        if not result:
            return []

        events = []
        for raw in result.split("\x1e"):
            raw = raw.strip()
            if not raw:
                continue
            parts = raw.split("\x1f")
            if len(parts) < 4:
                continue

            summary = parts[0].strip()
            start_dt = _parse_apple_date(parts[1].strip())
            end_dt = _parse_apple_date(parts[2].strip())
            cal_name = parts[3].strip()
            location = parts[4].strip() if len(parts) > 4 else ""
            notes = parts[5].strip() if len(parts) > 5 else ""
            uid = parts[6].strip() if len(parts) > 6 else ""

            if not start_dt or not end_dt:
                continue

            # Detect all-day events (24h or longer duration)
            all_day = (end_dt - start_dt).total_seconds() >= 86400

            events.append(CalendarEvent(
                summary=summary,
                start=start_dt,
                end=end_dt,
                calendar=cal_name,
                location=location,
                notes=notes[:500],
                all_day=all_day,
                uid=uid,
            ))

        events.sort(key=lambda e: e.start)
        return events
    except Exception as e:
        print(f"[calendar] get_events error: {e}")
        return []


async def create_event(
    summary: str,
    start: datetime,
    end: datetime,
    calendar: str = "notify@jcornelius.net",
    location: str = "",
    notes: str = "",
) -> bool:
    """Create a calendar event via osascript. Defaults to Google CalDAV calendar."""
    start_str = start.strftime("%B %d, %Y")
    end_str = end.strftime("%B %d, %Y")
    start_time = start.hour * 3600 + start.minute * 60
    end_time = end.hour * 3600 + end.minute * 60

    # Escape quotes in strings
    summary = summary.replace('"', '\\"')
    location = location.replace('"', '\\"')
    notes = notes.replace('"', '\\"')

    script = f'''
    tell application "Calendar"
        set targetCal to first calendar whose name is "{calendar}"
        set startDate to date "{start_str}"
        set time of startDate to {start_time}
        set endDate to date "{end_str}"
        set time of endDate to {end_time}
        set newEvent to make new event at end of events of targetCal with properties {{summary:"{summary}", start date:startDate, end date:endDate, location:"{location}", description:"{notes}"}}
        return uid of newEvent
    end tell
    '''
    try:
        result = await _run_osascript(script)
        return bool(result)
    except Exception as e:
        print(f"[calendar] create_event error: {e}")
        return False


def _compute_free_slots(
    events: list[CalendarEvent],
    day_start: datetime,
    day_end: datetime,
    min_gap_min: int = 30,
) -> list[FreeSlot]:
    """Compute free time slots from a list of events within a day window."""
    # Filter to non-all-day, non-skip-calendar events
    blocking = [
        e for e in events
        if not e.all_day and e.calendar not in SKIP_CALENDARS
    ]
    blocking.sort(key=lambda e: e.start)

    slots = []
    cursor = day_start
    for evt in blocking:
        if evt.start > cursor:
            gap = FreeSlot(start=cursor, end=evt.start)
            if gap.duration_min >= min_gap_min:
                slots.append(gap)
        if evt.end > cursor:
            cursor = evt.end
    # Trailing free time
    if cursor < day_end:
        gap = FreeSlot(start=cursor, end=day_end)
        if gap.duration_min >= min_gap_min:
            slots.append(gap)
    return slots


async def check_availability(start: datetime, end: datetime) -> list[FreeSlot]:
    """Get free time slots in a date range."""
    events = await get_events(start, end)
    return _compute_free_slots(events, start, end)


async def detect_conflicts(start: datetime, end: datetime) -> list[CalendarEvent]:
    """Find events that overlap with the given time window."""
    events = await get_events(
        start - timedelta(hours=1),
        end + timedelta(hours=1),
    )
    conflicts = []
    for e in events:
        if e.all_day or e.calendar in SKIP_CALENDARS:
            continue
        if e.start < end and e.end > start:
            conflicts.append(e)
    return conflicts


async def get_schedule_view(target_date: date | None = None) -> ScheduleView:
    """Unified schedule view: events + reminders + free slots + current status."""
    now = datetime.now()
    target = target_date or now.date()

    today_start = datetime.combine(target, datetime.min.time()).replace(hour=7)
    today_end = datetime.combine(target, datetime.min.time()).replace(hour=21)
    tomorrow_start = today_start + timedelta(days=1)
    tomorrow_end = today_end + timedelta(days=1)

    # Fetch today + tomorrow events
    all_events = await get_events(today_start, tomorrow_end)

    events_today = [e for e in all_events if e.start.date() == target]
    events_tomorrow = [e for e in all_events if e.start.date() == target + timedelta(days=1)]

    # Free slots for today
    free_slots = _compute_free_slots(events_today, max(today_start, now), today_end)

    # Next event
    upcoming = [e for e in events_today if e.start > now and not e.all_day]
    next_event = upcoming[0] if upcoming else None
    minutes_until = None
    if next_event:
        minutes_until = int((next_event.start - now).total_seconds() / 60)

    # Current status
    current_events = [
        e for e in events_today
        if e.start <= now < e.end and not e.all_day and e.calendar not in SKIP_CALENDARS
    ]
    if current_events:
        ce = current_events[0]
        end_str = ce.end.strftime("%-I:%M %p")
        current_status = f"In: {ce.summary} (ends {end_str})"
    elif next_event:
        next_str = next_event.start.strftime("%-I:%M %p")
        current_status = f"Free until {next_str}"
    else:
        current_status = "Free for the rest of the day"

    # Reminders — import from gather to avoid duplication
    reminders = {}
    try:
        from core.gather import gather_reminders
        reminders = await gather_reminders()
    except Exception:
        pass

    return ScheduleView(
        events_today=events_today,
        events_tomorrow=events_tomorrow,
        reminders=reminders,
        free_slots_today=free_slots,
        next_event=next_event,
        minutes_until_next=minutes_until,
        current_status=current_status,
    )


async def get_week_view() -> WeekView:
    """7-day lookahead with per-day summaries."""
    now = datetime.now()
    start = datetime.combine(now.date(), datetime.min.time()).replace(hour=7)
    end = start + timedelta(days=7)

    all_events = await get_events(start, end)

    days = []
    total_free = 0.0
    max_events = (-1, "")
    min_events = (999, "")

    for i in range(7):
        day = (now.date() + timedelta(days=i))
        day_name = day.strftime("%A")
        day_start = datetime.combine(day, datetime.min.time()).replace(hour=7)
        day_end = datetime.combine(day, datetime.min.time()).replace(hour=21)

        day_events = [e for e in all_events if e.start.date() == day]
        schedulable = [e for e in day_events if not e.all_day and e.calendar not in SKIP_CALENDARS]

        free_slots = _compute_free_slots(day_events, day_start, day_end)
        free_hrs = sum(s.duration_min for s in free_slots) / 60.0
        total_free += free_hrs

        # Find busiest block
        busiest = ""
        if schedulable:
            # Cluster adjacent events
            sorted_evts = sorted(schedulable, key=lambda e: e.start)
            busiest = f"{sorted_evts[0].start.strftime('%-I%p')}-{sorted_evts[-1].end.strftime('%-I%p')}"

        dv = DayView(
            date=day,
            day_name=day_name,
            events=day_events,
            event_count=len(schedulable),
            free_hours=free_hrs,
            busiest_block=busiest,
        )
        days.append(dv)

        if len(schedulable) > max_events[0]:
            max_events = (len(schedulable), day_name)
        if len(schedulable) < min_events[0]:
            min_events = (len(schedulable), day_name)

    # Build summary string
    parts = []
    for d in days:
        if d.event_count == 0:
            parts.append(f"{d.day_name[:3]}: free")
        else:
            block = f" ({d.busiest_block})" if d.busiest_block else ""
            parts.append(f"{d.day_name[:3]}: {d.event_count} events{block}")
    summary = ", ".join(parts)

    # Key events (interviews, appointments, deadlines — anything with location or notes)
    key_events = []
    for d in days:
        for e in d.events:
            if e.all_day or e.calendar in SKIP_CALENDARS:
                continue
            if e.location or e.notes or any(
                kw in e.summary.lower()
                for kw in ("interview", "appointment", "deadline", "dentist", "doctor", "call", "meeting")
            ):
                key_events.append({
                    "day": d.day_name,
                    "time": e.start.strftime("%-I:%M %p"),
                    "summary": e.summary,
                    "location": e.location,
                })

    return WeekView(
        days=days,
        summary=summary,
        key_events=key_events[:15],
        total_free_hours=total_free,
        busiest_day=max_events[1],
        lightest_day=min_events[1],
    )
