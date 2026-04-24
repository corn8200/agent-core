"""Mailhub Python client library — in-tree, for VPS agents.

For Mac/Air agents, a pip-installable version at agent-core/core/mailhub.py
mirrors this surface. Phase 4 ships the full package; Phase 2 ships this
in-tree module so mailhub-internal tests can exercise the API end-to-end.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx

DEFAULT_BASE_URL = os.environ.get("MAILHUB_URL", "http://127.0.0.1:8770")


class MailhubClient:
    def __init__(
        self,
        sender_app: str,
        base_url: str = DEFAULT_BASE_URL,
        api_token: str | None = None,
        timeout: float = 15.0,
    ):
        self.sender_app = sender_app
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token or os.environ.get(
            f"MAILHUB_TOKEN_{sender_app.upper().replace('-', '_')}"
        )
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_token:
            h["X-Mailhub-Token"] = self.api_token
        return h

    async def send(
        self,
        to: str,
        subject: str,
        body: str | None = None,
        *,
        from_addr: str | None = None,
        html: str | None = None,
        is_html: bool = False,
        category: str | None = None,
        cc: str | None = None,
        scheduled_at: str | None = None,
        in_reply_to: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sender_app": self.sender_app,
            "to": to, "subject": subject,
        }
        if body is not None:        payload["body"] = body
        if from_addr:               payload["from"] = from_addr
        if html:                    payload["html"] = html
        if is_html:                 payload["is_html"] = is_html
        if category:                payload["category"] = category
        if cc:                      payload["cc"] = cc
        if scheduled_at:            payload["scheduled_at"] = scheduled_at
        if in_reply_to:             payload["in_reply_to"] = in_reply_to
        async with httpx.AsyncClient(timeout=self.timeout) as cli:
            r = await cli.post(f"{self.base_url}/mail/send", json=payload, headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def reply(self, *, in_reply_to_inbound_id: int, body: str,
                    category: str = "fyi", is_html: bool = False) -> dict[str, Any]:
        payload = {
            "sender_app": self.sender_app,
            "in_reply_to_inbound_id": in_reply_to_inbound_id,
            "body": body, "category": category, "is_html": is_html,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as cli:
            r = await cli.post(f"{self.base_url}/mail/reply", json=payload,
                               headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def inbox(
        self,
        *,
        account: str | None = None,
        category: str | None = None,
        unread_by_agent: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if account:             params["account"] = account
        if category:            params["category"] = category
        if unread_by_agent:     params["unread_by_agent"] = unread_by_agent
        async with httpx.AsyncClient(timeout=self.timeout) as cli:
            r = await cli.get(f"{self.base_url}/mail/inbox", params=params,
                              headers=self._headers())
            r.raise_for_status()
            return r.json().get("results", [])

    async def mark_read(self, inbound_id: int, agent: str | None = None) -> dict[str, Any]:
        params = {"agent": agent} if agent else {}
        async with httpx.AsyncClient(timeout=self.timeout) as cli:
            r = await cli.post(
                f"{self.base_url}/mail/inbox/{inbound_id}/mark-read",
                params=params, headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()

    async def list_sends(
        self,
        *,
        category: str | None = None,
        from_addr: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if category:   params["category"] = category
        if from_addr:  params["from_addr"] = from_addr
        if status:     params["status"] = status
        async with httpx.AsyncClient(timeout=self.timeout) as cli:
            r = await cli.get(f"{self.base_url}/mail/sends", params=params,
                              headers=self._headers())
            r.raise_for_status()
            return r.json().get("results", [])


# ───── convenience functions (the most common shape) ────────────────────

async def send_mail(
    *,
    to: str,
    subject: str,
    body: str,
    sender_app: str,
    from_addr: str | None = None,
    category: str | None = None,
    html: str | None = None,
    is_html: bool = False,
) -> dict[str, Any]:
    client = MailhubClient(sender_app)
    return await client.send(
        to=to, subject=subject, body=body, from_addr=from_addr,
        category=category, html=html, is_html=is_html,
    )


async def reply_mail(
    *,
    in_reply_to_inbound_id: int,
    body: str,
    sender_app: str,
    category: str = "fyi",
) -> dict[str, Any]:
    client = MailhubClient(sender_app)
    return await client.reply(
        in_reply_to_inbound_id=in_reply_to_inbound_id,
        body=body, category=category,
    )
