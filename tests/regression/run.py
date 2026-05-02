"""Agent regression harness — promptfoo-style assertions over SDK-backed agents.

Why not promptfoo itself? The 9 named agents run under the Max subscription via
`claude_agent_sdk`. Promptfoo talks to the Console API (pay-per-token). Running
the harness through `core.mac_sdk` keeps it free, enforces the 50/hr cap, and
exercises the real AgentDefinition objects in core.agents.

Usage:
  python -m tests.regression.run                    # all agents, all cases
  python -m tests.regression.run --agent scout      # one agent
  python -m tests.regression.run --cases cases/all.yaml
  python -m tests.regression.run --dry              # list cases, don't fire SDK
  python -m tests.regression.run --json             # machine-readable output

Structural check (fast, no SDK) is at tests.regression.test_structural.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import agents as agents_mod  # noqa: E402
from core.mac_sdk import ClaudeAgentOptions, query  # noqa: E402
from core.thinking import LIGHT  # noqa: E402

DEFAULT_CASES = Path(__file__).parent / "cases" / "all.yaml"

AGENT_NAMES = [
    "scout", "forge", "wrench", "dispatch", "ledger",
    "toolsmith", "titan", "anvil", "critic",
]


def _lookup_agent(name: str):
    obj = getattr(agents_mod, name, None)
    if obj is None:
        raise KeyError(f"unknown agent: {name}")
    return obj


def _extract_text(messages: list[Any]) -> str:
    """Pull final assistant text from a list of SDK messages."""
    chunks: list[str] = []
    for m in messages:
        # AssistantMessage has .content blocks; TextBlock has .text
        content = getattr(m, "content", None)
        if not content:
            continue
        for block in content:
            t = getattr(block, "text", None)
            if isinstance(t, str):
                chunks.append(t)
    return "\n".join(chunks).strip()


async def _run_case(agent_name: str, prompt: str) -> tuple[str, dict]:
    """Fire one prompt against one agent, return (final_text, usage_summary)."""
    agent_def = _lookup_agent(agent_name)
    # Single-agent session — pass the AgentDefinition via agents={}, then route
    # to it by using its system prompt. Cleanest path: instantiate ClaudeAgentOptions
    # with the agent's own system_prompt + tools so the session IS the agent.
    opts = ClaudeAgentOptions(
        system_prompt=agent_def.prompt,
        allowed_tools=list(agent_def.tools or []),
        max_turns=3,  # regression probes must not chew turns
        # max_budget_usd removed 2026-04-22 (#183) — vestigial under Max
        permission_mode="bypassPermissions",
        model="sonnet",  # override: cheap for regression; agent's own pick ignored
        thinking=LIGHT,
        effort="max",
    )
    msgs: list[Any] = []
    async for m in query(prompt=prompt, options=opts):
        msgs.append(m)
    text = _extract_text(msgs)
    # Rough usage tally from ResultMessage, if present
    usage = {}
    for m in msgs:
        u = getattr(m, "usage", None)
        if u:
            usage = {
                "in": getattr(u, "input_tokens", 0),
                "out": getattr(u, "output_tokens", 0),
                "cost_usd": getattr(m, "total_cost_usd", 0.0),
            }
    return text, usage


def _check(text: str, assertions: dict) -> list[str]:
    """Return list of failure strings — empty list means pass."""
    fails: list[str] = []
    low = text.lower()
    for needle in assertions.get("contains", []) or []:
        if needle.lower() not in low:
            fails.append(f"missing required substring: {needle!r}")
    any_of = assertions.get("any_of") or []
    if any_of and not any(n.lower() in low for n in any_of):
        fails.append(f"none of {any_of} found")
    for needle in assertions.get("not_contains", []) or []:
        if needle.lower() in low:
            fails.append(f"forbidden substring present: {needle!r}")
    for pat in assertions.get("regex", []) or []:
        if not re.search(pat, text):
            fails.append(f"regex not matched: {pat}")
    mn = assertions.get("min_chars")
    if mn is not None and len(text) < mn:
        fails.append(f"response too short: {len(text)} < {mn}")
    mx = assertions.get("max_chars")
    if mx is not None and len(text) > mx:
        fails.append(f"response too long: {len(text)} > {mx}")
    return fails


def _load_cases(path: Path, only: str | None) -> dict:
    data = yaml.safe_load(path.read_text())
    cases = data.get("agents") or {}
    if only:
        if only not in cases:
            raise SystemExit(f"no cases defined for agent: {only}")
        cases = {only: cases[only]}
    return cases


async def _run_all(cases: dict, dry: bool) -> tuple[list[dict], dict]:
    results: list[dict] = []
    totals = {"cases": 0, "pass": 0, "fail": 0, "skip": 0,
              "cost_usd": 0.0, "elapsed_s": 0.0}
    t0 = time.time()
    for agent_name, prompts in cases.items():
        for i, case in enumerate(prompts):
            totals["cases"] += 1
            prompt = case.get("prompt", "")
            assertions = case.get("assertions", {}) or {}
            entry = {
                "agent": agent_name,
                "case": i,
                "prompt": prompt,
                "status": "pending",
                "fails": [],
                "text": "",
                "usage": {},
            }
            if dry:
                entry["status"] = "dry"
                totals["skip"] += 1
                results.append(entry)
                continue
            try:
                text, usage = await _run_case(agent_name, prompt)
            except Exception as exc:
                entry["status"] = "error"
                entry["fails"] = [f"exception: {exc!r}"]
                totals["fail"] += 1
                results.append(entry)
                continue
            entry["text"] = text
            entry["usage"] = usage
            totals["cost_usd"] += float(usage.get("cost_usd") or 0)
            fails = _check(text, assertions)
            if fails:
                entry["status"] = "fail"
                entry["fails"] = fails
                totals["fail"] += 1
            else:
                entry["status"] = "pass"
                totals["pass"] += 1
            results.append(entry)
    totals["elapsed_s"] = round(time.time() - t0, 2)
    return results, totals


def _print_human(results: list[dict], totals: dict) -> None:
    for r in results:
        mark = {"pass": "PASS", "fail": "FAIL", "dry": "DRY ", "error": "ERR "}.get(r["status"], "?")
        print(f"[{mark}] {r['agent']:<10} case={r['case']}  prompt={r['prompt'][:60]!r}")
        if r["fails"]:
            for f in r["fails"]:
                print(f"         - {f}")
        if r["text"] and r["status"] != "pass":
            snip = r["text"][:240].replace("\n", " ")
            print(f"         text: {snip}")
    print("-" * 60)
    cost = totals["cost_usd"]
    print(f"{totals['cases']} cases — pass={totals['pass']} fail={totals['fail']} "
          f"skip={totals['skip']} cost=${cost:.4f} elapsed={totals['elapsed_s']}s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    ap.add_argument("--agent", choices=AGENT_NAMES, help="run cases for one agent only")
    ap.add_argument("--dry", action="store_true", help="list cases, don't fire SDK")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    cases_path = Path(args.cases).expanduser()
    if not cases_path.exists():
        print(f"cases file not found: {cases_path}", file=sys.stderr)
        return 2

    cases = _load_cases(cases_path, args.agent)
    results, totals = asyncio.run(_run_all(cases, args.dry))

    if args.json:
        print(json.dumps({"results": results, "totals": totals}, indent=2, default=str))
    else:
        _print_human(results, totals)
    return 0 if totals["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
