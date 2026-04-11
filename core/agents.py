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
    description="Research & intelligence specialist — deep web research on any topic.",
    prompt=_load_prompt("scout"),
    model="opus",
    tools=["Read", "Grep", "Write", "WebSearch", "WebFetch"],
    maxTurns=20,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

forge = AgentDefinition(
    description="Document builder — creates polished, tailored documents from inputs.",
    prompt=_load_prompt("forge"),
    model="opus",
    tools=["Read", "Edit", "Write", "Bash"],
    maxTurns=15,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

wrench = AgentDefinition(
    description="Infrastructure & DevOps — health checks, troubleshooting, service management.",
    prompt=_load_prompt("wrench"),
    model="opus",
    tools=["Bash", "Read", "Grep", "Write"],
    maxTurns=25,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

dispatch = AgentDefinition(
    description="Communications & sending — email drafting, follow-ups, contact coordination.",
    prompt=_load_prompt("dispatch"),
    model="opus",
    tools=["Bash", "Read", "Write"],
    maxTurns=10,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

ledger = AgentDefinition(
    description="Data & financial analysis — budgets, data extraction, number crunching.",
    prompt=_load_prompt("ledger"),
    model="opus",
    tools=["Bash", "Read", "Write"],
    maxTurns=20,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

toolsmith = AgentDefinition(
    description="Meta-agent — reviews agent performance, proposes prompt improvements.",
    prompt=_load_prompt("toolsmith"),
    model="opus",
    tools=["Read", "Write", "Edit", "Grep", "Glob"],
    maxTurns=15,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

titan = AgentDefinition(
    description="Max-firepower problem solver — parallel sub-agent swarms, extended thinking, no rationing.",
    prompt=_load_prompt("titan"),
    model="opus",
    tools=["Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebSearch", "WebFetch", "Agent", "TodoWrite"],
    maxTurns=60,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

anvil = AgentDefinition(
    description="Code builder — worktree-first, test-driven, verifies its own work.",
    prompt=_load_prompt("anvil"),
    model="opus",
    tools=["Read", "Write", "Edit", "Grep", "Glob", "Bash", "TodoWrite"],
    maxTurns=40,
    permissionMode="bypassPermissions",
    effort="max",
    memory="project",
)

critic = AgentDefinition(
    description="Adversarial reviewer — read-only second opinion before merges/ops.",
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
