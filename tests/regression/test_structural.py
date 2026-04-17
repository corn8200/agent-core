"""Structural regression checks — run under pytest, zero SDK calls.

Guards the surface area of core.agents against silent drift:
- all 9 named agents exist as AgentDefinition
- each has non-empty system prompt and tool list
- prompt markdown file in ~/.claude/agents/<name>.md exists
- YAML cases file parses and covers every agent
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import agents as agents_mod  # noqa: E402

AGENT_NAMES = [
    "scout", "forge", "wrench", "dispatch", "ledger",
    "toolsmith", "titan", "anvil", "critic",
]
CASES_YAML = Path(__file__).parent / "cases" / "all.yaml"
AGENTS_DIR = Path.home() / ".claude" / "agents"


def test_all_nine_agents_present():
    missing = [n for n in AGENT_NAMES if getattr(agents_mod, n, None) is None]
    assert not missing, f"missing AgentDefinition exports: {missing}"


def test_agents_have_prompts_and_tools():
    problems = []
    for name in AGENT_NAMES:
        a = getattr(agents_mod, name)
        if not getattr(a, "prompt", "").strip():
            problems.append(f"{name}: empty prompt")
        if not getattr(a, "tools", None):
            problems.append(f"{name}: empty tool list")
    assert not problems, problems


def test_prompt_markdown_files_exist():
    missing = [n for n in AGENT_NAMES if not (AGENTS_DIR / f"{n}.md").exists()]
    assert not missing, f"missing prompt files in {AGENTS_DIR}: {missing}"


def test_cases_yaml_covers_all_agents():
    data = yaml.safe_load(CASES_YAML.read_text())
    cases = data.get("agents") or {}
    missing = [n for n in AGENT_NAMES if n not in cases]
    assert not missing, f"cases/all.yaml missing agents: {missing}"


def test_every_case_has_assertions():
    data = yaml.safe_load(CASES_YAML.read_text())
    cases = data.get("agents") or {}
    bad = []
    for name, lst in cases.items():
        for i, c in enumerate(lst or []):
            if not c.get("prompt"):
                bad.append(f"{name}[{i}]: missing prompt")
            if not c.get("assertions"):
                bad.append(f"{name}[{i}]: missing assertions")
    assert not bad, bad
