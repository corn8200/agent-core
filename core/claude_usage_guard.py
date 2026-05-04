"""Shared Claude Max usage gate for Mac automation."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

try:
    from core.usage_cache import read_active
except Exception:  # pragma: no cover
    read_active = None  # type: ignore


class ClaudeUsageGateError(RuntimeError):
    """Raised when unattended Claude work is blocked by usage headroom."""


def _truthy_env(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        value = float(value)
        return value / 100.0 if value > 1.0 else value
    return None


def _format_reset(epoch: Any) -> str | None:
    if not isinstance(epoch, (int, float)):
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def claude_usage_block_reason(phase: str = "claude", *, threshold: float | None = None) -> str | None:
    if _truthy_env("CLAUDE_USAGE_GUARD_IGNORE", "0"):
        return None
    if _truthy_env("CLAUDE_USAGE_GUARD_DISABLE", "0"):
        return None
    if read_active is None:
        return None
    if threshold is None:
        threshold = _float_env("CLAUDE_USAGE_REFUSE_PCT", 0.80)
    try:
        active = read_active()
    except Exception:
        return None
    if not active:
        return None

    reasons: list[str] = []
    for account, row in active.items():
        if not row.get("read_ok"):
            continue
        five = _as_float(row.get("five_hour_pct"))
        seven = _as_float(row.get("seven_day_pct"))
        primary = _as_float(row.get("primary_pct"))
        five_status = str(row.get("five_hour_status") or "").lower()
        seven_status = str(row.get("seven_day_status") or "").lower()
        try:
            http_code = int(row.get("http_code") or 0)
        except (TypeError, ValueError):
            http_code = 0
        reset = _format_reset(row.get("five_hour_reset") or row.get("seven_day_reset"))
        # Older cache writers used hit_wall for overage rejection, which is normal
        # on Max plans. Only a real 5h/7d rejection or HTTP 429 is a hard wall.
        if five_status == "rejected" or seven_status == "rejected" or http_code == 429:
            parts = []
            if five is not None:
                parts.append(f"5h={five:.0%}")
            if seven is not None:
                parts.append(f"7d={seven:.0%}")
            if reset:
                parts.append(f"reset={reset}")
            suffix = f" ({', '.join(parts)})" if parts else ""
            reasons.append(f"{account} is at Claude usage wall{suffix}")
            continue
        over = []
        if five is not None and five >= threshold:
            over.append(f"5h={five:.0%}")
        if seven is not None and seven >= threshold:
            over.append(f"7d={seven:.0%}")
        if primary is not None and primary >= threshold and not over:
            over.append(f"primary={primary:.0%}")
        if over:
            msg = f"{account} is above Claude automation gate for {phase}: {', '.join(over)} >= {threshold:.0%}"
            if reset:
                msg += f" reset={reset}"
            reasons.append(msg)
    return "; ".join(reasons) if reasons else None


def assert_claude_usage_allowed(phase: str = "claude", *, threshold: float | None = None) -> None:
    reason = claude_usage_block_reason(phase, threshold=threshold)
    if reason:
        raise ClaudeUsageGateError(reason)
