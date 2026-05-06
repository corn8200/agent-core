#!/usr/bin/env python3
"""Drain the cp-api cockpit iMessage outbound queue from the Mac.

The LaunchAgent runs this script every 30 seconds. Each invocation claims a
small batch, sends only those claimed rows through the existing reliable
iMessage transport, then marks each row sent or failed in cp-api.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_LIMIT = 5
DEFAULT_INTERVAL_SECONDS = 30


class OutboundApi(Protocol):
    def claim_pending(self, *, limit: int) -> list[dict[str, Any]]:
        ...

    def mark_sent(self, row_id: int, *, sent_at: str | None = None) -> dict[str, Any]:
        ...

    def mark_failed(self, row_id: int, *, error_text: str) -> dict[str, Any]:
        ...


SendFn = Callable[..., Awaitable[tuple[bool, str]]]


@dataclass(frozen=True)
class ApiClient:
    base_url: str
    token: str
    timeout: float = 10.0

    def __post_init__(self) -> None:
        if not self.token:
            raise RuntimeError("missing cp-api bearer token")

    def _url(self, path: str) -> str:
        return urllib.parse.urljoin(self.base_url.rstrip("/") + "/", path.lstrip("/"))

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url(path),
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    def claim_pending(self, *, limit: int) -> list[dict[str, Any]]:
        body = self._request(
            "GET",
            f"/api/cockpit/imessage/outbound/pending?limit={int(limit)}",
        )
        rows = body.get("rows", [])
        if not isinstance(rows, list):
            raise RuntimeError("cp-api pending response did not contain rows")
        return [dict(row) for row in rows]

    def mark_sent(self, row_id: int, *, sent_at: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if sent_at:
            payload["sent_at"] = sent_at
        return self._request(
            "POST",
            f"/api/cockpit/imessage/outbound/{int(row_id)}/sent",
            payload,
        )

    def mark_failed(self, row_id: int, *, error_text: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/cockpit/imessage/outbound/{int(row_id)}/failed",
            {"error_text": error_text[:2000]},
        )


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _load_token() -> str:
    from core.vault import get_secret, hydrate_env

    hydrate_env(["CP_API_BEARER_TOKEN", "APPLE_BRIDGE_TOKEN"])
    return (
        os.environ.get("CP_API_BEARER_TOKEN")
        or os.environ.get("APPLE_BRIDGE_TOKEN")
        or get_secret("CP_API_BEARER_TOKEN")
        or get_secret("APPLE_BRIDGE_TOKEN")
        or ""
    )


def _cp_api_base_url() -> str:
    if os.environ.get("AGENT_CP_URL"):
        return os.environ["AGENT_CP_URL"]
    from core.endpoints import get

    return get("agent_cp.base_url")


async def _default_send(thread_id: str, body: str) -> tuple[bool, str]:
    from core.tools import send_imessage_reliable

    return await send_imessage_reliable(thread_id, body, _approved=True)


async def poll_once(
    api: OutboundApi,
    *,
    send_fn: SendFn = _default_send,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, int]:
    rows = api.claim_pending(limit=limit)
    stats = {"claimed": len(rows), "sent": 0, "failed": 0}

    for row in rows:
        row_id = int(row["id"])
        thread_id = str(row.get("thread_id") or "").strip()
        body = str(row.get("body") or "")
        if not thread_id or not body:
            api.mark_failed(row_id, error_text="missing thread_id or body")
            stats["failed"] += 1
            continue

        try:
            ok, detail = await send_fn(thread_id, body, _approved=True)
        except TypeError:
            ok, detail = await send_fn(thread_id, body)
        except Exception as exc:  # noqa: BLE001 - report to queue and continue
            ok, detail = False, f"{type(exc).__name__}: {exc}"

        if ok:
            api.mark_sent(row_id, sent_at=_now_iso())
            stats["sent"] += 1
        else:
            api.mark_failed(row_id, error_text=detail or "send failed")
            stats["failed"] += 1

    return stats


async def run_loop(
    api: OutboundApi,
    *,
    send_fn: SendFn = _default_send,
    limit: int = DEFAULT_LIMIT,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
) -> None:
    while True:
        stats = await poll_once(api, send_fn=send_fn, limit=limit)
        print(
            "[cockpit-imessage-sender] "
            f"claimed={stats['claimed']} sent={stats['sent']} failed={stats['failed']}",
            flush=True,
        )
        await asyncio.sleep(interval_seconds)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=_cp_api_base_url())
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--loop", action="store_true", help="poll continuously instead of once")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    return parser.parse_args(argv)


async def async_main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    token = _load_token()
    if not token:
        print("[cockpit-imessage-sender] missing cp-api bearer token", file=sys.stderr)
        return 2
    api = ApiClient(args.base_url, token=token, timeout=args.timeout)
    try:
        if args.loop:
            await run_loop(api, limit=args.limit, interval_seconds=args.interval)
            return 0
        stats = await poll_once(api, limit=args.limit)
    except (urllib.error.URLError, RuntimeError, OSError) as exc:
        print(f"[cockpit-imessage-sender] cp-api error: {exc}", file=sys.stderr)
        return 1
    print(
        "[cockpit-imessage-sender] "
        f"claimed={stats['claimed']} sent={stats['sent']} failed={stats['failed']}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
