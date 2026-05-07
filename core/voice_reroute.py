"""Pushover-to-Voice reroute helper.

When the kill-switch flag is active, Pushover sends are rerouted to the
overseer voice pane (claude:9 on Mac) via pane-ask-v2, instead of going
to John's phone. Also writes a durable queue log so alerts are preserved
even if the dispatch fails.

Activated by either:
  - env var PUSHOVER_TO_VOICE=1
  - flag file /etc/pushover-to-voice.flag
  - flag file ~/.config/pushover-to-voice.flag

To revert: remove the flag files (or unset env). All callers immediately
go back to direct Pushover.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

VOICE_REROUTE_FLAGS = (
    Path("/etc/pushover-to-voice.flag"),
    Path.home() / ".config" / "pushover-to-voice.flag",
)
VOICE_QUEUE_LOG = Path("/tmp/pushover-voice-queue.log")
VOICE_TARGET_PANE = "claude:9"


def voice_reroute_active() -> bool:
    if os.environ.get("PUSHOVER_TO_VOICE") == "1":
        return True
    return any(p.exists() for p in VOICE_REROUTE_FLAGS)


def voice_reroute_send(
    title: str,
    message: str,
    priority: int = 0,
    url: str | None = None,
    url_title: str | None = None,
) -> bool:
    """Reroute a Pushover-style alert to claude:9 voice pane.

    Always returns True when reroute is active (the queue log captures the
    alert even if the pane-ask-v2 dispatch itself fails). Returns False if
    rerouting is not active — caller should proceed with normal Pushover.
    """
    if not voice_reroute_active():
        return False
    host = socket.gethostname()
    line = (
        f"{int(time.time())}\tP{priority}\t{host}\t{title}\t"
        f"{message[:600].replace(chr(10), ' / ')}\n"
    )
    try:
        with VOICE_QUEUE_LOG.open("a") as fh:
            fh.write(line)
    except OSError:
        pass
    payload = f"[REROUTED PUSHOVER P{priority} from {host}] {title}\n\n{message}"
    if url:
        payload += f"\n\n{url_title or 'Link'}: {url}"
    is_mac = sys.platform == "darwin"
    pane_ask = (
        "/Users/johncornelius/bin/pane-ask-v2" if is_mac else "/home/ubuntu/bin/pane-ask-v2"
    )
    cmd = [pane_ask, "--label", "pushover-reroute"]
    if not is_mac:
        cmd.extend(["--ssh", "mac"])
    cmd.extend([VOICE_TARGET_PANE, payload])
    try:
        subprocess.run(cmd, timeout=20, check=False, capture_output=True)
    except Exception:
        pass
    return True
