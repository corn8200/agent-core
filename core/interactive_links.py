"""Interactive notification links shared by local agent-core senders.

This intentionally does not mint cockpit reply tokens or approval tokens. It is
a short-lived signed GET action for one-way Pushover buttons that hands alert
context to a pane. When the Universal Approval Bus owns one-tap notification
actions, move this signing grammar behind that shared helper instead of adding
new query formats here.
"""
from __future__ import annotations

import hmac
import os
import time
import urllib.parse
from hashlib import sha256


CP_PUBLIC_BASE = os.environ.get("CP_PUBLIC_BASE", "https://cp.jcornelius.net").rstrip("/")
NOTIFY_PUBLIC_BASE = os.environ.get("NOTIFY_PUBLIC_BASE", "https://app.jcornelius.net/notify").rstrip("/")


def _clip(value: str, limit: int) -> str:
    value = str(value or "").strip().replace("\r", " ").replace("\n", " ")
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def portal_url(path: str = "/") -> str:
    path = "/" + path.lstrip("/")
    return f"{CP_PUBLIC_BASE}{path}"


def _action_secret() -> str | None:
    for name in ("NOTIFY_AUTH_TOKEN", "APPLE_BRIDGE_TOKEN", "CP_API_BEARER_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value
    try:
        from core.vault import get_secret

        for name in ("NOTIFY_AUTH_TOKEN", "APPLE_BRIDGE_TOKEN", "CP_API_BEARER_TOKEN"):
            value = get_secret(name)
            if value:
                return value
    except Exception:
        return None
    return None


def _canonical_query(params: dict[str, str]) -> str:
    return urllib.parse.urlencode(sorted(params.items()))


def alert_action_url(
    *,
    source: str,
    title: str,
    message: str,
    severity: str = "alert",
    target_pane: str = "claude:3",
) -> str:
    """Return a signed notify action URL that dispatches context to Mac pane 3.

    Falls back to the Codex portal if the action-signing secret is unavailable.
    """
    secret = _action_secret()
    if not secret:
        return portal_url("/codex")

    params = {
        "src": _clip(source, 48),
        "sev": _clip(severity, 24),
        "t": _clip(title, 80),
        "m": _clip(message, 180),
        "pane": _clip(target_pane, 24),
        "ts": str(int(time.time())),
    }
    canonical = _canonical_query(params)
    sig = hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), sha256).hexdigest()
    return f"{NOTIFY_PUBLIC_BASE}/alert/send-to-mac3?{canonical}&sig={sig}"
