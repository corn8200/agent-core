# Mac Capability Inventory

*Auto-generated 2026-05-08 16:18 UTC by `mac-capabilities-inventory`. Do not hand-edit.*

## Installed Apps (31 total, 6 AppleScript-capable)

| App | AppleScript | Path |
|-----|-------------|------|
| 1Password |  | `/Applications/1Password.app` |
| AnkerWork |  | `/Applications/AnkerWork.app` |
| Brother iPrint&Scan |  | `/Applications/Brother iPrint&Scan.app` |
| ChatGPT | ✓ | `/Applications/ChatGPT.app` |
| ChatGPT Atlas |  | `/Applications/ChatGPT Atlas.app` |
| Claude | ✓ | `/Applications/Claude.app` |
| Codex | ✓ | `/Applications/Codex.app` |
| Google Chrome | ✓ | `/Applications/Google Chrome.app` |
| Google Earth Pro |  | `/Applications/Google Earth Pro.app` |
| iTerm | ✓ | `/Applications/iTerm.app` |
| Keynote |  | `/Applications/Keynote.app` |
| LibreOffice |  | `/Applications/LibreOffice.app` |
| Microsoft Defender Shim |  | `/Applications/Microsoft Defender Shim.app` |
| Microsoft Excel |  | `/Applications/Microsoft Excel.app` |
| Microsoft OneNote |  | `/Applications/Microsoft OneNote.app` |
| Microsoft Outlook |  | `/Applications/Microsoft Outlook.app` |
| Microsoft PowerPoint |  | `/Applications/Microsoft PowerPoint.app` |
| Microsoft Teams |  | `/Applications/Microsoft Teams.app` |
| Microsoft Word |  | `/Applications/Microsoft Word.app` |
| Numbers |  | `/Applications/Numbers.app` |
| Numbers Creator Studio |  | `/Applications/Numbers Creator Studio.app` |
| OneDrive |  | `/Applications/OneDrive.app` |
| Pages |  | `/Applications/Pages.app` |
| Post-it® |  | `/Applications/Post-it®.app` |
| Raycast |  | `/Applications/Raycast.app` |
| Raycast Companion |  | `/Applications/Raycast Companion.app` |
| Safari | ✓ | `/Applications/Safari.app` |
| Screens 5 |  | `/Applications/Screens 5.app` |
| Snagit |  | `/Applications/Snagit.app` |
| Visual Studio Code |  | `/Applications/Visual Studio Code.app` |
| Windows App |  | `/Applications/Windows App.app` |

## MCP Servers (15)

| Name | Source | Command |
|------|--------|---------|
| playwright | `~/.claude/.mcp.json` | `npx` |
| playwrightChrome | `~/.claude/.mcp.json` | `npx` |
| playwrightFirefox | `~/.claude/.mcp.json` | `npx` |
| chrome-devtools | `~/.claude/.mcp.json` | `npx` |
| openai-image | `~/.claude/.mcp.json` | `node` |
| codex-image | `~/.claude/.mcp.json` | `node` |
| codex-agent-guarded | `~/.claude/.mcp.json` | `node` |
| codex-task | `~/.claude/.mcp.json` | `node` |
| home-assistant | `~/.claude/.mcp.json` | `uvx` |
| apple-voice-memos | `~/.claude/.mcp.json` | `npx` |
| sentry | `~/.claude/.mcp.json` | `node` |
| overseer | `~/.claude/.mcp.json` | `node` |
| codex:chatgpt-codex-bridge | `~/.codex/config.toml` | `` |
| codex:overseer | `~/.codex/config.toml` | `` |
| codex:openaiDeveloperDocs | `~/.codex/config.toml` | `` |

## Apple Data Sources (9)

| Source | Access Method | Notes |
|--------|---------------|-------|
| Mail | `osascript` | icloud+gmail IMAP |
| Calendar | `osascript` | Apple+Google CalDAV |
| Contacts | `osascript` | iCloud sync |
| Reminders | `osascript` | 6 owned lists |
| Notes | `osascript` | iCloud sync |
| Messages | `tmux_relay_shell` | chat.db TCC-protected; relay required from LaunchAgent |
| VoiceMemos | `tmux_relay_shell` | Audio in ~/Library/Group Containers/group.com.apple.VoiceMemos.shared/ |
| Music | `osascript` | library metadata |
| Safari | `osascript` | history+bookmarks |

## LaunchAgents — com.john.* (61)

| Label | Schedule |
|-------|----------|
| `com.john.ambient-context` | cron (1 intervals) |
| `com.john.architecture-drift-watch` | cron (1 intervals) |
| `com.john.atlas-collect-mac` | daily 02:00 |
| `com.john.atlas-watch-mac` | event/watch |
| `com.john.backup-prune` | daily 04:00 |
| `com.john.branch-drift-check` | cron (2 intervals) |
| `com.john.claude-config-refresh` | every 5m |
| `com.john.claude-ops-watch` | every 10m |
| `com.john.claude-rss-watchdog` | every 30m |
| `com.john.claude-session-archive` | daily 03:30 |
| `com.john.cockpit-imessage-sender` | every 30s |
| `com.john.codex-auth-watch` | daily 09:30 |
| `com.john.codex-cleanup` | daily 03:15 |
| `com.john.groundtruth-admin` | always-alive |
| `com.john.handler-agent` | every 30m |
| `com.john.imessage-bus-watchdog` | every 5m |
| `com.john.imessage-bus` | always-alive |
| `com.john.imessage-drainer` | always-alive |
| `com.john.imessage-inbound` | every 5m |
| `com.john.imessage-overseer-watcher` | always-alive |
| `com.john.imessage-triage` | every 60s |
| `com.john.imessage-vector-indexer-watchdog` | every 15m |
| `com.john.imessage-vector-indexer` | every 30m |
| `com.john.infra-docs` | daily 04:00 |
| `com.john.league-sync` | daily 06:00 |
| `com.john.mac-active-account-publish` | always-alive |
| `com.john.mac-tmux-state-publish` | always-alive |
| `com.john.mail-app-restart` | daily 04:00 |
| `com.john.memory-digest` | every 15m |
| `com.john.nightly-infra` | daily 23:00 |
| `com.john.notes-triage` | every 15m |
| `com.john.nudge-engine` | every 5m |
| `com.john.op-lint-watch` | daily 09:00 |
| `com.john.operator-calendar-collect` | every 5m |
| `com.john.operator-ops-data-collect` | every 15m |
| `com.john.overseer-voice-bootstrap` | every 5m |
| `com.john.pane-ask-drift` | every 6h |
| `com.john.pane-autoapprove` | always-alive |
| `com.john.pane-commander-daemon` | every 60s |
| `com.john.pane-heartbeat` | every 60s |
| `com.john.pane-msg-followup-watch` | every 5m |
| `com.john.pane-stale-creds` | every 10m |
| `com.john.routines-guard` | daily 07:30 |
| `com.john.rq-mac-scheduler` | always-alive |
| `com.john.rq-worker-2` | always-alive |
| `com.john.rq-worker-3` | always-alive |
| `com.john.rq-worker` | always-alive |
| `com.john.stale-pane-reaper` | every 10m |
| `com.john.swarm-usage-scraper-gmail` | always-alive |
| `com.john.titan-backlog-1000` | daily 10:00 |
| `com.john.tmux-snap` | every 30m |
| `com.john.toolsmith-weekly` | daily 09:05 |
| `com.john.voice-action-executor` | every 60s |
| `com.john.voice-blocker-stall-watch` | every 5m |
| `com.john.voice-down-watch` | every 5m |
| `com.john.voice-memo-intake` | every 60s |
| `com.john.voice-rotate-watch` | every 2m |
| `com.john.voice-state-publish` | every 60s |
| `com.john.watch-commander` | always-alive |
| `com.john.wc-watchdog` | every 60s |
| `com.john.weekly-status` | daily 17:00 |

## osascript Helpers (4)

| Name | Type | Path |
|------|------|------|
| AcrobatUtils.scpt | compiled | `/Users/johncornelius/Library/Application Scripts/com.microsoft.Powerpoint/AcrobatUtils.scpt` |
| AcrobatUtils.scpt | compiled | `/Users/johncornelius/Library/Application Scripts/com.microsoft.Word/AcrobatUtils.scpt` |
| AcrobatUtils.scpt | compiled | `/Users/johncornelius/Library/Application Scripts/com.microsoft.Excel/AcrobatUtils.scpt` |
| notes-dump.applescript | source | `/Users/johncornelius/bin/notes-dump.applescript` |

## Tool-Arbitration Routing

Canonical mapping from intent to tool. Query via `core.mac_capabilities.choose_tool(intent)`.

| Intent | Tool | Module | Channel |
|--------|------|--------|---------|
| `look-up-contact` | `contact_lookup` | `mcp__contact_lookup` | mcp |
| `query-calendar` | `get_events` | `core.calendar_service` | python |
| `query-mail` | `imap_search` | `core.mail_imap` | python |
| `query-notes` | `osascript` | `core.safari` | osascript |
| `schedule-event` | `add_event` | `core.calendar_service` | python |
| `send-email` | `send_email` | `core.mailhub` | python |
| `send-imessage` | `send_imessage_reliable` | `core.tools` | python |
| `send-pushover` | `push` | `core.pushover` | python |
| `set-reminder` | `add_reminder` | `core.reminders_service` | python |
| `transcribe-voice-memo` | `transcribe_memo` | `mcp__apple-voice-memos__transcribe_memo` | mcp |
