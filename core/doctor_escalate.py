"""doctor_escalate — Mac-side entry point for routing infra alerts to the doctor pane.

Replaces direct-to-Pushover / direct-email alerting from watchers, daemons, and
schedulers. Doctor (claude-vps:6) reasons about the alert, applies safe fixes,
writes backlog rows for sticky issues, and iMessages John only if human hands
are needed. See ~/.claude/rules/infra-alerts.md for the HARD RULE.

Mac specifics vs VPS version:
  - pane-ask-v2 is invoked with --ssh vps (doctor pane is always on VPS)
  - Log path is ~/logs/doctor.jsonl (VPS log is under /srv/apps/taskqueue/)
  - Redis connects to VPS via Tailscale (100.118.21.64:6379)

Usage (from any watcher on Mac):

    from core.doctor_escalate import doctor_escalate
    doctor_escalate(
        watcher="my-watcher",
        severity="warn",
        summary="short human-readable one-liner",
        context={"key": "structured details"},
        fix_hints=["systemctl restart foo"],   # optional
        dedup_scope="foo:/srv",                # optional, 6h TTL
    )

Fallback: 3 retries with backoff (5s/15s/45s) via pane-ask-v2 --ssh vps; if all
fail, direct Pushover with [DOCTOR-BYPASS] prefix so the alert still lands.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DOCTOR_PANE = "claude-vps:6"
PANE_ASK_PATH = "/Users/johncornelius/bin/pane-ask-v2"
DEDUP_TTL_SECONDS = 6 * 3600
BYPASS_WINDOW_SECONDS = 15 * 60
BYPASS_THRESHOLD = 3
DOCTOR_LOG = Path.home() / "logs" / "doctor.jsonl"
SEVERITIES = {"ok": 0, "notice": 0, "warn": 0, "critical": 1}


def _pane_ask_binary() -> Optional[str]:
    if os.path.exists(PANE_ASK_PATH) and os.access(PANE_ASK_PATH, os.X_OK):
        return PANE_ASK_PATH
    return None


def _get_redis():
    try:
        from redis import Redis
    except ImportError:
        return None
    host = os.environ.get("REDIS_HOST", "100.118.21.64")
    port = int(os.environ.get("REDIS_PORT", 6379))
    db = int(os.environ.get("REDIS_DB", 0))
    try:
        r = Redis(host=host, port=port, db=db, socket_timeout=3)
        r.ping()
        return r
    except Exception as e:
        logger.warning("doctor_escalate: redis unreachable (%s)", e)
        return None


def _fingerprint(watcher: str, severity: str, dedup_scope: Optional[str], context: dict) -> str:
    if dedup_scope:
        payload = f"{watcher}|{severity}|{dedup_scope}"
    else:
        items = sorted((k, str(v)[:200]) for k, v in (context or {}).items())
        payload = f"{watcher}|{severity}|" + "|".join(f"{k}={v}" for k, v in items)
    return sha1(payload.encode()).hexdigest()[:16]


def _log_event(event: dict) -> None:
    try:
        DOCTOR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DOCTOR_LOG.open("a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        logger.warning("doctor_escalate: log write failed (%s)", e)


def _pushover_direct(title: str, message: str, priority: int = 0) -> bool:
    token = os.environ.get("PUSHOVER_TOKEN")
    user = os.environ.get("PUSHOVER_USER")
    if not (token and user):
        secrets_path = os.path.expanduser("~/.config/secrets.env")
        if os.path.exists(secrets_path):
            for line in open(secrets_path):
                if line.startswith("PUSHOVER_TOKEN="):
                    token = line.strip().split("=", 1)[1].strip("\"'")
                elif line.startswith("PUSHOVER_USER="):
                    user = line.strip().split("=", 1)[1].strip("\"'")
    if not (token and user):
        logger.error("doctor_escalate: pushover credentials unavailable, alert LOST")
        return False
    data = urllib.parse.urlencode({
        "token": token, "user": user,
        "title": title[:250], "message": message[:1024],
        "priority": priority,
    }).encode()
    try:
        req = urllib.request.Request(
            "https://api.pushover.net/1/messages.json", data=data, method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        logger.error("doctor_escalate: pushover send failed (%s)", e)
        return False


def _record_bypass(redis_conn) -> int:
    if redis_conn is None:
        return 0
    key = "doctor:bypass:count"
    try:
        c = redis_conn.incr(key)
        redis_conn.expire(key, BYPASS_WINDOW_SECONDS)
        return int(c)
    except Exception:
        return 0


def _format_briefing(
    watcher: str,
    severity: str,
    summary: str,
    context: dict,
    fingerprint: str,
    fix_hints: Optional[list],
) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"[DOCTOR-ESCALATION {watcher} {ts}]",
        f"severity={severity} summary={summary[:200]}",
        f"fingerprint={fingerprint} (dedup TTL {DEDUP_TTL_SECONDS//3600}h)",
        "source=mac",
    ]
    if context:
        lines.append("")
        lines.append("context:")
        for k, v in context.items():
            if isinstance(v, (list, tuple)):
                lines.append(f"  {k}:")
                for item in list(v)[:10]:
                    lines.append(f"    - {str(item)[:200]}")
            elif isinstance(v, dict):
                lines.append(f"  {k}: {json.dumps(v)[:300]}")
            else:
                lines.append(f"  {k}: {str(v)[:300]}")
    if fix_hints:
        lines.append("")
        lines.append("fix_hints (prefer these if applicable):")
        for h in fix_hints[:8]:
            lines.append(f"  - {h}")
    lines += [
        "",
        "Your job:",
        "1. If a fix_hint is obviously safe and applicable, run it; log the outcome.",
        "2. If the issue is structural/recurring, POST /backlog to agent-cp with tags=[infra,auto-filed].",
        "3. iMessage John only if human hands are required.",
        "Do NOT escalate to Pushover/email directly from doctor — that's the watcher's bypass path.",
        "",
        "Standing briefing: ~/claude-config/doctor/BRIEFING.md",
        "Log this escalation: /srv/apps/taskqueue/logs/doctor.jsonl",
    ]
    return "\n".join(lines)


def doctor_escalate(
    watcher: str,
    severity: str,
    summary: str,
    context: Optional[dict] = None,
    fix_hints: Optional[list] = None,
    dedup_scope: Optional[str] = None,
    bypass_priority: Optional[int] = None,
) -> dict:
    """Route an infra alert from Mac through the doctor pane (claude-vps:6).

    Returns dict with keys: dispatched, dedup_hit, bypassed, fingerprint.
    """
    context = context or {}
    if severity not in SEVERITIES:
        severity = "warn"

    fp = _fingerprint(watcher, severity, dedup_scope, context)
    result = {"dispatched": False, "dedup_hit": False, "bypassed": False, "fingerprint": fp}

    r = _get_redis()
    if r is not None:
        dedup_key = f"doctor:escalation:{fp}"
        try:
            if r.exists(dedup_key):
                ttl = r.ttl(dedup_key)
                result["dedup_hit"] = True
                _log_event({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "watcher": watcher, "severity": severity, "summary": summary,
                    "fingerprint": fp, "event": "dedup_hit", "ttl_remaining_s": int(ttl),
                })
                return result
            r.setex(dedup_key, DEDUP_TTL_SECONDS, json.dumps({
                "watcher": watcher, "severity": severity, "summary": summary,
            }))
        except Exception as e:
            logger.warning("doctor_escalate: dedup check failed (%s) — firing anyway", e)

    briefing = _format_briefing(watcher, severity, summary, context, fp, fix_hints)
    binary = _pane_ask_binary()
    if not binary:
        _log_event({
            "ts": datetime.now(timezone.utc).isoformat(),
            "watcher": watcher, "severity": severity, "fingerprint": fp,
            "event": "bypass", "reason": "pane-ask-v2 binary not found",
        })
        result["bypassed"] = True
        _deliver_bypass(watcher, severity, summary, briefing, "no pane-ask-v2", r, bypass_priority)
        return result

    last_err = ""
    for attempt, delay in enumerate((0, 5, 15), start=1):
        if delay:
            time.sleep(delay)
        try:
            proc = subprocess.run(
                [binary, "--ssh", "vps", DOCTOR_PANE, briefing],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode == 0:
                result["dispatched"] = True
                _log_event({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "watcher": watcher, "severity": severity, "summary": summary,
                    "fingerprint": fp, "event": "dispatched", "attempt": attempt,
                })
                return result
            last_err = f"rc={proc.returncode} stderr={proc.stderr[:200]}"
        except subprocess.TimeoutExpired:
            last_err = "timeout"
        except Exception as e:
            last_err = f"exc={e}"

    if r is not None:
        try:
            r.delete(f"doctor:escalation:{fp}")
        except Exception:
            pass
    result["bypassed"] = True
    _deliver_bypass(watcher, severity, summary, briefing, last_err, r, bypass_priority)
    return result


def _deliver_bypass(
    watcher: str,
    severity: str,
    summary: str,
    briefing: str,
    reason: str,
    redis_conn,
    bypass_priority: Optional[int],
) -> None:
    bypass_count = _record_bypass(redis_conn)
    prio = bypass_priority if bypass_priority is not None else SEVERITIES.get(severity, 0)
    title = f"[DOCTOR-BYPASS] {watcher}/{severity}: {summary[:80]}"
    body_parts = [
        f"doctor unreachable - {reason}",
        f"bypass #{bypass_count} in last {BYPASS_WINDOW_SECONDS//60}m",
        "",
        briefing,
    ]
    if bypass_count >= BYPASS_THRESHOLD:
        prio = max(prio, 1)
        body_parts.insert(0, f"WARN: doctor appears DOWN ({bypass_count} bypasses in window)")
    _pushover_direct(title, "\n".join(body_parts), priority=prio)
    _log_event({
        "ts": datetime.now(timezone.utc).isoformat(),
        "watcher": watcher, "severity": severity, "summary": summary,
        "event": "bypass", "reason": reason, "bypass_count": bypass_count,
        "pushover_priority": prio,
    })


if __name__ == "__main__":
    print("doctor_escalate smoke test")
    out = doctor_escalate(
        watcher="smoke-test",
        severity="notice",
        summary="doctor_escalate import + dispatch smoke",
        context={"source": "cli test", "note": "safe to ignore"},
        dedup_scope="smoke-test:cli",
    )
    print(json.dumps(out, indent=2))
