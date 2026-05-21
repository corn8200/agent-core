"""
sdk_guard.py — Structural rate-limit + call-count guard for claude_agent_sdk.

Installed via sitecustomize.py in the agent-core venv. Runs automatically
before any code imports claude_agent_sdk. Zero per-agent changes needed.

What it does:
  1. Wraps query() to detect Max rate-limit responses → raises RateLimitError
     immediately so the caller stops making calls.
  2. Counts SDK invocations in /tmp/agent-sdk-calls.json. If >HOURLY_CAP
     calls in a rolling hour, sends a P0 Pushover alert (once per hour).

Neither check requires any caller to opt in.
"""

from __future__ import annotations

import inspect
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

CALL_LOG = Path("/tmp/agent-sdk-calls.json")
HOURLY_CAP = 50
ALERT_COOLDOWN = 3600  # only alert once per hour even if cap stays exceeded
DEFAULT_MODEL = "opus"
DEFAULT_MAX_TURNS = 20
DEFAULT_PERMISSION_MODE = "bypassPermissions"
DEFAULT_EFFORT = "max"
_EMPTY_MCP_CONFIG = str(Path(__file__).resolve().parent / "mcp-empty.json")


def _default_reason() -> str:
    frame = inspect.currentframe()
    for _ in range(4):
        frame = frame.f_back if frame else None
    if not frame:
        return "sdk_guard Claude automation"
    return f"sdk_guard Claude automation from {frame.f_code.co_filename}:{frame.f_lineno}"


def _intent_env(existing=None) -> dict:
    env = dict(existing or {})
    env.setdefault("CLAUDE_RUN_MODE", os.environ.get("CLAUDE_RUN_MODE", "automation"))
    env.setdefault("CLAUDE_RUN_PROFILE", os.environ.get("CLAUDE_RUN_PROFILE", "normal"))
    env.setdefault("CLAUDE_RUN_REASON", os.environ.get("CLAUDE_RUN_REASON") or _default_reason())
    return env


def _apply_intent_defaults(sdk, options):
    try:
        from core.hooks import AGENT_HOOKS
        from core.thinking import STANDARD
    except Exception:
        AGENT_HOOKS = None
        STANDARD = None

    if options is None:
        kwargs = {
            "model": DEFAULT_MODEL,
            "permission_mode": DEFAULT_PERMISSION_MODE,
            "max_turns": DEFAULT_MAX_TURNS,
            "cwd": str(Path.home()),
            "mcp_servers": _EMPTY_MCP_CONFIG,
            "env": _intent_env(),
            "effort": DEFAULT_EFFORT,
        }
        if AGENT_HOOKS is not None:
            kwargs["hooks"] = AGENT_HOOKS
        if STANDARD is not None:
            kwargs["thinking"] = STANDARD
        return sdk.ClaudeAgentOptions(**kwargs)
    try:
        options.env = _intent_env(getattr(options, "env", None))
    except (AttributeError, TypeError):
        pass
    defaults = {
        "model": DEFAULT_MODEL,
        "permission_mode": DEFAULT_PERMISSION_MODE,
        "max_turns": DEFAULT_MAX_TURNS,
        "cwd": str(Path.home()),
        "mcp_servers": _EMPTY_MCP_CONFIG,
        "effort": DEFAULT_EFFORT,
    }
    if AGENT_HOOKS is not None:
        defaults["hooks"] = AGENT_HOOKS
    if STANDARD is not None:
        defaults["thinking"] = STANDARD
    for attr, value in defaults.items():
        try:
            if getattr(options, attr, None) is None:
                setattr(options, attr, value)
        except (AttributeError, TypeError):
            pass
    return options


RATE_LIMIT_PHRASES = [
    "hit your limit",
    "you've hit your limit",
    "usage limit",
    "resets at",
    "resets 2pm",
    "rate limit",
    "ratelimit",
    "limit reached",
]


class RateLimitError(RuntimeError):
    """Raised when the Max CLI signals a rate limit in a response body."""


# ---------------------------------------------------------------------------
# Call counter
# ---------------------------------------------------------------------------

def _load_log() -> dict:
    try:
        return json.loads(CALL_LOG.read_text())
    except Exception:
        return {"calls": [], "last_alert": 0}


def _save_log(data: dict) -> None:
    try:
        CALL_LOG.write_text(json.dumps(data))
    except Exception:
        pass


def _record_and_check() -> tuple[int, bool]:
    """Record this invocation. Returns (rolling_count, should_alert)."""
    data = _load_log()
    now = time.time()
    calls = [t for t in data.get("calls", []) if now - t < 3600]
    calls.append(now)
    data["calls"] = calls

    count = len(calls)
    last_alert = data.get("last_alert", 0)
    should_alert = count > HOURLY_CAP and (now - last_alert) > ALERT_COOLDOWN
    if should_alert:
        data["last_alert"] = now

    _save_log(data)
    return count, should_alert


def _pushover_alert(count: int) -> None:
    try:
        import asyncio

        title = "SDK runaway alert"
        message = f"agent-core made {count} SDK calls in the last hour (cap={HOURLY_CAP}). Check logs."
        url = None
        url_title = None
        try:
            from core.interactive_links import alert_action_url

            url = alert_action_url(
                source="sdk-guard",
                title=title,
                message=message,
                severity="warn",
            )
            url_title = "Send to Mac panel 3"
        except Exception:
            pass
        from core.pushover import send_pushover

        coro = send_pushover(
            title=title,
            message=message,
            priority=0,
            url=url,
            url_title=url_title,
            timeout=5,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
        else:
            loop.create_task(coro)
    except Exception:
        pass  # never block a real agent call over an alert failure


# ---------------------------------------------------------------------------
# Rate-limit text detection
# ---------------------------------------------------------------------------

def _looks_rate_limited(text: str) -> bool:
    lower = text.lower()
    return any(p in lower for p in RATE_LIMIT_PHRASES)


# ---------------------------------------------------------------------------
# Patch
# ---------------------------------------------------------------------------

def patch() -> None:
    """Monkey-patch claude_agent_sdk.query with the guard wrapper."""
    try:
        import claude_agent_sdk as _sdk
    except ImportError:
        return

    original_query = _sdk.query

    async def guarded_query(*args, **kwargs):  # type: ignore[override]
        kwargs["options"] = _apply_intent_defaults(_sdk, kwargs.get("options"))
        count, should_alert = _record_and_check()
        if should_alert:
            _pushover_alert(count)

        async for msg in original_query(*args, **kwargs):
            # Inspect text blocks for rate-limit signals
            text = None
            if hasattr(msg, "text"):
                text = msg.text
            elif hasattr(msg, "content") and isinstance(msg.content, str):
                text = msg.content

            if text and _looks_rate_limited(text):
                raise RateLimitError(text[:300])

            yield msg

    _sdk.query = guarded_query  # type: ignore[assignment]
