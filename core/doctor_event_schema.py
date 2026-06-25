"""Canonical schema for doctor.jsonl events — TypedDict + validator + timestamp helpers.

Every helper-emitted log entry MUST pass through validate_event() before write.
Callers (doctor_escalate, doctor pane response logger) should use canonical_ts()
for the ts field rather than datetime.isoformat().

Event types (event field):

  Producer-side — written by doctor_escalate.py:
    dispatched   — briefing successfully delivered to Overseer Voice via pane-ask-v2
    dedup_hit    — suppressed; same fingerprint still live in Redis within TTL
    bypass       — Voice route failed; routed direct to Pushover as [OVERSEER-VOICE-BYPASS]
    rate_limited — dropped by producer-side token-bucket (#648)
    local_only   — synthetic/test escalation logged locally; no Redis/pane/Pushover route

  Doctor-side — written by doctor pane after handling an escalation:
    response     — doctor's assessment + action for a received escalation

Required fields (all events):
    ts            str   ISO-8601 UTC Z-suffix microsecond  "2026-04-25T12:00:00.123456Z"
    watcher       str   producer identity  (aliases: escalation, source)
    severity      str   ok|notice|warn|error|critical
    summary       str   one-liner for subject lines / search
    event         str   dispatched|dedup_hit|bypass|rate_limited|local_only|response

Optional — producer events (dispatched|dedup_hit|bypass|rate_limited|local_only):
    fingerprint   str   hex SHA-1 dedup key  (alias: dedup_key)
    context_json  str   JSON-serialized producer context dict (auto-derived from context if absent)
    source_host   str   mac|vps
    bypass        bool  True when Overseer Voice bypass path fired  (default False)
    attempt       int   dispatch attempt number             (dispatched only)
    ttl_remaining_s int seconds left on dedup key          (dedup_hit only)
    reason        str   why bypassed or deduped            (bypass|dedup_hit)
    bypass_count  int   cumulative bypasses in window       (bypass only)
    pushover_priority int  0/1/2                            (bypass only)
    pushover_sent bool True when direct Pushover actually fired
    soft_pane_hold bool True when pane-ask safety hold suppressed Pushover
    quota         dict  rate-limit quota applied            (rate_limited only)

Optional — doctor response events (event=response):
    fingerprint   str   links back to the producer row
    action_taken  str   no-op|executed_fix|filed_backlog|notified_user
    escalated_to_user bool  True if John was iMessaged
    fix_applied   str   shell command or action executed (if any)
    outcome       str   ok|failed|in_flight|deferred
    assessment    str   doctor's reasoning text
    backlog_id    int   agent-cp backlog ID if filed
    human_required bool  True if task needs John's hands
    notes         str   free-form doctor observations
    source        str   source host label (vps|mac|vps-claude:N)

Aliases handled by validate_event:
    escalation  → watcher
    source      → watcher  (when watcher absent — legacy)
    dedup_key   → fingerprint
    dispatched_at → ts  (when ts absent)
    context     dict → context_json  str  (auto-serialized when context present)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal, Optional

try:
    from typing import TypedDict
except ImportError:
    from typing_extensions import TypedDict  # type: ignore[no-redef]

Severity = Literal["ok", "notice", "warn", "error", "critical"]
EventType = Literal["dispatched", "dedup_hit", "bypass", "rate_limited", "local_only", "response"]

REQUIRED_FIELDS: frozenset = frozenset({"ts", "watcher", "severity", "summary", "event"})
VALID_SEVERITIES: frozenset = frozenset({"ok", "notice", "warn", "error", "critical"})
VALID_EVENTS: frozenset = frozenset({"dispatched", "dedup_hit", "bypass", "rate_limited", "local_only", "response"})


class DoctorEvent(TypedDict, total=False):
    # --- required ---
    ts: str
    watcher: str
    severity: str
    summary: str
    event: str
    # --- producer common ---
    fingerprint: str
    context_json: str
    source_host: str
    target_host: str
    bypass: bool
    # --- dispatched ---
    attempt: int
    # --- dedup_hit ---
    ttl_remaining_s: int
    # --- bypass ---
    reason: str
    bypass_count: int
    pushover_priority: int
    pushover_sent: bool
    soft_pane_hold: bool
    # --- rate_limited ---
    quota: dict
    # --- doctor response ---
    action_taken: str
    escalated_to_user: bool
    fix_applied: str
    outcome: str
    assessment: str
    backlog_id: int
    human_required: bool
    notes: str
    source: str


def canonical_ts(dt: Optional[datetime] = None) -> str:
    """Return canonical Z-suffix microsecond UTC timestamp.

    Uses strftime not isoformat() — isoformat emits +00:00, not Z.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def normalize_ts(ts_str: str) -> str:
    """Normalize any ISO-8601 timestamp string to canonical Z-suffix form."""
    if not ts_str:
        return canonical_ts()
    if ts_str.endswith("Z") and "." in ts_str and len(ts_str) == 27:
        return ts_str
    try:
        normalized = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return canonical_ts(dt)
    except (ValueError, TypeError):
        return canonical_ts()


def coerce_watcher(event: dict) -> dict:
    """Promote escalation/source → watcher when watcher absent (legacy aliases)."""
    if "watcher" not in event:
        for alias in ("escalation", "source"):
            if alias in event:
                event = dict(event)
                event["watcher"] = event[alias]
                break
    return event


def validate_event(event: dict) -> dict:
    """Validate and normalize a doctor event dict.

    Transformations applied (in order):
      1. Alias coercions: escalation/source → watcher, dedup_key → fingerprint,
         dispatched_at → ts (when ts absent).
      2. Normalize ts to canonical Z-suffix form.
      3. Serialize context dict → context_json string (when context present and
         context_json absent); strip inline context dict from output.
      4. Ensure bypass field present (default False).
      5. Validate required fields.
      6. Validate severity against VALID_SEVERITIES.
      7. Unknown event types are allowed through (doctor-side freeform entries
         should not be hard-rejected by the producer validator).

    Returns a new dict; never mutates input.
    Raises ValueError for missing required fields or invalid severity.
    """
    event = coerce_watcher(event)
    event = dict(event)

    # dedup_key → fingerprint
    if "dedup_key" in event and "fingerprint" not in event:
        event["fingerprint"] = event.pop("dedup_key")
    elif "dedup_key" in event:
        event.pop("dedup_key")

    # dispatched_at → ts when ts absent
    if "ts" not in event and "dispatched_at" in event:
        event["ts"] = event.pop("dispatched_at")
    elif "dispatched_at" in event:
        event.pop("dispatched_at")

    # normalize ts
    event["ts"] = normalize_ts(event.get("ts", ""))

    # context dict → context_json string (strip inline context from output)
    context_dict = event.pop("context", None)
    if context_dict is not None and "context_json" not in event:
        try:
            event["context_json"] = json.dumps(context_dict, default=str)
        except Exception:
            event["context_json"] = str(context_dict)

    # ensure bypass bool present
    if "bypass" not in event:
        event["bypass"] = False

    # required field check
    missing = REQUIRED_FIELDS - event.keys()
    if missing:
        raise ValueError(f"doctor event missing required fields: {sorted(missing)}")

    # severity check
    if event["severity"] not in VALID_SEVERITIES:
        raise ValueError(
            f"invalid severity {event['severity']!r}; must be one of {sorted(VALID_SEVERITIES)}"
        )

    return event


if __name__ == "__main__":
    cases = [
        # canonical producer event
        {
            "ts": "2026-04-25T12:00:00.000000Z",
            "watcher": "w", "severity": "warn", "summary": "t", "event": "dispatched",
        },
        # offset tz normalization
        {
            "ts": "2026-04-25T12:00:00.123456+00:00",
            "watcher": "w", "severity": "notice", "summary": "offset", "event": "dedup_hit",
        },
        # escalation alias
        {
            "ts": "2026-04-25T12:00:00Z",
            "escalation": "legacy-watcher", "severity": "ok", "summary": "alias", "event": "response",
        },
        # dedup_key alias
        {
            "ts": "2026-04-25T12:00:00.000000Z",
            "watcher": "w", "severity": "warn", "summary": "dk", "event": "bypass",
            "dedup_key": "abc123",
        },
        # context dict → context_json
        {
            "ts": "2026-04-25T12:00:00.000000Z",
            "watcher": "w", "severity": "notice", "summary": "ctx", "event": "dispatched",
            "context": {"disk_pct": 95, "host": "vps"},
        },
        # dispatched_at → ts when ts absent
        {
            "dispatched_at": "2026-04-25T12:00:00.000000Z",
            "watcher": "w", "severity": "notice", "summary": "da", "event": "dispatched",
        },
    ]
    for c in cases:
        r = validate_event(c)
        assert r["ts"].endswith("Z"), f"not Z: {r['ts']}"
        assert "watcher" in r, f"no watcher: {r}"
        assert "event" in r, f"no event: {r}"
        assert "bypass" in r, f"no bypass: {r}"
        if "dedup_key" in c:
            assert "fingerprint" in r and "dedup_key" not in r, f"dedup_key not promoted: {r}"
        if "context" in c:
            assert "context_json" in r and "context" not in r, f"context not serialized: {r}"
        if "dispatched_at" in c and "ts" not in c:
            assert r["ts"].endswith("Z"), f"dispatched_at not promoted: {r}"
        print(json.dumps(r))

    try:
        validate_event({"ts": "x", "severity": "warn", "summary": "x", "event": "dispatched"})
        assert False, "should have raised"
    except ValueError as e:
        print(f"OK missing watcher: {e}")

    try:
        validate_event({"ts": "x", "watcher": "x", "severity": "bad", "summary": "x", "event": "dispatched"})
        assert False, "should have raised"
    except ValueError as e:
        print(f"OK bad severity: {e}")

    print("smoke tests passed")
