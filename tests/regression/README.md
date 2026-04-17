# Agent regression harness

Catches prompt/tool-list drift for the 9 named agents (Scout, Forge, Wrench,
Dispatch, Ledger, Toolsmith, Titan, Anvil, Critic).

Two layers:

1. **Structural** (`test_structural.py`, pytest, zero SDK calls)
   - every agent is exported from `core.agents`
   - each has a non-empty prompt and tool list
   - `~/.claude/agents/<name>.md` exists
   - `cases/all.yaml` covers every agent and every case has assertions

2. **Live** (`run.py`, SDK-backed, free under Max)
   - loads cases from `cases/all.yaml`
   - for each case: builds a one-shot `ClaudeAgentOptions` using the agent's
     system prompt + tool list, fires via `core.mac_sdk.query`, captures final
     text, applies promptfoo-style assertions
   - forces `model=sonnet` + `max_turns=3` so regression probes are cheap

## Running

```
# Fast, no SDK — safe in CI, pre-commit, etc.
pytest tests/regression/test_structural.py -v

# Live, fires one case per agent (takes ~2–3 min, ~$0.10 equiv/agent)
python -m tests.regression.run                    # all agents
python -m tests.regression.run --agent scout      # one
python -m tests.regression.run --dry              # list cases, don't fire
python -m tests.regression.run --json             # machine output
```

Exit code: `0` all pass, `1` any fail, `2` bad arguments.

## Adding a case

Edit `cases/all.yaml` — each entry under `agents.<name>` is a dict with:

```yaml
- prompt: "what the user would ask"
  assertions:
    contains:     ["required", "substrings"]   # all must match
    any_of:       ["either", "this", "or that"]  # >=1 must match
    not_contains: ["refuses", "can't"]         # none may match
    regex:        ["^[A-Z]"]                   # each pattern must match
    min_chars:    20
    max_chars:    2000
```

All string checks are case-insensitive. `regex` is `re.search`, so anchor it
yourself if needed.

## Why not promptfoo?

Promptfoo defaults to the Console API (pay-per-token). Our agents run under the
Max subscription via `claude_agent_sdk`. Going through `core.mac_sdk` keeps it
free, enforces the 50-calls/hour hard cap in `mac_sdk.py`, and exercises the
actual `AgentDefinition` objects in `core.agents` — which is the shape that can
regress silently.

If you later want to fold in promptfoo proper for scenarios where you're OK
paying, build a custom provider that shells into `run.py` in `--json` mode.
