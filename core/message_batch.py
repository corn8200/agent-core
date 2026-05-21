"""Silent batching for message_bus.send_message (router-v2 Task D).

Problem: parallel agent fan-out produces 3+ iMessage pings inside 30s, which
reads as noise on the phone. Batching collapses N messages bound for the
same chat into one outbound within a configurable window.

Design:
  - Opt-in per call. send_message(..., batch_window=45) enables batching.
    Default (batch_window=None) = instant delivery, unchanged behavior.
  - Per-chat queue + per-chat timer. A new queued message resets the timer
    (sliding window) up to `window` seconds, capped at MAX_WINDOW_S.
  - Force-flush when queue length > MAX_QUEUE or when flush_all() is called
    (e.g. daemon shutdown).
  - Thread-safe via asyncio.Lock; all operations are async.
  - Zero external deps.

The actual delivery path (`_deliver`) is injected at module load time to
avoid an import cycle (message_bus ↔ message_batch).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable

MAX_WINDOW_S = 120
MAX_QUEUE = 10
SEP = "\n\n— — —\n\n"


@dataclass
class _QueuedMessage:
    recipient: str
    message: str
    tier: str
    agent: str
    log_id: int | None = None


@dataclass
class _ChatQueue:
    messages: list[_QueuedMessage] = field(default_factory=list)
    timer: asyncio.Task | None = None
    scheduled_flush_at: datetime | None = None


DeliverFn = Callable[[str, str, str], Awaitable[tuple[bool, str]]]


class BatchWindow:
    """Per-process batching state. One instance per process is plenty."""

    def __init__(self, deliver: DeliverFn):
        self._deliver = deliver
        self._queues: dict[str, _ChatQueue] = {}
        self._lock = asyncio.Lock()

    async def enqueue(
        self,
        *,
        recipient: str,
        message: str,
        tier: str,
        agent: str,
        log_id: int | None,
        window: int,
    ) -> None:
        """Queue a message; start/extend the flush timer."""
        window = max(1, min(int(window), MAX_WINDOW_S))
        async with self._lock:
            q = self._queues.setdefault(recipient, _ChatQueue())
            q.messages.append(_QueuedMessage(
                recipient=recipient,
                message=message,
                tier=tier,
                agent=agent,
                log_id=log_id,
            ))

            # Force-flush on queue overflow — no timer needed.
            if len(q.messages) > MAX_QUEUE:
                await self._flush_locked(recipient)
                return

            # Extend / restart the sliding window.
            if q.timer and not q.timer.done():
                q.timer.cancel()
            q.scheduled_flush_at = datetime.now()
            q.timer = asyncio.create_task(self._delayed_flush(recipient, window))

    async def _delayed_flush(self, recipient: str, window: int) -> None:
        try:
            await asyncio.sleep(window)
        except asyncio.CancelledError:
            return
        async with self._lock:
            await self._flush_locked(recipient)

    async def _flush_locked(self, recipient: str) -> None:
        q = self._queues.get(recipient)
        if not q or not q.messages:
            return
        msgs = q.messages
        q.messages = []
        if q.timer and not q.timer.done():
            q.timer.cancel()
        q.timer = None

        # Release the lock across the (slow) send to avoid head-of-line
        # blocking for other chats. Capture what we need as locals.
        recipient_local = recipient
        tier = msgs[0].tier  # mixed-tier queues use the first message's tier
        body = self._join(msgs)

        # Fire the send without holding the lock.
        asyncio.create_task(self._send_batched(recipient_local, body, tier, msgs))

    async def _send_batched(
        self,
        recipient: str,
        body: str,
        tier: str,
        source_msgs: list[_QueuedMessage],
    ) -> None:
        # Import here to avoid circular — message_bus owns log_outbound/mark_*.
        from core.message_db import log_outbound, mark_sent, mark_failed

        # Log batched row distinctly so audit can see aggregation happened.
        batch_agent = "+".join(sorted({m.agent for m in source_msgs})) or "batch"
        batch_row_id = log_outbound(
            agent=f"batch:{batch_agent}"[:64],
            recipient=recipient,
            message=body,
            tier=tier,
        )

        try:
            ok, result = await self._deliver(recipient, body, tier)
        except Exception as e:
            ok, result = False, f"batch deliver exception: {e}"

        if ok:
            mark_sent(batch_row_id)
            # Sources were already log_outbound'd as 'pending'; mark archived.
            for m in source_msgs:
                if m.log_id is not None:
                    # Repurpose status so they're not picked up by retry loop.
                    try:
                        import sqlite3
                        from core.message_db import _connect
                        with _connect() as conn:
                            conn.execute(
                                "UPDATE outbound SET status='batched', "
                                "sent_at=?, error=? WHERE id=?",
                                (datetime.now().isoformat(),
                                 f"merged into batch id={batch_row_id}",
                                 m.log_id),
                            )
                    except Exception:
                        pass
        else:
            mark_failed(batch_row_id, result)

    @staticmethod
    def _join(msgs: list[_QueuedMessage]) -> str:
        """Combine N queued messages into one outbound body.

        Format:
          [batched: N messages from a,b,c]
          <msg1>
          — — —
          <msg2>
          — — —
          ...
        """
        agents = sorted({m.agent for m in msgs})
        header = f"[batched: {len(msgs)} messages from {','.join(agents)}]"
        bodies = [m.message for m in msgs]
        return header + "\n" + SEP.join(bodies)

    async def flush_all(self) -> None:
        """Force-flush every pending chat queue. Call on daemon shutdown."""
        async with self._lock:
            recipients = list(self._queues.keys())
        for r in recipients:
            async with self._lock:
                await self._flush_locked(r)

    def pending_count(self) -> int:
        """Non-async peek — useful for tests."""
        return sum(len(q.messages) for q in self._queues.values())


# ---- Module-level singleton wiring -----------------------------------------
#
# The delivery function is injected by message_bus on its import so we avoid
# importing it here (message_bus depends on send_imessage_reliable which is
# in core.tools; message_batch must not depend on message_bus).

_window: BatchWindow | None = None


def get_window() -> BatchWindow:
    global _window
    if _window is None:
        # Lazy bind — at first access, pull message_bus._deliver.
        from core.message_bus import _deliver  # type: ignore
        _window = BatchWindow(_deliver)
    return _window


async def enqueue(
    *,
    recipient: str,
    message: str,
    tier: str,
    agent: str,
    log_id: int | None,
    window: int,
) -> None:
    await get_window().enqueue(
        recipient=recipient, message=message, tier=tier,
        agent=agent, log_id=log_id, window=window,
    )


async def flush_all() -> None:
    if _window is None:
        return
    await _window.flush_all()
