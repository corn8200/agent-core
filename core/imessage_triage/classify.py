"""Haiku 4.5 classifier for iMessage threads.

Categorizes a thread snippet into one of 8 categories and assigns an urgency
score 1-10. Uses core.mac_sdk (Max subscription OAuth) — free under flat-monthly
billing, no per-call cost.

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
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.mac_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    SDKQuotaExceeded,
    TextBlock,
    query,
)
from core.claude_usage_guard import ClaudeUsageGateError  # noqa: E402
from core.doctor_escalate import doctor_escalate  # noqa: E402

MODEL_ID = "claude-haiku-4-5-20251001"
CACHE_DIR = Path.home() / ".cache" / "imessage-triage"
CLASSIFY_FAILURE_STATE = CACHE_DIR / "classify-failures.json"
CLASSIFY_FAILURE_THRESHOLD = int(os.environ.get("IMESSAGE_TRIAGE_FAILURE_THRESHOLD", "3"))

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

SYSTEM_PROMPT = """\
You classify iMessage thread snippets. Return only a JSON object with two keys:
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

Return only the JSON object, no commentary."""

_PROMPT_TEMPLATE = """\
Classify this iMessage thread snippet:

  from_handle: {from_handle}
  messages (newest last):
{messages}"""


def _reset_failure_state() -> None:
    try:
        CLASSIFY_FAILURE_STATE.unlink(missing_ok=True)
    except Exception:
        pass


def _record_failure(exc: Exception) -> int:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    try:
        state = json.loads(CLASSIFY_FAILURE_STATE.read_text())
    except Exception:
        state = {}
    try:
        count = int(state.get("count") or 0) + 1
    except Exception:
        count = 1
    next_state = {
        "count": count,
        "first_ts": state.get("first_ts") or now,
        "last_ts": now,
        "last_error": str(exc),
    }
    try:
        CLASSIFY_FAILURE_STATE.write_text(json.dumps(next_state))
    except Exception:
        pass
    return count


async def _call_sdk(prompt: str) -> str:
    options = ClaudeAgentOptions(
        model=MODEL_ID,
        system_prompt=SYSTEM_PROMPT,
        max_turns=1,
        permission_mode="bypassPermissions",
        env={
            "CLAUDE_RUN_MODE": "automation",
            "CLAUDE_RUN_PROFILE": "quick",
            "CLAUDE_RUN_REASON": "imessage_triage classify thread",
        },
    )
    chunks: list[str] = []
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
    return "".join(chunks).strip()


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


async def classify_thread(
    from_handle: str,
    messages: list[str],
) -> dict[str, Any]:
    """Classify a thread snippet via Mac SDK (Max subscription). Returns {category, urgency}.

    Falls back to {category: noise, urgency: 1} on any error.
    """
    snippet = "\n".join(f"  - {m[:400]}" for m in messages[-10:])
    prompt = _PROMPT_TEMPLATE.format(from_handle=from_handle, messages=snippet)
    try:
        raw = await _call_sdk(prompt)
        result = _parse_response(raw)
        _reset_failure_state()
        return result
    except (SDKQuotaExceeded, ClaudeUsageGateError) as exc:
        print(f"[classify] deferred by Claude backpressure: {exc}", flush=True)
        return {
            "category": "noise",
            "urgency": 1,
            "deferred": True,
            "reason": str(exc),
        }
    except Exception as exc:
        fail_count = _record_failure(exc)
        print(
            f"[classify] sdk call failed ({fail_count}/{CLASSIFY_FAILURE_THRESHOLD}): {exc}",
            flush=True,
        )
        if fail_count >= CLASSIFY_FAILURE_THRESHOLD:
            doctor_escalate(
                watcher="imessage-triage",
                severity="warn",
                summary=f"iMessage triage classifier failed: {exc}",
                context={
                    "from_handle": from_handle,
                    "error": str(exc),
                    "consecutive_failures": fail_count,
                },
                fix_hints=[
                    "Check mac_sdk hourly cap (50/hr) at /tmp/mac-sdk-calls.json",
                    "Verify claude CLI is reachable: /opt/homebrew/bin/claude --version",
                ],
                dedup_scope="imessage-triage-classify-fail",
            )
        return {
            "category": "noise",
            "urgency": 1,
            "deferred": True,
            "reason": str(exc),
        }
