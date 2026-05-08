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
from core.pushover import send_pushover

AUDIT_LOG_PATH = Path(AUDIT_LOG)


async def _pushover_fail(detail: str) -> None:
    """Route a heartbeat failure through the Overseer-aware Pushover helper."""
    try:
        host = os.uname().nodename
        title = f"Audit log heartbeat FAILED ({host})"
        url = None
        url_title = None
        try:
            from core.interactive_links import alert_action_url

            url = alert_action_url(
                source="audit-heartbeat",
                title=title,
                message=detail,
                severity="warn",
            )
            url_title = "Send to Mac panel 3"
        except Exception:
            pass
        result = await send_pushover(
            title=title,
            message=detail[:1024],
            priority=0,
            url=url,
            url_title=url_title,
        )
        if not result.ok:
            print(f"[audit-heartbeat] pushover skipped: {result.detail}", file=sys.stderr)
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
        await _pushover_fail(msg)
        return 2

    try:
        tail = AUDIT_LOG_PATH.read_text().splitlines()[-50:]
    except Exception as e:
        msg = f"cannot read audit log at {AUDIT_LOG_PATH}: {e}"
        print(f"FAIL: {msg}", file=sys.stderr)
        await _pushover_fail(msg)
        return 2

    found = any(marker in line for line in tail)
    if not found:
        msg = f"marker {marker} not found in tail of {AUDIT_LOG_PATH}"
        print(f"FAIL: {msg}", file=sys.stderr)
        await _pushover_fail(msg)
        return 2

    print(f"OK: heartbeat {marker} round-tripped to {AUDIT_LOG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
