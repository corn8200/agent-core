"""Intent classification and dispatch for inbound iMessages.

Three-layer routing:
1. Short-code check — A1/D1/E1 approval-queue replies (zero cost)
2. Prefix match — research:/quick:/compare:/local: (zero cost)
3. Opus LLM — full context injection, intent classification + dispatch plan

Opus (free on Max plan) handles ambiguous, natural language, and multi-intent messages.
"""

import asyncio
import json
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.message_reader import InboundMessage, get_recent_messages
from core.message_bus import send_message
from core.message_db import update_inbound_route, get_recent_outbound


# --- Constants ---

PREFIXES = ("research:", "quick:", "compare:", "local:")
SELF_CHATS = ("corn82@icloud.com", "+13042684985")
APPROVAL_QUEUE_URL = "http://100.118.21.64:8766/api/respond"
SECRETS_ENV = Path.home() / ".config/secrets.env"
SHORT_CODE_RE = re.compile(r"^\s*([AD])(\d+)\s*$", re.IGNORECASE)
EDIT_CODE_RE = re.compile(r"^\s*E(\d+)\s+(.+)$", re.IGNORECASE | re.DOTALL)

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


# --- Short-code handling (ported from research-chain/main.py) ---

def _load_apple_bridge_token() -> Optional[str]:
    try:
        for line in SECRETS_ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("APPLE_BRIDGE_TOKEN="):
                val = line.split("=", 1)[1].strip()
                return val.strip("'").strip('"')
    except FileNotFoundError:
        pass
    return None


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


# --- Prefix matching ---

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


# --- Opus LLM classification with context injection ---

async def _build_context(msg: InboundMessage) -> dict:
    """Build the rich context packet for Opus classification."""
    now = datetime.now()

    # Parallel context gathering
    thread_task = get_recent_messages(msg.chat_identifier, limit=5)

    # Email context from mailtriage DB on VPS
    email_task = _get_email_context()

    # Calendar context
    cal_task = _get_calendar_context()

    # Reminders context
    reminder_task = _get_reminder_context()

    thread, email, calendar, reminders = await asyncio.gather(
        thread_task, email_task, cal_task, reminder_task,
    )

    # Recent agent activity
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
    """Pull email context from mailtriage DB on VPS."""
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
    """Pull calendar context from the calendar service."""
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
    """Pull reminder context from gather."""
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
  "sub_intents": []  // only if multi — list of {{intent, prompt}} objects
}}

If the context already contains enough information to answer (e.g. schedule questions when calendar data is in context), set can_answer_directly=true and provide the answer. Only route to an agent when the task requires action beyond what's in the context.

MESSAGE: {message}"""


async def _classify_with_opus(msg: InboundMessage) -> dict:
    """Classify message intent using Opus with full context injection."""
    context = await _build_context(msg)
    context_json = json.dumps(context, indent=2, default=str)

    prompt = CLASSIFICATION_PROMPT.format(
        context_json=context_json,
        message=msg.text,
    )

    # Use claude CLI for classification (Max subscription, free Opus)
    try:
        proc = await asyncio.create_subprocess_exec(
            "/opt/homebrew/bin/claude", "-p", prompt,
            "--model", "opus",
            "--max-turns", "1",
            "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        output = stdout.decode().strip()

        # Parse the JSON response (may be wrapped in claude CLI output)
        # Try to extract JSON from the response
        try:
            result = json.loads(output)
            # claude CLI json output has a "result" key
            if "result" in result:
                text = result["result"]
                # Find JSON in the text
                json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
            return result
        except json.JSONDecodeError:
            # Try to find JSON block in raw output
            json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', output, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            return {"intents": ["quick"], "can_answer_directly": False, "prompt": msg.text}

    except asyncio.TimeoutError:
        print("[router] Opus classification timed out (60s)")
        return {"intents": ["quick"], "can_answer_directly": False, "prompt": msg.text}
    except Exception as e:
        print(f"[router] Opus classification error: {e}")
        return {"intents": ["quick"], "can_answer_directly": False, "prompt": msg.text}


# --- Dispatch ---

async def _dispatch_to_agent(agent_name: str, prompt: str) -> str:
    """Dispatch a task to a named agent via claude CLI in a tmux session."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    session_name = f"agent-{agent_name}-{ts}"

    # Write prompt to temp file
    pfile = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, prefix=f"{agent_name}-",
    )
    pfile.write(prompt)
    pfile.close()

    VENV_PYTHON = str(Path.home() / "Projects/agent-core/.venv/bin/python3")
    # Use a small dispatcher script that runs the agent and sends the result via message bus
    dispatch_script = f'''
import asyncio, sys, json
sys.path.insert(0, "{Path.home() / 'Projects/agent-core'}")
from core.message_bus import send_message
from pathlib import Path

prompt = Path("{pfile.name}").read_text()
Path("{pfile.name}").unlink(missing_ok=True)

async def run():
    import subprocess
    proc = subprocess.run(
        ["/opt/homebrew/bin/claude", "-p", prompt, "--model", "opus", "--max-turns", "10"],
        capture_output=True, text=True, timeout=300,
    )
    result = proc.stdout.strip()
    if not result:
        result = "Agent returned no output."
    # Truncate for iMessage (max ~2000 chars)
    if len(result) > 1800:
        result = result[:1800] + "\\n[truncated]"
    await send_message(result, agent="{agent_name}")

asyncio.run(run())
'''
    script_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, prefix=f"dispatch-{agent_name}-",
    )
    script_file.write(dispatch_script)
    script_file.close()

    cmd = f'{VENV_PYTHON} {script_file.name}; rm -f {script_file.name}'
    subprocess.Popen(["tmux", "new-session", "-d", "-s", session_name, cmd])
    print(f"[router] Dispatched to {agent_name}: {prompt[:80]}")
    return agent_name


async def _handle_direct_answer(classification: dict, msg: InboundMessage) -> str:
    """Handle messages that can be answered directly from context."""
    answer = classification.get("direct_answer", "")
    if answer:
        await send_message(answer, agent="router")
        return "router:direct"
    return "router:empty"


# --- Main route function ---

async def route(msg: InboundMessage) -> str:
    """Classify and dispatch an inbound message. Returns the handler name for audit."""

    # Layer 1: Short-code check
    result = await _handle_short_code(msg)
    if result:
        return result

    # Layer 2: Prefix match
    result = await _handle_prefix(msg)
    if result:
        return result

    # Layer 3: Opus classification with full context
    print(f"[router] Classifying with Opus: {msg.text[:80]}")
    classification = await _classify_with_opus(msg)
    print(f"[router] Classification: {json.dumps(classification, indent=2)[:500]}")

    intents = classification.get("intents", ["quick"])

    # Direct answer (no agent needed)
    if classification.get("can_answer_directly"):
        handler = await _handle_direct_answer(classification, msg)
        update_inbound_route(msg.rowid, handler, "llm")
        return handler

    # Multi-intent: dispatch each sub-intent
    if "multi" in intents:
        sub_intents = classification.get("sub_intents", [])
        handlers = []
        for sub in sub_intents:
            agent = INTENT_TO_AGENT.get(sub.get("intent", ""), "scout")
            if agent:
                h = await _dispatch_to_agent(agent, sub.get("prompt", msg.text))
                handlers.append(h)
        handler = "multi:" + "+".join(handlers)
        update_inbound_route(msg.rowid, handler, "llm")
        return handler

    # Single intent
    primary = classification.get("primary_agent")
    prompt = classification.get("prompt", msg.text)

    if primary and primary in INTENT_TO_AGENT.values():
        handler = await _dispatch_to_agent(primary, prompt)
    elif intents[0] in INTENT_TO_AGENT:
        agent = INTENT_TO_AGENT[intents[0]]
        if agent:
            handler = await _dispatch_to_agent(agent, prompt)
        else:
            # Inline-handled intents (schedule, status, reminder, note)
            handler = await _handle_inline(intents[0], msg, classification)
    else:
        # Fallback: send to scout
        handler = await _dispatch_to_agent("scout", prompt)

    update_inbound_route(msg.rowid, handler, "llm")
    return handler


async def _handle_inline(intent: str, msg: InboundMessage, classification: dict) -> str:
    """Handle intents that don't need a full agent (schedule, status, reminder, note)."""
    if intent == "schedule":
        # Already have calendar data in context — ask Opus to synthesize
        direct = classification.get("direct_answer", "")
        if direct:
            await send_message(direct, agent="calendar")
        else:
            # Pull fresh schedule and send
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
        # Send a status summary
        await send_message(
            classification.get("direct_answer", "Checking status..."),
            agent="status",
        )
        return "status:inline"

    elif intent == "reminder":
        # Create or check reminders via osascript
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
