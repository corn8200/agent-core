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


async def main():
    print("[daemon] iMessage bus starting...")
    init_db()

    reader = MessageReader(poll_interval=5)
    reader.subscribe(route)

    print("[daemon] Reader + Router + Retry loop active")
    await asyncio.gather(
        reader.poll_loop(),
        retry_loop(interval=30.0),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[daemon] Shutting down.")
