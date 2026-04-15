# PRD: claude-code v2.1.109 — No agent-core changes required

**Source:** https://github.com/anthropics/claude-code/releases/tag/v2.1.109

## Summary

claude-code v2.1.109 changes the extended-thinking CLI spinner from a static indicator to a rotating progress hint. This is a cosmetic change rendered entirely by the `claude` binary subprocess. agent-core configures thinking via `effort="max"` on `AgentDefinition` objects but never renders, intercepts, or depends on the CLI's progress indicator. **No code changes needed.**

## Why no changes

1. The spinner is drawn by the `claude` TUI process — agent-core spawns it via `claude_agent_sdk` and reads structured JSON output, never terminal escape sequences.
2. `core/agents.py` sets `effort="max"` which enables extended thinking — it does not reference any progress/spinner/indicator API.
3. `core/hooks.py` hooks into `PreToolUse`/`PostToolUse` lifecycle events — thinking indicators are not tool-use events.
4. No file in agent-core imports, references, or monkeypatches any spinner, progress, or indicator class from `claude_agent_sdk`.

## Success Criteria

All commands exit 0 — confirming agent-core has no coupling to CLI progress rendering:

```bash
# No references to spinner/progress-indicator internals in agent-core
! grep -rn 'spinner\|progress_hint\|ThinkingIndicator\|ProgressIndicator' ~/Projects/agent-core/core/

# agents.py still configures effort="max" (thinking enabled, unchanged)
grep -q 'effort="max"' ~/Projects/agent-core/core/agents.py

# hooks.py unchanged — no thinking-indicator hooks
grep -q 'AGENT_HOOKS' ~/Projects/agent-core/core/hooks.py

# Python import smoke test passes
cd ~/Projects/agent-core && python -c "from core.agents import scout, forge, wrench; print('ok')"
```

## Requirements

### R0: No-op — verify and skip

- **Files to change:** None.
- **What:** Confirm agent-core has zero coupling to the CLI's thinking-phase progress indicator. Run the success criteria commands. If all pass, this PRD is complete.
- **Why:** Prevents Ralph from manufacturing unnecessary diffs. The rotating-hint change lives inside the `claude` binary; upgrading `claude-code` to ≥2.1.109 via `npm` or `brew` picks it up automatically.
- **Acceptance test:**
  ```bash
  cd ~/Projects/agent-core && python -c "from core.agents import scout, forge, wrench, dispatch, ledger, toolsmith, titan, anvil, critic; print('all agents load')" && ! grep -rn 'spinner\|progress_hint\|ThinkingIndicator' core/
  ```

## Test Steps

```bash
# 1. Confirm no spinner/indicator references leaked into codebase
! grep -rn 'spinner\|progress_hint\|ThinkingIndicator\|ProgressIndicator' ~/Projects/agent-core/core/

# 2. Import smoke — all agent definitions load cleanly
cd ~/Projects/agent-core && python -c "
from core.agents import scout, forge, wrench, dispatch, ledger, toolsmith, titan, anvil, critic
from core.hooks import AGENT_HOOKS
from core.tools import tmux_relay_shell
print('smoke ok')
"

# 3. effort="max" still set on all agents (thinking enabled)
test "$(grep -c 'effort=\"max\"' ~/Projects/agent-core/core/agents.py)" -ge 9

# 4. No uncommitted changes (ralph should not have touched anything)
cd ~/Projects/agent-core && test -z "$(git diff --name-only)"
```

## Out of Scope

- **Do not** update `claude-code` / `claude` CLI version — that's handled by `npm update -g @anthropic-ai/claude-code` outside this repo.
- **Do not** add spinner/progress wrapper code to agent-core — the SDK handles this internally.
- **Do not** modify `pyproject.toml`, `core/agents.py`, `core/hooks.py`, or `core/tools.py`.
- **Do not** refactor, rename, or add comments to any file.
- **Do not** create a branch — there are no changes to commit.