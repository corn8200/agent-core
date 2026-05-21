# agent-core

Personal [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview) automation hub for macOS: named agent personas, an MCP tool surface, an approval outbox, and pgvector-backed recall.

This is the engine the rest of my personal automation talks through. It defines who the agents are, what tools they can hold, how they coordinate, and how outbound side effects get approved.

## What's in here

- **Named agent personas** — `Scout`, `Forge`, `Wrench`, `Dispatch`, `Ledger`, `Toolsmith`, `Titan`, `Anvil`, `Critic`. Each is a [`AgentDefinition`](https://docs.claude.com/en/api/agent-sdk/agent-definition) with its own prompt, tool budget, turn budget, and model assignment. Defined in `core/agents.py`.
- **MCP tool surface** — `core/tools.py` exposes a curated set of tools to the agents: iMessage send/receive (via macOS `chat.db` + tmux relay), email (local + a VPS mailhub), calendar/reminders via Swift + EventKit, Safari (interactive) and Playwright (headless), Pushover, and an approval-gated outbox.
- **Approval outbox** — `core/outbox.py` routes any side effect that the user has tagged as needing approval through an `[APPROVE:uuid]` / `[DENY:uuid]` round-trip before it ships. The approval can land via iMessage, push notification, or a small HTTP UI.
- **Secret loader** — `core/vault.py` provides a unified `hydrate_env` / `get_secret` interface backed by either a process env, a `secrets.env` file, or 1Password via the `op` CLI service-account token. Falls through the chain until something resolves.
- **pgvector recall** — `core/vector.py` + `core/recall.py` push/pull memories to a Postgres pgvector store. `core/message_vector.py` runs a scheduled iMessage indexer so the agents can recall conversational context.
- **macOS-native integration** — `core/calendar_fetch.swift`, `core/reminders_fetch.swift`, `core/contacts_fetch.swift` are short Swift programs that read EventKit/Contacts directly. The Python layer (`core/calendar_service.py`, `core/reminders_service.py`) shells out to them.
- **Mail bus** — `core/mailhub.py` + `core/mailhub_client.py` are mirrors of a VPS-side outbound mail service so the same code can run on either host with the same API.

## Status

> This repo is **personal infrastructure shared as a worked example.** It is wired to the author's specific environment: a 1Password service-account token, a Postgres instance over Tailscale, a macOS host with EventKit / Mail.app / chat.db access, and a handful of helpers in `~/bin/`. It is intentionally *not* a turnkey clone.
>
> What's interesting here is the architecture: how named agents + their tool budgets are defined, how the approval outbox gates side effects, how the macOS-native EventKit subprocesses bolt onto a Python tool surface, and how the secret loader collapses three backends into one call.

## Architecture

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  AgentDefinition fleet (core/agents.py)                          │
   │    Scout · Forge · Wrench · Dispatch · Ledger · Toolsmith · ...  │
   └──────────────────────────────────────────────────────────────────┘
                              │
                              ▼  uses
   ┌──────────────────────────────────────────────────────────────────┐
   │  MCP tool surface (core/tools.py)                                │
   │   iMessage · email · Calendar · Reminders · Safari · Playwright  │
   │   Pushover · pgvector recall · approval outbox                   │
   └──────────────────────────────────────────────────────────────────┘
        │              │              │              │
        ▼              ▼              ▼              ▼
   chat.db /     mailhub.py /   *_fetch.swift   vector.py +
   tmux relay    SMTP / IMAP    (EventKit)      pgvector PG
        │              │              │              │
        └──────────────┴──────┬───────┴──────────────┘
                              │
                              ▼  side effects gated by
   ┌──────────────────────────────────────────────────────────────────┐
   │  Approval outbox (core/outbox.py)                                │
   │    [APPROVE:uuid] / [DENY:uuid] round-trip                       │
   └──────────────────────────────────────────────────────────────────┘
                              │
                              ▼  secrets from
   ┌──────────────────────────────────────────────────────────────────┐
   │  Secret loader (core/vault.py)                                   │
   │    env → secrets.env → op:// (1Password service account)         │
   └──────────────────────────────────────────────────────────────────┘
```

## Requirements

- macOS (chat.db, EventKit, osascript, Mail.app are macOS-specific)
- Python 3.12+
- Postgres + the `pgvector` extension reachable from the host
- Optional: 1Password CLI + a service-account token if you want vault-backed secrets
- Optional: a VPS running the matching mailhub service for cross-host email

## Quickstart

```bash
uv sync             # or: pip install -e .
playwright install  # headless browser dep used by core/browser.py
```

Then `from core.tools import *` from your own agent script. See `core/agents.py` for the personas wired up against this tool surface.

## License

MIT. See [LICENSE](LICENSE).
