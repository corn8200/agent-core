"""Intent classification and dispatch for inbound iMessages.

Layered routing (router-v2):
0. VPS reply tag — `[V:<service>:<ref>] <reply>` -> POST to agent-cp, done.
1. Short-code check — A1/D1/E1 approval-queue replies (zero cost).
2. Prefix match — research:/quick:/compare:/local: (zero cost).
2.5 Active session check — non-shortcode replies on a chat with an active
   agent session resume that session instead of starting a new one.
3. LLM classification — full context injection, intent -> dispatch plan.

Agents can end a turn with `CLARIFY: <question>` to ask a follow-up; the
router captures the question, sends it to the user, and parks the session
as `awaiting_reply`. The user's next message within SESSION_IDLE_TIMEOUT_MIN
resumes that agent via Claude CLI --resume <sdk_session_id>.
"""

import asyncio
import json
import re
import os
import subprocess
import tempfile
import urllib.request
import uuid as _uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.message_reader import InboundMessage, get_recent_messages
from core.message_bus import send_message
from core.message_db import (
    update_inbound_route,
    get_recent_outbound,
    create_session,
    get_active_session,
    touch_session,
    close_session,
    expire_stale_sessions,
)


# --- Constants ---

PREFIXES = ("research:", "quick:", "compare:", "local:")
SELF_CHATS = ("corn82@icloud.com", "+13042684985")
APPROVAL_QUEUE_URL = "http://100.118.21.64:8766/api/respond"

# Task C: VPS reply tag interface.
# Format (fixed by Mac+VPS coordination): "[V:<service>:<ref>] <reply text>"
# service is lowercase letters; ref is short alphanumeric assigned by the service.
VPS_HOST_IP = "100.118.21.64"
VPS_REPLY_BASE_URL = f"http://{VPS_HOST_IP}:8767"
VPS_REPLY_SERVICES = {"sentinel", "mailtriage", "jobagent", "notify"}
VPS_TAG_RE = re.compile(
    r"^\s*\[\s*v\s*:\s*(?P<service>[a-zA-Z]+)\s*:\s*(?P<ref>[A-Za-z0-9_-]+)\s*\]\s*",
    re.IGNORECASE,
)

SHORT_CODE_RE = re.compile(r"^\s*([AD])(\d+)\s*$", re.IGNORECASE)
EDIT_CODE_RE = re.compile(r"^\s*E(\d+)\s+(.+)$", re.IGNORECASE | re.DOTALL)

# Agents can ask a follow-up question by ending their response with this sentinel.
CLARIFY_RE = re.compile(r"^\s*CLARIFY\s*:\s*(.+)$", re.MULTILINE | re.IGNORECASE)

INTENT_TO_AGENT = {
    "research": "scout",
    "infra": "wrench",
    "comms": "dispatch",
    "finance": "ledger",
    "document": "forge",
    "code": "anvil",
    "review": "critic",
    "quick": "turbo",
    "heavy": "titan",
    "schedule": None,      # handled inline by router (calendar query)
    "status": None,        # handled inline (gather + synthesize)
    "reminder": None,      # handled inline (osascript)
    "note": None,          # handled inline (osascript)
}


# --- Layer 0: VPS reply tag -------------------------------------------------

def _load_apple_bridge_token() -> Optional[str]:
    tok = os.environ.get("APPLE_BRIDGE_TOKEN", "") or None
    if tok:
        return tok
    # LaunchAgents don't have env — read secrets file.
    try:
        secrets = Path.home() / ".config" / "secrets.env.legacy"
        for line in secrets.read_text().splitlines():
            if line.startswith("APPLE_BRIDGE_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except Exception:
        pass
    return None


def _post_vps_reply(service: str, ref: str, reply: str, chat_identifier: str,
                    timestamp: str) -> tuple[bool, int, str]:
    """POST a VPS reply. Returns (ok, status_code, body_preview)."""
    token = _load_apple_bridge_token()
    if not token:
        print("[router] APPLE_BRIDGE_TOKEN missing; cannot POST VPS reply")
        return False, 0, "no token"
    url = f"{VPS_REPLY_BASE_URL}/imessage-reply/{service}"
    body = {
        "ref": ref,
        "reply": reply,
        "from": chat_identifier,
        "timestamp": timestamp,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            status = resp.status
            print(f"[router] VPS reply {service}/{ref} -> {status} {resp_body[:200]}")
            return 200 <= status < 300, status, resp_body[:200]
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        print(f"[router] VPS reply {service}/{ref} HTTP {e.code}: {body_text}")
        return False, e.code, body_text
    except Exception as e:
        print(f"[router] VPS reply {service}/{ref} error: {e}")
        return False, 0, str(e)[:200]


async def _handle_vps_reply(msg: InboundMessage) -> Optional[str]:
    """Layer 0: If message opens with [V:<service>:<ref>], forward to VPS.

    Returns the handler name on success. On HTTP failure, returns None so the
    router falls through — we never swallow a user's message.
    """
    m = VPS_TAG_RE.match(msg.text)
    if not m:
        return None
    service = m.group("service").lower()
    ref = m.group("ref")
    if service not in VPS_REPLY_SERVICES:
        print(f"[router] Unknown VPS service '{service}' in tag — falling through")
        return None
    reply_body = msg.text[m.end():].strip()
    ok, status, _ = await asyncio.to_thread(
        _post_vps_reply, service, ref, reply_body,
        msg.chat_identifier, msg.timestamp,
    )
    if not ok:
        # Non-2xx (incl. 404 unknown ref): fall through to normal routing so
        # the message isn't silently eaten if the VPS side is down.
        return None
    handler = f"vps:{service}"
    update_inbound_route(msg.rowid, handler, "vps-tag")
    return handler


# --- Layer 1: Short-codes ---------------------------------------------------

def _post_approval(code: str, action: str, payload: dict | None = None) -> bool:
    token = _load_apple_bridge_token()
    if not token:
        print(f"[router] APPLE_BRIDGE_TOKEN missing; cannot POST {code}")
        return False
    body = {"code": code, "action": action}
    if payload is not None:
        body["payload"] = payload
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        APPROVAL_QUEUE_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            print(f"[router] approval {code} {action} -> {resp.status} {resp_body[:200]}")
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"[router] approval {code} {action} error: {e}")
        return False


async def _handle_short_code(msg: InboundMessage) -> Optional[str]:
    """Check if message is a short-code and handle it. Returns handler name or None."""
    text = msg.text.strip()

    m = SHORT_CODE_RE.match(text)
    if m:
        letter = m.group(1).upper()
        num = m.group(2)
        action = "approve" if letter == "A" else "deny"
        code = f"{letter}{num}"
        print(f"[router] short-code: {code} {action}")
        _post_approval(code, action)
        update_inbound_route(msg.rowid, "approval-queue", "shortcode")
        return "approval-queue"

    m = EDIT_CODE_RE.match(text)
    if m:
        num = m.group(1)
        body = m.group(2).strip()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            print(f"[router] E{num} edit payload not valid JSON: {e}")
            return None
        code = f"E{num}"
        _post_approval(code, "edit", payload)
        update_inbound_route(msg.rowid, "approval-queue", "shortcode")
        return "approval-queue"

    return None


def _looks_like_shortcode(text: str) -> bool:
    """Quick pre-check used by Layer 2.5 to avoid trapping A1/D1 replies."""
    t = text.strip()
    return bool(SHORT_CODE_RE.match(t) or EDIT_CODE_RE.match(t))


# --- Layer 2: Prefixes ------------------------------------------------------

async def _handle_prefix(msg: InboundMessage) -> Optional[str]:
    """Check for research-chain prefixes. Returns handler name or None."""
    text = msg.text.strip().lower()
    for prefix in PREFIXES:
        if text.startswith(prefix):
            question = msg.text[len(prefix):].strip()
            if not question:
                return None

            tag = prefix.rstrip(":")
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            session_name = f"research-{ts}"

            # Write question to temp file to avoid shell quoting issues
            qfile = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, prefix="rc-",
            )
            qfile.write(question)
            qfile.close()

            ORCHESTRATOR = str(Path.home() / "research-chain/orchestrator.py")
            VENV_PYTHON = str(Path.home() / "Projects/agent-core/.venv/bin/python3")
            cmd = f'Q=$(cat {qfile.name}); rm {qfile.name}; {VENV_PYTHON} {ORCHESTRATOR} {tag} "$Q"'
            subprocess.Popen(["tmux", "new-session", "-d", "-s", session_name, cmd])
            print(f"[router] Dispatched prefix: {tag} -> {question[:80]}")

            update_inbound_route(msg.rowid, f"research-chain:{tag}", "prefix")
            return f"research-chain:{tag}"

    return None


# --- Context for LLM classification ----------------------------------------

async def _build_context(msg: InboundMessage) -> dict:
    """Build the rich context packet for LLM classification."""
    now = datetime.now()

    # Parallel context gathering
    thread_task = get_recent_messages(msg.chat_identifier, limit=5)
    email_task = _get_email_context()
    cal_task = _get_calendar_context()
    reminder_task = _get_reminder_context()

    thread, email, calendar, reminders = await asyncio.gather(
        thread_task, email_task, cal_task, reminder_task,
    )

    recent_outbound = get_recent_outbound(limit=3)

    hour = now.hour
    if hour < 12:
        time_of_day = "morning"
    elif hour < 17:
        time_of_day = "afternoon"
    elif hour < 21:
        time_of_day = "evening"
    else:
        time_of_day = "night"

    return {
        "message": msg.text,
        "sender": msg.chat_identifier,
        "timestamp": msg.timestamp,
        "recent_thread": thread,
        "email": email,
        "calendar": calendar,
        "reminders": reminders,
        "recent_agent_results": [
            {"agent": r["agent"], "message": r["message"][:100], "status": r["status"]}
            for r in recent_outbound
        ],
        "current_time": now.isoformat(),
        "day_of_week": now.strftime("%A"),
        "time_of_day": time_of_day,
    }


async def _get_email_context() -> dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "vps",
            "sqlite3 /srv/data/mailtriage.db \""
            "SELECT 'urgent:' || count(*) FROM messages WHERE category LIKE 'urgent%' AND status='new' "
            "UNION ALL "
            "SELECT 'drafts:' || count(*) FROM messages WHERE status='draft_pending' "
            "UNION ALL "
            "SELECT 'recent:' || sender || '|' || subject FROM messages "
            "WHERE received_at > datetime('now','-4 hours') ORDER BY received_at DESC LIMIT 5"
            "\"",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        output = stdout.decode().strip()
        result = {"unread_urgent": 0, "pending_drafts": 0, "recent": []}
        for line in output.split("\n"):
            if line.startswith("urgent:"):
                result["unread_urgent"] = int(line.split(":")[1])
            elif line.startswith("drafts:"):
                result["pending_drafts"] = int(line.split(":")[1])
            elif line.startswith("recent:"):
                parts = line[7:].split("|", 1)
                result["recent"].append({"sender": parts[0], "subject": parts[1] if len(parts) > 1 else ""})
        return result
    except Exception:
        return {"unread_urgent": 0, "pending_drafts": 0, "recent": [], "error": "unavailable"}


async def _get_calendar_context() -> dict:
    try:
        from core.calendar_service import get_schedule_view, get_week_view
        view, week = await asyncio.gather(
            get_schedule_view(),
            get_week_view(),
        )
        return {
            "current_status": view.current_status,
            "next_event": view.next_event.to_dict() if view.next_event else None,
            "minutes_until_next": view.minutes_until_next,
            "events_today_count": len(view.events_today),
            "events_tomorrow_count": len(view.events_tomorrow),
            "free_slots_today": [s.to_dict() for s in view.free_slots_today[:3]],
            "week_summary": week.summary,
            "week_key_events": week.key_events[:5],
        }
    except Exception as e:
        return {"error": str(e)}


async def _get_reminder_context() -> dict:
    try:
        from home_ops.gather import gather_reminders
        reminders = await gather_reminders()
        return {
            "overdue_count": len(reminders.get("overdue", [])),
            "overdue": [r["name"] for r in reminders.get("overdue", [])[:5]],
            "due_today": [r["name"] for r in reminders.get("today", [])[:5]],
            "due_this_week_count": len(reminders.get("this_week", [])),
        }
    except Exception as e:
        return {"error": str(e)}


CLASSIFICATION_PROMPT = """You are the iMessage router for John's agent ecosystem. Classify the user's message and decide how to handle it.

CONTEXT (current state — use this to inform your classification):
{context_json}

INTENTS (pick one or more):
- research: deep web research, market analysis, company intel
- infra: VPS, Mac, Pi, services, Tailscale, Docker
- comms: email drafting, follow-ups, outreach, contact someone
- finance: spending, budget, bills, invoices, money
- document: resume, cover letter, proposal, report writing
- code: write/fix/refactor code
- review: code review, audit, second opinion
- quick: simple lookup, quick answer, fast task
- heavy: complex multi-step problem needing max firepower
- schedule: calendar query, "what's on my calendar", availability
- status: "what's going on", system overview, dashboard
- reminder: create/check reminders
- note: save a note
- lead: business leads, prospects
- multi: multiple intents (decompose into sub-intents)

RESPOND with valid JSON only:
{{
  "intents": ["intent1"],
  "primary_agent": "agent_name or null if handled inline",
  "prompt": "the task to give the agent (rewritten for clarity)",
  "can_answer_directly": true/false,
  "direct_answer": "if can_answer_directly, the answer using context above",
  "sub_intents": []
}}

If the context already contains enough information to answer (e.g. schedule questions when calendar data is in context), set can_answer_directly=true and provide the answer. Only route to an agent when the task requires action beyond what's in the context.

MESSAGE: {message}"""


_FALLBACK_CLASSIFICATION = {"intents": ["quick"], "can_answer_directly": False, "prompt": ""}


def _is_api_error(obj: dict) -> bool:
    if obj.get("is_error"):
        return True
    if obj.get("type") == "error" and "error" in obj:
        return True
    return False


def _validate_classification(obj: dict) -> bool:
    return isinstance(obj.get("intents"), list) and len(obj["intents"]) > 0


async def _classify_with_opus(msg: InboundMessage) -> dict:
    """Classify message intent using LLM with full context injection."""
    context = await _build_context(msg)
    context_json = json.dumps(context, indent=2, default=str)

    recall_block = ""
    try:
        from core.recall import get_context
        recall_block = get_context(msg.text, kind="imessage")
    except Exception:
        recall_block = ""

    prompt = CLASSIFICATION_PROMPT.format(
        context_json=context_json,
        message=msg.text,
    )
    if recall_block:
        prompt = f"{recall_block}\n\n{prompt}"

    fallback = {**_FALLBACK_CLASSIFICATION, "prompt": msg.text}

    try:
        proc = await asyncio.create_subprocess_exec(
            "/opt/homebrew/bin/claude", "-p", prompt,
            "--model", "haiku",
            "--max-turns", "1",
            "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        output = stdout.decode().strip()
        err_output = stderr.decode().strip()

        if proc.returncode != 0:
            print(f"[router] Opus CLI exited {proc.returncode}: {err_output[:300]}", flush=True)
            return fallback

        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', output, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                except json.JSONDecodeError:
                    print(f"[router] Opus returned unparseable output: {output[:300]}", flush=True)
                    return fallback
            else:
                print(f"[router] Opus returned no JSON: {output[:300]}", flush=True)
                return fallback

        if _is_api_error(result):
            err_detail = result.get("error", {})
            err_type = err_detail.get("type", "unknown") if isinstance(err_detail, dict) else str(err_detail)
            err_msg = err_detail.get("message", "") if isinstance(err_detail, dict) else ""
            print(f"[router] Opus API error ({err_type}): {err_msg}", flush=True)
            return fallback

        if "result" in result:
            text = result["result"]
            json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
            if json_match:
                try:
                    parsed = json.loads(json_match.group())
                    if _is_api_error(parsed):
                        print(f"[router] Opus API error in result: {json_match.group()[:300]}", flush=True)
                        return fallback
                    if _validate_classification(parsed):
                        return parsed
                except json.JSONDecodeError:
                    pass
            print(f"[router] Opus result has no valid classification JSON: {text[:300]}", flush=True)
            return fallback

        if _validate_classification(result):
            return result

        print(f"[router] Opus response missing intents: {json.dumps(result)[:300]}", flush=True)
        return fallback

    except asyncio.TimeoutError:
        print("[router] Opus classification timed out (60s)", flush=True)
        return fallback
    except Exception as e:
        print(f"[router] Opus classification error: {e}", flush=True)
        return fallback


# --- Dispatch (with resumable sessions — Task B) ---------------------------

def _resolve_agent_model(agent_name: str) -> str:
    """Resolve an agent name to its Claude model. Defaults to opus."""
    try:
        from core.agents import ALL_AGENTS
        agent_def = ALL_AGENTS.get(agent_name.lower())
        if agent_def and getattr(agent_def, "model", None):
            return agent_def.model
    except Exception:
        pass
    return "opus"


def _new_sdk_session_id() -> str:
    """UUID for Claude CLI --session-id pinning."""
    return str(_uuid.uuid4())


def _build_dispatch_script(
    prompt_path: str,
    agent_name: str,
    model: str,
    chat_identifier: str,
    session_id: str,
    sdk_session_id: str,
    resume: bool,
) -> str:
    """Emit the small Python dispatcher used inside the tmux session.

    The dispatcher:
      1. Reads prompt from temp file and unlinks it.
      2. Runs `claude -p <prompt> --model <m> --session-id <sdk> [--resume <sdk>] --max-turns 10`.
      3. Captures stdout.
      4. If the output ends with `CLARIFY: <question>`, splits and sends just the
         question to the user, marks session awaiting_reply + records last_question.
         Otherwise, sends the full result and closes the session.
    """
    session_flag = f'"--resume", "{sdk_session_id}"' if resume else f'"--session-id", "{sdk_session_id}"'
    return f'''
import asyncio, sys, re, os
sys.path.insert(0, "{Path.home() / 'Projects/agent-core'}")
from pathlib import Path

# Scrub Anthropic env vars so CLI uses Max subscription, not pay-as-you-go API.
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

from core.message_bus import send_message
from core.message_db import touch_session, close_session

CLARIFY_RE = re.compile(r"^\\s*CLARIFY\\s*:\\s*(.+)$", re.MULTILINE | re.IGNORECASE)

prompt = Path("{prompt_path}").read_text()
Path("{prompt_path}").unlink(missing_ok=True)

async def run():
    import subprocess
    proc = subprocess.run(
        ["/opt/homebrew/bin/claude", "-p", prompt,
         "--model", "{model}", {session_flag}, "--max-turns", "10"],
        capture_output=True, text=True, timeout=300,
    )
    result = proc.stdout.strip()
    if not result:
        result = "Agent returned no output."

    # CLARIFY sentinel on the LAST non-empty line?
    question = None
    # Walk lines from the end to find a CLARIFY line at the tail.
    lines = [ln for ln in result.splitlines() if ln.strip()]
    if lines:
        m = re.match(r"\\s*CLARIFY\\s*:\\s*(.+)$", lines[-1], re.IGNORECASE)
        if m:
            question = m.group(1).strip()

    if question:
        # Send just the question; park the session.
        out = f"[{agent_name}] {{question}}"
        if len(out) > 1800:
            out = out[:1800] + "\\n[truncated]"
        await send_message(question, agent="{agent_name}", recipient="{chat_identifier}")
        touch_session("{session_id}", status="awaiting_reply", last_question=question)
    else:
        # Full result. Truncate for iMessage.
        if len(result) > 1800:
            result = result[:1800] + "\\n[truncated]"
        await send_message(result, agent="{agent_name}", recipient="{chat_identifier}")
        close_session("{session_id}")

asyncio.run(run())
'''


async def _spawn_dispatch(
    agent_name: str,
    prompt: str,
    chat_identifier: str,
    session_id: str,
    sdk_session_id: str,
    resume: bool,
) -> None:
    """Write script + prompt to temp files and spawn via tmux."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    session_name = f"agent-{agent_name}-{ts}-{session_id[:6]}"

    pfile = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, prefix=f"{agent_name}-",
    )
    pfile.write(prompt)
    pfile.close()

    model = _resolve_agent_model(agent_name)
    script = _build_dispatch_script(
        prompt_path=pfile.name,
        agent_name=agent_name,
        model=model,
        chat_identifier=chat_identifier,
        session_id=session_id,
        sdk_session_id=sdk_session_id,
        resume=resume,
    )

    script_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, prefix=f"dispatch-{agent_name}-",
    )
    script_file.write(script)
    script_file.close()

    VENV_PYTHON = str(Path.home() / "Projects/agent-core/.venv/bin/python3")
    cmd = f'{VENV_PYTHON} {script_file.name}; rm -f {script_file.name}'
    subprocess.Popen(["tmux", "new-session", "-d", "-s", session_name, cmd])


async def _dispatch_to_agent(
    agent_name: str, prompt: str, chat_identifier: str,
) -> str:
    """Fresh dispatch: creates a session row, spawns the claude CLI with
    --session-id <uuid>. Returns the handler name (agent_name)."""
    # Block concurrent sessions on the same chat — user can only converse
    # with one agent at a time.
    existing = get_active_session(chat_identifier)
    if existing and existing["status"] in ("open", "awaiting_reply"):
        await send_message(
            f"[session locked on {existing['agent_name']}]",
            agent="router",
            recipient=chat_identifier,
        )
        return f"blocked:{existing['agent_name']}"

    sdk_session_id = _new_sdk_session_id()
    session_id = create_session(
        chat_identifier=chat_identifier,
        agent_name=agent_name,
        sdk_session_id=sdk_session_id,
        initial_prompt=prompt,
        status="open",
    )

    await _spawn_dispatch(
        agent_name=agent_name,
        prompt=prompt,
        chat_identifier=chat_identifier,
        session_id=session_id,
        sdk_session_id=sdk_session_id,
        resume=False,
    )
    print(f"[router] Dispatched to {agent_name} (session {session_id[:8]}): {prompt[:80]}")
    return agent_name


async def _resume_session(session: dict, user_reply: str) -> str:
    """Continue an existing session: reuse sdk_session_id + --resume."""
    agent_name = session["agent_name"]
    sdk_session_id = session["sdk_session_id"]
    if not sdk_session_id:
        # Session predates session_id pinning — can't resume. Close and fall through.
        close_session(session["session_id"])
        return ""

    touch_session(session["session_id"], prompt=user_reply, status="open",
                  last_question=None)

    await _spawn_dispatch(
        agent_name=agent_name,
        prompt=user_reply,
        chat_identifier=session["chat_identifier"],
        session_id=session["session_id"],
        sdk_session_id=sdk_session_id,
        resume=True,
    )
    print(f"[router] Resumed {agent_name} (session {session['session_id'][:8]}): {user_reply[:80]}")
    return f"resume:{agent_name}"


async def _handle_direct_answer(classification: dict, msg: InboundMessage) -> str:
    answer = classification.get("direct_answer", "")
    if answer:
        await send_message(answer, agent="router")
        return "router:direct"
    return "router:empty"


# --- Main route function ----------------------------------------------------

async def route(msg: InboundMessage) -> str:
    """Classify and dispatch an inbound message. Returns the handler name for audit."""

    # Layer 0: VPS reply tag (Task C). Must come before anything else so
    # tag-prefixed replies never leak into short-code or session matching.
    result = await _handle_vps_reply(msg)
    if result:
        return result

    # Layer 1: Short-code check (approval queue A1/D1/E1).
    result = await _handle_short_code(msg)
    if result:
        return result

    # Layer 2: Prefix match (research:, quick:, etc.)
    result = await _handle_prefix(msg)
    if result:
        return result

    # Layer 2.5: Active session resume (Task B). If a session is open for
    # this chat and the reply isn't a shortcode/prefix, resume it.
    if not _looks_like_shortcode(msg.text):
        active = get_active_session(msg.chat_identifier)
        if active and active["sdk_session_id"]:
            handler = await _resume_session(active, msg.text)
            if handler:
                update_inbound_route(msg.rowid, handler, "session-resume")
                return handler

    # Layer 3: LLM classification with full context
    print(f"[router] Classifying with LLM: {msg.text[:80]}")
    classification = await _classify_with_opus(msg)
    print(f"[router] Classification: {json.dumps(classification, indent=2)[:500]}")

    intents = classification.get("intents", ["quick"])

    # Direct answer (no agent needed)
    if classification.get("can_answer_directly"):
        handler = await _handle_direct_answer(classification, msg)
        update_inbound_route(msg.rowid, handler, "llm")
        return handler

    # Multi-intent: dispatch each sub-intent (Task B: each gets its own session,
    # but we block on chat-level concurrency — first wins, rest get warned).
    if "multi" in intents:
        sub_intents = classification.get("sub_intents", [])
        handlers = []
        for sub in sub_intents:
            agent = INTENT_TO_AGENT.get(sub.get("intent", ""), "scout")
            if agent:
                h = await _dispatch_to_agent(
                    agent, sub.get("prompt", msg.text), msg.chat_identifier,
                )
                handlers.append(h)
        handler = "multi:" + "+".join(handlers)
        update_inbound_route(msg.rowid, handler, "llm")
        return handler

    # Single intent
    primary = classification.get("primary_agent")
    prompt = classification.get("prompt", msg.text)

    if primary and primary in INTENT_TO_AGENT.values():
        handler = await _dispatch_to_agent(primary, prompt, msg.chat_identifier)
    elif intents[0] in INTENT_TO_AGENT:
        agent = INTENT_TO_AGENT[intents[0]]
        if agent:
            handler = await _dispatch_to_agent(agent, prompt, msg.chat_identifier)
        else:
            handler = await _handle_inline(intents[0], msg, classification)
    else:
        handler = await _dispatch_to_agent("scout", prompt, msg.chat_identifier)

    update_inbound_route(msg.rowid, handler, "llm")
    return handler


async def _handle_inline(intent: str, msg: InboundMessage, classification: dict) -> str:
    """Handle intents that don't need a full agent (schedule, status, reminder, note)."""
    if intent == "schedule":
        direct = classification.get("direct_answer", "")
        if direct:
            await send_message(direct, agent="calendar")
        else:
            try:
                from core.calendar_service import get_schedule_view
                view = await get_schedule_view()
                parts = [view.current_status]
                if view.events_today:
                    parts.append(f"Today: {len(view.events_today)} events")
                    for e in view.events_today[:5]:
                        parts.append(f"  {e.start.strftime('%-I:%M %p')} — {e.summary}")
                if view.free_slots_today:
                    free_strs = [f"{s.start.strftime('%-I:%M')}-{s.end.strftime('%-I:%M %p')}" for s in view.free_slots_today[:3]]
                    parts.append(f"Free: {', '.join(free_strs)}")
                await send_message("\n".join(parts), agent="calendar")
            except Exception as e:
                await send_message(f"Calendar error: {e}", agent="calendar")
        return "calendar:inline"

    elif intent == "status":
        await send_message(
            classification.get("direct_answer", "Checking status..."),
            agent="status",
        )
        return "status:inline"

    elif intent == "reminder":
        await send_message(
            classification.get("direct_answer", "Reminder noted."),
            agent="reminder",
        )
        return "reminder:inline"

    elif intent == "note":
        await send_message(
            classification.get("direct_answer", "Note saved."),
            agent="note",
        )
        return "note:inline"

    return f"{intent}:unhandled"


# --- Daemon lifecycle hook --------------------------------------------------


async def on_daemon_start() -> int:
    """Called by imessage_daemon at startup. Expire any stale sessions."""
    n = expire_stale_sessions()
    if n:
        print(f"[router] Expired {n} stale session(s) on daemon start")
    return n
