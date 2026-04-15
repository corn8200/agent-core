# PRD: claude-code v2.1.109 — No agent-core changes required

**Release:** https://github.com/anthropics/claude-code/releases/tag/v2.1.109
**Classification:** UX-only (rotating extended-thinking progress hint in CLI TUI)

## Summary

claude-code v2.1.109 changes the CLI's extended-thinking spinner from a static indicator to rotating progress hints. This is purely cosmetic TUI output. agent-core invokes the CLI programmatically via `claude_agent_sdk` (subprocess), never parses spinner/progress output, and configures thinking via API-level parameters (`effort="max"` on `AgentDefinition`). **Zero code changes needed.**

## Success Criteria

```bash
# SC-1: agent-core has no references to CLI spinner/progress indicator parsing
! grep -rn 'thinking_indicator\|progress_hint\|spinner\|rotating.*hint' ~/Projects/agent-core/core/

# SC-2: effort and thinking config remain API-level only (AgentDefinition.effort)
grep -c 'effort="max"' ~/Projects/agent-core/core/agents.py | grep -qE '^[1-9]'

# SC-3: No stdout/stderr parsing of claude CLI output anywhere in core/
! grep -rn 'stdout.*parse\|stderr.*parse\|\.stdout\.read\|capture_output.*True' ~/Projects/agent-core/core/

# SC-4: Package still imports cleanly
cd ~/Projects/agent-core && python -c "from core import agents, hooks, tools; print('ok')"
```

## Requirements

### R0: No-op — do not modify any files

The extended-thinking progress indicator change is internal to the `claude` CLI binary's TUI rendering layer. agent-core's integration point is `claude_agent_sdk`, which communicates via JSON-RPC over subprocess stdio — it never reads or depends on the visual TUI output stream.

Relevant code confirming no coupling:
- `core/agents.py` — `AgentDefinition` objects set `effort="max"` (API parameter, not TUI config)
- `core/hooks.py` — hooks receive structured `input` dicts via JSON-RPC callback, not TUI text
- `core/tools.py` — MCP tools run shell commands independently of CLI progress display

**Do not create a branch. Do not edit any files. Do not commit.**

Acceptance test:
```bash
cd ~/Projects/agent-core && git status --porcelain | wc -l | grep -q '^0$'
```

## Test Steps

```bash
# 1. Verify working tree is clean (no accidental edits)
cd ~/Projects/agent-core && test -z "$(git status --porcelain)"

# 2. Verify core imports
cd ~/Projects/agent-core && python -c "from core.agents import scout, forge, wrench, anvil, titan; print('agents ok')"
cd ~/Projects/agent-core && python -c "from core.hooks import AGENT_HOOKS, guard_hook, audit_hook; print('hooks ok')"
cd ~/Projects/agent-core && python -c "from core.tools import tmux_relay_shell, send_imessage_reliable; print('tools ok')"

# 3. Confirm no TUI coupling exists
! grep -rn 'spinner\|progress_hint\|thinking_indicator\|tui\|ansi.*escape' ~/Projects/agent-core/core/
```

## Out of Scope

- **Do not** update `claude-agent-sdk` version pin — v2.1.109 is a CLI release, not an SDK release
- **Do not** add thinking/spinner configuration to `AgentDefinition` objects
- **Do not** refactor `core/agents.py` effort settings
- **Do not** create any branches, commits, or PRs
- **Do not** touch `home_ops/`, `briefs/`, `swarm/`, or `handler/`