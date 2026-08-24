"""Retired VPS service client compatibility wrappers."""

import os
from functools import lru_cache
from pathlib import Path

import httpx

from core.retired_services import is_retired_route, retired_result

BASE_URL = os.environ.get("AGENT_CORE_VPS_BASE_URL", "").strip()

TOKEN_KEYS = [
    "SANDBOX_AUTH_TOKEN",
    "GATEWAY_AUTH_TOKEN",
    "NOTIFY_AUTH_TOKEN",
    "FILECONV_AUTH_TOKEN",
    "EXECUTOR_AUTH_TOKEN",
]


@lru_cache(maxsize=1)
def _load_tokens() -> dict[str, str]:
    return {k: os.environ.get(k, "") for k in TOKEN_KEYS}


def _token(name: str) -> str:
    return _load_tokens().get(name, "")


def _auth(token_name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(token_name)}"}


async def _request(method: str, path: str, token_name: str,
                   timeout: float = 30, **kwargs) -> dict:
    if not BASE_URL or is_retired_route(BASE_URL):
        return retired_result("vps-service-client", status_code=410)
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout) as client:
            resp = await client.request(method, path, headers=_auth(token_name), **kwargs)
            if resp.status_code >= 400:
                return {"error": resp.text, "status_code": resp.status_code}
            return resp.json()
    except httpx.HTTPError as e:
        return {"error": str(e), "status_code": None}
    except Exception as e:
        return {"error": str(e), "status_code": None}


# --- Sandbox ---

async def sandbox_run(commands: list[str], timeout: int = 30) -> dict:
    return await _request("POST", "/sandbox/run", "SANDBOX_AUTH_TOKEN",
                          timeout=60, json={"commands": commands, "timeout": timeout})


async def sandbox_session_create(timeout_minutes: int = 60) -> str:
    result = await _request("POST", "/sandbox/session", "SANDBOX_AUTH_TOKEN",
                            json={"timeout_minutes": timeout_minutes})
    return result.get("session_id", result.get("error", ""))


async def sandbox_session_exec(session_id: str, command: str, timeout: int = 30) -> dict:
    return await _request("POST", f"/sandbox/session/{session_id}/exec", "SANDBOX_AUTH_TOKEN",
                          json={"command": command, "timeout": timeout})


async def sandbox_session_delete(session_id: str) -> dict:
    return await _request("DELETE", f"/sandbox/session/{session_id}", "SANDBOX_AUTH_TOKEN")


# --- Gateway ---

async def gateway_scrape(url: str, selectors: list[str] | None = None, mode: str = "text") -> dict:
    payload: dict = {"url": url, "mode": mode}
    if selectors:
        payload["selectors"] = selectors
    return await _request("POST", "/gateway/scrape", "GATEWAY_AUTH_TOKEN",
                          timeout=60, json=payload)


async def gateway_proxy(method: str, url: str,
                        headers: dict | None = None, body: str | None = None) -> dict:
    payload: dict = {"method": method, "url": url}
    if headers:
        payload["headers"] = headers
    if body:
        payload["body"] = body
    return await _request("POST", "/gateway/proxy", "GATEWAY_AUTH_TOKEN", json=payload)


# --- Notify ---

async def notify_send(title: str, message: str, priority: str = "normal",
                      source: str = "claude-code", channel: str = "auto") -> dict:
    return await _request("POST", "/notify/send", "NOTIFY_AUTH_TOKEN",
                          json={"title": title, "message": message,
                                "priority": priority, "source": source, "channel": channel})


# --- Fileconv ---

async def fileconv_convert(file_path: str, operation: str,
                           params: dict | None = None) -> dict:
    if not BASE_URL or is_retired_route(BASE_URL):
        return retired_result("vps-fileconv", status_code=410)
    p = Path(file_path)
    if not p.exists():
        return {"error": f"File not found: {file_path}", "status_code": None}
    data = {"operation": operation}
    if params:
        data["params"] = str(params)
    files = {"file": (p.name, p.read_bytes())}
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=60) as client:
            resp = await client.post("/fileconv/convert",
                                     headers=_auth("FILECONV_AUTH_TOKEN"),
                                     data=data, files=files)
            if resp.status_code >= 400:
                return {"error": resp.text, "status_code": resp.status_code}
            return resp.json()
    except httpx.HTTPError as e:
        return {"error": str(e), "status_code": None}
    except Exception as e:
        return {"error": str(e), "status_code": None}


async def fileconv_status(job_id: str) -> dict:
    return await _request("GET", f"/fileconv/status/{job_id}", "FILECONV_AUTH_TOKEN")


# --- Executor ---

async def executor_submit(job_type: str, title: str,
                          params: dict | None = None, timeout_minutes: int = 120) -> dict:
    payload: dict = {"job_type": job_type, "title": title, "timeout_minutes": timeout_minutes}
    if params:
        payload["params"] = params
    return await _request("POST", "/executor/submit", "EXECUTOR_AUTH_TOKEN", json=payload)


async def executor_status(job_id: str) -> dict:
    return await _request("GET", f"/executor/status/{job_id}", "EXECUTOR_AUTH_TOKEN")


async def executor_cancel(job_id: str) -> dict:
    return await _request("POST", f"/executor/cancel/{job_id}", "EXECUTOR_AUTH_TOKEN")
