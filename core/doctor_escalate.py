"""doctor_escalate — canonical entry point for routing infra alerts to Overseer.

Replaces direct-to-Pushover / direct-email alerting from watchers, daemons, and
schedulers. Doctor panes were retired in the 2026-05-08 pane reduction; alerts
now land in the Mac Overseer Voice pane, which coordinates fixes, writes
backlog rows for sticky issues, and wakes John only if human hands are needed.
See ~/.claude/rules/infra-alerts.md.

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
        target_host="mac",                     # optional: "mac" | "vps" | None (= calling host)
    )

Routing semantics:
    target_host  — where the FIX may need to run.
                   Default: the host the call was made from.
    source_host  — where the watcher detected the symptom from (the calling
                   host). Stamped in the briefing for human context only.

Fallback: 3 retries with backoff (5s/15s/45s) via pane-ask-v2; if all fail,
last-ditch direct Pushover with [OVERSEER-VOICE-BYPASS-<host>] prefix so a
real Overseer/transport failure still lands.

Set DOCTOR_ESCALATE_LOCAL_ONLY=1 only from synthetic probes that must prove a
producer logs a durable doctor event without touching Redis, pane-ask, or
Pushover.
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
from zoneinfo import ZoneInfo

# Self-bootstrap sys.path: callers can `from doctor_escalate import …`
# without pre-loading sys.path, as long as Python can locate this file
# (PYTHONPATH, an explicit sys.path.insert, or absolute spec_from_file_location).
# Once that import succeeds, this block ensures sibling modules
# (doctor_event_schema, etc.) resolve from the same dir
# regardless of how the caller set things up. Critic finding #7 (#686).
_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
if _LIB_DIR and _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from doctor_event_schema import canonical_ts, validate_event as _validate_schema_event
from hashlib import sha1
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SOURCE_HOST = "mac" if sys.platform == "darwin" else "vps"

OVERSEER_VOICE_PANE = "claude:5"
OVERSEER_VOICE_HOST = "mac"
DOCTOR_PANES = {
    "mac": OVERSEER_VOICE_PANE,
    "vps": OVERSEER_VOICE_PANE,
}
DOCTOR_LOG_PATHS = {
    "mac": Path.home() / "Library/Logs/doctor.jsonl",
    "vps": Path("/srv/apps/taskqueue/logs/doctor.jsonl"),
}
PANE_ASK_PATHS = (
    "/home/ubuntu/bin/pane-ask-v2",           # VPS
    "/Users/johncornelius/bin/pane-ask-v2",   # Mac
)
DEDUP_TTL_SECONDS = 6 * 3600
# On bypass delivery the dedup latch is re-armed to this shorter TTL instead
# of deleted: deleting it made a held/busy pane route re-fire direct Pushover
# for the SAME fingerprint every watcher cycle (18 fires in 4.5h, 2026-06-04).
BYPASS_REARM_TTL_SECONDS = 30 * 60
BYPASS_WINDOW_SECONDS = 15 * 60
BYPASS_THRESHOLD = 3          # N bypasses in window -> wake John about Voice transport failure
# Doctor bypass alerts are still infra alerts. Per rules/messaging.md, P2 is
# family/home safety only; P1 is reserved for rare true infra emergencies.
SEVERITIES = {"ok": 0, "notice": 0, "warn": 0, "error": 0, "critical": 1}
# Phone quiet hours are evaluated in John's local timezone. Ordinary doctor
# "critical" events are P1 and are held; only an explicit P2 emergency (the
# system's critical-hard boundary) may wake the phone overnight.
PHONE_QUIET_TZ = ZoneInfo("America/New_York")
PHONE_QUIET_START_MINUTE = 21 * 60 + 30
PHONE_QUIET_END_MINUTE = 7 * 60
CRITICAL_HARD_PRIORITY = 2
_SOFT_PANE_HOLD_MARKERS = (
    "held: automated pane send",
    "fail-closed after ambiguous prior delivery",
    "is in tmux copy-mode; refusing pane paste",
    "busy: pane",
)

# #683 cluster-dedup: when N+ distinct fingerprints fire for the same watcher
# within CLUSTER_WINDOW_SECONDS, the Nth fire is rewritten as one "cluster"
# escalation summarising all N, and the cluster fingerprint is latched for
# CLUSTER_LATCH_SECONDS so subsequent individual fires from that watcher get
# suppressed. Reduces doctor noise floor when a class-of-workers fails together
# (e.g. 4 gather_worker subscribers all going stale at once on phase-7 incidents).
CLUSTER_WINDOW_SECONDS = 5 * 60       # rolling window for counting distinct fps
CLUSTER_THRESHOLD = 3                  # min distinct fps in window to switch to cluster mode
CLUSTER_LATCH_SECONDS = 5 * 60         # how long after clustering to suppress individuals

# Producer-side rate limiting (#648): token bucket defaults applied when watcher
# omits the quota kwarg. Each call to doctor_escalate costs one token.
_DEFAULT_QUOTA = {"max_per_hour": 10, "burst": 3}
_RL_STATE_PATH = Path("/tmp/doctor_rl_state.json")  # Redis-less fallback

_SECRET_KEY_RE = re.compile(r"(?i)(token|key|secret|password|auth|credential|bearer)")
_REDIS_URL_ENV_KEYS = (
    "DOCTOR_REDIS_URL",
    "REDIS_URL",
    "OVERSEER_GATEWAY_REDIS_URL",
    "CP_API_REDIS_URL",
    "RQ_REDIS_URL",
)
_REDIS_PASSWORD_ENV_KEYS = (
    "DOCTOR_REDIS_PASSWORD",
    "REDIS_PASSWORD",
    "RQ_REDIS_PASSWORD",
)
_REDIS_ENV_FILE_PATHS = (
    Path(os.environ.get("DOCTOR_REDIS_ENV_FILE", "")).expanduser()
    if os.environ.get("DOCTOR_REDIS_ENV_FILE")
    else None,
    Path.home() / ".config" / "secrets.env",
    Path.home() / ".config" / "unified-task-engine" / "api.env",
    Path("/etc/overseer-gateway.env"),
)


def _on_mac() -> bool:
    return sys.platform == "darwin"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _doctor_log_path() -> Path:
    return DOCTOR_LOG_PATHS["mac" if _on_mac() else "vps"]


def _resolve_target(target_host: Optional[str]) -> tuple[str, str, list[str]]:
    """Return (resolved_host, pane, ssh_args).

    ``target_host`` still describes where the fix may need to run. The receiving
    pane is always Mac Overseer Voice after the doctor-pane retirement.
    """
    if target_host is None:
        target_host = _SOURCE_HOST
    if target_host not in DOCTOR_PANES:
        raise ValueError(
            f"target_host must be 'mac' or 'vps', got {target_host!r}"
        )
    pane = DOCTOR_PANES[target_host]
    on_mac = _on_mac()
    ssh_args = [] if on_mac else ["--ssh", OVERSEER_VOICE_HOST]
    return target_host, pane, ssh_args


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


def _parse_env_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value


def _load_redis_env_file_values() -> dict[str, str]:
    values: dict[str, str] = {}
    interesting = {
        *_REDIS_URL_ENV_KEYS,
        *_REDIS_PASSWORD_ENV_KEYS,
        "REDIS_HOST",
        "REDIS_PORT",
        "REDIS_DB",
    }
    for path in _REDIS_ENV_FILE_PATHS:
        if path is None:
            continue
        try:
            with path.open() as f:
                for line in f:
                    parsed = _parse_env_line(line)
                    if parsed is None:
                        continue
                    key, value = parsed
                    if key in interesting and key not in values:
                        values[key] = value
        except FileNotFoundError:
            continue
        except OSError as e:
            logger.warning("doctor_escalate: redis env file unreadable (%s): %s", path, e)
    return values


def _redis_config_value(keys: tuple[str, ...], file_values: dict[str, str]) -> str | None:
    for key in keys:
        value = os.environ.get(key)
        if value:
            return value
    for key in keys:
        value = file_values.get(key)
        if value:
            return value
    return None


def _get_redis():
    """Lazy Redis import — works from both VPS (localhost:6379) and Mac (tailscale)."""
    try:
        from redis import Redis
    except ImportError:
        return None
    file_values = _load_redis_env_file_values()
    redis_url = _redis_config_value(_REDIS_URL_ENV_KEYS, file_values)
    try:
        if redis_url:
            r = Redis.from_url(redis_url, socket_timeout=3)
        else:
            host = os.environ.get("REDIS_HOST") or file_values.get("REDIS_HOST") or "100.118.21.64"
            port = int(os.environ.get("REDIS_PORT") or file_values.get("REDIS_PORT") or 6379)
            db = int(os.environ.get("REDIS_DB") or file_values.get("REDIS_DB") or 0)
            kwargs: dict[str, Any] = {
                "host": host,
                "port": port,
                "db": db,
                "socket_timeout": 3,
            }
            password = _redis_config_value(_REDIS_PASSWORD_ENV_KEYS, file_values)
            if password:
                kwargs["password"] = password
            r = Redis(**kwargs)
        r.ping()
        return r
    except Exception as e:
        logger.warning("doctor_escalate: redis unreachable (%s)", e)
        return None


def _check_cluster(watcher: str, fp: str, redis_conn) -> tuple[str, list[str]]:
    """#683 cluster-dedup. Returns (mode, recent_fps).

    mode is one of:
      - "individual": fire as normal — under cluster threshold or no Redis
      - "first_cluster": this fire promotes to a cluster — caller rewrites
        summary/fingerprint to a cluster shape, latches further suppression
      - "suppressed": cluster is latched — caller should treat this as a
        dedup_hit and return without dispatching

    Race-safety: the promotion step uses `SET latch NX EX` (atomic
    set-if-not-exists with TTL), so two concurrent processes hitting the
    threshold at the exact same moment cannot both win and ship two cluster
    summaries.

    Best-effort: any Redis error falls through to "individual" so we never
    silence a real alert.
    """
    if redis_conn is None:
        return ("individual", [])
    cluster_key = f"doctor:cluster:{watcher}"
    latch_key = f"doctor:cluster_latch:{watcher}"
    now = time.time()
    try:
        if redis_conn.exists(latch_key):
            return ("suppressed", [])
        redis_conn.zadd(cluster_key, {fp: now})
        redis_conn.expire(cluster_key, CLUSTER_WINDOW_SECONDS * 2)
        redis_conn.zremrangebyscore(cluster_key, 0, now - CLUSTER_WINDOW_SECONDS)
        distinct_count = redis_conn.zcard(cluster_key)
        if distinct_count < CLUSTER_THRESHOLD:
            return ("individual", [])
        won = redis_conn.set(latch_key, "1", nx=True, ex=CLUSTER_LATCH_SECONDS)
        if not won:
            return ("suppressed", [])
        recent = redis_conn.zrange(cluster_key, 0, -1)
        recent_fps = [m.decode() if isinstance(m, bytes) else m for m in recent]
        return ("first_cluster", recent_fps)
    except Exception as e:
        logger.warning("doctor_escalate: cluster check failed (%s) — firing individual", e)
        return ("individual", [])


def _fingerprint(
    watcher: str,
    severity: str,
    dedup_scope: Optional[str],
    context: dict,
    target_host: str,
) -> str:
    """[CRITIC-FIX SEV-1#1] Fingerprint scoped to target_host.

    Mac and VPS doctor get distinct dedup keyspaces. Same watcher firing on
    both hosts produces different fingerprints — no cross-host silencing.
    """
    base = f"{target_host}|{watcher}|{severity}"
    if dedup_scope:
        payload = f"{base}|{dedup_scope}"
    else:
        items = sorted((k, str(v)[:200]) for k, v in (context or {}).items())
        payload = f"{base}|" + "|".join(f"{k}={v}" for k, v in items)
    return sha1(payload.encode()).hexdigest()[:16]


def _log_event(event: dict) -> None:
    log_path = _doctor_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            to_write = _validate_schema_event(event)
        except Exception as val_err:
            to_write = dict(event)
            to_write["_validate_error"] = str(val_err)
            logger.warning("doctor_escalate: event validation failed (%s), writing raw", val_err)
        with log_path.open("a") as f:
            f.write(json.dumps(to_write) + "\n")
    except Exception as e:
        logger.warning("doctor_escalate: log write failed (%s)", e)


def _in_phone_quiet_hours(now: datetime | None = None) -> bool:
    """Return whether ``now`` falls in the DST-safe 21:30-07:00 ET window."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_now = now.astimezone(PHONE_QUIET_TZ)
    local_minute = local_now.hour * 60 + local_now.minute
    return (
        local_minute >= PHONE_QUIET_START_MINUTE
        or local_minute < PHONE_QUIET_END_MINUTE
    )


def _pushover_direct(
    title: str,
    message: str,
    priority: int = 0,
    *,
    url: str | None = None,
    url_title: str | None = None,
    now: datetime | None = None,
) -> bool:
    if priority < CRITICAL_HARD_PRIORITY and _in_phone_quiet_hours(now):
        logger.warning(
            "doctor_escalate: direct Pushover held during phone quiet hours "
            "(priority=%s, window=21:30-07:00 America/New_York)",
            priority,
        )
        return False
    try:
        from core.voice_reroute import voice_reroute_send
        if voice_reroute_send(title, message, priority, url, url_title):
            return True
    except Exception:
        pass
    token = os.environ.get("PUSHOVER_APP_TOKEN")
    user = os.environ.get("PUSHOVER_USER_KEY")
    if not (token and user):
        for path in ("/home/ubuntu/.config/secrets.env", os.path.expanduser("~/.config/secrets.env")):
            if os.path.exists(path):
                for line in open(path):
                    if line.startswith("PUSHOVER_APP_TOKEN="):
                        token = line.strip().split("=", 1)[1].strip("\"'")
                    elif line.startswith("PUSHOVER_USER_KEY="):
                        user = line.strip().split("=", 1)[1].strip("\"'")
                if token and user:
                    break
    if not (token and user):
        logger.error("doctor_escalate: pushover credentials unavailable, alert LOST")
        return False
    clamped_priority = min(priority, 2)
    payload: dict = {
        "token": token, "user": user,
        "title": title[:250], "message": message[:1024],
        "priority": clamped_priority,
    }
    if url:
        payload["url"] = url[:512]
    if url_title:
        payload["url_title"] = url_title[:100]
    # Pushover P2 (emergency) requires retry + expire or the API returns HTTP 400.
    if clamped_priority >= 2:
        payload["retry"] = 60    # retry interval in seconds (minimum 30)
        payload["expire"] = 3600  # stop retrying after 1 hour
    data = urllib.parse.urlencode(payload).encode()
    try:
        req = urllib.request.Request(
            "https://api.pushover.net/1/messages.json", data=data, method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        logger.error("doctor_escalate: pushover send failed (%s)", e)
        return False


def _record_bypass(redis_conn, target_host: str) -> int:
    """[CRITIC-FIX SEV-1#2] Bypass counter scoped per host.

    Mac and VPS targets get separate bypass counters. A failure on one target
    should never trigger a Voice-route alarm on the other.
    """
    if redis_conn is not None:
        try:
            key = f"doctor:bypass:{target_host}:count"
            c = redis_conn.incr(key)
            redis_conn.expire(key, BYPASS_WINDOW_SECONDS)
            return int(c)
        except Exception:
            pass  # fall through to JSONL scan
    # Fallback: scan THIS host's log for event=bypass within window.
    log_path = _doctor_log_path()
    cutoff = time.time() - BYPASS_WINDOW_SECONDS
    count = 0
    try:
        with log_path.open("r") as f:
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
                    # Only count bypasses for the same target_host
                    if ev.get("target_host") and ev.get("target_host") != target_host:
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


def _is_soft_pane_hold(reason: str) -> bool:
    """True when pane-ask protected the target instead of proving alert loss."""
    reason_l = (reason or "").lower()
    return any(marker in reason_l for marker in _SOFT_PANE_HOLD_MARKERS)


def _format_briefing(
    watcher: str,
    severity: str,
    summary: str,
    context: dict,
    fingerprint: str,
    fix_hints: Optional[list],
    target_host: str,
) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log_path = _doctor_log_path()
    lines = [
        f"[DOCTOR-ESCALATION {watcher} {ts}]",
        f"target_host={target_host} source_host={_SOURCE_HOST} receiver={OVERSEER_VOICE_PANE} (Overseer coordinates the fix)",
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
        "1. If a fix_hint is obviously safe and applicable, run it or assign it; log the outcome.",
        "2. If the issue is structural/recurring, POST /backlog to agent-cp with tags=[infra,auto-filed].",
        "3. iMessage John only if human hands are required.",
        "Do NOT escalate to Pushover/email directly from an agent pane; use the Overseer/gateway path.",
        "",
        "Standing briefing: ~/claude-config/doctor/COMMON.md + ~/claude-config/doctor/{MAC,VPS}.md",
        f"Log this escalation: {log_path}",
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
    target_host: Optional[str] = None,
) -> dict:
    """Route an infra alert through a doctor pane.

    target_host — "mac" | "vps" | None. None = the calling host. Picks which
        doctor pane gets the dispatch and which dedup/bypass keyspace is used.
    Returns dict with keys: dispatched, dedup_hit, bypassed, rate_limited,
    clustered, cluster_suppressed, fingerprint, target_host.
    """
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

    resolved_host, doctor_pane, ssh_args = _resolve_target(target_host)

    effective_quota = dict(_DEFAULT_QUOTA)
    if quota:
        effective_quota.update(quota)

    fp = _fingerprint(watcher, severity, dedup_scope, context, resolved_host)
    result = {
        "dispatched": False,
        "dedup_hit": False,
        "bypassed": False,
        "rate_limited": False,
        "local_only": False,
        "clustered": False,
        "cluster_suppressed": False,
        "fingerprint": fp,
        "target_host": resolved_host,
    }

    if _env_flag("DOCTOR_ESCALATE_LOCAL_ONLY"):
        result["local_only"] = True
        _log_event({
            "ts": canonical_ts(),
            "watcher": watcher, "severity": severity, "summary": summary,
            "fingerprint": fp, "event": "local_only",
            "reason": "DOCTOR_ESCALATE_LOCAL_ONLY=1",
            "source_host": _SOURCE_HOST, "target_host": resolved_host,
            **({"context": context} if context else {}),
        })
        return result

    r = _get_redis()

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
            "source_host": _SOURCE_HOST, "target_host": resolved_host,
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
                    "source_host": _SOURCE_HOST, "target_host": resolved_host,
                })
                return result
            r.setex(dedup_key, DEDUP_TTL_SECONDS, json.dumps({
                "watcher": watcher, "severity": severity, "summary": summary,
            }))
        except Exception as e:
            logger.warning("doctor_escalate: dedup check failed (%s) — firing anyway", e)

    cluster_mode, cluster_fps = _check_cluster(watcher, fp, r)
    if cluster_mode == "suppressed":
        result["dedup_hit"] = True
        result["cluster_suppressed"] = True
        _log_event({
            "ts": canonical_ts(),
            "watcher": watcher, "severity": severity, "summary": summary,
            "fingerprint": fp, "event": "cluster_suppressed",
            "source_host": _SOURCE_HOST, "target_host": resolved_host,
        })
        return result
    if cluster_mode == "first_cluster":
        cluster_fp = sha1(
            f"cluster|{resolved_host}|{watcher}|{','.join(sorted(cluster_fps))}".encode()
        ).hexdigest()[:16]
        original_summary = summary
        summary = f"[CLUSTER {len(cluster_fps)} fps in {CLUSTER_WINDOW_SECONDS//60}m] {original_summary[:160]}"
        context = {
            **context,
            "cluster_member_fingerprints": cluster_fps,
            "cluster_member_count": len(cluster_fps),
            "cluster_window_seconds": CLUSTER_WINDOW_SECONDS,
            "cluster_latch_seconds": CLUSTER_LATCH_SECONDS,
            "original_fingerprint": fp,
            "original_summary": original_summary,
        }
        fp = cluster_fp
        result["fingerprint"] = fp
        result["clustered"] = True

    briefing = _format_briefing(
        watcher, severity, summary, context, fp, fix_hints, resolved_host,
    )
    binary = _pane_ask_binary()
    if not binary:
        _log_event({
            "ts": canonical_ts(),
            "watcher": watcher, "severity": severity, "summary": summary,
            "fingerprint": fp, "event": "bypass",
            "reason": "pane-ask-v2 binary not found",
            "source_host": _SOURCE_HOST, "target_host": resolved_host,
        })
        result["bypassed"] = True
        _deliver_bypass(
            watcher, severity, summary, briefing, "no pane-ask-v2",
            r, bypass_priority, fp, resolved_host,
        )
        return result

    label_args: list[str] = []
    if not os.environ.get("TMUX_PANE"):
        label_value = os.environ.get("PANE_ASK_LABEL") or watcher or "doctor-escalate"
        label_args = ["--label", label_value]

    target_args = [*ssh_args, doctor_pane]
    last_err = ""
    for attempt, delay in enumerate((0, 5, 15), start=1):
        if delay:
            time.sleep(delay)
        try:
            proc = subprocess.run(
                [binary, "--require-ack", "--auto-recover-wedge", *label_args, *target_args, briefing],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode == 0:
                result["dispatched"] = True
                _log_event({
                    "ts": canonical_ts(),
                    "watcher": watcher, "severity": severity, "summary": summary,
                    "fingerprint": fp, "event": "dispatched", "attempt": attempt,
                    "source_host": _SOURCE_HOST, "target_host": resolved_host,
                    **({"context": context} if context else {}),
                })
                return result
            stderr_out = proc.stderr or ""
            stdout_out = proc.stdout or ""
            combined = stderr_out + stdout_out
            last_err = f"rc={proc.returncode} stderr={stderr_out[:200]}"
            if "not found" in combined and attempt < 3:
                time.sleep(30 * attempt)
                continue
            if proc.returncode == 7:
                last_err = f"rate_limited rc=7 stderr={stderr_out[:200]}"
        except subprocess.TimeoutExpired:
            last_err = "timeout"
        except Exception as e:
            last_err = f"exc={e}"

    if r is not None:
        try:
            r.setex(f"doctor:escalation:{fp}", BYPASS_REARM_TTL_SECONDS, json.dumps({
                "watcher": watcher, "severity": severity, "summary": summary,
            }))
        except Exception:
            pass
    result["bypassed"] = True
    _deliver_bypass(
        watcher, severity, summary, briefing, last_err,
        r, bypass_priority, fp, resolved_host,
    )
    return result


def _deliver_bypass(
    watcher: str,
    severity: str,
    summary: str,
    briefing: str,
    reason: str,
    redis_conn,
    bypass_priority: Optional[int],
    fingerprint: Optional[str],
    target_host: str,
) -> None:
    bypass_count = _record_bypass(redis_conn, target_host)
    prio = bypass_priority if bypass_priority is not None else SEVERITIES.get(severity, 0)
    soft_pane_hold = _is_soft_pane_hold(reason)
    send_pushover = not soft_pane_hold or bypass_count >= BYPASS_THRESHOLD
    # [CRITIC-FIX SEV-2#1] Distinct subtype for rate-limited bypasses.
    if "rate_limited" in reason or "rc=7" in reason:
        title_prefix = f"[OVERSEER-VOICE-RATE-LIMITED-{target_host}]"
        transport_status = f"Overseer Voice route rate-limited - {reason}"
    elif soft_pane_hold:
        title_prefix = f"[OVERSEER-VOICE-HOLD-{target_host}]"
        transport_status = f"Overseer Voice route held by pane safety gate - {reason}"
    else:
        title_prefix = f"[OVERSEER-VOICE-BYPASS-{target_host}]"
        transport_status = f"Overseer Voice route failed - {reason}"
    title = f"{title_prefix} {watcher}/{severity}: {summary[:80]}"
    body_parts = [
        transport_status,
        f"bypass #{bypass_count} in last {BYPASS_WINDOW_SECONDS//60}m (host={target_host})",
        "",
        briefing,
    ]
    if bypass_count >= BYPASS_THRESHOLD and "rate_limited" not in reason:
        prio = max(prio, 1)
        body_parts.insert(0, f"WARN: Overseer Voice route for {target_host} failed {bypass_count} times in window")
    elif soft_pane_hold:
        body_parts.insert(0, "SUPPRESSED: pane delivery hold logged without Pushover until transport threshold")
    body = "\n".join(body_parts)
    url = None
    delivery_now = datetime.now(timezone.utc)
    quiet_hours_held = (
        send_pushover
        and prio < CRITICAL_HARD_PRIORITY
        and _in_phone_quiet_hours(delivery_now)
    )
    pushover_sent = False
    if send_pushover:
        try:
            from interactive_links import alert_action_url

            url = alert_action_url(
                source=f"overseer-voice-bypass-{target_host}",
                title=title,
                message=body,
                severity=severity,
            )
        except Exception:
            url = None
        pushover_sent = bool(_pushover_direct(
            title,
            body,
            priority=prio,
            url=url,
            url_title="Send to Overseer Voice" if url else None,
            now=delivery_now,
        ))
    _log_event({
        "ts": canonical_ts(),
        "watcher": watcher, "severity": severity, "summary": summary,
        "event": "bypass", "reason": reason, "bypass_count": bypass_count,
        "pushover_priority": prio, "pushover_sent": pushover_sent,
        "quiet_hours_held": quiet_hours_held,
        "soft_pane_hold": soft_pane_hold,
        "source_host": _SOURCE_HOST, "target_host": target_host,
        **({"fingerprint": fingerprint} if fingerprint else {}),
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
