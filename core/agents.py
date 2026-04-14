"""AgentDefinition objects for all named agents, translated from ~/.claude/agents/*.md."""

from pathlib import Path

from claude_agent_sdk import AgentDefinition

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
    description="Research & intelligence specialist — deep web research, market analysis, company background, regulatory landscape, competitor scans. Uses WebSearch + WebFetch with Read/Grep/Write to surface facts, citations, and structured briefings. Pick over Turbo when a question needs multi-source synthesis. Read-only on local files (no Edit, no Bash). 20-turn budget, opus, max effort.",
    prompt=_load_prompt("scout"),
    model="opus",
    tools=["Read", "Grep", "Write", "WebSearch", "WebFetch"],
    maxTurns=20,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

forge = AgentDefinition(
    description="Document builder for polished, tailored deliverables — resumes, cover letters, proposals, business plans, briefings, cold outreach. Reads source inputs, edits drafts in place, writes finals via Read/Edit/Write/Bash. Pick over Anvil when the deliverable is prose, not code. No web access — caller supplies the facts. 15-turn budget, opus, max effort.",
    prompt=_load_prompt("forge"),
    model="opus",
    tools=["Read", "Edit", "Write", "Bash"],
    maxTurns=15,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

wrench = AgentDefinition(
    description="Infrastructure & DevOps specialist — health checks, troubleshooting, service management across Mac Mini, VPS, and Raspberry Pi. Uses Bash + Read/Grep/Write to inspect logs, restart daemons, check disk and network, and chase service errors. Pick when something is broken or you need a status snapshot. No web tools — diagnose locally. 25-turn budget, opus, max effort.",
    prompt=_load_prompt("wrench"),
    model="opus",
    tools=["Bash", "Read", "Grep", "Write"],
    maxTurns=25,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

dispatch = AgentDefinition(
    description="Communications & sending specialist — email drafting, follow-ups, contact coordination via sentry-mailqueue (business) and SMTP (personal). Uses Bash + Read/Write to compose and ship messages, plus Mail.app via osascript for inbox checks. Pick when the deliverable is a sent message, not research. No web search, no Edit — composes from given facts. 10-turn budget, opus.",
    prompt=_load_prompt("dispatch"),
    model="opus",
    tools=["Bash", "Read", "Write"],
    maxTurns=10,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

ledger = AgentDefinition(
    description="Data & financial analysis specialist — budgets, spending reconciliation, CSV crunching, bill scanning, Monarch Money queries, transaction categorization. Uses Bash + Read/Write to run sqlite queries, build summary tables, and cross-reference Mail for upcoming bills. Pick when the answer is a number, table, or financial comparison. No web tools — local data only. 20-turn budget, opus.",
    prompt=_load_prompt("ledger"),
    model="opus",
    tools=["Bash", "Read", "Write"],
    maxTurns=20,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

toolsmith = AgentDefinition(
    description="Meta-agent for reviewing agent performance logs and pruning memory — reads agent_performance.json, groups failures by pattern, proposes surgical prompt improvements for recurring issues, audits learned/*.md entries. Uses Read/Write/Edit/Grep/Glob only — no Bash, no web, no shell side effects. Pick when fixing recurring agent failures or curating learning files. 15-turn budget, opus, surgical scope.",
    prompt=_load_prompt("toolsmith"),
    model="opus",
    tools=["Read", "Write", "Edit", "Grep", "Glob"],
    maxTurns=15,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

titan = AgentDefinition(
    description="Maximum-firepower opus solver for genuinely hard problems — architectural decisions, multi-system debugging, deep research, complex refactors, end-to-end design work. Spawns parallel sub-agent swarms via the Agent tool, extended thinking, no token rationing. Full toolset: Read/Write/Edit/Grep/Glob/Bash/WebSearch/WebFetch/Agent/TodoWrite. Pick when the problem deserves the heaviest hammer; skip for quick lookups. 60-turn budget.",
    prompt=_load_prompt("titan"),
    model="opus",
    tools=["Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebSearch", "WebFetch", "Agent", "TodoWrite"],
    maxTurns=60,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

anvil = AgentDefinition(
    description="Code builder and implementation specialist — writes, refactors, and ships working code. Worktree-first, test-driven, verifies its own diffs in an isolated branch before reporting done. Uses Read/Write/Edit/Grep/Glob/Bash/TodoWrite. Pick over Forge for code deliverables, over Titan for routine implementation work, over Scout when the task ends in shipped code rather than research. 40-turn budget, opus, max effort.",
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
}
