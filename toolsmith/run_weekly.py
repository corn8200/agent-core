#!/usr/bin/env python3
"""Weekly Toolsmith review — reads agent_performance.json and sends Pushover summary.

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

from core.vault import hydrate_env
hydrate_env()

from core.constants import HOME
from core.pushover import send_pushover
from core.hooks import AGENT_HOOKS
from core.thinking import STANDARD

DRY_RUN = "--dry-run" in sys.argv
LOG_PATH = Path.home() / "logs" / "toolsmith-weekly.log"

PERF_LOG = Path.home() / ".claude/projects/-Users-johncornelius/memory/agent_performance.json"

SYSTEM = """You are Toolsmith, a meta-agent that reviews agent performance logs.
Analyze the provided performance data and return a concise weekly report covering:
1. Top failing/partial agents in the past 7 days (name + pattern of failure)
2. Any systemic issues (same error across multiple agents)
3. Specific fix recommendations for agents with 3+ failures of the same type

Rules:
- Keep total response under 350 words
- Plain text only, no markdown
- Be direct and specific (file paths, function names, root causes)
- Skip agents with no issues"""


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


async def run():
    try:
        from core.mac_sdk import query, ClaudeAgentOptions
    except ImportError as e:
        _log(f"Import failed: {e}")
        if not DRY_RUN:
            await send_pushover(title="Toolsmith Error", message=f"Import failed: {e}", priority=0)
        return

    if not PERF_LOG.exists():
        _log("No agent_performance.json found — nothing to review.")
        if not DRY_RUN:
            await send_pushover(title="Toolsmith Weekly", message="No agent_performance.json found — nothing to review.", priority=0)
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

    _log(f"Running SDK call — {len(recent)} recent entries (last 7d), {total} total")

    brief_text = ""
    try:
        async for msg in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                system_prompt=SYSTEM,
                model="opus",
                permission_mode="bypassPermissions",
                max_turns=3,
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
    except Exception as exc:
        _log(f"SDK exception (may be normal CLI exit): {exc!r}")

    if brief_text.strip():
        _log(f"Got {len(brief_text)} chars of output")
        if DRY_RUN:
            print(f"[DRY-RUN] Would send Pushover:\n{brief_text.strip()}")
        else:
            result = await send_pushover(
                title="Toolsmith Weekly",
                message=brief_text.strip(),
                priority=0,
            )
            _log(f"Pushover: {result.detail}")
    else:
        # Empty output is not actionable — log silently, no notification
        _log("SDK returned empty output — logging silently, skipping notification")


if __name__ == "__main__":
    asyncio.run(run())
