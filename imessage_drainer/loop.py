#!/usr/bin/env python3
"""imessage-drainer — KeepAlive LaunchAgent (ThrottleInterval=30).

Drains cp-api cockpit_imessage_outbound queue → apple-bridge POST /messages/send.

Protocol:
  1. GET  cp-api /api/cockpit/imessage/outbound/pending  (claim up to N rows)
  2. For each row:
     - POST apple-bridge /messages/send with Idempotency-Key: cockpit-imsg-{row.id}
     - 200 → POST cp-api /api/cockpit/imessage/outbound/{id}/sent + claim_token
     - non-200 → POST /api/cockpit/imessage/outbound/{id}/failed + reason
  3. Sleep 10s, repeat

DO NOT MODIFY routers/cockpit_imessage.py — that side is already live.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

import httpx
from core.endpoints import get as _ep

try:
    CP_API_BASE = _ep("agent_cp.base_url")
except Exception:
    CP_API_BASE = os.environ.get("AGENT_CP_URL", "")
BRIDGE_BASE = "http://100.122.35.56:8765"
POLL_INTERVAL = 10  # seconds between polls
BATCH_SIZE = 5


def _load_cp_token() -> str:
    for key in ("CP_API_TOKEN", "APPLE_BRIDGE_TOKEN"):
        val = os.environ.get(key, "")
        if val:
            return val
    secrets = Path.home() / ".config" / "secrets.env"
    if secrets.exists():
        for line in secrets.read_text().splitlines():
            for key in ("APPLE_BRIDGE_TOKEN=", "CP_API_TOKEN="):
                if line.startswith(key):
                    return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def _load_bridge_write_token() -> str:
    token = os.environ.get("BRIDGE_WRITE_TOKEN", "")
    if token:
        return token
    secrets = Path.home() / ".config" / "secrets.env"
    if secrets.exists():
        for line in secrets.read_text().splitlines():
            if line.startswith("BRIDGE_WRITE_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def _get_pending(cp_token: str, client: httpx.Client) -> list[dict]:
    try:
        resp = client.get(
            f"{CP_API_BASE}/api/cockpit/imessage/outbound/pending",
            headers={"Authorization": f"Bearer {cp_token}"},
            params={"limit": BATCH_SIZE},
        )
        if resp.status_code == 200:
            return resp.json().get("rows", [])
        print(f"[imessage-drainer] pending returned {resp.status_code}", file=sys.stderr)
        return []
    except Exception as exc:
        print(f"[imessage-drainer] pending fetch error: {exc}", file=sys.stderr)
        return []


def _send_message(row: dict, bridge_token: str, client: httpx.Client) -> tuple[bool, str]:
    try:
        resp = client.post(
            f"{BRIDGE_BASE}/messages/send",
            headers={
                "Authorization": f"Bearer {bridge_token}",
                "Idempotency-Key": f"cockpit-imsg-{row['id']}",
                "Content-Type": "application/json",
            },
            json={
                "to": row["thread_id"],
                "body": row["body"],
            },
            timeout=15.0,
        )
        if resp.status_code == 200:
            return True, ""
        return False, f"{resp.status_code} {resp.text[:200]}"
    except Exception as exc:
        return False, str(exc)[:200]


def _mark_sent(row_id: int, claim_token: str, cp_token: str, client: httpx.Client) -> None:
    try:
        resp = client.post(
            f"{CP_API_BASE}/api/cockpit/imessage/outbound/{row_id}/sent",
            headers={"Authorization": f"Bearer {cp_token}"},
            json={"claim_token": claim_token},
        )
        if resp.status_code not in (200, 204):
            print(f"[imessage-drainer] /sent returned {resp.status_code} for row {row_id}", file=sys.stderr)
    except Exception as exc:
        print(f"[imessage-drainer] /sent error for row {row_id}: {exc}", file=sys.stderr)


def _mark_failed(row_id: int, claim_token: str, error: str, cp_token: str, client: httpx.Client) -> None:
    try:
        resp = client.post(
            f"{CP_API_BASE}/api/cockpit/imessage/outbound/{row_id}/failed",
            headers={"Authorization": f"Bearer {cp_token}"},
            json={"claim_token": claim_token, "error_text": error},
        )
        if resp.status_code not in (200, 204):
            print(f"[imessage-drainer] /failed returned {resp.status_code} for row {row_id}", file=sys.stderr)
    except Exception as exc:
        print(f"[imessage-drainer] /failed error for row {row_id}: {exc}", file=sys.stderr)


def run_loop() -> None:
    cp_token = _load_cp_token()
    bridge_token = _load_bridge_write_token()

    if not cp_token:
        print("[imessage-drainer] CP API token not found", file=sys.stderr)
        sys.exit(1)
    if not bridge_token:
        print("[imessage-drainer] BRIDGE_WRITE_TOKEN not found", file=sys.stderr)
        sys.exit(1)

    print("[imessage-drainer] started", flush=True)

    while True:
        try:
            with httpx.Client(timeout=10.0) as client:
                rows = _get_pending(cp_token, client)
                if rows:
                    print(f"[imessage-drainer] {len(rows)} pending rows", flush=True)

                for row in rows:
                    row_id = row["id"]
                    claim_token = row.get("claim_token", "")
                    sent, err = _send_message(row, bridge_token, client)

                    if sent:
                        _mark_sent(row_id, claim_token, cp_token, client)
                        print(f"[imessage-drainer] sent row {row_id}", flush=True)
                    else:
                        _mark_failed(row_id, claim_token, err, cp_token, client)
                        print(f"[imessage-drainer] failed row {row_id}: {err}", file=sys.stderr)

        except Exception as exc:
            print(f"[imessage-drainer] loop error: {exc}", file=sys.stderr)

        time.sleep(POLL_INTERVAL)


def main() -> None:
    try:
        run_loop()
    except KeyboardInterrupt:
        print("[imessage-drainer] stopped by keyboard interrupt", flush=True)
    except Exception as exc:
        print(f"[imessage-drainer] fatal: {exc}", file=sys.stderr)
        time.sleep(5)
        raise


if __name__ == "__main__":
    main()
