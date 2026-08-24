"""Ambient Context System.

Hourly snapshot of home/life state that other agents can inject into prompts
without paying the gather_all() cost every call. Wraps home_ops.gather.gather_all()
and adds metadata (generated_at, ttl_minutes, version).

Two consumers:
- load_ambient() → full dict (or None if stale/missing)
- load_ambient_text() → ≤500 char paragraph for prompt injection

Freshness: file >70 min old is considered stale. LaunchAgent refreshes hourly.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from home_ops.gather import gather_all

AMBIENT_PATH = Path("/tmp/ambient-context.json")
TTL_MINUTES = 60
STALE_MINUTES = 70
VERSION = 1


async def refresh() -> dict:
    """Run gather_all() (force=True) and write ambient context with metadata."""
    data = await gather_all(force=True)
    data["_ambient"] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ttl_minutes": TTL_MINUTES,
        "version": VERSION,
    }
    AMBIENT_PATH.write_text(json.dumps(data, indent=2, default=str))
    return data


def load_ambient() -> dict | None:
    """Read the ambient context JSON. Returns None if missing or stale (>70 min)."""
    if not AMBIENT_PATH.exists():
        return None
    try:
        data = json.loads(AMBIENT_PATH.read_text())
    except Exception:
        return None

    generated_at = data.get("_ambient", {}).get("generated_at")
    if not generated_at:
        return None
    try:
        gen_dt = datetime.fromisoformat(generated_at)
    except ValueError:
        return None
    if gen_dt.tzinfo is None:
        gen_dt = gen_dt.replace(tzinfo=timezone.utc)

    age_minutes = (datetime.now(timezone.utc) - gen_dt).total_seconds() / 60
    if age_minutes > STALE_MINUTES:
        return None
    return data


def load_ambient_text() -> str:
    """Compact ≤500 char paragraph for prompt injection. Empty string if no data."""
    data = load_ambient()
    if not data:
        return ""

    now = datetime.now()
    as_of = now.strftime("%-I:%M%p").lower()

    parts: list[str] = [f"As of {as_of}:"]

    cal = data.get("calendar_7d", []) or []
    today_events = [e for e in cal if e.get("bucket") == "today"]
    if today_events:
        pieces = []
        for e in today_events[:3]:
            summary = (e.get("summary") or "").strip()
            t = (e.get("time") or "").strip()
            if summary and t:
                pieces.append(f"{summary.lower()} {t.lower()}")
            elif summary:
                pieces.append(summary.lower())
        sample = ", ".join(pieces)
        parts.append(f"{len(today_events)} calendar events today ({sample}).")
    else:
        parts.append("no calendar events today.")

    rem = data.get("reminders", {}) or {}
    overdue = len(rem.get("overdue", []) or [])
    due_today = len(rem.get("today", []) or [])
    if overdue and due_today:
        parts.append(f"{overdue} overdue + {due_today} due today.")
    elif overdue:
        parts.append(f"{overdue} overdue reminder{'s' if overdue != 1 else ''}.")
    elif due_today:
        parts.append(f"{due_today} reminder{'s' if due_today != 1 else ''} due today.")

    pi = data.get("pi", {}) or {}
    pi_ok = isinstance(pi, dict) and "error" not in pi and bool(pi.get("uptime"))
    parts.append(f"VPS retired, Pi {'OK' if pi_ok else 'DOWN'}.")

    wx = (data.get("weather", {}) or {}).get("pirate", {}) or {}
    temp = wx.get("temp")
    currently = (wx.get("currently") or "").strip()
    if temp is not None and currently:
        wx_str = f"Weather: {round(temp)}F {currently.lower()}"
        hours = wx.get("hours", []) or []
        afternoon_precip = [
            h.get("precip", 0) for h in hours[:8]
            if isinstance(h.get("precip"), (int, float))
        ]
        if afternoon_precip:
            peak = max(afternoon_precip)
            if peak >= 0.15:
                wx_str += f", {int(peak * 100)}% rain next 8h"
        parts.append(wx_str + ".")

    unread = (data.get("apple", {}) or {}).get("mail_unread", -1)
    if isinstance(unread, int) and unread >= 0:
        parts.append(f"{unread} unread email{'s' if unread != 1 else ''}.")

    text = " ".join(parts)
    if len(text) > 500:
        text = text[:497] + "..."
    return text


async def _main() -> None:
    data = await refresh()
    print(f"ambient written: {AMBIENT_PATH}")
    print(f"keys: {sorted(k for k in data.keys() if not k.startswith('_'))}")
    print(f"generated_at: {data['_ambient']['generated_at']}")
    print(f"text: {load_ambient_text()}")


if __name__ == "__main__":
    asyncio.run(_main())
