#!/usr/bin/env python3
"""Unified iMessage daemon — single process for inbound reader + router + outbound retry.

Replaces research-chain/main.py as the sole iMessage listener.
Run as LaunchAgent: com.john.imessage-bus.plist

Usage:
    python3 daemon/imessage_daemon.py
"""

import asyncio
import os
import sys
from pathlib import Path

# Force unbuffered stdout for launchd log capture
os.environ["PYTHONUNBUFFERED"] = "1"

# Ensure agent-core is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.message_reader import MessageReader
from core.message_router import route
from core.message_bus import retry_loop
from core.message_db import init_db
import core.agent_cp_client as cp  # noqa: E402
CP_AGENT = "imessage-daemon"

async def _kill_watchdog():
    import asyncio as _a
    while True:
        if cp.is_killed(CP_AGENT):
            print(f"[daemon] {CP_AGENT} killed via agent-cp, exiting", flush=True)
            import os as _os; _os._exit(0)
        await _a.sleep(60)


async def main():
    print("[daemon] iMessage bus starting...")
    try: cp.event(CP_AGENT, "start")
    except Exception: pass
    init_db()

    reader = MessageReader(poll_interval=5)
    reader.subscribe(route)

    print("[daemon] Reader + Router + Retry loop active")
    await asyncio.gather(
        reader.poll_loop(),
        retry_loop(interval=30.0),
        _kill_watchdog(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[daemon] Shutting down.")
    except BaseException as _e:
        import traceback as _tb
        _tbs = _tb.format_exc()
        try:
            cp.event(CP_AGENT, "error",
                     payload={"exc": type(_e).__name__, "msg": str(_e)[:500]})
        except Exception: pass
        sys.stderr.write(_tbs)
        raise
