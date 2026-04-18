"""mac_sdk — max-authenticated claude_agent_sdk wrapper for Mac-side agents.

Mirrors the VPS /srv/apps/lib/vps_sdk/ pattern. Import from here instead of
claude_agent_sdk directly:

    from core.mac_sdk import query, ClaudeAgentOptions

What this guarantees (so individual callers don't have to):
  1. Billing scrub runs at module-import time — pops ANTHROPIC_API_KEY and
     friends from os.environ BEFORE claude_agent_sdk is loaded. Bypass is
     impossible: you can't import the SDK through this module without the
     scrub running first.
  2. Wrapped query() appends every invocation to /tmp/mac-sdk-calls.json
     and raises SDKQuotaExceeded HARD (not a Pushover warning) when the
     50-calls-per-hour ceiling is hit. Prevents runaway loops.
  3. When called with options=None, a sane default ClaudeAgentOptions is
     built with AGENT_HOOKS installed and max_budget_usd=1.00. If the
     caller supplies their own options, only hooks is back-filled (when
     hooks is None) — max_budget_usd is left exactly as the caller passed
     it (including explicit None), matching existing call-site behavior.
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_BILLING_LEAK_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_CONSOLE_KEY",
    "ANTHROPIC_CONSOLE_KEY_MAC",
    "ANTHROPIC_CONSOLE_KEY_VPS",
)

for _var in _BILLING_LEAK_VARS:
    os.environ.pop(_var, None)

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_agent_sdk import query as _real_query  # noqa: E402
from claude_agent_sdk import *  # noqa: E402, F401, F403
from claude_agent_sdk import ClaudeAgentOptions  # noqa: E402

from core.hooks import AGENT_HOOKS  # noqa: E402


CALL_LOG = Path("/tmp/mac-sdk-calls.json")
HOURLY_CAP = 50
HOURLY_WINDOW = 3600
PRUNE_WINDOW = 7200
DEFAULT_MAX_BUDGET_USD = 1.00

_OPTIONS_SUPPORTS_BUDGET = "max_budget_usd" in inspect.signature(ClaudeAgentOptions).parameters

_EMPTY_MCP_CONFIG = str(Path(__file__).resolve().parent / "mcp-empty.json")


class SDKQuotaExceeded(RuntimeError):
    """Raised when the hourly SDK call ceiling is reached. HARD stop."""


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _load_entries() -> list[dict]:
    try:
        raw = CALL_LOG.read_text()
    except FileNotFoundError:
        return []
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return data


def _write_entries(entries: list[dict]) -> None:
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".mac-sdk-calls.", suffix=".tmp", dir=str(CALL_LOG.parent)
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(entries, f)
        os.replace(tmp_path, CALL_LOG)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _check_and_record_call() -> None:
    """Append this call to the log, pruning old entries, and enforce the cap.

    Raises SDKQuotaExceeded if there are already >= HOURLY_CAP entries within
    the last HOURLY_WINDOW seconds (i.e., this would be the 51st).
    """
    now = _now_ts()
    entries = _load_entries()
    fresh = [e for e in entries if isinstance(e, dict) and now - _entry_ts(e) < PRUNE_WINDOW]

    window_count = sum(1 for e in fresh if now - _entry_ts(e) < HOURLY_WINDOW)
    if window_count >= HOURLY_CAP:
        _write_entries(fresh)
        raise SDKQuotaExceeded(
            f"rate ceiling: {HOURLY_CAP} calls/hr reached "
            f"(seen {window_count} in last {HOURLY_WINDOW}s)"
        )

    fresh.append({"ts": datetime.now(timezone.utc).isoformat()})
    _write_entries(fresh)


def _entry_ts(entry: dict) -> float:
    try:
        return datetime.fromisoformat(entry["ts"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return 0.0


def _apply_defaults(options: ClaudeAgentOptions | None) -> ClaudeAgentOptions:
    if options is None:
        kwargs: dict = {"hooks": AGENT_HOOKS, "mcp_servers": _EMPTY_MCP_CONFIG}
        if _OPTIONS_SUPPORTS_BUDGET:
            kwargs["max_budget_usd"] = DEFAULT_MAX_BUDGET_USD
        return ClaudeAgentOptions(**kwargs)

    if getattr(options, "hooks", None) is None:
        try:
            options.hooks = AGENT_HOOKS
        except (AttributeError, TypeError):
            pass

    if not getattr(options, "mcp_servers", None):
        try:
            options.mcp_servers = _EMPTY_MCP_CONFIG
        except (AttributeError, TypeError):
            pass
    return options


async def query(prompt, options: ClaudeAgentOptions | None = None):
    """Guarded async generator wrapping claude_agent_sdk.query.

    - Enforces 50/hr HARD cap via SDKQuotaExceeded
    - Back-fills AGENT_HOOKS when options has none
    - Builds a sane default ClaudeAgentOptions when options is None
    - Forwards all messages from the underlying SDK query
    """
    _check_and_record_call()
    options = _apply_defaults(options)
    async for msg in _real_query(prompt=prompt, options=options):
        yield msg


__all__ = [
    "ClaudeAgentOptions",
    "SDKQuotaExceeded",
    "query",
]
