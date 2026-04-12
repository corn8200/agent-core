"""Parallel data gathering from all systems. Pure Python — no LLM tokens."""

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from core.constants import GATHER_CACHE, GATHER_TTL_SECONDS, VPS_SSH, PI_IP, PI_SSH_USER


async def _run(cmd: str) -> str:
    """Run a shell command and return stdout."""
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    return stdout.decode().strip()


async def _run_osascript(script: str) -> str:
    """Run an AppleScript snippet."""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode().strip()


# --- Apple Data ---

async def gather_calendar() -> list[dict]:
    """Get today's + tomorrow's calendar events from Apple Calendar.

    Returns structured list of {summary, start, calendar, bucket} sorted by start.
    bucket is one of: 'today', 'tomorrow'. Uses \\x1f as the inter-field
    delimiter to avoid comma-in-date shredding.
    """
    # ASCII unit separator (0x1F) between fields, record separator (0x1E) between events.
    script = r'''
    set FS to (ASCII character 31)
    set RS to (ASCII character 30)
    tell application "Calendar"
        set today to current date
        set time of today to 0
        set endDate to today + (2 * days)
        set output to ""
        repeat with cal in calendars
            try
                set evts to (every event of cal whose start date >= today and start date < endDate)
                repeat with e in evts
                    set sd to start date of e
                    set ed to end date of e
                    set output to output & (summary of e) & FS & (sd as string) & FS & (ed as string) & FS & (name of cal) & RS
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
        today_str = datetime.now().strftime("%A, %B %-d, %Y")
        tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%A, %B %-d, %Y")
        for raw in result.split("\x1e"):
            raw = raw.strip()
            if not raw:
                continue
            parts = raw.split("\x1f")
            if len(parts) < 4:
                continue
            summary, start, end, cal_name = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()
            # Bucket
            if today_str in start:
                bucket = "today"
            elif tomorrow_str in start:
                bucket = "tomorrow"
            else:
                bucket = "other"
            events.append({
                "summary": summary,
                "start": start,
                "end": end,
                "calendar": cal_name,
                "bucket": bucket,
            })
        # Sort: today first, then tomorrow, by start time string (lexicographic is fine for same-day)
        bucket_order = {"today": 0, "tomorrow": 1, "other": 2}
        events.sort(key=lambda e: (bucket_order.get(e["bucket"], 9), e["start"]))
        return events
    except Exception as e:
        return [{"error": str(e)}]


async def gather_reminders() -> dict:
    """Get incomplete reminders from Apple Reminders, bucketed by urgency.

    Returns:
        {
            "count": int,
            "overdue": [...],       # due date in the past
            "today": [...],         # due today
            "this_week": [...],     # due in the next 7 days
            "later": [...],         # due later
            "undated": [...],       # no due date
        }
    Each item is {name, list, due} where due is a string or None.
    """
    # Use AppleScript to get name | list | due date (ISO-ish) for each incomplete reminder.
    # Skip items that look like decorative headers (emoji-only or emoji-prefixed section markers).
    script = r'''
    set FS to (ASCII character 31)
    set RS to (ASCII character 30)
    tell application "Reminders"
        set output to ""
        repeat with reminderList in lists
            try
                set incompleteReminders to (every reminder of reminderList whose completed is false)
                repeat with r in incompleteReminders
                    set rName to name of r
                    set listName to name of reminderList
                    set dueStr to ""
                    try
                        set d to due date of r
                        if d is not missing value then set dueStr to (d as string)
                    end try
                    set output to output & rName & FS & listName & FS & dueStr & RS
                end repeat
            end try
        end repeat
        return output
    end tell
    '''
    try:
        import re
        from datetime import datetime, timedelta
        result = await _run_osascript(script)
        if not result:
            return {"count": 0, "overdue": [], "today": [], "this_week": [], "later": [], "undated": []}

        now = datetime.now()
        today_date = now.date()
        week_end = today_date + timedelta(days=7)
        # Day-name -> ISO weekday map not needed; we'll parse the date string.

        buckets = {"overdue": [], "today": [], "this_week": [], "later": [], "undated": []}

        def parse_apple_date(s: str):
            """Parse AppleScript date string like 'Sunday, April 5, 2026 at 18:00:00'."""
            if not s:
                return None
            # Strip day-of-week prefix
            s2 = re.sub(r"^\w+,\s*", "", s)
            for fmt in (
                "%B %d, %Y at %H:%M:%S",
                "%B %d, %Y at %I:%M:%S %p",
                "%B %d, %Y",
            ):
                try:
                    return datetime.strptime(s2, fmt)
                except ValueError:
                    continue
            return None

        total = 0
        for raw in result.split("\x1e"):
            raw = raw.strip("\r\n ")
            if not raw:
                continue
            parts = raw.split("\x1f")
            if len(parts) < 2:
                continue
            name = parts[0].strip()
            list_name = parts[1].strip() if len(parts) > 1 else ""
            due_raw = parts[2].strip() if len(parts) > 2 else ""

            # Skip decorative header-style entries (start with emoji 📅/📋/🔖 etc and no real action text)
            if name and name[0] in "📅📋🔖🗓️📌🔹▪️•":
                continue
            # Skip empty names
            if not name:
                continue

            total += 1
            due_dt = parse_apple_date(due_raw) if due_raw else None
            entry = {"name": name, "list": list_name, "due": due_raw or None}
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

        return {
            "count": total,
            "overdue": buckets["overdue"],
            "today": buckets["today"],
            "this_week": buckets["this_week"],
            "later": buckets["later"][:10],
            "undated": buckets["undated"][:15],
        }
    except Exception as e:
        return {"count": 0, "error": str(e), "overdue": [], "today": [], "this_week": [], "later": [], "undated": []}


async def gather_mail_unread() -> int:
    """Count unread messages in Mail.app."""
    script = 'tell application "Mail" to return unread count of inbox'
    try:
        result = await _run_osascript(script)
        return int(result) if result.isdigit() else 0
    except Exception:
        return -1


# --- Schedule (unified calendar + reminders + free slots) ---

async def gather_schedule() -> dict:
    """Rich schedule view for briefs, agents, and router context injection."""
    try:
        from core.calendar import get_schedule_view, get_week_view
        view, week = await asyncio.gather(
            get_schedule_view(),
            get_week_view(),
        )
        return {
            "today": [e.to_dict() for e in view.events_today],
            "tomorrow": [e.to_dict() for e in view.events_tomorrow],
            "free_slots_today": [s.to_dict() for s in view.free_slots_today],
            "next_event": view.next_event.to_dict() if view.next_event else None,
            "minutes_until_next": view.minutes_until_next,
            "current_status": view.current_status,
            "reminders": view.reminders,
            "week_summary": week.summary,
            "week_key_events": week.key_events,
            "week_total_free_hours": week.total_free_hours,
            "week_busiest_day": week.busiest_day,
            "week_lightest_day": week.lightest_day,
        }
    except Exception as e:
        return {"error": str(e)}


# --- VPS Data ---

async def gather_vps_full() -> dict:
    """Comprehensive VPS data via script file. Avoids quote-mangling in SSH."""
    script_path = Path(__file__).parent / "vps_gather.sh"
    try:
        # SCP script to VPS, run it, get output
        proc = await asyncio.create_subprocess_exec(
            "scp", str(script_path), f"{VPS_SSH}:/tmp/agent_gather.sh",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        result = await _run(f"ssh -o ConnectTimeout=10 {VPS_SSH} 'bash /tmp/agent_gather.sh'")
        return {"raw": result[:5000]}
    except Exception as e:
        return {"error": str(e)}


# --- External ---

async def gather_weather() -> str:
    """Get Harpers Ferry weather from wttr.in."""
    try:
        result = await _run("curl -s 'wttr.in/Harpers+Ferry+WV?format=3' 2>/dev/null")
        return result.strip()[:200]
    except Exception:
        return "weather unavailable"


# --- Pi ---

async def gather_pi_health() -> dict:
    """Basic Pi health."""
    try:
        uptime = await _run(f"ssh -o ConnectTimeout=10 {PI_SSH_USER}@{PI_IP} uptime")
        docker = await _run(
            f"ssh -o ConnectTimeout=10 {PI_SSH_USER}@{PI_IP} 'cd /opt/home-stack && docker compose ps --format json 2>/dev/null | head -20'"
        )
        return {"uptime": uptime.strip(), "docker": docker.strip()[:500]}
    except Exception as e:
        return {"error": str(e)}


# --- Mac ---

async def gather_mac_health() -> dict:
    """Local Mac Mini health."""
    try:
        uptime = await _run("uptime")
        disk = await _run("df -h / | tail -1 | awk '{print $5}'")
        return {"uptime": uptime.strip(), "disk_pct": disk.strip()}
    except Exception as e:
        return {"error": str(e)}


# --- Orchestrator ---

async def gather_all(force: bool = False) -> dict:
    """
    Gather data from all systems in parallel.
    Returns cached data if fresh (< GATHER_TTL_SECONDS old) unless force=True.
    """
    # Check cache
    if not force and GATHER_CACHE.exists():
        mtime = GATHER_CACHE.stat().st_mtime
        if time.time() - mtime < GATHER_TTL_SECONDS:
            return json.loads(GATHER_CACHE.read_text())

    # Parallel gather
    (
        calendar, reminders, mail_unread, schedule,
        vps_full, weather, weather_hf, weather_fred,
        pi_health, mac_health,
    ) = await asyncio.gather(
        gather_calendar(),
        gather_reminders(),
        gather_mail_unread(),
        gather_schedule(),
        gather_vps_full(),
        gather_weather(),
        _run("curl -s 'wttr.in/Harpers+Ferry+WV?format=%l:+%c+%t+%h+humidity,+wind+%w.+Feels+like+%f.' 2>/dev/null"),
        _run("curl -s 'wttr.in/Frederick+MD?format=%l:+%c+%t+%h+humidity,+wind+%w.+Feels+like+%f.' 2>/dev/null"),
        gather_pi_health(),
        gather_mac_health(),
    )

    data = {
        "timestamp": datetime.now().isoformat(),
        "apple": {
            "calendar": calendar,
            "reminders": reminders,
            "mail_unread": mail_unread,
        },
        "schedule": schedule,
        "vps": vps_full,
        "pi": pi_health,
        "mac": mac_health,
        "weather": {
            "summary": weather,
            "harpers_ferry": weather_hf,
            "frederick": weather_fred,
        },
    }

    # Write cache
    GATHER_CACHE.write_text(json.dumps(data, indent=2, default=str))
    return data
