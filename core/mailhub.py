"""Mailhub send helpers for Mac-local agent callers.

This mirrors the canonical VPS helper at ``/srv/apps/lib/mailhub.py`` so
Mac-side callers can use the same auth resolution chain and send/reply API
without shelling out over SSH.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE_URL = os.environ.get("MAILHUB_URL", "http://127.0.0.1:8770")
DEFAULT_TIMEOUT = 15.0

log = logging.getLogger("mailhub")

# Auth resolution chain — first non-empty source wins. Cached per-process so
# repeated send/reply calls do not keep re-walking the filesystem or invoking
# the `op` CLI.
_TOKEN_ENV_FILES: tuple[Path, ...] = (
    Path.home() / ".config/secrets.env",
    Path("/etc/cp-api.env"),
    Path("/srv/apps/mailhub/config/mailhub.env"),
)
_OP_REF = "op://MachineAutoBiz/MAILHUB_TOKEN/password"
_OP_TIMEOUT_S = 5.0

_token_cache: tuple[str | None, str] | None = None


class MailhubError(RuntimeError):
    """Mailhub returned a non-2xx response."""

    def __init__(self, status_code: int, detail: str, body: Any = None):
        super().__init__(f"mailhub HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.body = body


class MailhubAuthError(MailhubError):
    """No mailhub token could be resolved from any source in the chain."""

    def __init__(self, sources_tried: list[str]):
        chain = " -> ".join(sources_tried)
        super().__init__(
            401,
            (
                "no mailhub token resolved; tried (in order): "
                f"{chain}. Set MAILHUB_TOKEN env, write to "
                "~/.config/secrets.env, /etc/cp-api.env, or "
                "/srv/apps/mailhub/config/mailhub.env, or store as "
                f"{_OP_REF}."
            ),
            body=None,
        )
        self.sources_tried = sources_tried


def _resolve_sender_app(sender_app: str | None) -> str:
    sa = sender_app or os.environ.get("MAILHUB_SENDER_APP")
    if not sa:
        raise ValueError(
            "sender_app required (pass explicitly or set MAILHUB_SENDER_APP)"
        )
    return sa


def _read_token_from_env_file(path: Path) -> str | None:
    """Read MAILHUB_TOKEN from a KEY=value env file."""

    try:
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if not line.startswith("MAILHUB_TOKEN="):
                continue
            value = line.split("=", 1)[1].strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            return value or None
    except (OSError, UnicodeDecodeError):
        return None
    return None


def _read_token_from_1password() -> str | None:
    """Read MAILHUB_TOKEN via the 1Password CLI as a last resort."""

    env = dict(os.environ)
    if "OP_SERVICE_ACCOUNT_TOKEN" not in env:
        for tok_path in (
            Path.home() / ".config/op-service-account-token",
            Path("/etc/op-service-account-token"),
        ):
            try:
                if tok_path.exists():
                    env["OP_SERVICE_ACCOUNT_TOKEN"] = tok_path.read_text().strip()
                    break
            except OSError:
                continue
    if "OP_SERVICE_ACCOUNT_TOKEN" not in env:
        return None
    try:
        result = subprocess.run(
            ["op", "read", _OP_REF],
            env=env,
            capture_output=True,
            text=True,
            timeout=_OP_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def _resolve_token(force_refresh: bool = False) -> str | None:
    """Walk the auth chain and cache the first non-empty token found."""

    global _token_cache
    if not force_refresh and _token_cache is not None:
        return _token_cache[0]

    sources_tried: list[str] = []

    env_value = os.environ.get("MAILHUB_TOKEN") or ""
    sources_tried.append("env:MAILHUB_TOKEN")
    if env_value.strip():
        log.debug("mailhub token resolved from env:MAILHUB_TOKEN")
        _token_cache = (env_value.strip(), "env:MAILHUB_TOKEN")
        return _token_cache[0]

    for path in _TOKEN_ENV_FILES:
        sources_tried.append(f"file:{path}")
        if not path.exists():
            continue
        value = _read_token_from_env_file(path)
        if value:
            log.debug("mailhub token resolved from file:%s", path)
            _token_cache = (value, f"file:{path}")
            return _token_cache[0]

    sources_tried.append(f"1password:{_OP_REF}")
    op_value = _read_token_from_1password()
    if op_value:
        log.debug("mailhub token resolved from %s", _OP_REF)
        _token_cache = (op_value, f"1password:{_OP_REF}")
        return _token_cache[0]

    log.debug(
        "mailhub token not found in any source (tried: %s)",
        ", ".join(sources_tried),
    )
    _token_cache = (None, "|".join(sources_tried))
    return None


def _resolve_token_or_raise() -> str:
    """Resolve MAILHUB_TOKEN or raise a pre-flight auth error."""

    token = _resolve_token()
    if token:
        return token
    sources = (
        ["env:MAILHUB_TOKEN"]
        + [f"file:{p}" for p in _TOKEN_ENV_FILES]
        + [f"1password:{_OP_REF}"]
    )
    raise MailhubAuthError(sources)


def _headers(token: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Mailhub-Token"] = token
    return headers


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        body = response.json()
        detail = body.get("detail") if isinstance(body, dict) else str(body)
    except Exception:
        body = response.text
        detail = response.text
    raise MailhubError(response.status_code, str(detail), body)


def _normalize_attachments(
    attachments: list[Any] | None,
) -> list[dict[str, Any]] | None:
    if not attachments:
        return None
    out: list[dict[str, Any]] = []
    for attachment in attachments:
        if hasattr(attachment, "model_dump"):
            data = attachment.model_dump()
        elif isinstance(attachment, dict):
            data = dict(attachment)
        else:
            raise TypeError(
                "attachment must be dict or pydantic model, got "
                f"{type(attachment).__name__}"
            )
        if "filename" not in data or "content_b64" not in data:
            raise ValueError(
                "attachment requires 'filename' and 'content_b64' keys"
            )
        data.setdefault("mime_type", "application/octet-stream")
        data.setdefault("cid", None)
        out.append(
            {
                "filename": data["filename"],
                "content_b64": data["content_b64"],
                "mime_type": data["mime_type"],
                "cid": data["cid"],
            }
        )
    return out


def _build_send_payload(
    *,
    to: str,
    subject: str,
    sender_app: str,
    body: str | None,
    html: str | None,
    is_html: bool,
    from_addr: str | None,
    cc: str | None,
    bcc: str | None,
    category: str | None,
    scheduled_at: str | None,
    in_reply_to: str | None,
    priority: int | None,
    approval_required: bool | None,
    attachments: list[Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sender_app": sender_app,
        "to": to,
        "subject": subject,
        "is_html": bool(is_html),
    }
    if body is not None:
        payload["body"] = body
    if html is not None:
        payload["html"] = html
        payload["is_html"] = True
    if from_addr:
        payload["from"] = from_addr
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    if category:
        payload["category"] = category
    if scheduled_at:
        if "T" in scheduled_at:
            raise ValueError(
                "scheduled_at must be space-separated UTC "
                "('YYYY-MM-DD HH:MM:SS'), not ISO-T."
            )
        payload["scheduled_at"] = scheduled_at
    if in_reply_to:
        payload["in_reply_to"] = in_reply_to
    if priority is not None:
        payload["priority"] = int(priority)
    if approval_required is not None:
        payload["approval_required"] = bool(approval_required)
    normalized_attachments = _normalize_attachments(attachments)
    if normalized_attachments:
        payload["attachments"] = normalized_attachments
    return payload


async def send_email_async(
    *,
    to: str,
    subject: str,
    sender_app: str | None = None,
    body: str | None = None,
    html: str | None = None,
    is_html: bool = False,
    from_addr: str | None = None,
    cc: str | None = None,
    bcc: str | None = None,
    category: str | None = None,
    scheduled_at: str | None = None,
    in_reply_to: str | None = None,
    priority: int | None = None,
    approval_required: bool | None = None,
    attachments: list[Any] | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    sa = _resolve_sender_app(sender_app)
    payload = _build_send_payload(
        to=to,
        subject=subject,
        sender_app=sa,
        body=body,
        html=html,
        is_html=is_html,
        from_addr=from_addr,
        cc=cc,
        bcc=bcc,
        category=category,
        scheduled_at=scheduled_at,
        in_reply_to=in_reply_to,
        priority=priority,
        approval_required=approval_required,
        attachments=attachments,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/mail/send",
            json=payload,
            headers=_headers(_resolve_token_or_raise()),
        )
    _raise_for_status(response)
    return response.json()


async def reply_email_async(
    *,
    in_reply_to_inbound_id: int,
    body: str,
    sender_app: str | None = None,
    is_html: bool = False,
    category: str = "fyi",
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    sa = _resolve_sender_app(sender_app)
    payload = {
        "sender_app": sa,
        "in_reply_to_inbound_id": int(in_reply_to_inbound_id),
        "body": body,
        "is_html": bool(is_html),
        "category": category,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/mail/reply",
            json=payload,
            headers=_headers(_resolve_token_or_raise()),
        )
    _raise_for_status(response)
    return response.json()


def send_email(
    *,
    to: str,
    subject: str,
    sender_app: str | None = None,
    body: str | None = None,
    html: str | None = None,
    is_html: bool = False,
    from_addr: str | None = None,
    cc: str | None = None,
    bcc: str | None = None,
    category: str | None = None,
    scheduled_at: str | None = None,
    in_reply_to: str | None = None,
    priority: int | None = None,
    approval_required: bool | None = None,
    attachments: list[Any] | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    sa = _resolve_sender_app(sender_app)
    payload = _build_send_payload(
        to=to,
        subject=subject,
        sender_app=sa,
        body=body,
        html=html,
        is_html=is_html,
        from_addr=from_addr,
        cc=cc,
        bcc=bcc,
        category=category,
        scheduled_at=scheduled_at,
        in_reply_to=in_reply_to,
        priority=priority,
        approval_required=approval_required,
        attachments=attachments,
    )
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            f"{base_url.rstrip('/')}/mail/send",
            json=payload,
            headers=_headers(_resolve_token_or_raise()),
        )
    _raise_for_status(response)
    return response.json()


def reply_email(
    *,
    in_reply_to_inbound_id: int,
    body: str,
    sender_app: str | None = None,
    is_html: bool = False,
    category: str = "fyi",
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    sa = _resolve_sender_app(sender_app)
    payload = {
        "sender_app": sa,
        "in_reply_to_inbound_id": int(in_reply_to_inbound_id),
        "body": body,
        "is_html": bool(is_html),
        "category": category,
    }
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            f"{base_url.rstrip('/')}/mail/reply",
            json=payload,
            headers=_headers(_resolve_token_or_raise()),
        )
    _raise_for_status(response)
    return response.json()


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "MailhubError",
    "MailhubAuthError",
    "send_email",
    "send_email_async",
    "reply_email",
    "reply_email_async",
]
