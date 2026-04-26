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
  agents.py         — 12 AgentDefinitions: Scout, Forge, Wrench, Dispatch, Ledger, Toolsmith, Titan, Anvil, Critic, Herald, Foreman, Turbo
  agent_cp_client.py — Control plane telemetry (events, kill switches). Silent-fail. Reads APPLE_BRIDGE_TOKEN from env or secrets.env.legacy.
  vault.py          — 1Password MachineAuto loader: hydrate_env(), get_secret(). Billing guard excludes ANTHROPIC_* keys.
  sdk_guard.py      — Structural rate-limit guard. Auto-patches query() via sitecustomize.py.
  modes.py          — Thrifty mode helpers (check/enter/exit thrifty state)
  browser.py        — Playwright persistent sessions: with_site(), capture_session(), list_sites()
  safari.py         — Safari osascript+JS helpers for interactive browser mode
  constants.py      — HOME, PERSONAL_EMAIL, VPS_SSH, IPs, DB paths, SKIP_CALENDARS
  calendar.py       — Calendar service: get_events, create_event, availability, conflicts, schedule/week views
  message_db.py     — SQLite schema + helpers for ~/logs/message_bus.db (inbound + outbound + sessions)
  message_bus.py    — Unified outbound: send_message() with tiers, attribution, retry, reply_tag, batch_window
  message_reader.py — Inbound chat.db poller via tmux relay, subscriber pattern, attachment enrichment
  message_router.py — Layered router: VPS-tag → short-codes → prefix → session-resume → LLM classification
  message_attachments.py — Task A: image (Sonnet vision) + audio (whisper) enrichment for inbound
  message_batch.py  — Task D: per-chat batching window with force-flush on overflow
home_ops/
  engine.py         — Consolidated daily brief (6:30 AM + 8 PM). Gather→synthesize(opus)→email+TTS+iMessage audio
  gather.py         — Parallel data gathering incl gather_schedule() (30-min cache at /tmp/claude-gather.json)
  prompts.py        — Brief system prompts (weather, sleep, business, calendar, infra, reminders)
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
- `~/logs/message_bus.db` — inbound + outbound message audit trail + resumable `sessions` table (router-v2)
- `~/logs/nudge-state.db` — nudge dedup (event_uid + tier unique index)
- `/tmp/claude-gather.json` — gather cache (30 min TTL)

## iMessage Router v2 Patterns
- **Layer 0 — VPS reply tag:** `[V:<service>:<ref>] <reply>` (service ∈ {sentinel, mailtriage, jobagent, notify}) is POSTed to `http://100.118.21.64:8767/imessage-reply/<service>` with `APPLE_BRIDGE_TOKEN`. On non-2xx, falls through to normal routing. VPS services originate these threads by calling `send_message(..., reply_tag="<service>:<ref>")` which prepends `[V:...]`. Never swallow a user message on HTTP failure.
- **Sessions (Task B):** every dispatch creates a `sessions` row with `sdk_session_id` (UUID pinned via `claude --session-id <uuid>`). Agents end a turn with `CLARIFY: <question>` to pause; the router parks the session as `awaiting_reply` and the user's next non-shortcode reply in <30 min resumes the agent via `claude --resume <sdk_session_id>`. Concurrent dispatches on the same chat get `[session locked on <agent>]`. Stale sessions are swept on daemon start (`on_daemon_start`) and opportunistically via `expire_stale_sessions()`.
- **Attachment enrichment (Task A):** `core/message_attachments.py` extracts via `message_attachment_join` + `attachment` tables through `tmux_relay_shell`, stages to `/tmp/ab-attach-<uuid>/<file>`, describes images via Sonnet 4.6 vision and transcribes audio via OpenAI Whisper (falls back to local `whisper`). Output `[image: <desc>]` / `[voice: <transcript>]` / `[attachment: <name> — unsupported]` is prepended to text before routing.
- **Batching (Task D):** `send_message(..., batch_window=N)` queues per-recipient and flushes after `N` seconds (sliding window, capped at 120s, force-flush at queue > 10). Default `None` = instant delivery; no existing caller is affected. Batched deliveries are joined with `\n\n— — —\n\n` and prefixed `[batched: N messages from a,b,c]`. `core.message_batch.flush_all()` is called from the daemon shutdown path.

## LaunchAgents
- `com.john.home-ops` — Daily brief: 6:30 AM (morning) + 8 PM (evening). Email + iMessage audio.
- `com.john.handler-agent` — Every 30 min anomaly detection (Mac-side, VPS has matching timer)
- `com.john.watch-commander` — Always-on, SDK Opus, iMessage bus
- `com.john.imessage-bus` — KeepAlive daemon, polls chat.db, routes to agents
- `com.john.nudge-engine` — every 5 min, calendar nudges via iMessage

## Model Strategy (Max subscription = flat-monthly, no per-call $ cap)
- **Tier system is canonical** — see `~/.claude/rules/agent-routing.md` and `AGENT_TIERS` in `core/agents.py`
  - **heavy** (opus always): titan, critic, herald
  - **adaptive** (sonnet → opus on keyword / `!opus`): anvil, wrench, scout, forge, foreman
  - **standard** (sonnet, never opus): dispatch, toolsmith, ledger
  - **light** (haiku): turbo
- **Workload-level guidance** (where automation chooses model directly, not via named agent):
  - Synthesis / multi-system diagnosis / hard tailoring → Opus
  - Watcher loops / classification / general agent work → Sonnet
  - Pure extraction / template filling → Haiku
- **Dollar caps were retired 2026-04-22 (#183).** Usage-window readout (`5h:X% wk:Y%`) is the gate — `max_budget_usd` kwargs removed; only `max_turns` is enforced. See `~/.claude/rules/agent-routing.md`.

## Rules
- All automated agents: `permission_mode="bypassPermissions"`, always set `max_turns`. Do NOT set `max_budget_usd` on oat01-metered work — it was retired 2026-04-22 (#183) as vestigial under the flat-monthly Max subscription. Runaway-loop protection is `max_turns` + the 50-calls/hour HOURLY_CAP in `core/mac_sdk.py` + R5 fan-out / R6 titan hooks.
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
- **Canonical agent path: mailhub** — `from mailhub import send_email, reply_email` (helper at `/srv/apps/lib/mailhub.py` on VPS, `agent-core/core/mailhub.py` on Mac/Air). Picks SMTP backend by `from_addr`, enforces reply-from-received-at server-side, auto-gates third-party approval queue, schedule-send + retry/bounce handled. Each agent passes `sender_app="<name>"`. See `~/.claude/rules/messaging.md`.
- **Sentry biz** (info@sentryaithermal.com): `send_business_email` MCP tool → sentry-mailqueue → Resend (still authoritative — mailhub Phase 4+ will subsume).
- **Legacy MCP tools** (`send_personal_email`, `send_business_email` in `core/tools.py`): still functional but route via the legacy ssh `send-email` path, NOT mailhub. New code should call mailhub directly. These remain for automated agents (handler, watch-commander) that have them registered as MCP tools.
- NEVER SCP scripts to VPS for email — use mailhub
- NEVER use raw smtplib from agent code — go through mailhub
- NEVER use Gmail MCP drafts

## Email Receive
- corn82@icloud.com: mailtriage daemon (auto-classify, Pushover urgent, approval-queue drafts)
- corn82@gmail.com: mailgw-idle (forwards important → iCloud → mailtriage)
