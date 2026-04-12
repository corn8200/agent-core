"""agent-cp client — shared between Mac and VPS.

Usage:
    import agent_cp_client as cp
    cp.event("handler-agent", "start")
    cp.event("handler-agent", "complete", payload={"count": 3}, cost=0.012)
    if cp.is_killed("handler-agent"):
        sys.exit(0)

Silent fail by design — never crash the caller.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.request
from pathlib import Path

VPS_URL = "http://100.118.21.64:8767"
LOCAL_DB = Path("/srv/apps/agent-cp/events.db")
LOCAL_FLAGS = Path("/srv/apps/agent-cp/flags")
SECRETS = Path.home() / ".config" / "secrets.env"

_TOKEN_CACHE: str | None = None
_KILL_CACHE: dict[str, tuple[float, bool]] = {}
_KILL_TTL = 60.0


def _token() -> str:
    global _TOKEN_CACHE
    if _TOKEN_CACHE is not None:
        return _TOKEN_CACHE
    tok = os.environ.get("APPLE_BRIDGE_TOKEN", "")
    if not tok and SECRETS.exists():
        try:
            for line in SECRETS.read_text().splitlines():
                line = line.strip()
                if line.startswith("APPLE_BRIDGE_TOKEN="):
                    tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except Exception:
            pass
    _TOKEN_CACHE = tok
    return tok


def _on_vps() -> bool:
    return LOCAL_DB.parent.exists()


def _detect_host() -> str:
    if _on_vps():
        return "vps"
    try:
        hn = socket.gethostname().lower()
    except Exception:
        hn = ""
    if "mini" in hn or "macbook" in hn or "mac" in hn or sys.platform == "darwin":
        return "mac"
    if "pi" in hn or "raspberry" in hn:
        return "pi"
    return hn or "unknown"


def _log_stderr(msg: str) -> None:
    try:
        sys.stderr.write(f"[agent_cp_client] {msg}\n")
    except Exception:
        pass


def _insert_local(host: str, agent: str, kind: str, payload, cost) -> int | None:
    try:
        import sqlite3
        conn = sqlite3.connect(str(LOCAL_DB))
        try:
            pl = json.dumps(payload) if payload is not None else None
            cur = conn.execute(
                "INSERT INTO events (host, agent, kind, payload, cost_usd) VALUES (?,?,?,?,?)",
                (host, agent, kind, pl, cost),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception as e:
        _log_stderr(f"local insert failed: {e}")
        return None


def _post_remote(host: str, agent: str, kind: str, payload, cost) -> int | None:
    try:
        body = json.dumps({
            "host": host, "agent": agent, "kind": kind,
            "payload": payload, "cost_usd": cost,
        }).encode()
        req = urllib.request.Request(
            f"{VPS_URL}/ingest",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_token()}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            return data.get("id")
    except Exception as e:
        _log_stderr(f"remote post failed: {e}")
        return None


def event(agent: str, kind: str, payload=None, cost=None, host: str | None = None) -> int | None:
    h = host or _detect_host()
    try:
        if _on_vps():
            return _insert_local(h, agent, kind, payload, cost)
        return _post_remote(h, agent, kind, payload, cost)
    except Exception as e:
        _log_stderr(f"event failed: {e}")
        return None


def is_killed(agent: str) -> bool:
    now = time.time()
    if _on_vps():
        return (LOCAL_FLAGS / f"kill:{agent}").exists()
    cached = _KILL_CACHE.get(agent)
    if cached and (now - cached[0]) < _KILL_TTL:
        return cached[1]
    killed = False
    try:
        req = urllib.request.Request(
            f"{VPS_URL}/is_killed/{agent}",
            headers={"Authorization": f"Bearer {_token()}"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            killed = bool(json.loads(resp.read().decode()).get("killed"))
    except Exception as e:
        _log_stderr(f"is_killed check failed: {e}")
    _KILL_CACHE[agent] = (now, killed)
    return killed
