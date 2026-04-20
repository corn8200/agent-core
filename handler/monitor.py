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
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
sys_path_inserted = True
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'core'))
import agent_cp_client as cp  # noqa: E402
CP_AGENT = 'handler-agent'

# ⚠️ Scrub API billing vars before importing claude_agent_sdk. See
# ~/Projects/anthropic-update-watcher/watcher.py:182-183 for rationale.
# Prevents silent pay-as-you-go billing when Max is intended.
for _leak_var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                  "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
    os.environ.pop(_leak_var, None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.vault import hydrate_env
hydrate_env()

from home_ops.gather import gather_all, quick_gather
from core.constants import HOME, PERSONAL_EMAIL, VPS_SSH

# --- Dedup State ---
STATE_FILE = Path.home() / "logs" / "handler-state.json"
DIAGNOSIS_CACHE_FILE = Path.home() / "logs" / "handler-diagnosis-cache.json"
ANOMALY_AGE_FILE = Path.home() / "logs" / "handler-anomaly-age.json"
EMAIL_COOLDOWN = timedelta(hours=6)  # Don't re-email same anomaly set within this window
DIAGNOSIS_COOLDOWN = timedelta(hours=6)  # Don't re-diagnose same anomaly within this window
QUIET_HOURS = (22, 7)  # 22:00-07:00 = push-only, no email

# Break dedup silence when a fingerprint has been suppressed for this long
# with no remediation firing. One iMessage per ESCALATION_COOLDOWN, not spam.
SUPPRESSED_ANOMALY_MAX_AGE = 24 * 3600
ESCALATION_COOLDOWN = timedelta(hours=24)


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


def load_diagnosis_cache() -> dict:
    if DIAGNOSIS_CACHE_FILE.exists():
        try:
            return json.loads(DIAGNOSIS_CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_diagnosis_cache(cache: dict):
    DIAGNOSIS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    DIAGNOSIS_CACHE_FILE.write_text(json.dumps(cache, indent=2))


def diagnosis_fingerprint(anomalies: list[dict]) -> str:
    """Per-anomaly fingerprint keyed on source (host) + message (service+type)."""
    key = sorted(f"{a['source']}|{a['message']}" for a in anomalies)
    return hashlib.sha1("\n".join(key).encode()).hexdigest()[:12]


def prune_diagnosis_cache(cache: dict, now: datetime) -> dict:
    """Drop entries older than the cooldown window."""
    fresh = {}
    for fp, ts_str in cache.items():
        try:
            ts = datetime.fromisoformat(ts_str)
        except Exception:
            continue
        if (now - ts) < DIAGNOSIS_COOLDOWN:
            fresh[fp] = ts_str
    return fresh


def load_anomaly_age() -> dict:
    if ANOMALY_AGE_FILE.exists():
        try:
            return json.loads(ANOMALY_AGE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_anomaly_age(age_map: dict):
    ANOMALY_AGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ANOMALY_AGE_FILE.write_text(json.dumps(age_map, indent=2))


def prune_anomaly_age(age_map: dict, active_fps: set, now: datetime) -> dict:
    """Keep entries still active this run, plus anything seen within the last 7 days."""
    keep = {}
    for fp, rec in age_map.items():
        if fp in active_fps:
            keep[fp] = rec
            continue
        try:
            first = datetime.fromisoformat(rec.get("first_seen", ""))
        except Exception:
            continue
        if (now - first) < timedelta(days=7):
            keep[fp] = rec
    return keep


async def send_imessage_escalation(summary: str):
    try:
        from core.tools import send_imessage_reliable
    except Exception as e:
        print(f"[handler] escalation import failed: {e}", file=sys.stderr)
        return
    try:
        ok, detail = await send_imessage_reliable("corn82@icloud.com", summary, _approved=True)
        if ok:
            print("[handler] escalation iMessage sent")
        else:
            print(f"[handler] escalation iMessage failed: {detail}", file=sys.stderr)
    except Exception as e:
        print(f"[handler] escalation iMessage exception: {e}", file=sys.stderr)


def is_quiet_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    h = now.hour
    start, end = QUIET_HOURS
    if start > end:  # wraps midnight (22 -> 7)
        return h >= start or h < end
    return start <= h < end


# --- Anomaly Detection (pure Python, 0 tokens) ---

def _vps_thrifty() -> bool:
    """Return True if VPS is in thrifty mode (services intentionally killed)."""
    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "vps", "test -f ~/.thrifty.mode"],
        capture_output=True,
    )
    return result.returncode == 0


def detect_anomalies(data: dict) -> list[dict]:
    """Check gathered data for anomalies. Returns list of {severity, source, message}."""
    anomalies = []

    vps_raw = data.get("vps", {}).get("raw", "")
    if not _vps_thrifty():
        # VPS services down
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
                if error_count > 10:
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

    import base64, shlex
    subject = f"Handler Alert — {now.strftime('%b %d %H:%M')}"
    b64 = base64.b64encode(html.encode()).decode()
    cmd = (
        f"send-email --from notify@jcornelius.net "
        f"--to {shlex.quote(PERSONAL_EMAIL)} "
        f"--subject {shlex.quote(subject)} "
        f"--body-b64 {b64} --html"
    )
    proc = await asyncio.create_subprocess_exec(
        "ssh", VPS_SSH, cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        print(f"[handler] email failed: {stderr.decode().strip()}", file=sys.stderr)
    else:
        print(f"[handler] {stdout.decode().strip()}")


# --- SDK Diagnosis ---

async def diagnose_anomalies(anomalies: list[dict], data: dict, heal_context: str = "") -> str:
    """Use SDK to diagnose anomalies and suggest fixes."""
    from core.mac_sdk import query, ClaudeAgentOptions
    from core.hooks import AGENT_HOOKS
    from core.thinking import STANDARD

    heal_section = f"\n\nAuto-remediation results:\n{heal_context}" if heal_context else ""

    anomaly_query = ' '.join(a.get('message', '') for a in anomalies[:3]) or 'infrastructure anomaly'
    try:
        from core.recall import get_context
        recall_block = get_context(anomaly_query, kind="handler")
    except Exception:
        recall_block = ""
    recall_section = f"\n\n{recall_block}" if recall_block else ""

    try:
        from core.memory import search as memory_search
        past_diagnoses = memory_search(anomaly_query, k=3, agent='handler', category='diagnosis')
        if past_diagnoses:
            history_text = '\n'.join(f"- [{d['timestamp'][:10]}] {d['content'][:200]}" for d in past_diagnoses)
        else:
            history_text = ''
    except Exception:
        history_text = ''

    history_section = f"\n\n## Recent similar diagnoses\n{history_text}" if history_text else ""

    prompt = f"""You are a systems handler for John's infrastructure. Anomalies detected:

{json.dumps(anomalies, indent=2)}
{heal_section}{recall_section}{history_section}

Raw system data:
{json.dumps(data, indent=2, default=str)[:8000]}

For each remaining anomaly (skip auto-healed ones):
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
                hooks=AGENT_HOOKS,
                thinking=STANDARD,
                effort="max",
            ),
        ):
            if hasattr(msg, "content"):
                for block in msg.content:
                    if hasattr(block, "text"):
                        result += block.text
            if hasattr(msg, "result") and msg.result:
                result = msg.result
        diagnosis_text = result.strip()
        if diagnosis_text:
            try:
                from core.memory import store as memory_store
                memory_store(
                    diagnosis_text,
                    agent='handler',
                    category='diagnosis',
                    metadata={'anomalies': anomalies},
                )
            except Exception:
                pass
        return diagnosis_text
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

    # --- Auto-Remediation ---
    from handler.playbooks import attempt_remediation
    print(f"[{datetime.now():%H:%M:%S}] Attempting auto-remediation...")
    remediation_results = await attempt_remediation(actionable)

    healed = [r for r in remediation_results if r.success]
    failed = [r for r in remediation_results if not r.success and r.tier != "red"]
    escalated = [r for r in remediation_results if r.tier == "red"]

    if healed:
        print(f"[{datetime.now():%H:%M:%S}] Auto-healed {len(healed)}: {', '.join(r.action for r in healed)}")

    # Remove successfully healed anomalies from the alert pipeline
    healed_msgs = {r.anomaly.get("message") for r in healed}
    actionable = [a for a in actionable if a["message"] not in healed_msgs]
    high = [a for a in actionable if a["severity"] == "high"]

    if not actionable and not escalated:
        print(f"[{datetime.now():%H:%M:%S}] All issues auto-healed. No alert needed.")
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

    # Build remediation context for diagnosis
    heal_context = ""
    if healed:
        heal_context += "\nAuto-healed (no action needed):\n" + "\n".join(
            f"  ✓ {r.playbook}: {r.action} — {r.detail}" for r in healed)
    if failed:
        heal_context += "\nAuto-fix attempted but failed:\n" + "\n".join(
            f"  ✗ {r.playbook}: {r.action} — {r.detail}" for r in failed)
    if escalated:
        heal_context += "\nEscalated (needs manual intervention):\n" + "\n".join(
            f"  ⚠ {r.playbook}: {r.detail}" for r in escalated)

    # --- Diagnosis Cooldown (prevents burning Opus credits on unresolved anomalies) ---
    diag_cache = prune_diagnosis_cache(load_diagnosis_cache(), now)
    diag_fp = diagnosis_fingerprint(actionable)
    suppressed = diag_fp in diag_cache
    if suppressed:
        last_diag = datetime.fromisoformat(diag_cache[diag_fp])
        age = now - last_diag
        print(f"[{now:%H:%M:%S}] Skipping diagnosis: same anomaly (fp={diag_fp}) diagnosed {age} ago, cooldown={DIAGNOSIS_COOLDOWN}.")
        diagnosis = ""
        save_diagnosis_cache(diag_cache)
    else:
        print(f"[{now:%H:%M:%S}] Diagnosing...")
        diagnosis = await diagnose_anomalies(actionable, data, heal_context)
        diag_cache[diag_fp] = now.isoformat()
        save_diagnosis_cache(diag_cache)

    # --- Chronic-anomaly escalation ---
    # Dedup can swallow a recurring anomaly forever if no playbook matches.
    # Track first_seen per diag_fp; if we've been suppressing for > max_age
    # and auto-remediation didn't clear it, break silence with one iMessage.
    age_map = load_anomaly_age()
    rec = age_map.get(diag_fp) or {"first_seen": now.isoformat(), "last_escalated": None, "fires": 0}
    rec["fires"] = int(rec.get("fires", 0)) + 1
    rec["sample"] = actionable[:5]
    age_map[diag_fp] = rec
    if suppressed and not healed:
        try:
            first_seen = datetime.fromisoformat(rec["first_seen"])
        except Exception:
            first_seen = now
        suppressed_seconds = (now - first_seen).total_seconds()
        last_esc_str = rec.get("last_escalated")
        last_esc = None
        if last_esc_str:
            try:
                last_esc = datetime.fromisoformat(last_esc_str)
            except Exception:
                last_esc = None
        escalation_due = (
            suppressed_seconds > SUPPRESSED_ANOMALY_MAX_AGE
            and (last_esc is None or (now - last_esc) >= ESCALATION_COOLDOWN)
        )
        if escalation_due:
            hours = int(suppressed_seconds // 3600)
            summary_msgs = "; ".join(a.get("message", "") for a in actionable[:3])[:180]
            msg = (
                f"[handler] Chronic anomaly fp={diag_fp} — {hours}h old, "
                f"{rec['fires']} fires, no remediation. {summary_msgs}. "
                "Chronic — add remediation or mark benign."
            )
            await send_imessage_escalation(msg)
            rec["last_escalated"] = now.isoformat()
            age_map[diag_fp] = rec
    age_map = prune_anomaly_age(age_map, {diag_fp}, now)
    save_anomaly_age(age_map)

    # Push notification always fires (cheap, silent on phone at night)
    heal_prefix = f"[{len(healed)} auto-healed] " if healed else ""
    title = f"Handler: {heal_prefix}{len(actionable)} issue{'s' if len(actionable)>1 else ''}"
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
    from core.mac_sdk import query, ClaudeAgentOptions
    from core.tools import create_core_server
    from core.hooks import AGENT_HOOKS
    from core.thinking import HEAVY

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
            thinking=HEAVY,
            effort="max",
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
    if cp.is_killed(CP_AGENT):
        print(f"[handler] {CP_AGENT} killed via agent-cp, exiting")
        return
    try: cp.event(CP_AGENT, "start")
    except Exception: pass
    dry_run = "--dry-run" in sys.argv
    full = "--full" in sys.argv

    try:
        if full:
            await full_handler()
        else:
            await quick_check(dry_run=dry_run)
    except BaseException as _e:
        import traceback as _tb
        _tbs = _tb.format_exc()
        try:
            cp.event(CP_AGENT, "error",
                     payload={"exc": type(_e).__name__, "msg": str(_e)[:500]})
        except Exception: pass
        sys.stderr.write(_tbs)
        raise
    try: cp.event(CP_AGENT, "complete")
    except Exception: pass


if __name__ == "__main__":
    asyncio.run(main())
