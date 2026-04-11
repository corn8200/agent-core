"""Extended thinking presets for claude-agent-sdk calls.

Claude Opus 4.6 supports extended thinking — an explicit reasoning budget the
model burns before emitting the final answer. Measurably improves quality on
hard reasoning tasks (synthesis, multi-constraint optimization, judgment calls).
Wasted tokens on trivial tasks (email drafts, status pings).

Usage:
    from core.thinking import HEAVY, STANDARD, LIGHT, ADAPTIVE

    options = ClaudeAgentOptions(
        model="opus",
        thinking=HEAVY,          # 12k budget for hard reasoning
        effort="max",            # always max per user preference
        hooks=AGENT_HOOKS,
        ...
    )

Picking a preset:
    HEAVY    — brief synthesis, research synthesis, resume/cover letter tailoring,
               swarm result merge, job-fit judgment, proposal drafting
    STANDARD — handler diagnosis, watch-commander memo execution, single swarm agent
    LIGHT    — simple Q&A, status checks, straightforward classification
    ADAPTIVE — let Claude decide (good default when budget is unknown)
    OFF      — explicitly disable (rare — only for pure formatting tasks)
"""

from typing import Any

HEAVY: dict[str, Any] = {"type": "enabled", "budget_tokens": 12000}
STANDARD: dict[str, Any] = {"type": "enabled", "budget_tokens": 6000}
LIGHT: dict[str, Any] = {"type": "enabled", "budget_tokens": 3000}
ADAPTIVE: dict[str, Any] = {"type": "adaptive"}
OFF: dict[str, Any] = {"type": "disabled"}
