"""Small Pushover client for agent-core notifications."""
from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from core.vault import get_secret


PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"


@dataclass(frozen=True)
class PushoverResult:
    ok: bool
    detail: str
    response: dict[str, Any] | None = None


def _clip(value: str, limit: int) -> str:
    value = (value or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _credentials() -> tuple[str | None, str | None]:
    token = get_secret("PUSHOVER_APP_TOKEN")
    user = get_secret("PUSHOVER_USER_KEY")
    return token, user


def _build_payload(
    *,
    token: str,
    user: str,
    title: str,
    message: str,
    priority: int = 0,
    sound: str | None = None,
    url: str | None = None,
    url_title: str | None = None,
    device: str | None = None,
    html: bool = False,
    retry: int | None = None,
    expire: int | None = None,
    timestamp: int | None = None,
) -> dict[str, str | int]:
    priority = max(-2, min(2, int(priority)))
    payload: dict[str, str | int] = {
        "token": token,
        "user": user,
        "title": _clip(title, 250),
        "message": _clip(message, 1024),
        "priority": priority,
    }
    if sound:
        payload["sound"] = sound
    if url:
        payload["url"] = url
    if url_title:
        payload["url_title"] = _clip(url_title, 100)
    if device:
        payload["device"] = device
    if html:
        payload["html"] = 1
    if timestamp is not None:
        payload["timestamp"] = int(timestamp)
    if priority >= 2:
        payload["retry"] = max(30, int(retry or 60))
        payload["expire"] = max(payload["retry"], int(expire or 1800))
    return payload


def _send_sync(payload: dict[str, str | int], *, timeout: int = 10) -> PushoverResult:
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(PUSHOVER_API_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode(errors="replace")
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"raw": raw[:500]}
            if 200 <= resp.status < 300:
                receipt = parsed.get("receipt")
                detail = f"pushover sent{f' receipt={receipt}' if receipt else ''}"
                return PushoverResult(True, detail, parsed)
            return PushoverResult(False, f"pushover HTTP {resp.status}: {raw[:300]}", parsed)
    except Exception as exc:
        return PushoverResult(False, f"pushover error: {exc}")


async def send_pushover(
    *,
    title: str,
    message: str,
    priority: int = 0,
    sound: str | None = None,
    url: str | None = None,
    url_title: str | None = None,
    device: str | None = None,
    html: bool = False,
    retry: int | None = None,
    expire: int | None = None,
    timestamp: int | None = None,
    timeout: int = 10,
) -> PushoverResult:
    """Send a Pushover notification without blocking the event loop."""
    try:
        from core.voice_reroute import voice_reroute_send
        if voice_reroute_send(title, message, priority, url, url_title):
            return PushoverResult(True, "rerouted to voice (claude:9)")
    except Exception:
        pass
    token, user = _credentials()
    if not token or not user:
        return PushoverResult(False, "pushover credentials unavailable")
    payload = _build_payload(
        token=token,
        user=user,
        title=title,
        message=message,
        priority=priority,
        sound=sound,
        url=url,
        url_title=url_title,
        device=device,
        html=html,
        retry=retry,
        expire=expire,
        timestamp=timestamp,
    )
    return await asyncio.to_thread(_send_sync, payload, timeout=timeout)
