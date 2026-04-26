"""doctor_escalate — canonical entry point for routing infra alerts to the doctor pane.

Replaces direct-to-Pushover / direct-email alerting from watchers, daemons, and
schedulers. Doctor (claude-vps:6) reasons about the alert, applies safe fixes,
writes backlog rows for sticky issues, and iMessages John only if human hands
are needed. See ~/.claude/rules/infra-alerts.md for the HARD RULE.

Usage (from any watcher on VPS or Mac):

    from doctor_escalate import doctor_escalate
    doctor_escalate(
        watcher="my-watcher",
        severity="warn",
        summary="short human-readable one-liner",
        context={"key": "structured details"},
        fix_hints=["systemctl restart foo"],   # optional
        dedup_scope="foo:/srv",                # optional, 6h TTL
        quota={"max_per_hour": 5, "burst": 2}, # optional, overrides 10/h burst-3 default
    )

Fallback: 3 retries with backoff (5s/15s/45s) via pane-ask-v2; if all fail,
direct Pushover with [DOCTOR-BYPASS] prefix so the alert still lands.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from doctor_event_schema import canonical_ts, validate_event as _validate_schema_event
except ModuleNotFoundError:
    from core.doctor_event_schema import canonical_ts, validate_event as _validate_schema_event
from hashlib import sha1
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DOCTOR_PANE = "claude-vps:6"
PANE_ASK_PATHS = (
    "/home/ubuntu/bin/pane-ask-v2",           # VPS
    "/Users/johncornelius/bin/pane-ask-v2",   # Mac
)
DEDUP_TTL_SECONDS = 6 * 3600
BYPASS_WINDOW_SECONDS = 15 * 60
BYPASS_THRESHOLD = 3          # N bypasses in window → wake John about doctor-down
DOCTOR_LOG = Path("/srv/apps/taskqueue/logs/doctor.jsonl")
SEVERITIES = {"ok": 0, "notice": 0, "warn": 0, "error": 1, "critical": 2}   # pushover priority map

# Producer-side rate limiting (#648): token bucket defaults applied when watcher
# omits the quota kwarg. Each call to doctor_escalate costs one token.
_DEFAULT_QUOTA = {"max_per_hour": 10, "burst": 3}
_RL_STATE_PATH = Path("/tmp/doctor_rl_state.json")  # Redis-less fallback

_SECRET_KEY_RE = re.compile(r"(?i)(token|key|secret|password|auth|credential|bearer)")


def _token_bucket_check(watcher: str, quota: dict, redis_conn) -> bool:
    """Return True (allow) or False (rate-limited).

    Uses a leaky-bucket: tokens refill at rate=max_per_hour/3600 per second up
    to burst capacity. One token is consumed per allowed escalation. Bucket
    state lives in Redis when available; falls back to /tmp JSON otherwise.
    Fail-open on any storage error so a broken rate-limiter never silences a
    real alert.
    """
    max_per_hour = int(quota.get("max_per_hour", _DEFAULT_QUOTA["max_per_hour"]))
    burst = int(quota.get("burst", _DEFAULT_QUOTA["burst"]))
    rate = max_per_hour / 3600.0  # tokens per second
    now = time.time()

    if redis_conn is not None:
        tokens_key = f"doctor:rl:{watcher}:tokens"
        refill_key = f"doctor:rl:{watcher}:last_refill"
        try:
            pipe = redis_conn.pipeline()
            pipe.get(tokens_key)
            pipe.get(refill_key)
            tokens_raw, last_refill_raw = pipe.execute()

            tokens = float(tokens_raw) if tokens_raw is not None else float(burst)
            last_refill = float(last_refill_raw) if last_refill_raw is not None else now

            elapsed = now - last_refill
            tokens = min(float(burst), tokens + elapsed * rate)

            if tokens < 1.0:
                return False

            pipe = redis_conn.pipeline()
            # TTL = 2× the per-hour window so idle watchers age out cleanly
            pipe.set(tokens_key, tokens - 1.0, ex=7200)
            pipe.set(refill_key, now, ex=7200)
            pipe.execute()
            return True
        except Exception as e:
            logger.warning("doctor_escalate: rate-limit redis op failed (%s), allowing", e)
            return True  # fail-open

    # Redis-less fallback: local JSON file
    try:
        try:
            state = json.loads(_RL_STATE_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            state = {}

        bucket = state.get(watcher, {"tokens": float(burst), "last_refill": now})
        tokens = float(bucket["tokens"])
        last_refill = float(bucket["last_refill"])

        elapsed = now - last_refill
        tokens = min(float(burst), tokens + elapsed * rate)

        if tokens < 1.0:
            return False

        state[watcher] = {"tokens": tokens - 1.0, "last_refill": now}
        _RL_STATE_PATH.write_text(json.dumps(state))
        return True
    except Exception as e:
        logger.warning("doctor_escalate: rate-limit file op failed (%s), allowing", e)
        return True  # fail-open


def _validate_context(context) -> None:
    if not context:
        return
    if not isinstance(context, dict):
        raise ValueError(
            f"doctor_escalate: context must be dict or None, got {type(context).__name__}"
        )
    for k in context.keys():
        if _SECRET_KEY_RE.search(str(k)):
            raise ValueError(
                f"doctor_escalate: context key {k!r} matches secret pattern; "
                f"redact at producer (use opaque names like 'secret_count' or 'token_present')"
            )


def _pane_ask_binary() -> Optional[str]:
    for p in PANE_ASK_PATHS:
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return None


def _on_mac() -> bool:
    return sys.platform == "darwin"


def _get_redis():
    """Lazy Redis import — works from both VPS (localhost:6379) and Mac (tailscale)."""
    try:
        from redis import Redis
    except ImportError:
        return None
    host = os.environ.get("REDIS_HOST") or "100.118.21.64"
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
        try:
            to_write = _validate_schema_event(event)
        except Exception as val_err:
            to_write = dict(event)
            to_write["_validate_error"] = str(val_err)
            logger.warning("doctor_escalate: event validation failed (%s), writing raw", val_err)
        with DOCTOR_LOG.open("a") as f:
            f.write(json.dumps(to_write) + "\n")
    except Exception as e:
        logger.warning("doctor_escalate: log write failed (%s)", e)


def _pushover_direct(title: str, message: str, priority: int = 0) -> bool:
    token = os.environ.get("PUSHOVER_TOKEN")
    user = os.environ.get("PUSHOVER_USER")
    if not (token and user):
        for path in ("/home/ubuntu/.config/secrets.env", os.path.expanduser("~/.config/secrets.env")):
            if os.path.exists(path):
                for line in open(path):
                    if line.startswith("PUSHOVER_TOKEN="):
                        token = line.strip().split("=", 1)[1].strip("\"'")
                    elif line.startswith("PUSHOVER_USER="):
                        user = line.strip().split("=", 1)[1].strip("\"'")
                if token and user:
                    break
    if not (token and user):
        logger.error("doctor_escalate: pushover credentials unavailable, alert LOST")
        return False
    data = urllib.parse.urlencode({
        "token": token, "user": user,
        "title": title[:250], "message": message[:1024],
        "priority": min(priority, 2),
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
    if redis_conn is not None:
        try:
            c = redis_conn.incr("doctor:bypass:count")
            redis_conn.expire("doctor:bypass:count", BYPASS_WINDOW_SECONDS)
            return int(c)
        except Exception:
            pass  # fall through to JSONL scan
    # Fallback: scan log for event=bypass within window. Survives Redis down + reboots.
    cutoff = time.time() - BYPASS_WINDOW_SECONDS
    count = 0
    try:
        with DOCTOR_LOG.open("r") as f:
            try:
                f.seek(0, 2)
                size = f.tell()
                seek_to = max(0, size - 200_000)
                f.seek(seek_to)
                if seek_to > 0:
                    f.readline()  # discard partial line only when mid-file
            except Exception:
                f.seek(0)
            for line in f:
                try:
                    ev = json.loads(line)
                    if ev.get("event") != "bypass":
                        continue
                    ts_str = ev.get("ts", "")
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                    if ts >= cutoff:
                        count += 1
                except Exception:
                    continue
    except FileNotFoundError:
        return 1   # first-ever bypass
    return count + 1   # +1 for the bypass we're about to log


def _format_briefing(
    watcher: str,
    severity: str,
    summary: str,
    context: dict,
    fingerprint: str,
    fix_hints: Optional[list],
) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    source_host = "mac" if _on_mac() else "vps"
    lines = [
        f"[DOCTOR-ESCALATION {watcher} {ts}]",
        f"source_host={source_host} (fix runs HERE — never dispatch to the other host's panes)",
        f"severity={severity} summary={summary[:200]}",
        f"fingerprint={fingerprint} (dedup TTL {DEDUP_TTL_SECONDS//3600}h)",
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
    watcher: Optional[str] = None,
    severity: str = "",
    summary: str = "",
    context: Optional[dict] = None,
    fix_hints: Optional[list] = None,
    dedup_scope: Optional[str] = None,
    source: Optional[str] = None,
    bypass_priority: Optional[int] = None,
    quota: Optional[dict] = None,
) -> dict:
    """Route an infra alert through the doctor pane.

    Returns dict with keys: dispatched, dedup_hit, bypassed, rate_limited, fingerprint.

    quota — optional per-watcher rate limit override:
        {"max_per_hour": int, "burst": int}
        Defaults to _DEFAULT_QUOTA (10/h, burst 3).
    """
    # coerce source -> watcher (legacy alias)
    if watcher is None and source is not None:
        watcher = source
    if not watcher:
        raise ValueError("doctor_escalate: watcher (or source) is required")
    if not severity:
        raise ValueError("doctor_escalate: severity is required")
    if not summary:
        raise ValueError("doctor_escalate: summary is required")
    _validate_context(context)
    context = context or {}
    if severity not in SEVERITIES:
        raise ValueError(
            f"doctor_escalate: invalid severity {severity!r}; must be one of {sorted(SEVERITIES)}"
        )

    effective_quota = dict(_DEFAULT_QUOTA)
    if quota:
        effective_quota.update(quota)

    fp = _fingerprint(watcher, severity, dedup_scope, context)
    result = {
        "dispatched": False,
        "dedup_hit": False,
        "bypassed": False,
        "rate_limited": False,
        "fingerprint": fp,
    }

    # Single Redis connection reused by rate-limit check, dedup check, and bypass counter.
    r = _get_redis()

    # Producer-side rate limit (#648) — drop before touching dedup or dispatch.
    if not _token_bucket_check(watcher, effective_quota, r):
        result["rate_limited"] = True
        logger.warning(
            "doctor_escalate: rate-limited watcher=%s quota=%s/%sh burst=%s — dropped",
            watcher,
            effective_quota["max_per_hour"],
            1,
            effective_quota["burst"],
        )
        _log_event({
            "ts": canonical_ts(),
            "watcher": watcher, "severity": severity, "summary": summary,
            "fingerprint": fp, "event": "rate_limited",
            "quota": effective_quota,
        })
        return result

    if r is not None:
        dedup_key = f"doctor:escalation:{fp}"
        try:
            if r.exists(dedup_key):
                ttl = r.ttl(dedup_key)
                result["dedup_hit"] = True
                _log_event({
                    "ts": canonical_ts(),
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
            "ts": canonical_ts(),
            "watcher": watcher, "severity": severity, "fingerprint": fp,
            "event": "bypass", "reason": "pane-ask-v2 binary not found",
        })
        result["bypassed"] = True
        _deliver_bypass(watcher, severity, summary, briefing, "no pane-ask-v2", r, bypass_priority)
        return result

    # Fix #1: when invoked from a daemon (no $TMUX_PANE), pane-ask-v2 needs
    # explicit --label NAME for the signed banner. Read $PANE_ASK_LABEL set
    # by the calling LaunchAgent/systemd unit; fall back to the watcher name
    # so every escalation has a verified identity rather than failing exit 8.
    label_args: list[str] = []
    if not os.environ.get("TMUX_PANE"):
        label_value = os.environ.get("PANE_ASK_LABEL") or watcher or "doctor-escalate"
        label_args = ["--label", label_value]

    target_args = ["--ssh", "vps", DOCTOR_PANE] if _on_mac() else [DOCTOR_PANE]
    last_err = ""
    for attempt, delay in enumerate((0, 5, 15), start=1):
        if delay:
            time.sleep(delay)
        try:
            proc = subprocess.run(
                [binary, *label_args, *target_args, briefing],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode == 0:
                result["dispatched"] = True
                _log_event({
                    "ts": canonical_ts(),
                    "watcher": watcher, "severity": severity, "summary": summary,
                    "fingerprint": fp, "event": "dispatched", "attempt": attempt,
                })
                return result
            stderr_out = proc.stderr or ""
            stdout_out = proc.stdout or ""
            combined = stderr_out + stdout_out
            last_err = f"rc={proc.returncode} stderr={stderr_out[:200]}"
            if "not found" in combined and attempt < 3:
                time.sleep(30 * attempt)
                continue
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
        "ts": canonical_ts(),
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
