"""Haiku 4.5 classifier for iMessage threads.

Categorizes a thread snippet into one of 8 categories and assigns an urgency
score 1-10. Loaded API key via vault.get_secret (explicit, not auto-hydrated
into env — intentional pay-per-token for Haiku).

Categories:
  action_me       -- John needs to do something
  action_them     -- other person needs to act
  action_us       -- mutual action needed
  scheduling      -- appointment / meeting / timing related
  social          -- casual conversation
  status_update   -- informational only
  marketing       -- promo / commercial
  noise           -- no action, nothing relevant
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.vault import get_secret  # noqa: E402
from core.doctor_escalate import doctor_escalate  # noqa: E402

MODEL_ID = "claude-haiku-4-5-20251001"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"

VALID_CATEGORIES = frozenset({
    "action_me",
    "action_them",
    "action_us",
    "scheduling",
    "social",
    "status_update",
    "marketing",
    "noise",
})

_PROMPT_TEMPLATE = """\
Classify this iMessage thread snippet. Return only a JSON object with two keys:
  "category": one of action_me | action_them | action_us | scheduling | social | status_update | marketing | noise
  "urgency": integer 1-10 (1=completely ignorable, 10=needs immediate attention)

Definitions:
  action_me     -- John must reply, follow up, or take action
  action_them   -- other person must act; John can wait
  action_us     -- both sides need to do something
  scheduling    -- contains a time, date, appointment, or meeting logistics
  social        -- greeting, small talk, catching up (no tasks)
  status_update -- purely informational; no one needs to act
  marketing     -- promotional, advertisement, spam-adjacent
  noise         -- muted, empty, or irrelevant

Thread info:
  from_handle: {from_handle}
  messages (newest last):
{messages}

Return only the JSON object, no commentary."""


def _api_key() -> str:
    key = get_secret("ANTHROPIC_API_KEY", vault="MachineAuto") or get_secret(
        "ANTHROPIC_API_KEY", vault="MachineAutoBiz"
    )
    return key or ""


def _call_haiku(prompt: str, *, api_key: str) -> str:
    response = httpx.post(
        ANTHROPIC_MESSAGES_URL,
        headers={
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "x-api-key": api_key,
        },
        json={
            "model": MODEL_ID,
            "max_tokens": 128,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    content = payload.get("content") or []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text") or "")
    return ""


def _parse_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start : end + 1]
    raw = json.loads(cleaned)
    category = str(raw.get("category") or "").strip().lower()
    if category not in VALID_CATEGORIES:
        category = "noise"
    urgency = int(raw.get("urgency") or 3)
    urgency = max(1, min(10, urgency))
    return {"category": category, "urgency": urgency}


def classify_thread(
    from_handle: str,
    messages: list[str],
    *,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Classify a thread snippet. Returns {category, urgency}.

    Falls back to {category: noise, urgency: 1} on any error.
    """
    key = api_key or _api_key()
    if not key:
        return {"category": "noise", "urgency": 1}
    snippet = "\n".join(f"  - {m[:400]}" for m in messages[-10:])
    prompt = _PROMPT_TEMPLATE.format(from_handle=from_handle, messages=snippet)
    try:
        raw = _call_haiku(prompt, api_key=key)
        return _parse_response(raw)
    except Exception as exc:
        print(f"[classify] haiku call failed: {exc}", flush=True)
        doctor_escalate(
            watcher="imessage-triage",
            severity="warn",
            summary=f"iMessage triage classifier failed: {exc}",
            context={"from_handle": from_handle, "error": str(exc)},
            fix_hints=["Check ANTHROPIC_API_KEY in MachineAuto vault",
                       "Verify Haiku API quota at console.anthropic.com"],
            dedup_scope="imessage-triage-classify-fail",
        )
        return {"category": "noise", "urgency": 1}
