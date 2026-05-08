#!/usr/bin/env python3
"""
Weekly STATUS.md updater — runs Friday 5 PM ET via LaunchAgent.

Replaces the Anthropic-hosted "routine" that was burning the 15/day cap.
Uses Max subscription via claude_agent_sdk (free), iterates Bash/Read/Edit
to refresh ~/claude-config/STATUS.md and commit+push.

Usage:
  python3 scripts/weekly_status_update.py
  python3 scripts/weekly_status_update.py --dry-run
"""

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

for _leak_var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                  "ANTHROPIC_CONSOLE_KEY", "ANTHROPIC_CONSOLE_KEY_MAC", "ANTHROPIC_CONSOLE_KEY_VPS",
                  "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
    os.environ.pop(_leak_var, None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.vault import hydrate_env  # noqa: E402
hydrate_env()

from core.mac_sdk import query, ClaudeAgentOptions
from core.hooks import AGENT_HOOKS
from core.thinking import HEAVY


STATUS_PATH = "/Users/johncornelius/claude-config/STATUS.md"
CONFIG_DIR = "/Users/johncornelius/claude-config"
SWIFT_CAL = "/Users/johncornelius/Projects/agent-core/core/calendar_fetch.swift"


PROMPT_TEMPLATE = """You are updating John Cornelius's weekly status dashboard at {status_path}.

Today is {today} (Friday). You need to produce a refreshed STATUS.md reflecting the week that just ended and set up next week's tracking.

## Step 1: Read the current file
Read {status_path} in full. Match its existing markdown structure exactly — this is a 1-page dashboard, not a journal.

## Step 2: Collect this week's signal

Run these commands with Bash:

1. Git activity in claude-config:
   cd {config_dir} && git log --oneline --since='last monday' --until='now' | head -40

2. Git activity in agent-core:
   cd /Users/johncornelius/Projects/agent-core && git log --oneline --since='last monday' --until='now' 2>/dev/null | head -40

3. Git activity in anthropic-update-watcher if it exists:
   [ -d /Users/johncornelius/Projects/anthropic-update-watcher ] && cd /Users/johncornelius/Projects/anthropic-update-watcher && git log --oneline --since='last monday' --until='now' 2>/dev/null | head -20

4. Sentry AI Thermal dir if it exists:
   [ -d /Users/johncornelius/Desktop/Sentry-AI-Thermal ] && cd /Users/johncornelius/Desktop/Sentry-AI-Thermal && git log --oneline --since='last monday' --until='now' 2>/dev/null | head -20

5. Read significant business emails from Apple Mail via osascript (NOT Gmail MCP):
   osascript -e 'launch application "Mail"' -e 'tell application "Mail"
     set output to ""
     try
       set msgs to (messages of inbox whose date received > (current date) - 7 * days)
       repeat with m in msgs
         try
           set output to output & (sender of m) & " | " & (subject of m) & linefeed
         end try
       end repeat
     end try
     return output
   end tell' | head -60

6. Read this week's calendar + next week preview via Swift/EventKit (NOT osascript — does not expand recurring events):
   START=$(date -v-monday +%s) && END=$(date -v+monday -v+7d +%s) && swift {swift_cal} "$START" "$END" 2>/dev/null | head -80

## Step 3: Update STATUS.md

Edit {status_path} with these targeted changes — use the Edit tool, do NOT rewrite the whole file:

- "Last updated:" line → today's date {today} with a short parenthetical summarizing the biggest thing shipped this week
- "## This Week" heading date → next Monday's date (format YYYY-MM-DD)
- "### Done This Week" section → collapse the existing bullets into ONE concise bullet at the top summarizing the week's headline accomplishments (keep it 1-3 sentences), then REMOVE the older bullets so next week starts fresh. Do not delete the historical "Completed/Stable" section.
- "### Active Projects" table → update Status/Next Step cells based on git activity and email signal. Don't add new rows unless a new project clearly started.
- "### Focus" → rewrite for next week based on what has momentum (git velocity, email threads, what was shipped vs blocked).

Rules:
- NEVER reference TAMKO or "employer" — Sentry AI Thermal is the only business mentioned.
- Keep the file under ~180 lines total. It's a dashboard.
- Do NOT touch the "### Completed/Stable", "### Upcoming", or "### Notes" sections unless you have a concrete reason from the signal.
- Preserve all existing emoji/markdown formatting conventions.

## Step 4: Commit and push

{commit_block}

When done, print a one-line summary of what changed.
"""


COMMIT_BLOCK_LIVE = """Run these commands with Bash:

   cd {config_dir} && git add STATUS.md && git diff --cached --stat
   cd {config_dir} && git commit -m "weekly status update: {today}"
   cd {config_dir} && git push
"""

COMMIT_BLOCK_DRY = """DRY RUN MODE — do NOT commit, do NOT push, do NOT actually edit the file.
Instead, print a diff of what you WOULD change using:
   cd {config_dir} && git diff STATUS.md | head -80

Then revert any edits you made with:
   cd {config_dir} && git checkout STATUS.md
"""


async def main():
    dry_run = "--dry-run" in sys.argv
    today = datetime.now().strftime("%Y-%m-%d")

    commit_block = (COMMIT_BLOCK_DRY if dry_run else COMMIT_BLOCK_LIVE).format(
        config_dir=CONFIG_DIR, today=today
    )
    prompt = PROMPT_TEMPLATE.format(
        status_path=STATUS_PATH,
        config_dir=CONFIG_DIR,
        swift_cal=SWIFT_CAL,
        today=today,
        commit_block=commit_block,
    )

    print(f"[{datetime.now():%H:%M:%S}] Weekly status update starting (dry_run={dry_run})")

    options = ClaudeAgentOptions(
        model="opus",
        permission_mode="bypassPermissions",
        max_turns=8,
        # max_budget_usd removed 2026-04-22 (#183) — vestigial under Max
        cwd=CONFIG_DIR,
        hooks=AGENT_HOOKS,
        thinking=HEAVY,
        effort="max",
        allowed_tools=["Bash", "Read", "Edit", "Grep", "Glob"],
    )

    final_text = ""
    try:
        async for msg in query(prompt=prompt, options=options):
            if hasattr(msg, "content"):
                for block in msg.content:
                    if hasattr(block, "text"):
                        final_text += block.text
            if hasattr(msg, "result") and msg.result:
                final_text = msg.result
    except Exception as e:
        print(f"[weekly-status] SDK exception (often benign on CLI exit): {e}", file=sys.stderr)

    print(f"[{datetime.now():%H:%M:%S}] SDK run complete.")
    if final_text:
        print("--- AGENT SUMMARY ---")
        print(final_text.strip()[:2000])
    else:
        print("[weekly-status] no final text returned")


if __name__ == "__main__":
    asyncio.run(main())
