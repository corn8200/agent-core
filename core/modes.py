"""
modes.py — Runtime mode flags for agent-core. Currently tracks thrifty mode.

Any agent that wants to change its behavior during a cost-cutting window
(skip Opus, skip paid APIs, skip heavy work) checks these helpers instead of
probing env/files ad-hoc. The helpers are cheap and safe to call from hot paths.

Usage:
    from core.modes import is_thrifty_on, thrifty_expires_at

    if is_thrifty_on():
        model = "haiku"
    else:
        model = "opus"
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path

_ENV_FILE = Path.home() / ".config" / "thrifty.env"
_STATE_FILE = Path.home() / ".config" / "thrifty.state"


def is_thrifty_on() -> bool:
    """True if THRIFTY_MODE is active (env var OR thrifty.env flag file)."""
    if os.environ.get("THRIFTY_MODE") == "1":
        return True
    if not _ENV_FILE.exists():
        return False
    try:
        for line in _ENV_FILE.read_text().splitlines():
            if line.startswith("export THRIFTY_MODE=") and line.endswith("=1"):
                return True
    except Exception:
        pass
    return False


def thrifty_expires_at() -> _dt.datetime | None:
    """Return the activation expiry as a tz-aware datetime, or None if off."""
    if not _STATE_FILE.exists():
        return None
    try:
        s = json.loads(_STATE_FILE.read_text())
        return _dt.datetime.fromisoformat(s["expires_at"])
    except Exception:
        return None


def thrifty_remaining() -> _dt.timedelta | None:
    """Time remaining until auto-revert. None if off. Negative if expired."""
    exp = thrifty_expires_at()
    if exp is None:
        return None
    now = _dt.datetime.now(exp.tzinfo)
    return exp - now


def thrifty_skip_keys() -> frozenset[str]:
    """Paid-API env var names that get suppressed during thrifty mode.

    Mirror of `core.vault._THRIFTY_SKIP_NAMES` — exported here so callers
    don't need to reach into vault internals. Returns an empty set when
    thrifty mode is off so callers can unconditionally `in` against it.
    """
    if not is_thrifty_on():
        return frozenset()
    try:
        from core.vault import _THRIFTY_SKIP_NAMES
        return _THRIFTY_SKIP_NAMES
    except Exception:
        return frozenset({
            "OPENAI_API_KEY",
            "TAVILY_API_KEY",
            "ELEVENLABS_API_KEY",
            "MAPBOX_API_KEY",
            "GOOGLE_MAPS_API_KEY",
        })


def thrifty_model(default: str = "opus") -> str:
    """Return the preferred SDK model given current mode. Default is opus.

    During thrifty mode, callers that bypass the sitecustomize downgrade
    (e.g. direct Anthropic HTTP clients) can call this to pick haiku
    explicitly. The sitecustomize monkeypatch handles claude_agent_sdk
    automatically, so agents using query() don't need to check this at all.
    """
    if is_thrifty_on():
        return "haiku"
    return default


__all__ = [
    "is_thrifty_on",
    "thrifty_expires_at",
    "thrifty_remaining",
    "thrifty_skip_keys",
    "thrifty_model",
]
