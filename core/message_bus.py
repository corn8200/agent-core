"""Unified outbound iMessage API — tiers, attribution, retry, audit.

All agents send messages through send_message(). The low-level transport
(send_imessage_reliable) stays in tools.py — this module is policy, not plumbing.

When the VPS notification center is ready, swap/add a transport backend here
without changing any caller code.
"""

import asyncio

from core.constants import PERSONAL_EMAIL
from core.message_db import log_outbound, mark_sent, mark_failed, get_retry_queue

# Agent display names for attribution
AGENT_NAMES = {
    "scout": "Scout",
    "forge": "Forge",
    "wrench": "Wrench",
    "dispatch": "Dispatch",
    "ledger": "Ledger",
    "toolsmith": "Toolsmith",
    "titan": "Titan",
    "anvil": "Anvil",
    "critic": "Critic",
    "turbo": "Turbo",
    "foreman": "Foreman",
    "watch-commander": "WC",
    "wc": "WC",
    "nudge": "Nudge",
    "brief": "Brief",
    "handler": "Handler",
    "mailtriage": "Mail",
    "test": "Test",
}

# Max retry attempts per tier
TIER_MAX_RETRIES = {
    "critical": 3,
    "normal": 2,
    "archive": 0,
}


async def send_message(
    message: str,
    agent: str,
    recipient: str = "",
    tier: str = "normal",
    attribution: bool = True,
    reply_tag: str | None = None,
    batch_window: int | None = None,
) -> tuple[bool, str]:
    """Send a message through the unified bus.

    Args:
        message: The message text.
        agent: Required. Agent name (e.g. "scout", "watch-commander").
        recipient: iMessage buddy. Defaults to PERSONAL_EMAIL.
        tier: "critical" (iMessage+Pushover), "normal" (iMessage), "archive" (DB only).
        attribution: If True, prepend [AgentName] to the message.
        reply_tag: Optional VPS reply tag. Format "<service>:<ref>", e.g.
            "sentinel:abc12". When set, "[V:<tag>] " is prepended so the user's
            reply routes back to the VPS service via Layer 0 of the router.
            (router-v2 Task C, Mac side of Mac+VPS interface.)
        batch_window: If an int (seconds), queue this message and flush after
            the window expires along with any sibling messages bound for the
            same recipient. Default None = instant delivery (unchanged).
            Caps at 120s. Max queue length 10. (router-v2 Task D.)

    Returns:
        (success, status_description). For batched messages, returns
        (True, "Queued (batch_window=Ns)") immediately; delivery happens
        asynchronously.
    """
    recipient = recipient or PERSONAL_EMAIL

    # Attribution prefix
    display_name = AGENT_NAMES.get(agent, agent.title())
    if attribution:
        message = f"[{display_name}] {message}"

    # Task C: VPS reply tag. Prepended AFTER attribution so the tag is the
    # first token on the line — VPS detects "^[V:<service>:<ref>]" exactly.
    if reply_tag:
        message = f"[V:{reply_tag}] {message}"

    # Archive tier — log only, no send
    if tier == "archive":
        log_outbound(agent=agent, recipient=recipient, message=message, tier=tier)
        return True, f"Archived ({agent})"

    # Log to DB (pending status)
    row_id = log_outbound(agent=agent, recipient=recipient, message=message, tier=tier)

    # Task D: batching. Log the intent, hand off to the batch window, return.
    # Delivery (or re-log as 'batched' on send) happens inside message_batch.
    if batch_window is not None and batch_window > 0:
        try:
            from core.message_batch import enqueue as _batch_enqueue
            await _batch_enqueue(
                recipient=recipient,
                message=message,
                tier=tier,
                agent=agent,
                log_id=row_id,
                window=batch_window,
            )
            return True, f"Queued (batch_window={batch_window}s)"
        except Exception as e:
            # Fall through to instant delivery if batching itself fails.
            print(f"[message_bus] batch enqueue fell back: {e}")

    # Send via transport
    ok, result = await _deliver(recipient, message, tier)

    if ok:
        mark_sent(row_id)
        return True, result
    else:
        mark_failed(row_id, result)
        return False, result


async def _deliver(recipient: str, message: str, tier: str) -> tuple[bool, str]:
    """Execute delivery based on tier. Uses send_imessage_reliable as transport."""
    # Import here to avoid circular import (tools.py doesn't import message_bus)
    from core.tools import send_imessage_reliable

    ok, result = await send_imessage_reliable(recipient, message)

    if ok:
        return True, result

    # Critical tier: also try Pushover (send_imessage_reliable already does this,
    # but if it failed both, we log it)
    if tier == "critical":
        return False, f"Critical delivery failed: {result}"

    return False, result


async def retry_loop(interval: float = 30.0):
    """Background task that retries failed sends. Run inside the daemon's event loop."""
    while True:
        await asyncio.sleep(interval)
        queue = get_retry_queue()
        if not queue:
            continue

        for row in queue:
            tier = row["tier"]
            max_retries = TIER_MAX_RETRIES.get(tier, 2)
            if row["attempts"] >= max_retries:
                mark_failed(row["id"], f"Max retries ({max_retries}) exceeded")
                continue

            # Exponential backoff: 30s, 60s, 120s
            backoff = min(30 * (2 ** row["attempts"]), 300)
            await asyncio.sleep(backoff)

            ok, result = await _deliver(row["recipient"], row["message"], tier)
            if ok:
                mark_sent(row["id"])
                print(f"[message_bus] Retry succeeded: {row['id']} ({row['agent']})")
            else:
                mark_failed(row["id"], result)
                print(f"[message_bus] Retry failed: {row['id']} ({row['agent']}): {result}")
