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
  tools.py          — 12 MCP tools: SSH, iMessage, email, osascript, Pushover, calendar (4), swarm context (3)
  hooks.py          — guard_hook (blocks destructive cmds) + audit_hook (logs to ~/logs/agent-audit.jsonl)
  thinking.py       — Extended thinking presets: HEAVY / STANDARD / LIGHT / ADAPTIVE / OFF
  agents.py         — 9 AgentDefinitions: Scout, Forge, Wrench, Dispatch, Ledger, Toolsmith, Titan, Anvil, Critic
  gather.py         — Parallel data gathering incl gather_schedule() (30-min cache at /tmp/claude-gather.json)
  constants.py      — HOME, PERSONAL_EMAIL, VPS_SSH, IPs, DB paths, SKIP_CALENDARS
  calendar.py       — Calendar service: get_events, create_event, availability, conflicts, schedule/week views
  message_db.py     — SQLite schema + helpers for ~/logs/message_bus.db (inbound + outbound)
  message_bus.py    — Unified outbound: send_message() with tiers, attribution, retry
  message_reader.py — Inbound chat.db poller via tmux relay, subscriber pattern
  message_router.py — 3-layer router: short-codes → prefix → Opus intent classification w/ full context
briefs/
  morning_brief.py  — 5:30 AM cron. Gather→synthesize(opus)→email+TTS
handler/
  monitor.py        — Every 30 min. Pure Python anomaly detection → SDK diagnosis on alert
swarm/
  engine.py         — Swarm class: parallel/series/hybrid modes
daemon/
  imessage_daemon.py — Unified iMessage bus: reader + router + retry loop (LaunchAgent, KeepAlive)
nudge/
  engine.py         — Calendar nudge engine: 5 tiers, SQLite dedup, --dry-run (LaunchAgent, every 5 min)
```

## Key Patterns
- `query()` async generator for one-shot agent calls
- `AGENT_HOOKS` — always pass to `ClaudeAgentOptions(hooks=AGENT_HOOKS)` for safety + audit
- `create_core_server()` — 12 MCP tools: SSH, iMessage (via bus), email, osascript, Pushover, calendar (4), swarm context (3)
- `send_message(msg, agent=, tier=)` — ALL outbound goes through message_bus. Tiers: critical/normal/archive. Attribution auto-prepends [AgentName].
- `SwarmContext` — in-process key-value store for inter-agent memory during swarm runs
- `memory="project"` on all AgentDefinitions — auto-injects CLAUDE.md into agent context
- `session_id` on swarm agents — enables conversation resume for series/hybrid workflows
- `ALL_AGENTS` dict for named agent definitions in swarm orchestration
- `gather_all()` for parallel data collection (now includes `schedule` key with calendar + reminders + week view)
- `get_schedule_view()` / `get_week_view()` — calendar service for agents and nudge engine

## Consumers (import from core/)
- `~/bin/watch-commander.py` — imports core.message_bus + core.hooks via sys.path
- `~/research-chain/orchestrator.py` — uses agent-core venv + SDK patterns
- `~/Projects/job-agent/docgen/tailor.py` — SDK query() for tailoring
- `daemon/imessage_daemon.py` — unified iMessage bus (reader + router + retry)
- `nudge/engine.py` — calendar nudge engine

## Databases
- `~/logs/message_bus.db` — inbound + outbound message audit trail
- `~/logs/nudge-state.db` — nudge dedup (event_uid + tier unique index)
- `/tmp/claude-gather.json` — gather cache (30 min TTL)

## LaunchAgents
- `com.john.imessage-bus` — KeepAlive daemon, polls chat.db, routes to agents
- `com.john.nudge-engine` — every 5 min, calendar nudges via iMessage

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
