"""agent-cp compatibility client.

Usage:
    import agent_cp_client as cp
    cp.event("handler-agent", "start")
    cp.event("handler-agent", "complete", payload={"count": 3}, cost=0.012)
    if cp.is_killed("handler-agent"):
        sys.exit(0)

    @cp.track("my-agent")
    def main(): ...

The old control plane was removed with the VPS. Calls fail closed unless a
replacement URL or local DB path is explicitly configured. Silent fail by
design — never crash the caller.
"""
from __future__ import annotations

import contextlib
import functools
import inspect
import json
import os
import socket
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from pathlib import Path

from core.retired_services import retired_message

CP_URL = os.environ.get("AGENT_CP_URL", "").strip()
LOCAL_DB = (
    Path(os.environ["AGENT_CP_LOCAL_DB"])
    if os.environ.get("AGENT_CP_LOCAL_DB")
    else None
)
LOCAL_FLAGS = (
    Path(os.environ["AGENT_CP_LOCAL_FLAGS"])
    if os.environ.get("AGENT_CP_LOCAL_FLAGS")
    else None
)

_TOKEN_CACHE: str | None = None
_KILL_CACHE: dict[str, tuple[float, bool]] = {}
_KILL_TTL = 60.0


def _token() -> str:
    global _TOKEN_CACHE
    if _TOKEN_CACHE is not None:
        return _TOKEN_CACHE
    _TOKEN_CACHE = os.environ.get("APPLE_BRIDGE_TOKEN", "")
    if not _TOKEN_CACHE:
        # LaunchAgents don't have this in env — read from secrets file
        try:
            secrets = Path.home() / ".config" / "secrets.env.legacy"
            for line in secrets.read_text().splitlines():
                if line.startswith("APPLE_BRIDGE_TOKEN="):
                    _TOKEN_CACHE = line.split("=", 1)[1].strip().strip("'\"")
                    break
        except Exception:
            pass
    return _TOKEN_CACHE


def _on_vps() -> bool:
    return LOCAL_DB is not None and LOCAL_DB.parent.exists()


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


def _insert_local(host, agent, kind, payload, cost, duration_ms, trace_id, error_text) -> int | None:
    if LOCAL_DB is None:
        _log_stderr(retired_message("agent-cp-local-db"))
        return None
    try:
        import sqlite3
        with contextlib.closing(sqlite3.connect(str(LOCAL_DB))) as conn:
            pl = json.dumps(payload) if payload is not None else None
            cur = conn.execute(
                "INSERT INTO events (host, agent, kind, payload, cost_usd, duration_ms, trace_id, error_text)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (host, agent, kind, pl, cost, duration_ms, trace_id, error_text),
            )
            conn.commit()
            return cur.lastrowid
    except Exception as e:
        _log_stderr(f"local insert failed: {e}")
        return None


def _post_remote(host, agent, kind, payload, cost, duration_ms, trace_id, error_text) -> int | None:
    if not CP_URL:
        _log_stderr(retired_message("agent-cp-remote"))
        return None
    try:
        body = json.dumps({
            "host": host, "agent": agent, "kind": kind,
            "payload": payload, "cost_usd": cost,
            "duration_ms": duration_ms, "trace_id": trace_id, "error_text": error_text,
        }).encode()
        req = urllib.request.Request(
            f"{CP_URL}/api/ingest",
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


def event(
    agent: str,
    kind: str,
    payload=None,
    cost=None,
    host: str | None = None,
    duration_ms: int | None = None,
    trace_id: str | None = None,
    error_text: str | None = None,
) -> int | None:
    h = host or _detect_host()
    try:
        if _on_vps():
            return _insert_local(h, agent, kind, payload, cost, duration_ms, trace_id, error_text)
        return _post_remote(h, agent, kind, payload, cost, duration_ms, trace_id, error_text)
    except Exception as e:
        _log_stderr(f"event failed: {e}")
        return None


def event_async(
    agent: str,
    kind: str,
    payload=None,
    cost=None,
    host: str | None = None,
    duration_ms: int | None = None,
    trace_id: str | None = None,
    error_text: str | None = None,
) -> None:
    """Fire-and-forget version of event() — never blocks the caller."""
    t = threading.Thread(
        target=event,
        args=(agent, kind),
        kwargs={
            "payload": payload, "cost": cost, "host": host,
            "duration_ms": duration_ms, "trace_id": trace_id, "error_text": error_text,
        },
        daemon=True,
    )
    t.start()


def is_killed(agent: str) -> bool:
    now = time.time()
    if _on_vps():
        return LOCAL_FLAGS is not None and (LOCAL_FLAGS / f"kill:{agent}").exists()
    cached = _KILL_CACHE.get(agent)
    if cached and (now - cached[0]) < _KILL_TTL:
        return cached[1]
    killed = False
    if not CP_URL:
        _KILL_CACHE[agent] = (now, False)
        return False
    try:
        req = urllib.request.Request(
            f"{CP_URL}/api/agents/{agent}/is-killed",
            headers={"Authorization": f"Bearer {_token()}"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            killed = bool(json.loads(resp.read().decode()).get("killed"))
    except Exception as e:
        _log_stderr(f"is_killed check failed: {e}")
    _KILL_CACHE[agent] = (now, killed)
    return killed


def track(agent: str, capture_cost=None):
    """Decorator that emits start/complete/error events with timing + trace_id.

    @cp.track("my-agent")
    def main(): ...
    """
    def deco(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*args, **kwargs):
                tid = uuid.uuid4().hex[:12]
                t0 = time.monotonic()
                try:
                    event(agent, "start", trace_id=tid)
                except Exception:
                    pass
                try:
                    result = await fn(*args, **kwargs)
                except SystemExit as e:
                    dur = int((time.monotonic() - t0) * 1000)
                    kind = "complete" if (e.code is None or e.code == 0) else "error"
                    try:
                        if kind == "error":
                            event(agent, "error",
                                  payload={"exc": "SystemExit", "msg": str(e.code)[:500]},
                                  duration_ms=dur, trace_id=tid)
                        else:
                            event(agent, "complete", duration_ms=dur, trace_id=tid)
                    except Exception:
                        pass
                    raise
                except BaseException as e:
                    dur = int((time.monotonic() - t0) * 1000)
                    tb = traceback.format_exc()
                    try:
                        event(
                            agent, "error",
                            payload={"exc": type(e).__name__, "msg": str(e)[:500]},
                            duration_ms=dur, trace_id=tid, error_text=tb[:8000],
                        )
                    except Exception:
                        _log_stderr(f"error event emit failed for {agent}")
                    raise
                dur = int((time.monotonic() - t0) * 1000)
                cost = None
                if capture_cost:
                    try:
                        cost = capture_cost(result)
                    except Exception:
                        pass
                try:
                    event(agent, "complete", duration_ms=dur, trace_id=tid, cost=cost)
                except Exception:
                    pass
                return result
            return awrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            tid = uuid.uuid4().hex[:12]
            t0 = time.monotonic()
            try:
                event(agent, "start", trace_id=tid)
            except Exception:
                pass
            try:
                result = fn(*args, **kwargs)
            except SystemExit as e:
                dur = int((time.monotonic() - t0) * 1000)
                kind = "complete" if (e.code is None or e.code == 0) else "error"
                try:
                    if kind == "error":
                        event(agent, "error",
                              payload={"exc": "SystemExit", "msg": str(e.code)[:500]},
                              duration_ms=dur, trace_id=tid)
                    else:
                        event(agent, "complete", duration_ms=dur, trace_id=tid)
                except Exception:
                    pass
                raise
            except BaseException as e:
                dur = int((time.monotonic() - t0) * 1000)
                tb = traceback.format_exc()
                try:
                    event(
                        agent, "error",
                        payload={"exc": type(e).__name__, "msg": str(e)[:500]},
                        duration_ms=dur, trace_id=tid, error_text=tb[:8000],
                    )
                except Exception:
                    _log_stderr(f"error event emit failed for {agent}")
                raise
            dur = int((time.monotonic() - t0) * 1000)
            cost = None
            if capture_cost:
                try:
                    cost = capture_cost(result)
                except Exception:
                    pass
            try:
                event(agent, "complete", duration_ms=dur, trace_id=tid, cost=cost)
            except Exception:
                pass
            return result
        return wrapper
    return deco
