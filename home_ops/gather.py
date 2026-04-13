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


async def _osascript(script: str, timeout: int = 20) -> tuple[str, str]:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode(errors="replace").strip(), stderr.decode(errors="replace").strip()
    except asyncio.TimeoutError:
        proc.kill()
        return "", "TIMEOUT"


async def _ensure_app_running(app_name: str, app_path: str) -> None:
    """pgrep + open -gj pattern. Prevents AppleEvents -600 silent failures."""
    proc = await asyncio.create_subprocess_exec(
        "pgrep", "-xq", app_name,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    if await proc.wait() == 0:
        return
    proc = await asyncio.create_subprocess_exec(
        "open", "-gj", app_path,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    await asyncio.sleep(2)


# --- Calendar (7 days) ---

async def gather_calendar_7d() -> list[dict]:
    """Pull 10 days of events. Bucket as today / tomorrow / week_ahead.

    Window is today 00:00 through (today + 10 days) 23:59:59 — catches
    'next Tuesday'/'next Monday' style items the model should flag for prep.
    The previous 8-day midnight end silently dropped day-10 events (user hit
    this 2026-04-13 when next Tuesday's baseball practice vanished).
    """
    now = datetime.now()
    start = datetime.combine(now.date(), datetime.min.time())
    end = start + timedelta(days=11) - timedelta(seconds=1)
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
    NOTE: Variable names MUST NOT shadow Contacts properties (emails, phones, organization)
    or AppleScript silently returns nothing.
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
                set orgStr to ""
                try
                    set orgStr to organization of p
                    if orgStr is missing value then set orgStr to ""
                end try
                set emStr to ""
                try
                    repeat with e in (emails of p)
                        set emStr to emStr & (value of e) & ","
                    end repeat
                end try
                set phStr to ""
                try
                    repeat with x in (phones of p)
                        set phStr to phStr & (value of x) & ","
                    end repeat
                end try
                set relStr to ""
                try
                    repeat with r in (related names of p)
                        set relStr to relStr & (label of r) & "=" & (value of r) & ","
                    end repeat
                end try
                set bdayStr to ""
                try
                    set b to birth date of p
                    if b is not missing value then set bdayStr to (b as string)
                end try
                set output to output & nm & FS & orgStr & FS & emStr & FS & phStr & FS & relStr & FS & bdayStr & RS
            end try
        end repeat
        return output
    end tell
    '''
    await _ensure_app_running("Contacts", "/System/Applications/Contacts.app")
    raw, err = await _osascript(script, timeout=180)
    if not raw:
        if err:
            return [{"_error": f"osascript: {err[:200]}"}]
        return []
    out = []
    for entry in raw.split("\x1e"):
        # NOTE: do NOT use entry.strip() — Python treats \x1c-\x1f as whitespace
        # and would eat trailing unit separators, collapsing 6-field records to 4.
        entry = entry.strip(" \t\n\r")
        if not entry:
            continue
        parts = entry.split("\x1f")
        if len(parts) < 6 or not parts[0].strip(" \t\n\r"):
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
            "relations": [_clean_relation(r) for r in rels.strip(",").split(",") if r],
            "birthday": bday.strip() or None,
        })
    return out


# --- iMessages (all threads, 7 days) ---

def _extract_attributed_body(blob: bytes) -> str:
    """Extract plain text from a chat.db attributedBody NSKeyedArchiver blob.
    Modern macOS stores most iMessage text here when m.text is NULL.
    Strategy: find NSString marker, skip the 1-byte length prefix that follows,
    then read printable run until next typedstream control byte.
    """
    if not blob:
        return ""
    try:
        idx = blob.find(b"NSString")
        if idx == -1:
            return ""
        # After "NSString" there's a class-version byte, then the string token:
        #   0x01 0x2b (short, len < 0xff)  → followed by 1-byte length, then UTF-8 bytes
        #   0x00 0x81 (long)               → followed by 2-byte LE length, then bytes
        sub = blob[idx + 8:]
        # Walk forward to first non-control byte after possible length markers
        # Skip up to 5 leading control bytes, then read printable run
        i = 0
        while i < 6 and i < len(sub) and (sub[i] < 32 or sub[i] == 0x2b):
            i += 1
        out = []
        for b in sub[i:i + 2000]:
            if 32 <= b < 127 or b in (9, 10, 13) or b >= 0x80:
                out.append(b)
            else:
                if out:
                    break
        return bytes(out).decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _clean_relation(rel: str) -> str:
    """'_$!<Spouse>!$_=Ashley Cornelius' → 'Spouse=Ashley Cornelius'."""
    if "=" not in rel:
        return rel
    label, _, value = rel.partition("=")
    label = label.replace("_$!<", "").replace(">!$_", "")
    return f"{label}={value}"


def _decode_mime_header(s: str) -> str:
    """Decode MIME-encoded headers like '=?UTF-8?B?...?='"""
    if not s or "=?" not in s:
        return s
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def _read_chat_db_direct(days: int = 7) -> list[dict]:
    """Direct sqlite3 read of chat.db. Works when caller has FDA (Terminal/tmux).
    Returns list of {ts, from_me, sender, chat_id, text}.
    Falls back to attributedBody blob extraction when m.text is NULL (macOS 13+).
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
        m.attributedBody,
        m.cache_has_attachments
    FROM message m
    LEFT JOIN handle h ON m.handle_id = h.ROWID
    LEFT JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
    LEFT JOIN chat c ON cmj.chat_id = c.ROWID
    WHERE m.date > (CAST(strftime('%s', 'now', ?) AS INTEGER) - 978307200) * 1000000000
    ORDER BY m.date DESC
    LIMIT 1500
    """
    out = []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        for row in conn.execute(sql, (f"-{days} days",)):
            text = row["text"] or ""
            if not text and row["attributedBody"]:
                text = _extract_attributed_body(row["attributedBody"])
            text = text.strip()
            if not text:
                continue
            out.append({
                "ts": row["ts"],
                "from_me": bool(row["is_from_me"]),
                "sender": row["sender_id"] or "",
                "chat": row["chat_identifier"] or "",
                "chat_name": row["display_name"] or "",
                "text": text[:600],
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
    """Query mailtriage.db on VPS over SSH. Returns last N days of inbox messages.
    Uses sqlite3 -json + base64-piped SQL to dodge nested-quote hell.
    """
    import base64
    sql = (
        "SELECT date_received, from_name, from_addr, subject, "
        "category, urgency_score, substr(body_preview,1,400) AS preview "
        "FROM messages "
        f"WHERE date_received > datetime('now','-{days} days') "
        "ORDER BY date_received DESC LIMIT 200;"
    )
    sql_b64 = base64.b64encode(sql.encode()).decode()
    cmd = (
        f"ssh -o ConnectTimeout=10 {VPS_SSH} "
        f"\"echo {sql_b64} | base64 -d | sqlite3 -json /srv/apps/mailtriage/data/mailtriage.db\""
    )
    raw = await _run(cmd, timeout=25)
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except Exception as e:
        return [{"_error": f"json parse: {e}", "_raw": raw[:200]}]
    out = []
    for r in rows:
        from_name = (r.get("from_name") or "").strip()
        from_addr = (r.get("from_addr") or "").strip()
        urgency = r.get("urgency_score") or 0
        out.append({
            "ts": r.get("date_received", ""),
            "from": _decode_mime_header(from_name) or from_addr,
            "from_addr": from_addr,
            "subject": _decode_mime_header((r.get("subject") or "").strip())[:120],
            "category": (r.get("category") or "unknown").strip(),
            "urgency": int(urgency) if isinstance(urgency, (int, str)) and str(urgency).isdigit() else 0,
            "preview": (r.get("preview") or "").strip()[:300],
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
