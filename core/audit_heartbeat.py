"""Daily audit trail heartbeat.

Writes a known event via the same code path AGENT_HOOKS uses, reads it back
from the jsonl, and exits non-zero if the round-trip fails. Fires a
Pushover P0 notification on failure so silent audit-log breakage shows up
within 24 hours.

Invoked by `com.local.audit-heartbeat` LaunchAgent on Mac and the
`agent-audit-heartbeat.timer` systemd unit on VPS. Both run daily at 07:15
local/UTC.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.hooks import AUDIT_LOG, audit_write

AUDIT_LOG_PATH = Path(AUDIT_LOG)


def _pushover_fail(detail: str) -> None:
    """Fire a Pushover P0 alert when the heartbeat fails. Silent on any error."""
    try:
        import httpx

        from core.vault import get_secret

        user = get_secret("PUSHOVER_USER_KEY")
        token = get_secret("PUSHOVER_APP_TOKEN")
        if not user or not token:
            print("[audit-heartbeat] pushover skipped: no creds", file=sys.stderr)
            return
        host = os.uname().nodename
        httpx.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": token,
                "user": user,
                "title": f"Audit log heartbeat FAILED ({host})",
                "message": detail[:1024],
                "priority": 0,
            },
            timeout=10.0,
        )
    except Exception as e:
        print(f"[audit-heartbeat] pushover fallback failed: {e}", file=sys.stderr)


async def main() -> int:
    marker = f"heartbeat-{uuid.uuid4()}"
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "heartbeat",
        "source": "audit_heartbeat",
        "marker": marker,
        "host": os.uname().nodename,
    }

    audit_write(entry)
    await asyncio.sleep(0.5)

    if not AUDIT_LOG_PATH.exists():
        msg = f"audit log does not exist at {AUDIT_LOG_PATH}"
        print(f"FAIL: {msg}", file=sys.stderr)
        _pushover_fail(msg)
        return 2

    try:
        tail = AUDIT_LOG_PATH.read_text().splitlines()[-50:]
    except Exception as e:
        msg = f"cannot read audit log at {AUDIT_LOG_PATH}: {e}"
        print(f"FAIL: {msg}", file=sys.stderr)
        _pushover_fail(msg)
        return 2

    found = any(marker in line for line in tail)
    if not found:
        msg = f"marker {marker} not found in tail of {AUDIT_LOG_PATH}"
        print(f"FAIL: {msg}", file=sys.stderr)
        _pushover_fail(msg)
        return 2

    print(f"OK: heartbeat {marker} round-tripped to {AUDIT_LOG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
