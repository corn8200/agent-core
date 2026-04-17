"""Shared TTS delivery: brief text -> audio -> R2 -> iMessage URL.

Consolidated 2026-04-17. Both home_ops/engine.py and briefs/morning_brief.py
previously held near-identical copies of this pipeline; callers now import
`deliver_brief_tts` from here.

Pipeline:
  1. Write brief text to /tmp/home-ops-brief.txt
  2. Run ~/claude-config/scripts/brief-deliver.py (generates /tmp/brief-audio.m4a)
  3. Upload audio to R2 bucket `audio-share/<key>` via wrangler
  4. Send iMessage with the public R2 URL via tmux relay

Notes:
  - brief-deliver.py is invoked with --no-imessage because its bare osascript
    path hangs when spawned by launchd. We do iMessage here via tmux relay.
  - Returns True if audio generated + uploaded + iMessage sent. Partial
    success paths print to stderr but still return True if audio generated
    (consumer can decide what "delivered" means).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

from core.constants import HOME, PERSONAL_EMAIL

BRIEF_TEXT_PATH = Path("/tmp/home-ops-brief.txt")
BRIEF_AUDIO_PATH = Path("/tmp/brief-audio.m4a")
R2_PUBLIC_BASE = "https://pub-a5fc31bf3f0b42c69a2565c407a447cd.r2.dev"
R2_BUCKET = "audio-share"


async def deliver_brief_tts(
    brief_text: str,
    msg_prefix: str = "Morning Brief",
    r2_key: str = "brief.m4a",
    to: Optional[str] = None,
) -> bool:
    """Generate audio from brief_text, upload to R2, deliver link via iMessage.

    Args:
        brief_text: The brief body to narrate.
        msg_prefix: Leading label in the iMessage (e.g. "Morning Brief").
        r2_key: Key under audio-share/ bucket (default "brief.m4a").
        to: iMessage destination. Defaults to PERSONAL_EMAIL.

    Returns:
        True if audio was generated successfully. iMessage + R2 are
        best-effort — failures print to stderr but do not fail the call.
    """
    to_addr = to or PERSONAL_EMAIL
    BRIEF_TEXT_PATH.write_text(brief_text)

    deliver_script = HOME / "claude-config" / "scripts" / "brief-deliver.py"
    if not deliver_script.exists():
        # Fallback: macOS say — no audio file, no iMessage link
        try:
            subprocess.run(
                ["say", "-v", "Alex", "-f", str(BRIEF_TEXT_PATH)],
                timeout=120,
            )
            return True
        except Exception:
            return False

    try:
        result = subprocess.run(
            [
                str(HOME / "Projects" / "agent-core" / ".venv" / "bin" / "python3"),
                str(deliver_script),
                str(BRIEF_TEXT_PATH),
                "--no-imessage",
            ],
            capture_output=True, text=True, timeout=120,
        )
        audio_ok = result.returncode == 0
        if not audio_ok:
            print(
                f"brief-deliver.py failed: {result.stderr[:400]}",
                file=sys.stderr,
            )
    except subprocess.TimeoutExpired:
        print("brief-deliver.py timed out at 120s", file=sys.stderr)
        audio_ok = BRIEF_AUDIO_PATH.exists()

    if not audio_ok:
        return False

    r2_url: Optional[str] = None
    try:
        upload = subprocess.run(
            [
                "wrangler", "r2", "object", "put", f"{R2_BUCKET}/{r2_key}",
                "--file", str(BRIEF_AUDIO_PATH),
                "--content-type", "audio/mp4",
                "--remote",
            ],
            capture_output=True, text=True, timeout=60,
        )
        if upload.returncode == 0:
            r2_url = f"{R2_PUBLIC_BASE}/{r2_key}"
        else:
            print(f"R2 upload failed: {upload.stderr[:200]}", file=sys.stderr)
    except Exception as e:
        print(f"R2 upload error: {e}", file=sys.stderr)

    if r2_url:
        try:
            from core.tools import send_imessage_reliable
            first_line = brief_text.split("\n", 1)[0][:180]
            msg = f"{msg_prefix}: {r2_url}\n\n{first_line}"
            await send_imessage_reliable(to_addr, msg)
        except Exception as e:
            print(f"iMessage delivery failed: {e}", file=sys.stderr)
    else:
        print(
            "R2 upload failed -- skipping iMessage audio delivery",
            file=sys.stderr,
        )
    return True
