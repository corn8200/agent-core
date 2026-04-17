"""Swarm Engine — programmatic parallel agent orchestration.

Modes:
  PARALLEL — all agents launch at once (independent subtasks)
  SERIES   — agents run in waves, each wave's results feed the next
  HYBRID   — parallel within each wave, serial between waves

Features:
  - Shared SwarmContext: in-process key-value store agents can read/write via MCP tools
  - Session continuity: series/hybrid modes auto-inject prior wave summaries
  - memory="project": all agents auto-load CLAUDE.md for project context
  - Handoff files at /tmp/handoff/ for debugging and external consumption

Usage:
  from swarm.engine import Swarm

  s = Swarm(task="audit infrastructure", mode="parallel")
  s.add("mac", agent="wrench", prompt="Check Mac Mini health...")
  s.add("vps", agent="wrench", prompt="Check VPS health...")
  s.add("pi",  agent="wrench", prompt="Check Pi health...")
  results = await s.run()
  # results = {"mac": "...", "vps": "...", "pi": "..."}
  # s.context.dump() -> shared state written by agents during run

  # Series with shared context:
  s = Swarm(task="find and fix bug", mode="series")
  s.add("investigate", agent="scout", prompt="...", wave=1)
  s.add("fix", agent="wrench", prompt="Fix based on: {investigate}", wave=2)
  results = await s.run()
"""

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ⚠️ Scrub API billing vars before importing claude_agent_sdk. See
# ~/Projects/anthropic-update-watcher/watcher.py:182-183 for rationale
# (ralph leak 2026-04-12).
for _leak_var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                  "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
    os.environ.pop(_leak_var, None)

from core.mac_sdk import query, ClaudeAgentOptions

from core.agents import ALL_AGENTS
from core.tools import create_core_server, SwarmContext, set_active_context
from core.hooks import AGENT_HOOKS
from core.thinking import STANDARD

HANDOFF_DIR = Path("/tmp/handoff")
LOG_PATH = Path.home() / ".claude/projects/-Users-johncornelius/memory/agent_performance.json"


@dataclass
class SwarmAgent:
    name: str
    prompt: str
    agent: str = "scout"        # named agent from ALL_AGENTS
    model: Optional[str] = None  # override agent default
    max_turns: int = 15
    max_budget: float = 0.50
    wave: int = 1


@dataclass
class SwarmResult:
    agent: str
    name: str
    status: str              # "pass", "partial", "fail"
    output: str
    elapsed: float = 0.0
    error: Optional[str] = None
    session_id: Optional[str] = None  # SDK session for resume


class Swarm:
    def __init__(self, task: str, mode: str = "parallel", max_budget: float = 2.00):
        self.task = task
        self.mode = mode
        self.max_budget = max_budget
        self.agents: list[SwarmAgent] = []
        self.results: dict[str, SwarmResult] = {}
        self.context = SwarmContext()  # shared in-process state for this run
        self.run_id = f"swarm-{uuid.uuid4().hex[:8]}"

    def add(self, name: str, prompt: str, agent: str = "scout",
            model: str = None, max_turns: int = 15, max_budget: float = 0.50,
            wave: int = 1):
        """Add an agent to the swarm."""
        self.agents.append(SwarmAgent(
            name=name, prompt=prompt, agent=agent,
            model=model, max_turns=max_turns, max_budget=max_budget,
            wave=wave,
        ))

    async def run(self) -> dict[str, str]:
        """Execute the swarm. Returns {name: output_text}."""
        HANDOFF_DIR.mkdir(parents=True, exist_ok=True)

        # Activate this swarm's context so MCP tools use it
        set_active_context(self.context)

        if self.mode == "parallel":
            await self._run_wave(self.agents)
        elif self.mode == "series":
            waves = self._group_waves()
            for wave_num in sorted(waves.keys()):
                wave_agents = self._inject_results(waves[wave_num])
                await self._run_wave(wave_agents)
                # After each wave, store results in shared context
                self._sync_results_to_context(wave_num)
        elif self.mode == "hybrid":
            waves = self._group_waves()
            for wave_num in sorted(waves.keys()):
                wave_agents = self._inject_results(waves[wave_num])
                await self._run_wave(wave_agents)
                self._sync_results_to_context(wave_num)

        # Log performance
        self._log_performance()

        # Write full context dump for debugging
        context_dump = self.context.dump()
        if context_dump:
            (HANDOFF_DIR / f"{self.run_id}-context.json").write_text(
                json.dumps(context_dump, indent=2, default=str)
            )

        return {name: r.output for name, r in self.results.items()}

    async def _run_wave(self, agents: list[SwarmAgent]):
        """Run all agents in a wave concurrently."""
        tasks = [self._run_agent(a) for a in agents]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_agent(self, sa: SwarmAgent):
        """Run a single swarm agent."""
        start = time.time()
        agent_def = ALL_AGENTS.get(sa.agent)
        model = sa.model or (agent_def.model if agent_def else "opus")
        session_id = str(uuid.uuid4())

        recall_block = ""
        try:
            from core.recall import get_context
            # Capitalize agent name so AGENT_OVERRIDES (Titan/Critic) can match.
            agent_name = sa.agent.capitalize() if sa.agent else None
            recall_block = get_context(sa.prompt, kind="agent", agent_name=agent_name)
        except Exception:
            recall_block = ""
        agent_prompt = f"{recall_block}\n\n{sa.prompt}" if recall_block else sa.prompt

        try:
            result_text = ""
            try:
                async for msg in query(
                    prompt=agent_prompt,
                    options=ClaudeAgentOptions(
                        model=model,
                        permission_mode="bypassPermissions",
                        max_turns=sa.max_turns,
                        max_budget_usd=sa.max_budget,
                        cwd=str(Path.home()),
                        session_id=session_id,
                        mcp_servers={"core": create_core_server()},
                        hooks=AGENT_HOOKS,
                        thinking=STANDARD,
                        effort="max",
                    ),
                ):
                    if hasattr(msg, "content"):
                        for block in msg.content:
                            if hasattr(block, "text"):
                                result_text += block.text
                    if hasattr(msg, "result") and msg.result:
                        result_text = msg.result
            except Exception:
                pass  # SDK throws on CLI exit after result is received

            result_text = result_text.strip()
            elapsed = time.time() - start

            # Write handoff file
            handoff = {
                "agent": sa.agent,
                "name": sa.name,
                "timestamp": datetime.now().isoformat(),
                "task": sa.prompt[:200],
                "status": "complete",
                "output": result_text[:5000],
                "elapsed": round(elapsed, 1),
                "session_id": session_id,
                "run_id": self.run_id,
            }
            (HANDOFF_DIR / f"{sa.name}-output.json").write_text(
                json.dumps(handoff, indent=2)
            )

            self.results[sa.name] = SwarmResult(
                agent=sa.agent, name=sa.name, status="pass",
                output=result_text, elapsed=elapsed,
                session_id=session_id,
            )

        except Exception as e:
            elapsed = time.time() - start
            self.results[sa.name] = SwarmResult(
                agent=sa.agent, name=sa.name, status="fail",
                output="", elapsed=elapsed, error=str(e),
            )

    def _group_waves(self) -> dict[int, list[SwarmAgent]]:
        """Group agents by wave number."""
        waves: dict[int, list[SwarmAgent]] = {}
        for a in self.agents:
            waves.setdefault(a.wave, []).append(a)
        return waves

    def _inject_results(self, agents: list[SwarmAgent]) -> list[SwarmAgent]:
        """Replace {name} placeholders in prompts with prior results.

        Also prepends a PRIOR FINDINGS context block so agents in later waves
        know what earlier waves discovered, even without explicit placeholders.
        """
        # Build context summary from all prior results
        prior_summary = ""
        if self.results:
            summaries = []
            for name, result in self.results.items():
                status = result.status.upper()
                output_preview = result.output[:1500] if result.output else "(no output)"
                summaries.append(f"[{name}] ({result.agent}, {status}, {result.elapsed:.1f}s):\n{output_preview}")
            prior_summary = (
                "\n\n--- PRIOR WAVE FINDINGS (from earlier agents in this swarm) ---\n"
                + "\n\n".join(summaries)
                + "\n--- END PRIOR FINDINGS ---\n\n"
            )

        # Also include any shared context written by agents via MCP tools
        shared_ctx = self.context.dump()
        if shared_ctx:
            ctx_lines = [f"  {k}: {v[:200]}" for k, v in shared_ctx.items()]
            prior_summary += (
                "\n--- SHARED SWARM CONTEXT (written by agents via swarm_context_write) ---\n"
                + "\n".join(ctx_lines)
                + "\n--- END SHARED CONTEXT ---\n\n"
            )

        injected = []
        for a in agents:
            prompt = a.prompt

            # Replace explicit {name} placeholders
            for name, result in self.results.items():
                placeholder = f"{{{name}}}"
                if placeholder in prompt:
                    prompt = prompt.replace(placeholder, result.output[:3000])

            # Prepend prior findings for context
            if prior_summary:
                prompt = prior_summary + prompt

            injected.append(SwarmAgent(
                name=a.name, prompt=prompt, agent=a.agent,
                model=a.model, max_turns=a.max_turns,
                max_budget=a.max_budget, wave=a.wave,
            ))
        return injected

    def _sync_results_to_context(self, wave_num: int):
        """Store wave results in shared context for MCP tool access."""
        for name, result in self.results.items():
            self.context.write(f"wave{wave_num}:{name}:status", result.status)
            self.context.write(f"wave{wave_num}:{name}:output", result.output[:2000])

    def _log_performance(self):
        """Append run entries to agent_performance.json."""
        try:
            entries = json.loads(LOG_PATH.read_text()) if LOG_PATH.exists() else []
        except (json.JSONDecodeError, FileNotFoundError):
            entries = []

        date = datetime.now().strftime("%Y-%m-%d")
        for name, result in self.results.items():
            entries.append({
                "date": date,
                "agent": result.agent,
                "task": f"swarm:{self.task[:60]} / {name}",
                "result": result.status,
                "elapsed": round(result.elapsed, 1),
                "notes": result.error or "",
                "run_id": self.run_id,
            })

        LOG_PATH.write_text(json.dumps(entries, indent=2))

    def summary(self) -> str:
        """Human-readable summary of results."""
        lines = [f"Swarm: {self.task} ({self.mode}) [{self.run_id}]"]
        for name, r in self.results.items():
            status = r.status.upper()
            lines.append(f"  [{status}] {name} ({r.agent}, {r.elapsed:.1f}s)")
            if r.error:
                lines.append(f"    Error: {r.error}")
        ctx_keys = self.context.list_keys()
        if ctx_keys:
            lines.append(f"  Shared context: {len(ctx_keys)} keys")
        return "\n".join(lines)
