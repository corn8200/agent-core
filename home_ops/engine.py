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
import re
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

from core.vault import hydrate_env  # noqa: E402
hydrate_env()

from core.constants import HOME, PERSONAL_EMAIL  # noqa: E402
try:
    import core.agent_cp_client as cp  # noqa: E402
except Exception:
    cp = None

from home_ops import gather as gather_mod  # noqa: E402
from home_ops import prompts as prompts_mod  # noqa: E402
from home_ops import render as render_mod  # noqa: E402
from home_ops import state as state_mod  # noqa: E402

GATHER_DUMP_PATH = Path("/tmp/home-ops-gather.json")
BRIEF_TEXT_PATH = Path("/tmp/home-ops-brief.txt")


async def deliver_tts(brief_text: str) -> bool:
    """Thin wrapper over core.tts_delivery.deliver_brief_tts."""
    from core.tts_delivery import deliver_brief_tts
    return await deliver_brief_tts(brief_text, msg_prefix="Morning Brief")


async def _pushover_shrink_alert(
    mode: str, orig: int, final: int, lim: int, cleared: bool
) -> None:
    """Fire P0 Pushover when shrink fires 2 days in a row. HARD RULE: P0 only."""
    import asyncio as _asyncio
    from core.constants import PUSHOVER_USER, PUSHOVER_TOKEN

    title = f"home-ops shrink 2 days in a row ({mode})"
    msg = (
        f"Shrink fired today AND yesterday. Today: {orig}→{final}B "
        f"(limit {lim}). Cleared={cleared}. Payload growing; investigate "
        f"home_ops/prompts.py _shrink_payload reason."
    )
    proc = await _asyncio.create_subprocess_exec(
        "curl", "-s", "-o", "/dev/null", "--max-time", "10",
        "-F", f"token={PUSHOVER_TOKEN}",
        "-F", f"user={PUSHOVER_USER}",
        "-F", f"title={title}",
        "-F", f"message={msg}",
        "-F", "priority=0",
        "https://api.pushover.net/1/messages.json",
        stdout=_asyncio.subprocess.PIPE,
        stderr=_asyncio.subprocess.PIPE,
    )
    await _asyncio.wait_for(proc.communicate(), timeout=12)


async def synthesize(gather: dict, mode: str) -> tuple[str, dict]:
    """Single-pass Opus call. Returns (brief_text, shrink_info)."""
    from core.mac_sdk import query, ClaudeAgentOptions
    from core.hooks import AGENT_HOOKS
    from core.thinking import HEAVY

    system_prompt = prompts_mod.SYSTEM_PROMPT
    user_prompt, shrink_info = prompts_mod.build_user_prompt(gather, mode)

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
        print(f"synthesize error: {e}", file=sys.stderr)

    brief_text = brief_text.strip()
    if brief_text:
        lower = brief_text.lower()
        last_line = brief_text.splitlines()[-1].strip() if brief_text.splitlines() else ""
        malformed = (
            "loose ends" not in lower
            or not last_line
            or last_line in ("-", "•", "*")
            or last_line.endswith(("—", "-"))
        )
        if malformed:
            print(
                "WARN: synthesized brief may be truncated or malformed "
                f"(last_line={last_line!r})",
                file=sys.stderr,
            )
    return brief_text, shrink_info


_DAY_START_RE = re.compile(
    r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun|"
    r"today|tomorrow|next)\b",
    re.IGNORECASE,
)


def extract_loose_ends(brief_text: str) -> list[str]:
    """Parse the 'Loose ends:' section of the brief into bullet lines.

    Enter the section on a "loose ends" header. Exit on:
      - blank line after captures, OR
      - a day-label line (Mon/Tue/.../Today/Tomorrow/Next ...), OR
      - a "Weather" line, OR
      - any other Title-Case section label ending with ':'.
    Only bullet-prefixed lines (-, •, *) inside the section are captured.
    """
    out: list[str] = []
    in_section = False
    for line in brief_text.split("\n"):
        stripped = line.strip()
        if not in_section:
            low = stripped.lower().rstrip(":")
            if low == "loose ends" or low.startswith("loose ends"):
                in_section = True
            continue

        if not stripped:
            if out:
                break
            continue

        low = stripped.lower()
        if low.startswith("weather"):
            break
        if _DAY_START_RE.match(stripped):
            break
        if (
            not stripped.startswith(("-", "•", "*"))
            and stripped.endswith(":")
            and stripped[0].isupper()
        ):
            break

        if not stripped.startswith(("-", "•", "*")):
            continue

        cleaned = stripped.lstrip("-•* ").strip()
        if cleaned:
            out.append(cleaned[:200])
    return out


async def run(mode: str, dry_run: bool = False, gather_only: bool = False,
              force: bool = False, recipients: list[str] | None = None,
              audio: bool = False) -> int:
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
    brief_text, shrink_info = await synthesize(data, mode)
    if not brief_text:
        print("FAIL: synthesizer returned empty", file=sys.stderr)
        return 1

    # Stage 2b: shrink visibility — if the payload shrinker fired, prepend a
    # marker to the brief body (so I can see it in email/iMessage) AND log to
    # state.py. If shrink also fired yesterday → Pushover P0 (S2.5, 2026-04-17).
    if shrink_info.get("fired"):
        orig = shrink_info["original_size"]
        final = shrink_info["final_size"]
        lim = shrink_info["limit"]
        cleared = shrink_info.get("cleared", False)
        cleared_note = " [imessages+mail CLEARED]" if cleared else ""
        marker = (
            f"⚠ SHRINK FIRED: payload {orig}→{final}B "
            f"(limit {lim}){cleared_note}\n\n"
        )
        brief_text = marker + brief_text
        print(
            f"[{datetime.now():%H:%M:%S}] shrink fired: "
            f"{orig}→{final} bytes (limit {lim}, cleared={cleared})",
            file=sys.stderr,
        )
        try:
            state_mod.log_shrink_event(mode, orig, final, cleared)
        except Exception as e:
            print(f"shrink log error: {e}", file=sys.stderr)

        if state_mod.shrink_fired_yesterday():
            try:
                await _pushover_shrink_alert(mode, orig, final, lim, cleared)
            except Exception as e:
                print(f"pushover shrink alert error: {e}", file=sys.stderr)

    BRIEF_TEXT_PATH.write_text(brief_text)
    print(f"[{datetime.now():%H:%M:%S}] brief: {len(brief_text)} chars → {BRIEF_TEXT_PATH}")

    # Stage 3: dedup — salt with mode+date so morning/evening same day are distinct
    salted = f"{mode}|{datetime.now().date().isoformat()}|{brief_text}"
    fingerprint = state_mod.fingerprint_brief(salted)
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

    # Morning brief only: spoken audio via iMessage
    if audio and mode == "morning":
        print(f"[{datetime.now():%H:%M:%S}] TTS delivery...")
        tts_ok = await deliver_tts(brief_text)
        print(f"[{datetime.now():%H:%M:%S}] TTS: {'delivered' if tts_ok else 'FAILED'}")

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
    parser.add_argument("--no-audio", action="store_true",
                        help="skip TTS audio delivery (morning mode only)")
    args = parser.parse_args()

    mode = "evening" if args.evening else "morning"
    recipients = [PERSONAL_EMAIL]

    agent_name = f"home-ops-{mode}"
    if cp:
        try: cp.event(agent_name, "start")
        except Exception: pass
        if cp.is_killed(agent_name):
            print(f"[home-ops] {agent_name} killed via agent-cp, exiting")
            sys.exit(0)
    try:
        code = asyncio.run(run(
            mode=mode,
            dry_run=args.dry_run,
            gather_only=args.gather_only,
            force=args.force,
            recipients=recipients,
            audio=(mode == "morning" and not args.no_audio),
        ))
    except BaseException as _e:
        import traceback as _tb
        _tbs = _tb.format_exc()
        if cp:
            try:
                cp.event(agent_name, "error",
                         payload={"exc": type(_e).__name__, "msg": str(_e)[:500]})
            except Exception: pass
        sys.stderr.write(_tbs)
        raise
    if cp:
        try: cp.event(agent_name, "complete", payload={"code": code})
        except Exception: pass
    sys.exit(code)


if __name__ == "__main__":
    main()
