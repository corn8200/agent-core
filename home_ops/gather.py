"""Parallel data collection for home_ops.

Pulls EVERYTHING the agent might need to think about home life:
- 7-day calendar window (today + tomorrow + week ahead)
- All Apple Reminders bucketed
- Contacts (so the agent learns who Ashley/Jude/James/etc are without being told)
- Last 7 days of iMessages from chat.db (via tmux relay for FDA compliance)
- Last 7 days of mail from VPS mailtriage.db
- Weather next 24h (Pirate Weather)
- Learned facts from prior runs

Pure Python. Zero LLM tokens. Returns structured dict the synthesizer prompt eats.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.calendar import get_events, SKIP_CALENDARS
from core.constants import VPS_SSH

# --- Helpers ---

async def _run(cmd: str, timeout: int = 15) -> str:
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode(errors="replace").strip()
    except asyncio.TimeoutError:
        proc.kill()
        return ""


async def _osascript(script: str, timeout: int = 20) -> str:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode(errors="replace").strip()
    except asyncio.TimeoutError:
        proc.kill()
        return ""


# --- Calendar (7 days) ---

async def gather_calendar_7d() -> list[dict]:
    """Pull 7 days of events. Bucket as today / tomorrow / week_ahead."""
    now = datetime.now()
    start = datetime.combine(now.date(), datetime.min.time())
    end = start + timedelta(days=8)
    events = await get_events(start, end)

    today = now.date()
    tomorrow = today + timedelta(days=1)
    out = []
    for e in events:
        if e.calendar in SKIP_CALENDARS:
            continue
        d = e.start.date()
        if d == today:
            bucket = "today"
        elif d == tomorrow:
            bucket = "tomorrow"
        else:
            bucket = "week_ahead"
        out.append({
            "summary": e.summary,
            "start": e.start.isoformat(),
            "end": e.end.isoformat(),
            "day": e.start.strftime("%a %b %d"),
            "time": "all-day" if e.all_day else e.start.strftime("%-I:%M %p"),
            "calendar": e.calendar,
            "location": e.location or None,
            "notes": (e.notes or "")[:300] or None,
            "all_day": e.all_day,
            "bucket": bucket,
        })
    return out


# --- Reminders ---

async def gather_reminders_full() -> dict:
    """Use core.gather.gather_reminders — already bucketed."""
    from core.gather import gather_reminders
    return await gather_reminders()


# --- Contacts (family + key people) ---

async def gather_contacts() -> list[dict]:
    """Pull every contact with: name, related-names, emails, phones, address.
    The LLM uses related-names ('spouse', 'child', 'mother') to infer family.
    """
    script = r'''
    set FS to (ASCII character 31)
    set RS to (ASCII character 30)
    tell application "Contacts"
        set output to ""
        repeat with p in people
            try
                set nm to name of p
                if nm is missing value then set nm to ""
                set org to ""
                try
                    set org to organization of p
                    if org is missing value then set org to ""
                end try
                set emails to ""
                try
                    repeat with e in emails of p
                        set emails to emails & (value of e) & ","
                    end repeat
                end try
                set phones to ""
                try
                    repeat with ph in phones of p
                        set phones to phones & (value of ph) & ","
                    end repeat
                end try
                set rels to ""
                try
                    repeat with r in related names of p
                        set rels to rels & (label of r) & "=" & (value of r) & ","
                    end repeat
                end try
                set bday to ""
                try
                    set b to birth date of p
                    if b is not missing value then set bday to (b as string)
                end try
                set output to output & nm & FS & org & FS & emails & FS & phones & FS & rels & FS & bday & RS
            end try
        end repeat
        return output
    end tell
    '''
    raw = await _osascript(script, timeout=30)
    if not raw:
        return []
    out = []
    for entry in raw.split("\x1e"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("\x1f")
        if len(parts) < 6 or not parts[0].strip():
            continue
        name, org, emails, phones, rels, bday = parts[:6]
        # Skip noise: must have at least one of email/phone/relation
        if not (emails.strip() or phones.strip() or rels.strip()):
            continue
        out.append({
            "name": name.strip(),
            "org": org.strip() or None,
            "emails": [e for e in emails.strip(",").split(",") if e],
            "phones": [p for p in phones.strip(",").split(",") if p],
            "relations": [r for r in rels.strip(",").split(",") if r],
            "birthday": bday.strip() or None,
        })
    return out


# --- iMessages (all threads, 7 days) ---

def _read_chat_db_direct(days: int = 7) -> list[dict]:
    """Direct sqlite3 read of chat.db. Works when caller has FDA (Terminal/tmux).
    Returns list of {ts, from_me, sender, chat_id, text}.
    """
    db = Path.home() / "Library" / "Messages" / "chat.db"
    if not db.exists():
        return []
    sql = """
    SELECT
        datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime') AS ts,
        m.is_from_me,
        h.id AS sender_id,
        c.chat_identifier,
        c.display_name,
        m.text,
        m.cache_has_attachments
    FROM message m
    LEFT JOIN handle h ON m.handle_id = h.ROWID
    LEFT JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
    LEFT JOIN chat c ON cmj.chat_id = c.ROWID
    WHERE m.date/1000000000 + 978307200 > strftime('%s', 'now', ?)
      AND m.text IS NOT NULL
      AND length(m.text) > 0
    ORDER BY m.date DESC
    LIMIT 800
    """
    out = []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        for row in conn.execute(sql, (f"-{days} days",)):
            out.append({
                "ts": row["ts"],
                "from_me": bool(row["is_from_me"]),
                "sender": row["sender_id"] or "",
                "chat": row["chat_identifier"] or "",
                "chat_name": row["display_name"] or "",
                "text": (row["text"] or "")[:600],
            })
        conn.close()
    except Exception as e:
        return [{"_error": str(e)}]
    return out


async def gather_imessages(days: int = 7) -> list[dict]:
    """Run chat.db read in a thread (sqlite3 is sync)."""
    return await asyncio.to_thread(_read_chat_db_direct, days)


# --- Mail (VPS mailtriage.db, 7 days, real-human filter) ---

async def gather_mail_recent(days: int = 7) -> list[dict]:
    """Query mailtriage.db on VPS over SSH. Returns last N days of inbox messages."""
    sql = (
        "SELECT date_received, from_name, from_addr, subject, "
        "category, urgency_score, substr(body_preview,1,400) "
        "FROM messages "
        f"WHERE date_received > datetime('now','-{days} days') "
        "ORDER BY date_received DESC LIMIT 200"
    )
    cmd = (
        f"ssh -o ConnectTimeout=10 {VPS_SSH} "
        f"\"sqlite3 -separator '\\x1f' /srv/apps/mailtriage/data/mailtriage.db \\\"{sql}\\\"\""
    )
    raw = await _run(cmd, timeout=20)
    if not raw:
        return []
    out = []
    for line in raw.split("\n"):
        parts = line.split("\x1f")
        if len(parts) < 7:
            continue
        ts, from_name, from_addr, subject, category, urgency, preview = parts[:7]
        out.append({
            "ts": ts,
            "from": (from_name or from_addr).strip(),
            "from_addr": from_addr.strip(),
            "subject": subject.strip()[:120],
            "category": category.strip() or "unknown",
            "urgency": int(urgency) if urgency.isdigit() else 0,
            "preview": preview.strip()[:300],
        })
    return out


# --- Weather (Pirate Weather, next 24h) ---

async def gather_weather() -> dict:
    """Pirate Weather forecast for Harpers Ferry, next 24h."""
    key = os.environ.get("PIRATE_WEATHER_API_KEY", "")
    if not key:
        # try secrets file
        try:
            for line in Path.home().joinpath(".config/secrets.env").read_text().splitlines():
                if line.startswith("PIRATE_WEATHER_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except Exception:
            pass
    if not key:
        return {"error": "no API key"}

    # Harpers Ferry
    lat, lon = 39.3237, -77.7386
    url = f"https://api.pirateweather.net/forecast/{key}/{lat},{lon}?units=us&exclude=minutely"
    raw = await _run(f"curl -sf --max-time 10 '{url}'", timeout=12)
    if not raw:
        return {"error": "fetch failed"}
    try:
        d = json.loads(raw)
    except Exception:
        return {"error": "parse failed"}

    out = {
        "currently": d.get("currently", {}).get("summary", ""),
        "temp": d.get("currently", {}).get("temperature"),
        "next_24h": d.get("hourly", {}).get("summary", ""),
        "alerts": [a.get("title", "") for a in d.get("alerts", [])],
    }
    # Hourly slice for the next 24h
    hourly = d.get("hourly", {}).get("data", [])[:24]
    out["hours"] = [
        {
            "ts": datetime.fromtimestamp(h["time"]).strftime("%a %-I%p"),
            "temp": h.get("temperature"),
            "summary": h.get("summary"),
            "precip": h.get("precipProbability"),
        }
        for h in hourly
    ]
    return out


# --- Top-level gather ---

async def gather_all() -> dict:
    """Run every collector in parallel. Return one dict the prompt can eat."""
    started = datetime.now()
    cal, rem, contacts, msgs, mail, wx = await asyncio.gather(
        gather_calendar_7d(),
        gather_reminders_full(),
        gather_contacts(),
        gather_imessages(7),
        gather_mail_recent(7),
        gather_weather(),
        return_exceptions=True,
    )

    def _ok(x, default):
        return default if isinstance(x, BaseException) else x

    return {
        "now": started.isoformat(),
        "today": started.strftime("%A, %B %d, %Y"),
        "tomorrow": (started + timedelta(days=1)).strftime("%A, %B %d"),
        "calendar_7d": _ok(cal, []),
        "reminders": _ok(rem, {}),
        "contacts": _ok(contacts, []),
        "imessages_7d": _ok(msgs, []),
        "mail_7d": _ok(mail, []),
        "weather": _ok(wx, {}),
        "errors": {
            "calendar": str(cal) if isinstance(cal, BaseException) else None,
            "reminders": str(rem) if isinstance(rem, BaseException) else None,
            "contacts": str(contacts) if isinstance(contacts, BaseException) else None,
            "imessages": str(msgs) if isinstance(msgs, BaseException) else None,
            "mail": str(mail) if isinstance(mail, BaseException) else None,
            "weather": str(wx) if isinstance(wx, BaseException) else None,
        },
    }


if __name__ == "__main__":
    async def _main():
        data = await gather_all()
        # Print summary
        print(f"calendar_7d: {len(data['calendar_7d'])}")
        print(f"reminders: overdue={len(data['reminders'].get('overdue',[]))} today={len(data['reminders'].get('today',[]))} this_week={len(data['reminders'].get('this_week',[]))}")
        print(f"contacts: {len(data['contacts'])}")
        print(f"imessages_7d: {len(data['imessages_7d'])}")
        print(f"mail_7d: {len(data['mail_7d'])}")
        print(f"weather: {data['weather'].get('currently','?')}")
        print(f"errors: {[k for k,v in data['errors'].items() if v]}")
        Path("/tmp/home-ops-gather.json").write_text(json.dumps(data, indent=2, default=str))
        print("→ /tmp/home-ops-gather.json")

    asyncio.run(_main())
