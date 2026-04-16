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
from contextlib import closing
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.calendar_service import get_events
from core.constants import (
    GATHER_CACHE,
    GATHER_TTL_SECONDS,
    PI_IP,
    PI_SSH_USER,
    VPS_SSH,
)

import time

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


# --- Reminders (canonical, bucketed) ---

async def gather_reminders() -> dict:
    """Get incomplete reminders bucketed by urgency.

    Uses a Swift/EventKit helper (`core/reminders_fetch.swift`) because the
    AppleScript path (`tell application "Reminders" to get every reminder
    whose completed is false`) crashes Reminders.app under macOS 26.4.1 with
    a Swift runtime assertion during NSScriptCommand property evaluation
    (crash: Reminders-2026-04-14-203044.ips). EventKit talks directly to the
    reminders store — no AppleEvents, no Reminders.app launch.

    Returns:
        {
            "count": int,
            "overdue": [...],
            "today": [...],
            "this_week": [...],
            "later": [...],
            "undated": [...],
        }
    Each item is {name, list, due} where due is an ISO string or None.
    """
    empty = {"count": 0, "overdue": [], "today": [], "this_week": [], "later": [], "undated": []}
    script_path = Path(__file__).resolve().parent.parent / "core" / "reminders_fetch.swift"
    if not script_path.exists():
        return {**empty, "error": f"missing {script_path}"}

    try:
        proc = await asyncio.create_subprocess_exec(
            "swift", str(script_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            return {**empty, "error": "swift reminders_fetch timeout"}
        if proc.returncode != 0:
            return {**empty, "error": f"swift rc={proc.returncode}: {stderr.decode(errors='replace').strip()[:200]}"}

        raw = stdout.decode(errors="replace")
        now = datetime.now()
        today_date = now.date()
        week_end = today_date + timedelta(days=7)
        buckets = {"overdue": [], "today": [], "this_week": [], "later": [], "undated": []}
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

            if not name:
                continue
            if name[0] in "📅📋🔖🗓📌🔹▪•🔮⭐✨":
                continue

            total += 1
            due_dt = None
            if due_iso:
                try:
                    due_dt = datetime.fromisoformat(due_iso.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
                except ValueError:
                    due_dt = None

            entry = {"name": name, "list": list_name, "due": due_iso or None}
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
        return {**empty, "error": str(e)}


async def gather_reminders_full() -> dict:
    """Back-compat alias; use gather_reminders()."""
    return await gather_reminders()


# --- Mac / VPS / Pi infra ---

async def gather_mail_unread() -> int:
    """Count unread messages in Mail.app."""
    script = 'tell application "Mail" to return unread count of inbox'
    try:
        result, _ = await _osascript(script, timeout=15)
        return int(result) if result.isdigit() else 0
    except Exception:
        return -1


async def gather_vps_full() -> dict:
    """Comprehensive VPS data via script file. Also pulls auth-watcher state in the same SSH session."""
    script_path = Path(__file__).resolve().parent.parent / "core" / "vps_gather.sh"
    try:
        proc = await asyncio.create_subprocess_exec(
            "scp", str(script_path), f"{VPS_SSH}:/tmp/agent_gather.sh",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        # Single SSH call: run gather script + auth-watcher state. Delimiter for split.
        combined_cmd = (
            f"ssh -o ConnectTimeout=10 {VPS_SSH} "
            f"'bash /tmp/agent_gather.sh; echo \"=== AUTH_WATCHER ===\"; "
            f"cat /srv/apps/auth-watcher/state.json 2>/dev/null || echo {{}}'"
        )
        result = await _run(combined_cmd, timeout=30)
        raw, _, auth_raw = result.partition("=== AUTH_WATCHER ===")
        auth_state = {}
        try:
            auth_state = json.loads(auth_raw.strip()) if auth_raw.strip() else {}
        except Exception:
            auth_state = {}
        return {"raw": raw[:5000], "auth": auth_state}
    except Exception as e:
        return {"error": str(e)}


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


async def gather_mac_health() -> dict:
    """Local Mac Mini health."""
    try:
        uptime = await _run("uptime")
        disk = await _run("df -h / | tail -1 | awk '{print $5}'")
        return {"uptime": uptime.strip(), "disk_pct": disk.strip()}
    except Exception as e:
        return {"error": str(e)}


# --- Simple calendar (today + tomorrow only, used by core/handler consumers) ---

async def gather_calendar_simple() -> list[dict]:
    """Light 'today + tomorrow' calendar view. Wraps gather_calendar_7d and filters."""
    full = await gather_calendar_7d()
    return [e for e in full if e.get("bucket") in ("today", "tomorrow")]


# --- wttr.in weather (consolidated single call) ---

async def gather_wttr_summary() -> dict:
    """Single wttr.in call, parsed into all fields previously fetched separately."""
    fmt = "%l:+%c+%t+%h+humidity,+wind+%w.+Feels+like+%f."
    try:
        hf = await _run(f"curl -s 'wttr.in/Harpers+Ferry+WV?format={fmt}' 2>/dev/null", timeout=10)
        return {"harpers_ferry": hf.strip()[:300], "summary": hf.strip()[:200]}
    except Exception:
        return {"harpers_ferry": "", "summary": "weather unavailable"}


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
        # Skip noise: must have at least one of email/phone/relation/birthday
        if not (emails.strip() or phones.strip() or rels.strip() or bday.strip()):
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

    Modern macOS stores most iMessage text here when m.text is NULL. The blob
    is a typedstream (NSArchiver, not NSKeyedArchiver despite the column name).
    The text lives after an NSString class descriptor; we look for the reliable
    three-byte sequence `84 01 2B` where `2B` is the typedstream type code for
    a C string (+), followed by a length prefix in one of three forms:
        short:    one byte < 0x81 = length
        0x81 XY:  little-endian uint16 length
        0x82 WXYZ: little-endian uint32 length
    then exactly `length` UTF-8 bytes.
    """
    if not blob:
        return ""
    try:
        start = blob.find(b"NSString")
        if start == -1:
            return ""
        marker = blob.find(b"\x84\x01\x2b", start)
        if marker == -1:
            return ""
        p = marker + 3
        if p >= len(blob):
            return ""
        ln = blob[p]
        if ln == 0x81:
            if p + 3 > len(blob):
                return ""
            length = int.from_bytes(blob[p + 1:p + 3], "little")
            p += 3
        elif ln == 0x82:
            if p + 5 > len(blob):
                return ""
            length = int.from_bytes(blob[p + 1:p + 5], "little")
            p += 5
        else:
            length = ln
            p += 1
        if length <= 0 or p + length > len(blob):
            return ""
        return blob[p:p + length].decode("utf-8", errors="replace").strip()
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
        with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)) as conn:
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
        "AND (urgency_score >= 3 OR category IN ('action','personal')) "
        "ORDER BY date_received DESC LIMIT 30;"
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

async def gather_all(force: bool = False) -> dict:
    """Canonical gather for both home_ops briefs and infra handler.

    Returns a unified dict with BOTH home_ops keys (calendar_7d, imessages_7d,
    mail_7d, contacts, weather.pirate) AND legacy core-shaped keys
    (apple.mail_unread, apple.reminders, apple.calendar, vps.raw, vps.auth,
    mac, pi). Cached at GATHER_CACHE for GATHER_TTL_SECONDS.
    """
    if not force and GATHER_CACHE.exists():
        mtime = GATHER_CACHE.stat().st_mtime
        if time.time() - mtime < GATHER_TTL_SECONDS:
            try:
                return json.loads(GATHER_CACHE.read_text())
            except Exception:
                pass

    started = datetime.now()
    (
        cal_7d, rem, contacts, msgs, mail_recent, pirate_wx,
        mail_unread, vps_full, mac_health, pi_health, wttr_wx,
    ) = await asyncio.gather(
        gather_calendar_7d(),
        gather_reminders(),
        gather_contacts(),
        gather_imessages(7),
        gather_mail_recent(7),
        gather_weather(),
        gather_mail_unread(),
        gather_vps_full(),
        gather_mac_health(),
        gather_pi_health(),
        gather_wttr_summary(),
        return_exceptions=True,
    )

    def _ok(x, default):
        return default if isinstance(x, BaseException) else x

    cal_7d = _ok(cal_7d, [])
    rem = _ok(rem, {})
    mail_unread = _ok(mail_unread, -1)
    vps_full = _ok(vps_full, {})
    wttr = _ok(wttr_wx, {})

    calendar_today_tomorrow = [e for e in cal_7d if e.get("bucket") in ("today", "tomorrow")]

    data = {
        "timestamp": started.isoformat(),
        "now": started.isoformat(),
        "today": started.strftime("%A, %B %d, %Y"),
        "tomorrow": (started + timedelta(days=1)).strftime("%A, %B %d"),
        "calendar_7d": cal_7d,
        "reminders": rem,
        "contacts": _ok(contacts, []),
        "imessages_7d": _ok(msgs, []),
        "mail_7d": _ok(mail_recent, []),
        "apple": {
            "calendar": calendar_today_tomorrow,
            "reminders": rem,
            "mail_unread": mail_unread,
        },
        "vps": vps_full,
        "vps_auth": vps_full.get("auth", {}) if isinstance(vps_full, dict) else {},
        "pi": _ok(pi_health, {}),
        "mac": _ok(mac_health, {}),
        "weather": {
            "summary": wttr.get("summary", ""),
            "harpers_ferry": wttr.get("harpers_ferry", ""),
            "pirate": _ok(pirate_wx, {}),
        },
        "errors": {
            "calendar": str(cal_7d) if isinstance(cal_7d, BaseException) else None,
            "reminders": str(rem) if isinstance(rem, BaseException) else None,
            "contacts": str(contacts) if isinstance(contacts, BaseException) else None,
            "imessages": str(msgs) if isinstance(msgs, BaseException) else None,
            "mail": str(mail_recent) if isinstance(mail_recent, BaseException) else None,
            "weather": str(pirate_wx) if isinstance(pirate_wx, BaseException) else None,
        },
    }

    try:
        GATHER_CACHE.write_text(json.dumps(data, indent=2, default=str))
    except Exception:
        pass
    return data


async def quick_gather(force: bool = False) -> dict:
    """Lean gather for handler quick_check: mail_unread, reminders, today's calendar.

    Skips weather, VPS SSH, Pi SSH, contacts, imessages, mail DB — the slow and
    network-bound collectors. Reuses GATHER_CACHE if fresh.
    """
    if not force and GATHER_CACHE.exists():
        mtime = GATHER_CACHE.stat().st_mtime
        if time.time() - mtime < GATHER_TTL_SECONDS:
            try:
                return json.loads(GATHER_CACHE.read_text())
            except Exception:
                pass

    started = datetime.now()
    mail_unread, rem, cal_7d, mac_health = await asyncio.gather(
        gather_mail_unread(),
        gather_reminders(),
        gather_calendar_7d(),
        gather_mac_health(),
        return_exceptions=True,
    )

    def _ok(x, default):
        return default if isinstance(x, BaseException) else x

    cal_7d = _ok(cal_7d, [])
    calendar_today_tomorrow = [e for e in cal_7d if e.get("bucket") in ("today", "tomorrow")]

    return {
        "timestamp": started.isoformat(),
        "apple": {
            "calendar": calendar_today_tomorrow,
            "reminders": _ok(rem, {}),
            "mail_unread": _ok(mail_unread, -1),
        },
        "mac": _ok(mac_health, {}),
        "vps": {},
        "pi": {},
        "weather": {},
    }


if __name__ == "__main__":
    async def _main():
        data = await gather_all(force=True)
        print(f"calendar_7d: {len(data['calendar_7d'])}")
        rem = data.get("reminders", {})
        print(f"reminders: overdue={len(rem.get('overdue',[]))} today={len(rem.get('today',[]))} this_week={len(rem.get('this_week',[]))}")
        print(f"contacts: {len(data.get('contacts', []))}")
        print(f"imessages_7d: {len(data.get('imessages_7d', []))}")
        print(f"mail_7d: {len(data.get('mail_7d', []))}")
        print(f"mail_unread: {data.get('apple', {}).get('mail_unread')}")
        print(f"weather: {data.get('weather', {}).get('pirate', {}).get('currently','?')}")
        print(f"errors: {[k for k,v in data.get('errors', {}).items() if v]}")
        Path("/tmp/home-ops-gather.json").write_text(json.dumps(data, indent=2, default=str))
        print("→ /tmp/home-ops-gather.json")

    asyncio.run(_main())
