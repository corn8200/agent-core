# PRD: agent-core adaptations for claude-code v2.1.105

Adapt agent-core to exploit new capabilities and clean up workarounds made obsolete by claude-code v2.1.105.

## Success Criteria

```bash
cd /Users/johncornelius/Projects/agent-core
python3 -c "import core.agents, core.tools, core.hooks"
# Each R1–R5 acceptance test exits 0
# ruff check passes on changed files
```

## Requirements

### R1: Expand AgentDefinition descriptions (250→1,536 char cap)

**File:** `/Users/johncornelius/Projects/agent-core/core/agents.py`

**What:** All 9 `description` fields in the `AgentDefinition` objects are under 100 chars. The skill-picker cap was raised to 1,536. Expand each to 120–500 chars. Include: what the agent does, when to pick it over alternatives, notable tools it wields, and any constraints (e.g. read-only, high turn budget). Keep double-quoted single-line strings. Do not touch `prompt`, `tools`, `model`, or any other field.

**Why:** Richer descriptions let the skill picker and swarm router surface better agent matches.

**Acceptance test:**
```bash
python3 -c "
import re, sys
descs = re.findall(r'description=\"(.+?)\"', open('/Users/johncornelius/Projects/agent-core/core/agents.py').read())
assert len(descs) >= 9, f'found {len(descs)}'
short = [d[:50] for d in descs if len(d) < 120]
assert not short, f'too short: {short}'
over = [d[:50] for d in descs if len(d) > 1536]
assert not over, f'over cap: {over}'
"
```

### R2: Harden @tool error handling

**File:** `/Users/johncornelius/Projects/agent-core/core/tools.py`

**What:** Audit every `@tool`-decorated function. Each must wrap its body in `try / except Exception` that returns an error string. None may raise an unhandled exception. Add the wrapper only where missing — do not restructure functions that already have one.

**Why:** An unhandled raise in a @tool produces a non-JSON traceback on stdio. v2.1.105 now fails-fast instead of hanging, but returning a clean error string is still correct.

**Acceptance test:**
```bash
python3 -c "
import ast, sys
tree = ast.parse(open('/Users/johncornelius/Projects/agent-core/core/tools.py').read())
for node in ast.walk(tree):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        continue
    if any((isinstance(d, ast.Name) and d.id == 'tool') or
           (isinstance(d, ast.Call) and getattr(d.func, 'id', '') == 'tool')
           for d in node.decorator_list):
        assert any(isinstance(s, ast.Try) for s in ast.walk(node)), \
            f'@tool {node.name} line {node.lineno} has no try/except'
print('OK')
"
```

### R3: Remove headless first-turn MCP workarounds

**Files:** Scan all four — edit only if workarounds exist:
- `/Users/johncornelius/Projects/agent-core/briefs/morning_brief.py`
- `/Users/johncornelius/Projects/agent-core/handler/monitor.py`
- `/Users/johncornelius/Projects/agent-core/swarm/engine.py`
- `/Users/johncornelius/Projects/agent-core/home_ops/engine.py`

**What:** v2.1.105 fixed MCP tools being unavailable on the first turn of headless sessions. Search for dummy first turns, tool-availability retries, sleep-before-first-prompt patterns, or comments referencing this workaround. Remove any found. No-op if clean.

**Why:** Workarounds waste a turn and add startup latency to every headless agent.

**Acceptance test:**
```bash
! grep -rn -iE '(first.turn.workaround|dummy.*(turn|prompt|ping)|mcp.*(warm|not.ready|wait)|sleep.*tool.*(avail|connect))' \
  /Users/johncornelius/Projects/agent-core/briefs/morning_brief.py \
  /Users/johncornelius/Projects/agent-core/handler/monitor.py \
  /Users/johncornelius/Projects/agent-core/swarm/engine.py \
  /Users/johncornelius/Projects/agent-core/home_ops/engine.py 2>/dev/null
```

### R4: Remove custom stream timeout/retry wrappers

**Files:** Same four as R3.

**What:** v2.1.105 aborts stalled API streams after 5 min and retries non-streaming automatically. Search for `asyncio.wait_for` wrapping agent/SDK calls, custom hung-stream detection, manual stream-abort logic, or comments about stream timeouts. Remove any found. No-op if clean.

**Why:** Custom wrappers may conflict with the built-in 5-min timeout or cause double retries.

**Acceptance test:**
```bash
! grep -rn -iE '(stream.*(timeout|hung|abort|stall)|wait_for.*(agent|sdk|claude)|manual.*(retry|abort).*(stream|api))' \
  /Users/johncornelius/Projects/agent-core/briefs/morning_brief.py \
  /Users/johncornelius/Projects/agent-core/handler/monitor.py \
  /Users/johncornelius/Projects/agent-core/swarm/engine.py \
  /Users/johncornelius/Projects/agent-core/home_ops/engine.py 2>/dev/null
```

### R5: Remove redundant WebFetch style/script stripping

**File:** `/Users/johncornelius/Projects/agent-core/home_ops/gather.py`

**What:** v2.1.105 WebFetch strips `<style>` and `<script>` tags natively. Search for BeautifulSoup `.decompose()` calls or regex removing style/script tags from fetched HTML. Remove if found. No-op if clean.

**Why:** Redundant post-processing adds latency and import overhead.

**Acceptance test:**
```bash
! grep -n -E '(\.decompose\(\)|remove.*(script|style)|strip.*(script|style)|re\.sub.*<(script|style))' \
  /Users/johncornelius/Projects/agent-core/home_ops/gather.py 2>/dev/null
```

## Test Steps

Run after all changes, in order:

```bash
cd /Users/johncornelius/Projects/agent-core
python3 -c "import core.agents, core.tools, core.hooks; print('imports OK')"
python3 -c "from core.agents import ALL_AGENTS; assert len(ALL_AGENTS) == 9, f'{len(ALL_AGENTS)}'"
# Run each R1–R5 acceptance test (copy from above)
ruff check core/agents.py core/tools.py home_ops/gather.py --select E,W,F --ignore E501
```

## Out of Scope

- All 24 upstream UI/terminal/keybinding bug fixes — no agent-core code involved
- `core/hooks.py` — no PreCompact hook needed; evaluator did not flag
- `pyproject.toml` — no dependency version bump for v2.1.105
- `~/.claude/agents/*.md` prompt files — do not touch
- LaunchAgent plists, `run.sh` scripts
- No new files, no new abstractions, no renames, no refactoring beyond what each R specifies