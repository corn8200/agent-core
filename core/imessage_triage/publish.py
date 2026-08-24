"""Publish an iMessage triage result to cp-api cockpit /inbox."""

from __future__ import annotations

import re
import os
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.retired_services import is_retired_route, retired_message  # noqa: E402

def _publish_url() -> str:
    return os.environ.get("IMESSAGE_TRIAGE_PUBLISH_URL", "").strip()

# Only these categories warrant a cockpit item (per spec)
_PUBLISH_CATEGORIES = frozenset({"action_me", "scheduling"})

# NANPA 555-01xx numbers are reserved for fictional/test use — never a real sender.
_TEST_HANDLE_RE = re.compile(r"^\+1555\d{7}$")


def should_publish(category: str) -> bool:
    return category in _PUBLISH_CATEGORIES


def is_test_handle(from_handle: str) -> bool:
    return bool(_TEST_HANDLE_RE.match(from_handle))


def publish_imessage_triage(
    *,
    chat_db_msg_id: int,
    category: str,
    urgency: int,
    from_handle: str,
    preview: str,
    received_at: str | None = None,
    dry_run: bool = False,
) -> bool:
    """POST the classified thread to cp-api. Returns True on 201.

    Silently returns False on any network/API error — never crashes the caller.
    """
    if is_test_handle(from_handle):
        print(f"[publish] skipping test/placeholder handle {from_handle}", flush=True)
        return False
    if not should_publish(category):
        return False
    if dry_run:
        print(
            f"[publish] DRY_RUN: chat_db_msg_id={chat_db_msg_id} "
            f"category={category} urgency={urgency} from={from_handle}",
            flush=True,
        )
        return True
    publish_url = _publish_url()
    if not publish_url or is_retired_route(publish_url):
        print(f"[publish] {retired_message('imessage-triage-publish')}", flush=True)
        return False
    payload: dict[str, Any] = {
        "chat_db_msg_id": chat_db_msg_id,
        "category": category,
        "urgency": urgency,
        "from_handle": from_handle,
        "preview": preview[:500],
    }
    if received_at:
        payload["received_at"] = received_at
    try:
        r = httpx.post(publish_url, json=payload, timeout=10)
        return r.status_code in (200, 201)
    except Exception as exc:
        print(f"[publish] post failed: {exc}", flush=True)
        return False
