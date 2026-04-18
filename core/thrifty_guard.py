"""
thrifty_guard.py — When THRIFTY_MODE=1, downgrade every SDK call from
opus → haiku at query() time. Chains after sdk_guard.patch() in sitecustomize.

Rewrites three surfaces:
  1. options.model          — the top-level model on ClaudeAgentOptions
  2. options.agents[*].model — every AgentDefinition passed via options.agents
  3. options.fallback_model — if the SDK sets one, downgrade that too

Thrifty is considered "on" if either THRIFTY_MODE=1 is set in os.environ OR
~/.config/thrifty.env contains `export THRIFTY_MODE=1`. The second case
matters for LaunchAgents that started before the thrifty env file existed
but whose ClaudeAgentOptions is built after the file appears.
"""

from __future__ import annotations

import os
from pathlib import Path


def _thrifty_on() -> bool:
    if os.environ.get("THRIFTY_MODE") == "1":
        return True
    env_file = Path.home() / ".config" / "thrifty.env"
    if not env_file.exists():
        return False
    try:
        for line in env_file.read_text().splitlines():
            if line.startswith("export THRIFTY_MODE=") and line.endswith("=1"):
                return True
    except Exception:
        pass
    return False


def _thrifty_soft_on() -> bool:
    if _thrifty_on():
        return False  # hard wins
    if os.environ.get("THRIFTY_SOFT_MODE") == "1":
        return True
    env_file = Path.home() / ".config" / "thrifty-soft.env"
    if not env_file.exists():
        return False
    try:
        for line in env_file.read_text().splitlines():
            if line.startswith("export THRIFTY_SOFT_MODE=") and line.endswith("=1"):
                return True
    except Exception:
        pass
    return False


def _downgrade(model: str | None) -> str | None:
    """Map a model name to its thrifty equivalent.

    Hard thrifty:  opus → haiku, sonnet → haiku
    Soft thrifty:  opus → sonnet, sonnet → sonnet (unchanged)
    Neither on:    passthrough
    """
    if not model:
        return model
    lower = model.lower()
    if _thrifty_on():
        if "opus" in lower or "sonnet" in lower:
            return "haiku"
        return model
    if _thrifty_soft_on():
        if "opus" in lower:
            return "sonnet"
        return model
    return model


def _rewrite_options(options) -> None:
    if options is None:
        return
    try:
        orig = getattr(options, "model", None)
        new = _downgrade(orig)
        if new and new != orig:
            options.model = new
    except Exception:
        pass
    try:
        fb = getattr(options, "fallback_model", None)
        new_fb = _downgrade(fb)
        if new_fb and new_fb != fb:
            options.fallback_model = new_fb
    except Exception:
        pass
    try:
        agents = getattr(options, "agents", None)
        if agents:
            for agent_def in agents.values():
                orig = getattr(agent_def, "model", None)
                new = _downgrade(orig)
                if new and new != orig:
                    agent_def.model = new
    except Exception:
        pass


def patch() -> None:
    """Chain a thrifty layer on top of whatever claude_agent_sdk.query is now.

    Must run AFTER sdk_guard.patch() so our wrapper sits outside the guard —
    we rewrite kwargs, then hand them to the guarded query.
    """
    try:
        import claude_agent_sdk as _sdk
    except ImportError:
        return

    wrapped_query = _sdk.query

    async def thrifty_query(**kwargs):  # type: ignore[override]
        if _thrifty_on() or _thrifty_soft_on():
            _rewrite_options(kwargs.get("options"))
        async for msg in wrapped_query(**kwargs):
            yield msg

    _sdk.query = thrifty_query  # type: ignore[assignment]
