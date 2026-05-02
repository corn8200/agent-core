"""AgentDefinition objects for all named agents, translated from ~/.claude/agents/*.md."""

import warnings
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message=r".*claude-(?:opus|sonnet)-4-20250514",
)

from pathlib import Path

from claude_agent_sdk import AgentDefinition  # allow-direct-sdk: type symbol only

_AGENTS_DIR = Path.home() / ".claude" / "agents"


def _load_prompt(name: str) -> str:
    """Load an agent's system prompt from its markdown file (content after frontmatter)."""
    path = _AGENTS_DIR / f"{name}.md"
    text = path.read_text()
    # Strip YAML frontmatter (between --- markers)
    parts = text.split("---", 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return text.strip()


# --- Agent Definitions ---

scout = AgentDefinition(
    description="Research & intelligence specialist — deep web research, market analysis, company background, regulatory landscape, competitor scans. Uses WebSearch + WebFetch with Read/Grep/Write to surface facts, citations, and structured briefings. Pick over Turbo when a question needs multi-source synthesis. Read-only on local files (no Edit, no Bash). 20-turn budget, sonnet by default (escalates to opus on !opus or deep-dive keywords).",
    prompt=_load_prompt("scout"),
    model="sonnet",
    tools=["Read", "Grep", "Write", "WebSearch", "WebFetch"],
    maxTurns=20,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

forge = AgentDefinition(
    description="Document builder for polished, tailored deliverables — resumes, cover letters, proposals, business plans, briefings, cold outreach. Reads source inputs, edits drafts in place, writes finals via Read/Edit/Write/Bash. Pick over Anvil when the deliverable is prose, not code. No web access — caller supplies the facts. 15-turn budget, sonnet by default (escalates to opus on !opus for high-stakes deliverables).",
    prompt=_load_prompt("forge"),
    model="sonnet",
    tools=["Read", "Edit", "Write", "Bash"],
    maxTurns=15,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

wrench = AgentDefinition(
    description="Infrastructure & DevOps specialist — health checks, troubleshooting, service management across Mac Mini, VPS, and Raspberry Pi. Uses Bash + Read/Grep/Write to inspect logs, restart daemons, check disk and network, and chase service errors. Pick when something is broken or you need a status snapshot. No web tools — diagnose locally. 25-turn budget, sonnet by default (auto-escalates to opus on outage keywords: down, broken, outage, debug, root cause, crashed, failing).",
    prompt=_load_prompt("wrench"),
    model="sonnet",
    tools=["Bash", "Read", "Grep", "Write"],
    maxTurns=25,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

dispatch = AgentDefinition(
    description="Communications & sending specialist — email drafting, follow-ups, contact coordination via sentry-mailqueue (business) and SMTP (personal). Uses Bash + Read/Write to compose and ship messages, plus Mail.app via osascript for inbox checks. Pick when the deliverable is a sent message, not research. No web search, no Edit — composes from given facts. 10-turn budget, sonnet.",
    prompt=_load_prompt("dispatch"),
    model="sonnet",
    tools=["Bash", "Read", "Write"],
    maxTurns=10,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

ledger = AgentDefinition(
    description="Data & financial analysis specialist — budgets, spending reconciliation, CSV crunching, bill scanning, Monarch Money queries, transaction categorization. Uses Bash + Read/Write to run sqlite queries, build summary tables, and cross-reference Mail for upcoming bills. Pick when the answer is a number, table, or financial comparison. No web tools — local data only. 20-turn budget, sonnet.",
    prompt=_load_prompt("ledger"),
    model="sonnet",
    tools=["Bash", "Read", "Write"],
    maxTurns=20,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

toolsmith = AgentDefinition(
    description="Meta-agent for reviewing agent performance logs and pruning memory — reads agent_performance.json, groups failures by pattern, proposes surgical prompt improvements for recurring issues, audits learned/*.md entries. Uses Read/Write/Edit/Grep/Glob only — no Bash, no web, no shell side effects. Pick when fixing recurring agent failures or curating learning files. 15-turn budget, sonnet, surgical scope.",
    prompt=_load_prompt("toolsmith"),
    model="sonnet",
    tools=["Read", "Write", "Edit", "Grep", "Glob"],
    maxTurns=15,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

titan = AgentDefinition(
    description="Maximum-firepower opus solver for genuinely hard problems — architectural decisions, multi-system debugging, deep research, complex refactors, end-to-end design work. Spawns parallel sub-agent swarms via the Agent tool, extended thinking, no token rationing. Full toolset: Read/Write/Edit/Grep/Glob/Bash/WebSearch/WebFetch/Agent/TodoWrite. Pick when the problem deserves the heaviest hammer; skip for quick lookups. 60-turn budget, xhigh effort.",
    prompt=_load_prompt("titan"),
    model="opus",
    tools=["Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebSearch", "WebFetch", "Agent", "TodoWrite"],
    maxTurns=60,
    permissionMode="bypassPermissions",
    effort="xhigh",  # type: ignore[arg-type]  # xhigh added in CLI 2.1.112; SDK Literal not yet updated
    memory="project",
)

anvil = AgentDefinition(
    description="Code builder. Opus direct code lane — writes, refactors, and ships code. Worktree-first, test-driven, verifies its own diffs before reporting done. Uses Read/Write/Edit/Grep/Glob/Bash/TodoWrite. Pick over Forge for code deliverables, over Titan for routine implementation work, over Scout when the task ends in shipped code rather than research. 40-turn budget.",
    prompt=_load_prompt("anvil"),
    model="opus",
    tools=["Read", "Write", "Edit", "Grep", "Glob", "Bash", "TodoWrite"],
    maxTurns=40,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

critic = AgentDefinition(
    description="Adversarial code and decision reviewer — fresh-eyes second opinion before merges, destructive ops, or risky plans. Reads diffs, schemas, migrations, and proposals to hunt missed edge cases, broken invariants, lock risk, and rollback gaps. Read-only toolset: Read/Grep/Glob/Bash/WebSearch/WebFetch — never edits or writes. Pick when you want a cold take from outside the original author's head. 25-turn budget, opus.",
    prompt=_load_prompt("critic"),
    model="opus",
    tools=["Read", "Grep", "Glob", "Bash", "WebSearch", "WebFetch"],
    maxTurns=25,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

herald = AgentDefinition(
    # heavy — client-facing deliverables always justify Opus
    description="Brand & deliverable QC — reads brand-kit (3 tiers: Professional/Family/Sentry AI Thermal), enforces John's taste + industry-standard quality rules on resumes, proposals, decks, client reports, websites, emails, and any other artifact. Runs mechanical QC (page count, fill ratio, section-across-page splits, ATS text extraction via pdftotext, brand-token drift) and auto-fixes mechanical violations by editing content (compress/expand bullets, restructure sections, adjust page breaks). Blocks + reports on subjective violations: images in ATS resumes, wrong tier tokens, fancy fonts where plain is required, brand voice drift, factual errors (UEI/CAGE/phone). Uses Read/Write/Edit/Bash/Grep/Glob. Pick for any pre-delivery review or brand-consistency check. 25-turn budget, opus, max effort.",
    prompt=_load_prompt("herald"),
    model="opus",
    tools=["Read", "Write", "Edit", "Bash", "Grep", "Glob"],
    maxTurns=25,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

foreman = AgentDefinition(
    description="Invisible dispatcher — parses raw unstructured input (voice memos, multi-intent requests), classifies intents, fans out to named specialists in parallel, and synthesizes a tight unified reply. Never asks questions, always acts. Uses Agent tool to dispatch to Scout/Wrench/Dispatch/Ledger/Forge/Anvil/Critic/Herald/Turbo/Titan plus direct osascript for Reminders/notes. Default delivery: iMessage via tmux relay. Pick behind any pipeline feeding raw user input (voice memos, handler polling, inbound webhooks). 30-turn budget, sonnet by default (escalates to opus on !opus or adaptive keywords).",
    prompt=_load_prompt("foreman"),
    model="sonnet",
    tools=["Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebSearch", "WebFetch", "Agent", "TodoWrite"],
    maxTurns=30,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

turbo = AgentDefinition(
    description="Speed-optimized quick-action agent on Haiku — fast lookups, quick sends, simple tasks. Invoked with 'turbo, <thing>' prefix. Read/Write/Edit/Grep/Glob/Bash/WebSearch/WebFetch. Pick for single-shot lookups, quick grep, simple ping checks, short iMessage sends. Skip for architectural decisions, multi-file refactors, complex debugging. 10-turn budget, haiku.",
    prompt=_load_prompt("turbo"),
    model="haiku",
    tools=["Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebSearch", "WebFetch"],
    maxTurns=10,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

# Convenience dict for lookups by name
ALL_AGENTS = {
    "scout": scout,
    "forge": forge,
    "wrench": wrench,
    "dispatch": dispatch,
    "ledger": ledger,
    "toolsmith": toolsmith,
    "titan": titan,
    "anvil": anvil,
    "critic": critic,
    "herald": herald,
    "foreman": foreman,
    "turbo": turbo,
}


# --- Tier metadata (R1/R2 agent routing rules) ---
# Source of truth for the routing hook. Kept separate from AgentDefinition
# to avoid coupling to SDK dataclass fields. Mirrors the `tier:` frontmatter
# in ~/.claude/agents/*.md.
#
# Tiers:
#   heavy    — always Opus. Task requires it (titan, critic, anvil).
#   adaptive — default Sonnet, promotes to Opus on !opus or keyword triggers.
#   standard — always Sonnet. No promotion path.
#   light    — Haiku only.
AGENT_TIERS: dict[str, str] = {
    "scout":     "adaptive",
    "forge":     "adaptive",
    "wrench":    "adaptive",
    "anvil":     "heavy",
    "foreman":   "adaptive",
    "herald":    "heavy",
    "critic":    "heavy",
    "titan":     "heavy",
    "dispatch":  "standard",
    "toolsmith": "standard",
    "ledger":    "standard",
    "turbo":     "light",
}

# Keywords that auto-escalate an adaptive agent from Sonnet to Opus.
# Matched case-insensitive against the Agent() prompt body.
# Per-agent keyword lists keep the heuristic targeted (e.g. wrench cares about
# outage words; scout cares about research-depth words).
ADAPTIVE_ESCALATION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "wrench":  ("outage", "down", "broken", "debug", "root cause", "crashed", "failing", "unreachable"),
    "scout":   ("deep dive", "deep-dive", "comprehensive", "synthesize across", "cross-reference"),
    "forge":   ("proposal", "capability statement", "business plan", "client report"),
    "foreman": ("multi-intent", "complex memo", "dispatch swarm"),
}


def get_agent_tier(name: str) -> str:
    """Return the routing tier for a named agent. Unknown → 'standard'."""
    return AGENT_TIERS.get(name, "standard")


# Optional per-agent default output schemas (pydantic BaseModel class OR JSON
# schema dict). The swarm engine reads this via get_agent_schema(); when set,
# it appends "Return ONLY JSON matching: ..." to the prompt and parses the
# agent's final message into SwarmResult.parsed. Empty by default — existing
# agents keep plain-text behavior. Callers can also pass output_schema=
# directly to Swarm.add() to override per-call.
AGENT_OUTPUT_SCHEMAS: dict[str, object] = {}


def get_agent_schema(name: str) -> object | None:
    """Return the default output schema for a named agent, or None."""
    return AGENT_OUTPUT_SCHEMAS.get(name)


def prepend_recall(agent_name: str, task: str) -> str:
    """Return task prompt with pgvector-recall block prepended.

    Direct-invocation callers that build their own SDK ClaudeAgentOptions
    should wrap their task prompt with this helper. Matches the injection
    pattern used by `swarm/engine.py::_run_agent` so behavior stays uniform.

    Uses `kind="agent"` with the agent name for AGENT_OVERRIDES (Titan/Critic
    get rerank=True, limit=10). Never raises — falls back to the raw task
    on any recall failure.
    """
    if not task or not task.strip():
        return task
    try:
        from core.recall import get_context
        cap = agent_name.capitalize() if agent_name else None
        block = get_context(task, kind="agent", agent_name=cap)
    except Exception:
        block = ""
    return f"{block}\n\n{task}" if block else task
