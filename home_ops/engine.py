#!/usr/bin/env python3
"""home-ops engine — twice-daily household brief.

Stages:
  1. Gather household signals in parallel (calendar, reminders, contacts,
     iMessages, mail, weather) — pure Python, zero tokens.
  2. Pull prior loose_ends + learned_facts from state.py.
  3. Single-pass Opus synthesizer via claude_agent_sdk.
  4. Dedup via fingerprint. Log brief. Render + send via VPS SMTP.

Usage:
  python3 -m home_ops.engine --evening       # 8 PM scheduled run
  python3 -m home_ops.engine --morning       # 6:30 AM scheduled run
  python3 -m home_ops.engine --evening --dry-run    # skip email send
  python3 -m home_ops.engine --evening --gather-only  # dump gather dict
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Scrub API billing vars BEFORE importing claude_agent_sdk — or the SDK
# subprocess inherits them and bills pay-as-you-go instead of Max.
for _leak_var in (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
):
    os.environ.pop(_leak_var, None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.constants import HOME, PERSONAL_EMAIL, WIFE_EMAIL  # noqa: E402

from home_ops import gather as gather_mod  # noqa: E402
from home_ops import prompts as prompts_mod  # noqa: E402
from home_ops import render as render_mod  # noqa: E402
from home_ops import state as state_mod  # noqa: E402

GATHER_DUMP_PATH = Path("/tmp/home-ops-gather.json")
BRIEF_TEXT_PATH = Path("/tmp/home-ops-brief.txt")


async def synthesize(gather: dict, mode: str) -> str:
    """Single-pass Opus call. Returns plain-text brief."""
    from claude_agent_sdk import query, ClaudeAgentOptions
    from core.hooks import AGENT_HOOKS
    from core.thinking import HEAVY

    system_prompt = prompts_mod.SYSTEM_PROMPT
    user_prompt = prompts_mod.build_user_prompt(gather, mode)

    brief_text = ""
    try:
        async for msg in query(
            prompt=user_prompt,
            options=ClaudeAgentOptions(
                model="opus",
                system_prompt=system_prompt,
                permission_mode="bypassPermissions",
                max_turns=2,
                max_budget_usd=0.20,
                cwd=str(HOME),
                hooks=AGENT_HOOKS,
                thinking=HEAVY,
                effort="max",
            ),
        ):
            if hasattr(msg, "content"):
                for block in msg.content:
                    if hasattr(block, "text"):
                        brief_text += block.text
            if hasattr(msg, "result") and msg.result:
                brief_text = msg.result
    except Exception as e:
        # SDK throws on CLI exit after result is received — only real error
        # is one that leaves brief_text empty
        if not brief_text:
            print(f"synthesize error: {e}", file=sys.stderr)

    return brief_text.strip()


def extract_loose_ends(brief_text: str) -> list[str]:
    """Parse the 'loose ends:' section of the brief into individual bullet lines.
    Best-effort; if the model skipped the section, return [].
    """
    lines = brief_text.split("\n")
    out: list[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if not in_section:
            if stripped.lower().startswith("loose ends"):
                in_section = True
            continue
        if not stripped:
            # blank line — section ended IF we already captured items
            if out:
                break
            continue
        # stop when we hit another section header (lowercase word + no bullet)
        if not stripped.startswith(("-", "•", "*")) and stripped.lower().split()[0] in (
            "weather", "today", "tomorrow", "this", "week",
        ):
            break
        cleaned = stripped.lstrip("-•* ").strip()
        if cleaned:
            out.append(cleaned[:200])
    return out


async def run(mode: str, dry_run: bool = False, gather_only: bool = False,
              force: bool = False, recipients: list[str] | None = None) -> int:
    """Main orchestrator. Returns exit code."""
    t0 = datetime.now()
    print(f"[{t0:%H:%M:%S}] home-ops {mode} starting (dry_run={dry_run})")

    # Stage 0: init state
    state_mod.init_db()

    # Stage 1: gather
    print(f"[{datetime.now():%H:%M:%S}] gathering signals...")
    data = await gather_mod.gather_all()
    GATHER_DUMP_PATH.write_text(json.dumps(data, indent=2, default=str))

    # Merge in state-backed context
    data["mode"] = mode
    data["loose_ends"] = state_mod.get_open_loose_ends(limit=30)
    data["learned_facts"] = state_mod.get_learned_facts(limit=60)

    print(
        f"[{datetime.now():%H:%M:%S}] gathered: "
        f"cal={len(data.get('calendar_7d', []))} "
        f"rem={sum(len(v) for v in data.get('reminders', {}).values() if isinstance(v, list))} "
        f"contacts={len(data.get('contacts', []))} "
        f"msgs={len(data.get('imessages_7d', []))} "
        f"mail={len(data.get('mail_7d', []))} "
        f"loose={len(data['loose_ends'])} "
        f"facts={len(data['learned_facts'])}"
    )

    if gather_only:
        print(f"→ {GATHER_DUMP_PATH}")
        return 0

    # Stage 2: synthesize
    print(f"[{datetime.now():%H:%M:%S}] synthesizing brief (opus, HEAVY)...")
    brief_text = await synthesize(data, mode)
    if not brief_text:
        print("FAIL: synthesizer returned empty", file=sys.stderr)
        return 1
    BRIEF_TEXT_PATH.write_text(brief_text)
    print(f"[{datetime.now():%H:%M:%S}] brief: {len(brief_text)} chars → {BRIEF_TEXT_PATH}")

    # Stage 3: dedup
    fingerprint = state_mod.fingerprint_brief(brief_text)
    if not force and state_mod.already_sent(fingerprint):
        print(f"[{datetime.now():%H:%M:%S}] DEDUP: fingerprint already sent, skipping")
        return 0

    # Stage 4: extract loose ends + upsert into state
    loose = extract_loose_ends(brief_text)
    for desc in loose:
        state_mod.upsert_loose_end(desc, source="inferred")
    if loose:
        print(f"[{datetime.now():%H:%M:%S}] tracked {len(loose)} loose ends")

    # Stage 5: deliver or dry-run
    if dry_run:
        print("\n--- DRY RUN BRIEF ---")
        print(brief_text)
        print("--- END ---\n")
        return 0

    rcpts = recipients or [PERSONAL_EMAIL]
    subject = f"Home ops — {mode} brief, {datetime.now():%a %b %d}"
    print(f"[{datetime.now():%H:%M:%S}] sending to {rcpts}")
    ok, send_msg = await render_mod.send_brief(
        brief_text=brief_text,
        mode=mode,
        recipients=rcpts,
        subject=subject,
    )
    if not ok:
        print(f"FAIL: send — {send_msg}", file=sys.stderr)
        return 2

    state_mod.log_brief(fingerprint, mode, subject, brief_text)
    # Auto-resolve loose ends not seen in 30 days
    state_mod.mark_stale_loose_ends(days=30)

    elapsed = (datetime.now() - t0).total_seconds()
    print(f"[{datetime.now():%H:%M:%S}] done in {elapsed:.1f}s — {send_msg}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="home-ops household brief engine")
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--evening", action="store_true", help="8 PM evening brief")
    mode_group.add_argument("--morning", action="store_true", help="6:30 AM morning brief")
    parser.add_argument("--dry-run", action="store_true", help="gather + synthesize, skip send")
    parser.add_argument("--gather-only", action="store_true", help="dump gather dict only")
    parser.add_argument("--force", action="store_true", help="ignore dedup")
    parser.add_argument("--include-wife", action="store_true",
                        help="Phase 2: also email Ashley")
    args = parser.parse_args()

    mode = "evening" if args.evening else "morning"
    recipients = [PERSONAL_EMAIL]
    if args.include_wife:
        recipients.append(WIFE_EMAIL)

    code = asyncio.run(run(
        mode=mode,
        dry_run=args.dry_run,
        gather_only=args.gather_only,
        force=args.force,
        recipients=recipients,
    ))
    sys.exit(code)


if __name__ == "__main__":
    main()
