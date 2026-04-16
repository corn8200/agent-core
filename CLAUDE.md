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
  agent_cp_client.py — Control plane telemetry (events, kill switches). Silent-fail. Reads APPLE_BRIDGE_TOKEN from env or secrets.env.legacy.
  vault.py          — 1Password MachineAuto loader: hydrate_env(), get_secret(). Billing guard excludes ANTHROPIC_* keys.
  sdk_guard.py      — Structural rate-limit guard. Auto-patches query() via sitecustomize.py.
  modes.py          — Thrifty mode helpers (check/enter/exit thrifty state)
  browser.py        — Playwright persistent sessions: with_site(), capture_session(), list_sites()
  safari.py         — Safari osascript+JS helpers for interactive browser mode
  constants.py      — HOME, PERSONAL_EMAIL, VPS_SSH, IPs, DB paths, SKIP_CALENDARS
  calendar.py       — Calendar service: get_events, create_event, availability, conflicts, schedule/week views
  message_db.py     — SQLite schema + helpers for ~/logs/message_bus.db (inbound + outbound)
  message_bus.py    — Unified outbound: send_message() with tiers, attribution, retry
  message_reader.py — Inbound chat.db poller via tmux relay, subscriber pattern
  message_router.py — 3-layer router: short-codes → prefix → Opus intent classification w/ full context
home_ops/
  engine.py         — Consolidated daily brief (6:30 AM + 8 PM). Gather→synthesize(opus)→email+TTS+iMessage audio
  gather.py         — Parallel data gathering incl gather_schedule() (30-min cache at /tmp/claude-gather.json)
  prompts.py        — Brief system prompts (weather, sleep, business, calendar, infra, reminders)
briefs/
  morning_brief.py  — LEGACY (LaunchAgent disabled 2026-04-15, replaced by home_ops)
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
- `~/Projects/job-agent/docgen/tailor.py` — SDK query() for tailoring
- `daemon/imessage_daemon.py` — unified iMessage bus (reader + router + retry)
- `nudge/engine.py` — calendar nudge engine

## Databases
- `~/logs/message_bus.db` — inbound + outbound message audit trail
- `~/logs/nudge-state.db` — nudge dedup (event_uid + tier unique index)
- `/tmp/claude-gather.json` — gather cache (30 min TTL)

## LaunchAgents
- `com.john.home-ops` — Daily brief: 6:30 AM (morning) + 8 PM (evening). Email + iMessage audio.
- `com.john.handler-agent` — Every 30 min anomaly detection (Mac-side, VPS has matching timer)
- `com.john.watch-commander` — Always-on, SDK Opus, iMessage bus
- `com.john.imessage-bus` — KeepAlive daemon, polls chat.db, routes to agents
- `com.john.nudge-engine` — every 5 min, calendar nudges via iMessage

## Model Strategy (Max subscription = free)
- **Opus:** Morning brief synthesis, handler diagnosis, memo execution, research passes 1+2, job tailoring
- **Sonnet:** Ask commands, research passes 3-5, general agent work
- **Haiku:** Dictation fix only ($0.02 budget)

## Rules
- All automated agents: `permission_mode="bypassPermissions"`, always set `max_turns` + `max_budget_usd`
- Always include `hooks=AGENT_HOOKS` on SDK calls
- Always pass `thinking=<preset>` + `effort=<level>` — pick per task:
  - `ULTRA` (64k) + `effort="xhigh"` → **Titan** / architectural decisions / hardest multi-system problems
  - `HEAVY` (32k) + `effort="xhigh"` → synthesis, tailoring, proposal drafting, job-fit judgment, swarm merge
  - `STANDARD` (16k) + `effort="max"` → diagnosis, single swarm agent, general reasoning
  - `LIGHT` (6k) + `effort="max"` → structured extraction, template filling, straightforward classification
  - `ADAPTIVE` → when budget is unknown / let Claude decide
  - Effort guide: `xhigh` = new Opus 4.7 default (scores > old `max` at 100k tokens); `max` = proofs/audits/exhaustive
- VPS SSH hostname is `vps`, NOT jcornelius.net

## Email Sending
- Business (info@sentryaithermal.com): `send_business_email` MCP tool → sentry-mailqueue → Resend
- Personal/system (notify@jcornelius.net): `send_personal_email` MCP tool → VPS send-email wrapper → Gmail SMTP
- NEVER SCP scripts to VPS for email — use MCP tools
- NEVER use Gmail MCP drafts

## Email Receive
- corn82@icloud.com: mailtriage daemon (auto-classify, Pushover urgent, approval-queue drafts)
- corn82@gmail.com: mailgw-idle (forwards important → iCloud → mailtriage)
