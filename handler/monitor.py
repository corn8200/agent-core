#!/usr/bin/env python3
"""Handler Agent — automated infrastructure monitoring and anomaly alerting.

Two modes:
  1. Quick check (every 30 min): Pure Python gather + anomaly detection. No LLM.
     If anomaly found → SDK diagnosis + alert.
  2. Full handler (every 4 hrs): SDK-powered briefing → HTML email.

Usage:
  python3 handler/monitor.py              # quick check (default)
  python3 handler/monitor.py --full       # full handler briefing
  python3 handler/monitor.py --dry-run    # check + print, no alerts
"""

import asyncio
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.gather import gather_all
from core.constants import HOME, PERSONAL_EMAIL, VPS_SSH

# --- Dedup State ---
STATE_FILE = Path.home() / "logs" / "handler-state.json"
EMAIL_COOLDOWN = timedelta(hours=6)  # Don't re-email same anomaly set within this window
QUIET_HOURS = (22, 7)  # 22:00-07:00 = push-only, no email


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_alert_hash": None, "last_alert_ts": None, "alert_count": 0}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def anomaly_fingerprint(anomalies: list[dict]) -> str:
    """Stable hash of anomaly set (ignores ordering)."""
    key = sorted(f"{a['severity']}|{a['source']}|{a['message']}" for a in anomalies)
    return hashlib.sha1("\n".join(key).encode()).hexdigest()[:12]


def is_quiet_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    h = now.hour
    start, end = QUIET_HOURS
    if start > end:  # wraps midnight (22 -> 7)
        return h >= start or h < end
    return start <= h < end


# --- Anomaly Detection (pure Python, 0 tokens) ---

def detect_anomalies(data: dict) -> list[dict]:
    """Check gathered data for anomalies. Returns list of {severity, source, message}."""
    anomalies = []

    # VPS services down
    vps_raw = data.get("vps", {}).get("raw", "")
    if "=== SERVICES ===" in vps_raw:
        services_block = vps_raw.split("=== SERVICES ===")[1].split("===")[0]
        for line in services_block.strip().splitlines():
            if "inactive" in line or "failed" in line:
                svc = line.split(":")[0].strip()
                anomalies.append({
                    "severity": "high",
                    "source": "vps",
                    "message": f"Service down: {svc}",
                })

    # VPS disk usage
    if "=== HEALTH ===" in vps_raw:
        health_block = vps_raw.split("=== HEALTH ===")[1].split("===")[0]
        for line in health_block.strip().splitlines():
            if "%" in line and "/" in line:
                parts = line.split()
                for p in parts:
                    if p.endswith("%"):
                        pct = int(p.rstrip("%"))
                        if pct > 85:
                            anomalies.append({
                                "severity": "high" if pct > 95 else "medium",
                                "source": "vps",
                                "message": f"VPS disk at {pct}%",
                            })

    # VPS errors in last 24h
    if "=== ERRORS ===" in vps_raw:
        errors_block = vps_raw.split("=== ERRORS ===")[1].split("===")[0].strip()
        if errors_block and len(errors_block) > 10:
            error_count = len(errors_block.splitlines())
            if error_count > 3:
                anomalies.append({
                    "severity": "medium",
                    "source": "vps",
                    "message": f"{error_count} errors in last 24h",
                })

    # Mac disk
    mac = data.get("mac", {})
    disk_pct = mac.get("disk_pct", "0%").rstrip("%")
    try:
        if int(disk_pct) > 85:
            anomalies.append({
                "severity": "medium",
                "source": "mac",
                "message": f"Mac disk at {disk_pct}%",
            })
    except ValueError:
        pass

    # Pi offline
    pi = data.get("pi", {})
    if pi.get("error"):
        anomalies.append({
            "severity": "medium",
            "source": "pi",
            "message": f"Pi unreachable: {pi['error'][:100]}",
        })

    # Hot leads (3+ clicks, recent)
    if "--- RECENT_CLICKS ---" in vps_raw:
        clicks_block = vps_raw.split("--- RECENT_CLICKS ---")[1].split("---")[0].strip()
        click_lines = [l for l in clicks_block.splitlines() if l.strip()]
        if len(click_lines) >= 3:
            anomalies.append({
                "severity": "medium",
                "source": "business",
                "message": f"{len(click_lines)} hot leads with recent clicks — follow up",
            })

    # Unread business email pileup
    mail_unread = data.get("apple", {}).get("mail_unread", 0)
    if isinstance(mail_unread, int) and mail_unread > 20:
        anomalies.append({
            "severity": "low",
            "source": "email",
            "message": f"{mail_unread} unread emails in inbox",
        })

    # Overdue reminders
    reminders = data.get("apple", {}).get("reminders", {})
    reminder_count = reminders.get("count", 0)
    if reminder_count > 10:
        anomalies.append({
            "severity": "low",
            "source": "reminders",
            "message": f"{reminder_count} incomplete reminders",
        })

    return anomalies


# --- Alert Delivery ---

async def send_pushover(title: str, message: str):
    """Send push notification."""
    proc = await asyncio.create_subprocess_exec(
        "bash", "-c",
        f'if [ -x ~/bin/notify-moshi.sh ]; then ~/bin/notify-moshi.sh "{title}" "{message}"; fi',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()


async def send_alert_email(anomalies: list[dict], data: dict):
    """Send anomaly alert email via VPS SMTP."""
    now = datetime.now()
    items = "\n".join(
        f'<li style="color:{"#f87171" if a["severity"]=="high" else "#fbbf24" if a["severity"]=="medium" else "#e5e7eb"}">'
        f'[{a["severity"].upper()}] {a["source"]}: {a["message"]}</li>'
        for a in anomalies
    )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:system-ui;background:#1a1a2e;color:#e0e0e0;padding:20px;max-width:640px;margin:0 auto;">
<h1 style="color:#f87171;font-size:20px;">Handler Alert — {now.strftime('%H:%M')}</h1>
<ul style="font-size:14px;line-height:1.8;">{items}</ul>
<hr style="border-color:#333;">
<p style="color:#666;font-size:11px;">Generated {now.strftime('%Y-%m-%d %H:%M')} by agent-core/handler</p>
</body></html>"""

    email_script = f'''
import smtplib, os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv("/srv/apps/friday-email/.env")

msg = MIMEMultipart("alternative")
msg["Subject"] = "Handler Alert — {now.strftime('%b %d %H:%M')}"
msg["From"] = "Cornelius Family <notify@jcornelius.net>"
msg["To"] = "{PERSONAL_EMAIL}"
msg.attach(MIMEText("""{html.replace('"', '\\"')}""", "html"))

with smtplib.SMTP("smtp.gmail.com", 587) as s:
    s.starttls()
    s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
    s.send_message(msg)
print("sent")
'''
    script_path = Path("/tmp/handler_alert_send.py")
    script_path.write_text(email_script)

    proc = await asyncio.create_subprocess_exec(
        "scp", str(script_path), f"{VPS_SSH}:/tmp/handler_alert_send.py",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    proc = await asyncio.create_subprocess_exec(
        "ssh", VPS_SSH,
        "cd /srv/apps/friday-email && .venv/bin/python /tmp/handler_alert_send.py",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()


# --- SDK Diagnosis ---

async def diagnose_anomalies(anomalies: list[dict], data: dict) -> str:
    """Use SDK to diagnose anomalies and suggest fixes."""
    from claude_agent_sdk import query, ClaudeAgentOptions
    from core.tools import create_core_server
    from core.hooks import AGENT_HOOKS

    prompt = f"""You are a systems handler for John's infrastructure. Anomalies detected:

{json.dumps(anomalies, indent=2)}

Raw system data:
{json.dumps(data, indent=2, default=str)[:8000]}

For each anomaly:
1. What's likely wrong
2. Severity assessment (can it wait until morning?)
3. Suggested fix (specific commands if applicable)

Be concise. This goes to a push notification."""

    try:
        result = ""
        async for msg in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                model="opus",
                permission_mode="bypassPermissions",
                max_turns=5,
                max_budget_usd=0.30,
                cwd=str(HOME),
                mcp_servers={"core": create_core_server()},
                hooks=AGENT_HOOKS,
            ),
        ):
            if hasattr(msg, "content"):
                for block in msg.content:
                    if hasattr(block, "text"):
                        result += block.text
            if hasattr(msg, "result") and msg.result:
                result = msg.result
        return result.strip()
    except Exception as e:
        return f"Diagnosis unavailable: {e}"


# --- Main ---

async def quick_check(dry_run: bool = False):
    """Quick anomaly check (every 30 min). No LLM unless anomaly found."""
    print(f"[{datetime.now():%H:%M:%S}] Gathering data...")
    data = await gather_all(force=False)  # use cache if fresh
    print(f"[{datetime.now():%H:%M:%S}] Checking for anomalies...")

    anomalies = detect_anomalies(data)

    if not anomalies:
        print(f"[{datetime.now():%H:%M:%S}] All clear.")
        return

    high = [a for a in anomalies if a["severity"] == "high"]
    print(f"[{datetime.now():%H:%M:%S}] Found {len(anomalies)} anomalies ({len(high)} high)")

    if dry_run:
        for a in anomalies:
            print(f"  [{a['severity'].upper()}] {a['source']}: {a['message']}")
        return

    # Only use LLM + alert for high/medium severity
    actionable = [a for a in anomalies if a["severity"] in ("high", "medium")]
    if not actionable:
        print(f"[{datetime.now():%H:%M:%S}] Low severity only, skipping alert.")
        return

    # --- Deduplication ---
    state = load_state()
    fingerprint = anomaly_fingerprint(actionable)
    last_hash = state.get("last_alert_hash")
    last_ts_str = state.get("last_alert_ts")
    last_ts = datetime.fromisoformat(last_ts_str) if last_ts_str else None
    now = datetime.now()

    is_same_anomaly = (fingerprint == last_hash)
    within_cooldown = last_ts and (now - last_ts) < EMAIL_COOLDOWN
    quiet = is_quiet_hours(now)

    should_email = True
    skip_reason = None
    if is_same_anomaly and within_cooldown:
        should_email = False
        skip_reason = f"same anomaly set (hash={fingerprint}) within {EMAIL_COOLDOWN}"
    elif quiet:
        should_email = False
        skip_reason = f"quiet hours ({QUIET_HOURS[0]}:00-{QUIET_HOURS[1]}:00)"

    print(f"[{now:%H:%M:%S}] Diagnosing...")
    diagnosis = await diagnose_anomalies(actionable, data)

    # Push notification always fires (cheap, silent on phone at night)
    title = f"Handler: {len(actionable)} issue{'s' if len(actionable)>1 else ''}"
    push_msg = diagnosis[:400] if diagnosis else "; ".join(a["message"] for a in actionable)[:400]
    await send_pushover(title, push_msg)

    # Email ONLY if: high severity AND not deduped AND not quiet hours
    if high and should_email:
        await send_alert_email(anomalies, data)
        print(f"[{now:%H:%M:%S}] Alert email sent (hash={fingerprint}).")
        save_state({
            "last_alert_hash": fingerprint,
            "last_alert_ts": now.isoformat(),
            "alert_count": state.get("alert_count", 0) + 1,
            "anomalies": actionable,
        })
    elif high:
        print(f"[{now:%H:%M:%S}] Email skipped: {skip_reason}. Push sent instead.")
    else:
        print(f"[{now:%H:%M:%S}] Medium only, no email.")

    print(f"[{now:%H:%M:%S}] Handler check complete.")


async def full_handler():
    """Full handler briefing (every 4 hrs). SDK-powered."""
    from claude_agent_sdk import query, ClaudeAgentOptions
    from core.tools import create_core_server
    from core.hooks import AGENT_HOOKS

    print(f"[{datetime.now():%H:%M:%S}] Full handler run...")
    data = await gather_all(force=True)
    anomalies = detect_anomalies(data)

    prompt = f"""Write a concise infrastructure status report. This is for John's personal review.

System data:
{json.dumps(data, indent=2, default=str)[:8000]}

Anomalies detected: {json.dumps(anomalies, indent=2) if anomalies else 'None'}

Format:
- Overall status (green/yellow/red)
- Any issues needing attention (specific, actionable)
- Infrastructure metrics (disk, uptime, services)
- Business pipeline snapshot (leads, clicks, jobs)
- One-line recommendation

Keep it under 300 words. No fluff."""

    result = ""
    async for msg in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            model="opus",
            permission_mode="bypassPermissions",
            max_turns=5,
            max_budget_usd=0.25,
            cwd=str(HOME),
            mcp_servers={"core": create_core_server()},
            hooks=AGENT_HOOKS,
        ),
    ):
        if hasattr(msg, "content"):
            for block in msg.content:
                if hasattr(block, "text"):
                    result += block.text
        if hasattr(msg, "result") and msg.result:
            result = msg.result

    print(result.strip())
    # Could email this too, but keeping it lightweight
    # The morning brief already sends a comprehensive email


async def main():
    dry_run = "--dry-run" in sys.argv
    full = "--full" in sys.argv

    if full:
        await full_handler()
    else:
        await quick_check(dry_run=dry_run)


if __name__ == "__main__":
    asyncio.run(main())
