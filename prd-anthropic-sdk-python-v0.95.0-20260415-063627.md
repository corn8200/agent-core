# PRD: Suppress anthropic-sdk-python v0.95.0 Model Deprecation Warnings

Proactively filter `DeprecationWarning` for deprecated `claude-opus-4-20250514` / `claude-sonnet-4-20250514` model constants, preventing warning spam across agent-core and anthropic-update-watcher when SDK v0.95.0 lands.

## Context

SDK v0.95.0 deprecates model identifier constants for Claude Opus 4 and Sonnet 4 (dated 2025-05-14). All 9 `AgentDefinition` objects in `core/agents.py` use `model="opus"` which resolves through these constants. The `evaluator.py` and `implementer.py` in anthropic-update-watcher also reference Opus. No successor model string is announced — suppress warnings now, migrate model strings when a successor ships.

## Success Criteria

```bash
# Filter in agents.py precedes claude_agent_sdk import
python3 -c "import os; L=open(os.path.expanduser('~/Projects/agent-core/core/agents.py')).readlines(); fw=next(i for i,l in enumerate(L) if 'filterwarnings' in l); sdk=next(i for i,l in enumerate(L) if 'claude_agent_sdk' in l); assert fw<sdk"

# Filter in watcher.py
grep -q 'filterwarnings.*DeprecationWarning' ~/Projects/anthropic-update-watcher/watcher.py

# Both files parse cleanly
python3 -c "import ast,os; ast.parse(open(os.path.expanduser('~/Projects/agent-core/core/agents.py')).read()); print('ok')"
python3 -c "import ast,os; ast.parse(open(os.path.expanduser('~/Projects/anthropic-update-watcher/watcher.py')).read()); print('ok')"

# No deprecated constant imports anywhere
! grep -rq 'CLAUDE_OPUS_4_20250514\|CLAUDE_SONNET_4_20250514' ~/Projects/agent-core/ ~/Projects/anthropic-update-watcher/ --include='*.py'
```

## Requirements

### R1: Add DeprecationWarning filter to `core/agents.py`

**File:** `/Users/johncornelius/Projects/agent-core/core/agents.py`

**Change:** Insert the following block after the module docstring and before ALL other imports (including `from pathlib import Path`):

```python
import warnings
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message=r".*claude-(?:opus|sonnet)-4-20250514",
)
```

**Why:** Every agent-core entry point (`morning_brief.py`, `handler/monitor.py`, `swarm/engine.py`, `home_ops/engine.py`) imports from `core.agents`. Setting the filter before `claude_agent_sdk` is imported suppresses warnings process-wide. Do NOT change any `model="opus"` strings — the alias still functions correctly.

**Acceptance test:**
```bash
python3 -c "import os; L=open(os.path.expanduser('~/Projects/agent-core/core/agents.py')).readlines(); fw=next(i for i,l in enumerate(L) if 'filterwarnings' in l); sdk=next(i for i,l in enumerate(L) if 'claude_agent_sdk' in l); assert fw<sdk"
```

### R2: Add DeprecationWarning filter to `watcher.py`

**File:** `/Users/johncornelius/Projects/anthropic-update-watcher/watcher.py`

**Change:** Insert the identical `import warnings` + `filterwarnings` block at the top of the file, after the module docstring and before any other imports (`evaluator`, `implementer`, `anthropic`, `claude_agent_sdk`, etc.).

**Why:** `watcher.py` is the LaunchAgent entry point. It imports `evaluator.py` (direct `anthropic` SDK calls) and `implementer.py` (`claude_agent_sdk`). The filter must be set before those modules load.

**Acceptance test:**
```bash
grep -q 'filterwarnings.*DeprecationWarning' ~/Projects/anthropic-update-watcher/watcher.py && python3 -c "import ast,os; ast.parse(open(os.path.expanduser('~/Projects/anthropic-update-watcher/watcher.py')).read()); print('ok')"
```

### R3: Verify no deprecated constant imports

**Files:** All `*.py` in `/Users/johncornelius/Projects/agent-core/` and `/Users/johncornelius/Projects/anthropic-update-watcher/`

**Change:** None expected. If any file imports `CLAUDE_OPUS_4_20250514` or `CLAUDE_SONNET_4_20250514` as a Python name from the `anthropic` module, replace the reference with the string literal `"claude-opus-4-20250514"` (the filter covers the runtime warning; removing the constant import avoids the import-time warning).

**Acceptance test:**
```bash
! grep -rq 'CLAUDE_OPUS_4_20250514\|CLAUDE_SONNET_4_20250514' ~/Projects/agent-core/ ~/Projects/anthropic-update-watcher/ --include='*.py'
```

## Test Steps

Run in order after all changes:

1. `python3 -c "import ast,os; ast.parse(open(os.path.expanduser('~/Projects/agent-core/core/agents.py')).read()); print('agents.py ok')"`
2. `python3 -c "import ast,os; ast.parse(open(os.path.expanduser('~/Projects/anthropic-update-watcher/watcher.py')).read()); print('watcher.py ok')"`
3. `python3 -c "import os; L=open(os.path.expanduser('~/Projects/agent-core/core/agents.py')).readlines(); fw=next(i for i,l in enumerate(L) if 'filterwarnings' in l); sdk=next(i for i,l in enumerate(L) if 'claude_agent_sdk' in l); assert fw<sdk; print('ordering ok')"`
4. `! grep -rq 'CLAUDE_OPUS_4_20250514\|CLAUDE_SONNET_4_20250514' ~/Projects/agent-core/ ~/Projects/anthropic-update-watcher/ --include='*.py' && echo 'no deprecated constants'`
5. `python3 -W error::DeprecationWarning -c "import warnings; warnings.filterwarnings('ignore', category=DeprecationWarning, message=r'.*claude-(?:opus|sonnet)-4-20250514'); warnings.warn('claude-opus-4-20250514 is deprecated', DeprecationWarning); print('suppression ok')"`
6. `cd ~/Projects/agent-core && git diff --stat` — verify only `core/agents.py` changed in agent-core

## Out of Scope

- **Model string changes** — do NOT change `model="opus"` to any other value. Models still function; migrate when successor is announced.
- **Bedrock auth header change** — no impact, stack uses Max subscription via direct API.
- **SDK version pinning** — do NOT pin `anthropic` or `claude-agent-sdk` in `pyproject.toml`.
- **Refactoring model strings into constants** — no new abstractions.
- **Any file not listed above** — do NOT touch `tools.py`, `hooks.py`, `morning_brief.py`, `monitor.py`, `engine.py`, or any other file.