#!/usr/bin/env python3
"""Weekly Toolsmith review — reads agent_performance.json and sends iMessage summary.

Runs every Monday at 9 AM via com.john.toolsmith-weekly LaunchAgent.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Scrub API billing vars before importing SDK
for _var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
             "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
    os.environ.pop(_var, None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.constants import HOME, PERSONAL_EMAIL
from core.tools import send_imessage_reliable
from core.hooks import AGENT_HOOKS
from core.thinking import STANDARD

PERF_LOG = Path.home() / ".claude/projects/-Users-johncornelius/memory/agent_performance.json"

SYSTEM = """You are Toolsmith, a meta-agent that reviews agent performance logs.
Analyze the provided performance data and return a concise weekly report covering:
1. Top failing/partial agents in the past 7 days (name + pattern of failure)
2. Any systemic issues (same error across multiple agents)
3. Specific fix recommendations for agents with 3+ failures of the same type

Rules:
- Keep total response under 350 words
- Plain text only, no markdown, no bullet symbols — this goes via iMessage
- Be direct and specific (file paths, function names, root causes)
- Skip agents with no issues"""


async def run():
    try:
        from core.mac_sdk import query, ClaudeAgentOptions
    except ImportError as e:
        msg = f"[Toolsmith] Import failed: {e}"
        print(msg, file=sys.stderr)
        await send_imessage_reliable(PERSONAL_EMAIL, msg)
        return

    if not PERF_LOG.exists():
        await send_imessage_reliable(PERSONAL_EMAIL, "[Toolsmith] No agent_performance.json found — nothing to review.")
        return

    # Parse the log (may be mixed JSON array + NDJSON)
    raw = PERF_LOG.read_text().strip()
    entries = []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        # Try line-by-line fallback for NDJSON tail
        for line in raw.splitlines():
            line = line.strip().rstrip(",")
            if line.startswith("{"):
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass

    # Filter to last 7 days
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    recent = [e for e in entries if isinstance(e, dict) and e.get("date", "") >= cutoff]
    total = len(entries)

    prompt = f"""Today is {datetime.now().strftime('%Y-%m-%d %A')}.

All-time entries: {total}
Recent (last 7 days): {len(recent)}

Last 7 days of agent runs:
{json.dumps(recent, indent=2)}

Review these results and give me the weekly Toolsmith report."""

    brief_text = ""
    try:
        async for msg in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                system_prompt=SYSTEM,
                model="opus",
                permission_mode="bypassPermissions",
                max_turns=3,
                max_budget_usd=0.20,
                cwd=str(HOME),
                hooks=AGENT_HOOKS,
                thinking=STANDARD,
                effort="max",
            ),
        ):
            if hasattr(msg, "content"):
                for block in msg.content:
                    if hasattr(block, "text"):
                        brief_text += block.text
            if hasattr(msg, "result") and msg.result:
                brief_text = msg.result
    except Exception:
        pass  # SDK throws on CLI exit after result received

    if brief_text.strip():
        await send_imessage_reliable(PERSONAL_EMAIL, f"[Toolsmith Weekly]\n\n{brief_text.strip()}")
    else:
        await send_imessage_reliable(PERSONAL_EMAIL, "[Toolsmith] Review ran but no output captured.")


if __name__ == "__main__":
    asyncio.run(run())
