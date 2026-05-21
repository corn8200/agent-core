"""Extended thinking presets for claude-agent-sdk calls.

Claude Opus 4.6+ supports extended thinking — an explicit reasoning budget the
model burns before emitting the final answer. Measurably improves quality on
hard reasoning tasks (synthesis, multi-constraint optimization, judgment calls).
Wasted tokens on trivial tasks (email drafts, status pings).

Usage:
    from core.thinking import ULTRA, HEAVY, STANDARD, LIGHT, ADAPTIVE

    options = ClaudeAgentOptions(
        model="opus",
        thinking=ULTRA,          # 64k budget for Titan / hardest problems
        effort="xhigh",          # new xhigh tier (Opus 4.7+); max for proofs/audits
        hooks=AGENT_HOOKS,
        ...
    )

Picking a preset:
    ULTRA    — Titan runs, architectural decisions, complex multi-system debugging,
               any problem that needs the absolute most reasoning capacity
    HEAVY    — brief synthesis, research synthesis, resume/cover letter tailoring,
               swarm result merge, job-fit judgment, proposal drafting
    STANDARD — handler diagnosis, watch-commander memo execution, single swarm agent
    LIGHT    — simple Q&A, status checks, straightforward classification
    ADAPTIVE — let Claude decide (good default when budget is unknown)
    OFF      — explicitly disable (rare — only for pure formatting tasks)

Effort levels (Opus 4.7+):
    xhigh — new default in Claude Code; scores better than Opus 4.6 max at 100k tokens.
             Use for Titan + any heavy reasoning agent.
    max   — exhaustive reasoning; reserve for proofs, verification, security audits.

Opus 4.6+ supports up to 64k thinking tokens. Budgets are set high (cost not a concern).
"""

from typing import Any

ULTRA: dict[str, Any] = {"type": "enabled", "budget_tokens": 64000}
HEAVY: dict[str, Any] = {"type": "enabled", "budget_tokens": 32000}
STANDARD: dict[str, Any] = {"type": "enabled", "budget_tokens": 16000}
LIGHT: dict[str, Any] = {"type": "enabled", "budget_tokens": 6000}
ADAPTIVE: dict[str, Any] = {"type": "adaptive"}
OFF: dict[str, Any] = {"type": "disabled"}
