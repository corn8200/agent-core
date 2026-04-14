#!/usr/bin/env python3
"""
Morning Brief Agent — automated daily brief at 5:30 AM.

Three stages:
  1. Gather (pure Python, parallel, 0 tokens)
  2. Synthesize (single SDK query(), ~$0.03)
  3. Deliver (HTML email via VPS + optional TTS/iMessage)

Usage:
  python3 briefs/morning_brief.py              # full run
  python3 briefs/morning_brief.py --dry-run    # gather + synthesize, skip delivery
  python3 briefs/morning_brief.py --gather-only # just gather and cache
"""

import asyncio
import json
import os
import sys
import subprocess
from datetime import datetime
from pathlib import Path

# ⚠️ Scrub API billing vars BEFORE importing claude_agent_sdk.
# The SDK spawns the `claude` CLI subprocess and inherits our env — if any of
# these are set, the CLI silently bills pay-as-you-go instead of Max. See
# https://github.com/anthropics/claude-code/issues/42680 and the 2026-04-12
# ralph incident. secrets.env no longer exports these under the canonical names,
# but scrub defensively in case any parent (LaunchAgent, shell, subprocess) did.
for _leak_var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                  "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
    os.environ.pop(_leak_var, None)

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from home_ops.gather import gather_all
from core.constants import HOME, PERSONAL_EMAIL, VPS_SSH
try:
    import core.agent_cp_client as cp
except Exception:
    cp = None
CP_AGENT = "morning-brief"

BRIEF_TEXT_PATH = Path.home() / "logs" / "brief-text.txt"
BRIEF_HTML_PATH = Path.home() / "logs" / "morning-brief.html"
BRIEF_TEXT_PATH.parent.mkdir(parents=True, exist_ok=True)

SYNTHESIS_PROMPT = """You have the following system data gathered just now. Write a SPOKEN morning brief.

CRITICAL DATA RULES (read carefully):
- `apple.calendar` is a list of structured events with fields: summary, start, end, calendar, bucket. The bucket is "today", "tomorrow", or "other". **Every event with bucket='today' MUST be mentioned by name and time in the CALENDAR section.** Do not skip any. Same for bucket='tomorrow' (mention briefly).
- `apple.reminders` is pre-bucketed by urgency: overdue, today, this_week, later, undated. **Every overdue reminder MUST be called out explicitly** — these are late items. Every reminder in "today" MUST be mentioned. Summarize "this_week" briefly (item names). For undated, only mention count + a few standout items (don't list groceries).
- If a field is empty or the list is empty, it's truly empty. If a list has items, you must report them. Do NOT say "calendar is clear" if apple.calendar has any bucket='today' items. Do NOT say "reminder list is clean" if apple.reminders.count > 0.

RULES:
1. This is audio, not text. Write exactly as you would speak it aloud. Contractions. No bullets, no markdown, no headers. No "asterisk" or "dash". Spell out abbreviations.
2. Start with: "Good morning John. Here's your brief for {day_of_week}, {month_day}."
3. Section order:
   - WEATHER: Harpers Ferry and Frederick conditions. Keep it quick: temp, conditions, anything notable.
   - URGENT: Failed services, unanswered client emails, new quote requests, **any overdue reminders from apple.reminders.overdue**. Call them out by name.
   - BUSINESS: Sentry AI Thermal pipeline — clicks (hot leads!), sends, bounces, SAM.gov matches, form submissions. Then JobSignal — new matches worth reviewing. No TAMKO/day job.
   - CALENDAR & SCHEDULE: Walk through EVERY event in apple.calendar with bucket='today' — say the time and what it is. Then tomorrow's preview (bucket='tomorrow'). Then top three priority tasks for the day.
   - FAMILY & REMINDERS: apple.reminders.today items (by name), then apple.reminders.this_week items (by name, brief). If reminders.undated has non-grocery actionable items, mention a few. Groceries get a single line like "shopping list has X items".
   - AWARENESS: Only MAJOR breaking national news. If nothing major, skip entirely.
   - INFRASTRUCTURE: Only problems. If all green: "All systems running clean." If `data.vps_auth.recent_incidents` has entries from the last twenty-four hours, mention them as "VPS Claude auth flapped N times overnight, all auto-recovered, currently {{data.vps_auth.status}}" — this is ground-truth status, not a problem, just situation awareness so any alert emails John sees are already contextualized. If `data.vps_auth.status` is "fail" right now, that IS a problem — say "VPS Claude auth is currently broken, reauth needed" and put it in URGENT too.
4. Top three priorities after schedule section.
5. Be actionable. "You've got a lead who clicked three times — might be worth a direct call" not "3 click events detected."
6. Skip green. Don't report healthy services, zero bounces, no SAM matches.
7. Target 500-800 words. 3-5 minute audio.
8. End with one sentence: the single most important thing to do first today.
9. Numbers: "twelve" not "12", "forty percent" not "40%". Audio-friendly.
10. No TAMKO. Never reference employer name.
11. Verify before you finalize: did you mention every apple.calendar today event? Did you mention every apple.reminders.overdue and apple.reminders.today item? If not, fix the draft before returning.

DATA:
{data}"""

HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#1a1a2e;color:#e0e0e0;padding:20px;max-width:640px;margin:0 auto;">
<h1 style="color:#00d4ff;font-size:22px;border-bottom:1px solid #333;padding-bottom:8px;">Morning Brief — {date}</h1>
<div style="font-size:15px;line-height:1.6;white-space:pre-wrap;">{brief}</div>
<hr style="border-color:#333;margin:20px 0;">
<p style="color:#666;font-size:12px;">Generated {timestamp} by agent-core/briefs</p>
</body></html>"""


async def synthesize(data: dict) -> str:
    """Use SDK to write the brief from gathered data."""
    from claude_agent_sdk import query, ClaudeAgentOptions
    from core.hooks import AGENT_HOOKS
    from core.thinking import HEAVY

    now = datetime.now()
    prompt = SYNTHESIS_PROMPT.format(
        day_of_week=now.strftime("%A"),
        month_day=now.strftime("%B %d"),
        data=json.dumps(data, indent=2, default=str),
    )

    brief_text = ""
    try:
        async for msg in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                model="opus",
                permission_mode="bypassPermissions",
                max_turns=2,
                max_budget_usd=0.15,
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
    except Exception:
        pass  # SDK throws on CLI exit after result is received

    return brief_text.strip()


def build_html(brief_text: str) -> str:
    """Wrap brief text in a dark-themed HTML email."""
    now = datetime.now()
    return HTML_TEMPLATE.format(
        date=now.strftime("%A, %B %d, %Y"),
        brief=brief_text.replace("\n", "<br>"),
        timestamp=now.strftime("%Y-%m-%d %H:%M"),
    )


def deliver_email(html: str) -> bool:
    """Send HTML email via VPS SMTP."""
    email_script = f'''
import smtplib, os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv("/srv/apps/friday-email/.env")

msg = MIMEMultipart("alternative")
msg["Subject"] = "Morning Brief — {datetime.now().strftime('%A %b %d')}"
msg["From"] = "Cornelius Family <notify@jcornelius.net>"
msg["To"] = "{PERSONAL_EMAIL}"
msg.attach(MIMEText("""{html.replace('"', '\\"')}""", "html"))

with smtplib.SMTP("smtp.gmail.com", 587) as s:
    s.starttls()
    s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
    s.send_message(msg)
print("sent")
'''
    script_path = Path("/tmp/morning_brief_send.py")
    script_path.write_text(email_script)

    # SCP to VPS and run
    subprocess.run(["scp", str(script_path), f"{VPS_SSH}:/tmp/morning_brief_send.py"],
                   capture_output=True, timeout=15)
    result = subprocess.run(
        ["ssh", VPS_SSH, "cd /srv/apps/friday-email && .venv/bin/python /tmp/morning_brief_send.py"],
        capture_output=True, text=True, timeout=30,
    )
    return result.returncode == 0


async def deliver_tts(brief_text: str) -> bool:
    """Convert brief to speech (via brief-deliver.py) and deliver the audio link
    via iMessage using the tmux relay (send_imessage_reliable).

    brief-deliver.py is called with --no-imessage because its bare osascript
    iMessage path hangs when invoked from launchd context (Messages.app GUI
    session unreachable). We do the iMessage send here instead, using the
    tmux relay which works from any context.
    """
    BRIEF_TEXT_PATH.write_text(brief_text)
    deliver_script = HOME / "claude-config" / "scripts" / "brief-deliver.py"
    audio_ok = False

    if deliver_script.exists():
        try:
            result = subprocess.run(
                [
                    str(HOME / ".venvs" / "sora" / "bin" / "python3"),
                    str(deliver_script),
                    str(BRIEF_TEXT_PATH),
                    "--no-imessage",
                ],
                capture_output=True, text=True, timeout=120,
            )
            audio_ok = result.returncode == 0
            if not audio_ok:
                print(f"brief-deliver.py failed: {result.stderr[:400]}", file=sys.stderr)
        except subprocess.TimeoutExpired:
            print("brief-deliver.py timed out at 120s", file=sys.stderr)
            audio_ok = Path("/tmp/brief-audio.m4a").exists()
    else:
        # Fallback: macOS say (no iMessage delivery in this branch)
        try:
            subprocess.run(["say", "-v", "Alex", "-f", str(BRIEF_TEXT_PATH)], timeout=120)
            return True
        except Exception:
            return False

    # Upload audio to R2 and deliver HTTPS link via iMessage
    if audio_ok:
        audio_path = Path("/tmp/brief-audio.m4a")
        r2_url = None
        try:
            upload = subprocess.run(
                ["wrangler", "r2", "object", "put", "audio-share/brief.m4a",
                 "--file", str(audio_path), "--content-type", "audio/mp4", "--remote"],
                capture_output=True, text=True, timeout=60,
            )
            if upload.returncode == 0:
                r2_url = "https://pub-a5fc31bf3f0b42c69a2565c407a447cd.r2.dev/brief.m4a"
            else:
                print(f"R2 upload failed: {upload.stderr[:200]}", file=sys.stderr)
        except Exception as e:
            print(f"R2 upload error: {e}", file=sys.stderr)

        try:
            from core.tools import send_imessage_reliable
            url = r2_url or "http://100.122.35.56:8080/brief.m4a"
            first_line = brief_text.split("\n", 1)[0][:180]
            msg = f"Morning Brief: {url}\n\n{first_line}"
            await send_imessage_reliable(PERSONAL_EMAIL, msg)
        except Exception as e:
            print(f"iMessage delivery failed: {e}", file=sys.stderr)
    return audio_ok


async def main():
    dry_run = "--dry-run" in sys.argv
    gather_only = "--gather-only" in sys.argv

    # Stage 1: Gather
    print(f"[{datetime.now():%H:%M:%S}] Gathering data...")
    data = await gather_all(force=True)
    print(f"[{datetime.now():%H:%M:%S}] Gathered from {len(data)} sources")

    # Pull VPS auth-watcher state so the brief can surface overnight incidents
    try:
        r = subprocess.run(
            ["ssh", VPS_SSH, "cat /srv/apps/auth-watcher/state.json"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            data["vps_auth"] = json.loads(r.stdout)
    except Exception as e:
        print(f"[warn] could not fetch vps_auth state: {e}")

    if gather_only:
        print(f"Cache written to /tmp/claude-gather.json")
        return

    # Stage 2: Synthesize
    print(f"[{datetime.now():%H:%M:%S}] Synthesizing brief...")
    brief_text = await synthesize(data)
    BRIEF_TEXT_PATH.write_text(brief_text)
    print(f"[{datetime.now():%H:%M:%S}] Brief: {len(brief_text)} chars, saved to {BRIEF_TEXT_PATH}")

    if dry_run:
        print("\n--- DRY RUN OUTPUT ---")
        print(brief_text[:1000])
        if len(brief_text) > 1000:
            print(f"\n... ({len(brief_text) - 1000} more chars)")
        return

    # Stage 3: Deliver
    html = build_html(brief_text)
    BRIEF_HTML_PATH.write_text(html)

    print(f"[{datetime.now():%H:%M:%S}] Sending email...")
    email_ok = deliver_email(html)
    print(f"[{datetime.now():%H:%M:%S}] Email: {'sent' if email_ok else 'FAILED'}")

    print(f"[{datetime.now():%H:%M:%S}] TTS delivery...")
    tts_ok = await deliver_tts(brief_text)
    print(f"[{datetime.now():%H:%M:%S}] TTS: {'delivered' if tts_ok else 'FAILED'}")

    print(f"[{datetime.now():%H:%M:%S}] Morning brief complete.")


if __name__ == "__main__":
    if cp:
        try: cp.event(CP_AGENT, "start")
        except Exception: pass
        if cp.is_killed(CP_AGENT):
            print(f"[brief] {CP_AGENT} killed via agent-cp, exiting")
            sys.exit(0)
    try:
        asyncio.run(main())
    except BaseException as _e:
        import traceback as _tb
        _tbs = _tb.format_exc()
        if cp:
            try:
                cp.event(CP_AGENT, "error",
                         payload={"exc": type(_e).__name__, "msg": str(_e)[:500]})
            except Exception: pass
        sys.stderr.write(_tbs)
        raise
    if cp:
        try: cp.event(CP_AGENT, "complete")
        except Exception: pass
