# agent-core

John's Mac-side agent automation stack.

## iMessage Vector Indexer

The iMessage corpus is indexed by `bin/imessage_vector_indexer.py`, launched by
`com.john.imessage-vector-indexer` every 30 minutes on the Mac Mini. The script
reads `~/Library/Messages/chat.db` through `tmux_relay_shell`, so the Full Disk
Access target is Terminal.app/tmux relay, not raw Homebrew Python.

Failure fixed on 2026-05-04: the all-thread vector backfill existed only as a
manual script. The always-on iMessage bus only indexed recent routed self-chat
threads, so broad corpus sync stopped silently after the last manual backfill.
The scheduled indexer now catches up from the last successful sync, overlaps to
the start of the affected ISO week, retries Postgres connections, skips malformed
rows into `imessage_indexer_skipped`, and enqueues idempotent upserts keyed by
`source_type/source_id/chunk_idx`.

Launchd hardening lives in `~/claude-config/launchagents`:

- `com.john.imessage-vector-indexer`: `StartInterval=1800`, `RunAtLoad=true`,
  `KeepAlive.SuccessfulExit=false`.
- `com.john.imessage-vector-indexer-watchdog`: runs every 15 minutes and reloads
  the indexer plist if launchd loses the label.

Excluded corpus threads: `SENTINEL`, `OVERWATCH`, `Jannson`, and `Mercury`.
