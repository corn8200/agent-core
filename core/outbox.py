"""Universal Outbox — preview/approve gate for third-party outbound.

Every message an agent composes for someone who is NOT John must preview
to John via iMessage with [APPROVE:<uuid>] / [DENY:<uuid>] tokens and wait
for explicit approval before the real send executes. Self-recipients
(John's own iCloud/cell) bypass the gate and send directly.

Enforced by the existing send_* functions in core/tools.py which route
through queue_or_send() unless called with _approved=True.

Promotion path: message_router recognizes APPROVE:<uuid> / DENY:<uuid>
prefixes, re-imports the original send function, and calls it with
_approved=True so this module is bypassed on the second pass.

Storage: ~/logs/outbox/{pending,sent,denied}/<uuid>.json
"""

from __future__ import annotations

import importlib
import json
import re
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal


OUTBOX_ROOT = Path.home() / "logs" / "outbox"
PENDING_DIR = OUTBOX_ROOT / "pending"
SENT_DIR = OUTBOX_ROOT / "sent"
DENIED_DIR = OUTBOX_ROOT / "denied"

Channel = Literal["imessage", "email_personal", "email_business", "email_raw"]

# John's self-identifiers. Anything matching these bypasses preview.
# Normalized form is lowercase with +, -, space, parentheses, dots stripped.
ALLOWED_SELF = {
    "corn82@icloud.com",
    "corn82@gmail.com",
    "13042684985",
    "3042684985",
    # Business GV — biz inbound only but John owns it, so self-send is fine.
    "13018342430",
    "3018342430",
}

# Preview target — where the approval prompt gets sent.
PREVIEW_RECIPIENT = "corn82@icloud.com"

_NORMALIZE_RE = re.compile(r"[\s\-\+\(\)\.]")


def _normalize(recipient: str) -> str:
    """Lowercase, strip +/-/spaces/parens/dots."""
    if not recipient:
        return ""
    return _NORMALIZE_RE.sub("", recipient.strip().lower())


_ALLOWED_SELF_NORMALIZED = {_normalize(s) for s in ALLOWED_SELF}


def _is_self(recipient: str) -> bool:
    """True if recipient is John himself (bypass preview)."""
    norm = _normalize(recipient)
    if not norm:
        return False
    return norm in _ALLOWED_SELF_NORMALIZED


def _ensure_dirs() -> None:
    for d in (PENDING_DIR, SENT_DIR, DENIED_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _preview_text(channel: Channel, recipient: str, subject: str | None,
                  body: str, source: str, uid: str) -> str:
    """Format the preview iMessage John will see."""
    head = f"[OUTBOX] {channel} -> {recipient}"
    if source:
        head += f" (from {source})"
    parts = [head]
    if subject:
        parts.append(f"subj: {subject}")
    preview_body = (body or "").strip()
    # Keep under iMessage friendly budget — full body is in the JSON record.
    if len(preview_body) > 800:
        preview_body = preview_body[:800] + "\n[truncated — full body in pending record]"
    parts.append("")
    parts.append(preview_body)
    parts.append("")
    parts.append(f"Reply APPROVE:{uid} or DENY:{uid}")
    return "\n".join(parts)


async def _send_preview_imessage(text: str) -> None:
    """Send the preview to John. Imports locally to avoid circular imports."""
    from core.tools import send_imessage_reliable
    # PREVIEW_RECIPIENT is self -> the outbox gate recognizes it and sends
    # directly. _approved=True is redundant defense in case ALLOWED_SELF
    # is ever narrowed.
    await send_imessage_reliable(PREVIEW_RECIPIENT, text, _approved=True)


def _write_pending(record: dict) -> Path:
    _ensure_dirs()
    path = PENDING_DIR / f"{record['uuid']}.json"
    path.write_text(json.dumps(record, indent=2, default=str))
    return path


def _read_pending(uid: str) -> dict | None:
    path = PENDING_DIR / f"{uid}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _move_pending(uid: str, dest_dir: Path, decision: str) -> Path | None:
    """Move pending record to sent/ or denied/ with decision timestamp."""
    _ensure_dirs()
    src = PENDING_DIR / f"{uid}.json"
    if not src.exists():
        return None
    try:
        record = json.loads(src.read_text())
    except Exception:
        record = {"uuid": uid, "corrupt": True}
    record["decision"] = decision
    record["decided_at"] = datetime.now(timezone.utc).isoformat()
    dest = dest_dir / f"{uid}.json"
    dest.write_text(json.dumps(record, indent=2, default=str))
    src.unlink(missing_ok=True)
    return dest


async def queue_or_send(
    channel: Channel,
    recipient: str,
    subject: str | None,
    body: str,
    source: str,
    send_fn: Callable[..., Awaitable[Any]],
    send_fn_module: str,
    send_fn_name: str,
    send_kwargs: dict,
) -> dict:
    """Core gate. If recipient is John, send directly. Else queue for APPROVE.

    Returns:
        {"status": "sent_direct", "result": <send_fn return value>} when self
        {"status": "queued", "uuid": "<hex>"} when third-party
    """
    if _is_self(recipient):
        result = await send_fn(**send_kwargs)
        return {"status": "sent_direct", "result": result}

    uid = _uuid.uuid4().hex[:12]
    record = {
        "uuid": uid,
        "created": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "channel": channel,
        "recipient": recipient,
        "subject": subject,
        "body": body,
        "send_fn_module": send_fn_module,
        "send_fn_name": send_fn_name,
        "send_kwargs": send_kwargs,
    }
    _write_pending(record)
    preview = _preview_text(channel, recipient, subject, body, source, uid)
    try:
        await _send_preview_imessage(preview)
    except Exception as e:
        # Keep the pending record even if preview delivery failed — John can
        # still find it on disk. Log and return queued status.
        print(f"[outbox] preview delivery failed for {uid}: {e}", flush=True)
    return {"status": "queued", "uuid": uid}


async def promote(uid: str) -> dict:
    """Execute APPROVE:<uid>. Re-imports send_fn and calls with _approved=True.

    Returns:
        {"status": "sent"|"missing"|"error", "uuid": uid, ...}
    """
    record = _read_pending(uid)
    if not record:
        return {"status": "missing", "uuid": uid}

    try:
        mod = importlib.import_module(record["send_fn_module"])
        fn = getattr(mod, record["send_fn_name"])
        # claude-agent-sdk @tool decorator wraps async fns in SdkMcpTool
        # NamedTuples. The actual coroutine lives on .handler — unwrap here
        # so promote() can await it regardless of decoration.
        if hasattr(fn, "handler"):
            fn = fn.handler
    except Exception as e:
        return {"status": "error", "uuid": uid, "error": f"import failed: {e}"}

    kwargs = dict(record.get("send_kwargs") or {})
    # MCP @tool handlers take a single `args` dict — inject the approved flag
    # inside it rather than at the top level. Native async fns get it directly.
    if set(kwargs.keys()) == {"args"} and isinstance(kwargs["args"], dict):
        kwargs["args"] = {**kwargs["args"], "_approved": True}
    else:
        kwargs["_approved"] = True
    try:
        result = await fn(**kwargs)
    except Exception as e:
        return {"status": "error", "uuid": uid, "error": f"send failed: {e}"}

    _move_pending(uid, SENT_DIR, "approve")
    return {"status": "sent", "uuid": uid, "result": result}


async def deny(uid: str) -> dict:
    """Execute DENY:<uid>. Moves record to denied/ without sending."""
    record = _read_pending(uid)
    if not record:
        return {"status": "missing", "uuid": uid}
    _move_pending(uid, DENIED_DIR, "deny")
    return {"status": "denied", "uuid": uid}
