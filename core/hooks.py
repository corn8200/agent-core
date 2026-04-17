"""Audit and safety hooks for all SDK agents.

Provides:
  - audit_hook: logs every tool call to ~/logs/agent-audit.jsonl
  - guard_hook: blocks destructive commands (rm -rf, git push --force, DROP TABLE, etc.)
  - AGENT_HOOKS: pre-built hooks dict ready to pass to ClaudeAgentOptions

These hooks run via JSON-RPC callback bridge between Python and the CLI subprocess.
They MUST:
  - Never raise unhandled exceptions (kills the stream)
  - Always return a valid dict (even on error)
  - Complete within the timeout (10s)
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import HookMatcher  # allow-direct-sdk: type symbol only

AUDIT_LOG = os.environ.get("AGENT_AUDIT_LOG") or os.path.expanduser("~/logs/agent-audit.jsonl")

# --- Destructive command patterns ---
DESTRUCTIVE_PATTERNS = [
    r"\brm\s+(-[a-zA-Z]*)?r[a-zA-Z]*f",      # rm -rf, rm -fr
    r"\bgit\s+push\s+.*--force",                # git push --force
    r"\bgit\s+push\s+-f\b",                     # git push -f
    r"\bDROP\s+(TABLE|DATABASE)\b",              # DROP TABLE/DATABASE
    r"\bTRUNCATE\s+TABLE\b",                     # TRUNCATE TABLE
    r"\bgit\s+reset\s+--hard\b",                # git reset --hard
    r"\bgit\s+clean\s+-[a-zA-Z]*f",             # git clean -f
    r"\bmkfs\b",                                 # mkfs (format)
    r"\bdd\s+.*of=/dev/",                        # dd to device
    r"\bgit\s+branch\s+-D\b",                   # git branch -D (force delete)
]

DESTRUCTIVE_RE = re.compile("|".join(DESTRUCTIVE_PATTERNS), re.IGNORECASE)


def _safe_get(obj: Any, key: str, default: Any = "") -> Any:
    """Safely extract a field from hook input (may be dict or object)."""
    try:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)
    except Exception:
        return default


def _write_audit(entry: dict):
    """Append a JSON entry to the audit log. Never raises, but prints to stderr on failure."""
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        print(f"[audit] write failed to {AUDIT_LOG}: {e}", file=sys.stderr)


def audit_write(entry: dict) -> None:
    """Public helper for writing to the agent audit log.

    Used by audit heartbeat and any code that wants to log a structured event
    without going through a full tool_use hook. Never raises.
    """
    _write_audit(entry)


async def audit_hook(input: Any, tool_use_id: str | None, context: Any) -> dict:
    """PostToolUse: log every completed tool call."""
    try:
        tool_name = _safe_get(input, "tool_name", "unknown")
        tool_input = _safe_get(input, "tool_input", {})
        session_id = _safe_get(input, "session_id", "")
        cwd = _safe_get(input, "cwd", "")

        tool_input_str = ""
        try:
            tool_input_str = json.dumps(tool_input, default=str)
            if len(tool_input_str) > 500:
                tool_input_str = tool_input_str[:500] + "..."
        except Exception:
            tool_input_str = str(tool_input)[:500]

        _write_audit({
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "tool_use",
            "tool": tool_name,
            "input": tool_input_str,
            "session": str(session_id)[:12],
            "cwd": str(cwd),
        })
    except Exception:
        pass  # Never crash the stream
    return {}


async def guard_hook(input: Any, tool_use_id: str | None, context: Any) -> dict:
    """PreToolUse: block destructive commands."""
    try:
        tool_name = str(_safe_get(input, "tool_name", ""))
        tool_input = _safe_get(input, "tool_input", {})

        # Extract command text to check
        text = ""
        if tool_name in ("Bash", "bash", "shell"):
            text = str(_safe_get(tool_input, "command", ""))
        elif "sqlite" in tool_name.lower() or "sql" in tool_name.lower():
            text = str(_safe_get(tool_input, "query", "") or _safe_get(tool_input, "sql", ""))
        else:
            text = str(_safe_get(tool_input, "command", "") or _safe_get(tool_input, "query", ""))

        if text and DESTRUCTIVE_RE.search(text):
            _write_audit({
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": "BLOCKED",
                "tool": tool_name,
                "command": text[:200],
                "session": str(_safe_get(input, "session_id", ""))[:12],
            })
            return {
                "decision": "block",
                "reason": f"Destructive command blocked: {text[:100]}",
            }
    except Exception:
        pass  # Never crash the stream — fail open on error
    return {}


# Pre-built hooks dict for ClaudeAgentOptions
AGENT_HOOKS: dict = {
    "PreToolUse": [
        HookMatcher(matcher=None, hooks=[guard_hook], timeout=10.0),
    ],
    "PostToolUse": [
        HookMatcher(matcher=None, hooks=[audit_hook], timeout=10.0),
    ],
}
