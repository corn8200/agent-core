# Agent Core

Claude Agent SDK integration hub. All automation imports from here. Built 2026-04-08.

## Tech Stack
- Python 3.14, claude-agent-sdk 0.1.58
- Venv: `.venv/`
- Depends on: Claude Code CLI at `/opt/homebrew/bin/claude`
- Auth: Max subscription (free), no API key needed locally

## Structure
```
core/
  tools.py         — 5 MCP tools: ssh_command, send_imessage, send_business_email, osascript_run, moshi_push
  hooks.py         — guard_hook (blocks destructive cmds) + audit_hook (logs to ~/logs/agent-audit.jsonl)
  thinking.py      — Extended thinking presets: HEAVY / STANDARD / LIGHT / ADAPTIVE / OFF
  agents.py        — 9 AgentDefinitions: Scout, Forge, Wrench, Dispatch, Ledger, Toolsmith, Titan, Anvil, Critic
  gather.py        — Parallel data gathering (30-min cache at /tmp/claude-gather.json)
  constants.py     — HOME, PERSONAL_EMAIL, VPS_SSH, IPs
briefs/
  morning_brief.py — 5:30 AM cron. Gather→synthesize(opus)→email+TTS
handler/
  monitor.py       — Every 30 min. Pure Python anomaly detection → SDK diagnosis on alert
swarm/
  engine.py        — Swarm class: parallel/series/hybrid modes
```

## Key Patterns
- `query()` async generator for one-shot agent calls
- `AGENT_HOOKS` — always pass to `ClaudeAgentOptions(hooks=AGENT_HOOKS)` for safety + audit
- `create_core_server()` — 8 MCP tools: SSH, iMessage, email, osascript, Pushover + swarm context read/write/list
- `SwarmContext` — in-process key-value store for inter-agent memory during swarm runs
- `memory="project"` on all AgentDefinitions — auto-injects CLAUDE.md into agent context
- `session_id` on swarm agents — enables conversation resume for series/hybrid workflows
- `ALL_AGENTS` dict for named agent definitions in swarm orchestration
- `gather_all()` for parallel data collection

## Consumers (import from core/)
- `~/bin/watch-commander.py` — imports core.tools + core.hooks via sys.path
- `~/research-chain/orchestrator.py` — uses agent-core venv + SDK patterns
- `~/Projects/job-agent/docgen/tailor.py` — SDK query() for tailoring

## Model Strategy (Max subscription = free)
- **Opus:** Morning brief synthesis, handler diagnosis, memo execution, research passes 1+2, job tailoring
- **Sonnet:** Ask commands, research passes 3-5, general agent work
- **Haiku:** Dictation fix only ($0.02 budget)

## Rules
- All automated agents: `permission_mode="bypassPermissions"`, always set `max_turns` + `max_budget_usd`
- Always include `hooks=AGENT_HOOKS` on SDK calls
- Always pass `thinking=<preset>` + `effort="max"` — pick the preset per task:
  - `HEAVY` (12k) → synthesis, tailoring, proposal drafting, job-fit judgment, merging multi-agent output
  - `STANDARD` (6k) → diagnosis, single swarm agent, general reasoning
  - `LIGHT` (3k) → structured extraction, template filling, straightforward classification
  - `ADAPTIVE` → when budget is unknown / let Claude decide
- VPS SSH hostname is `vps`, NOT jcornelius.net
- NEVER use Gmail MCP drafts for sending email
