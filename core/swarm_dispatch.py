"""swarm_dispatch — failure-routing wrapper for swarm Agent invocations.

Post-substrate context (swarm.md, 2026-04-25): the shared work-pool substrate
was retired. All cross-pane work flows through pane-ask-v2. Automated swarm
agents (handler, watch-commander, home-ops) make SDK-level calls via
claude_agent_sdk. Neither path has built-in failure escalation; failed Agent
invocations previously either dropped silently or required every dispatcher to
hand-roll Pushover calls.

This module codifies the "doctor route on failure" HARD RULE
(~/.claude/rules/infra-alerts.md) for swarm use cases:

  - dispatched_pane_ask() — subprocess wrapper around ~/bin/pane-ask-v2.
    Retries transient exit codes (busy/rate-limited/ack-timeout), escalates
    terminal failures to doctor_escalate with structured context.

  - dispatched_sdk_agent() — wraps claude_agent_sdk query() calls. Same
    retry + escalate pattern. Returns a skipped sentinel if the SDK is not
    installed (deployment concern, not a runtime error to page about).

  - escalate_swarm_failure() — direct thin wrapper over doctor_escalate for
    the LLM-driven Agent() flow case, where the dispatcher LLM needs to
    escalate from a Bash one-liner.

doctor_escalate signature (from core/doctor_escalate.py):
    doctor_escalate(
        watcher: str,
        severity: str,           # "ok" | "notice" | "warn" | "critical"
        summary: str,
        context: dict | None,
        dedup_scope: str | None, # 6h TTL dedup key
    ) -> dict                    # {"dispatched": bool, "dedup_hit": bool, ...}

Every escalation call supplies dedup_scope so a crash-loop cannot flood the
doctor pane. Callers that want tighter dedup supply their own scope string.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

try:
    from core.doctor_escalate import doctor_escalate
except ImportError:
    from doctor_escalate import doctor_escalate  # type: ignore[no-redef]

PANE_ASK_BIN = "/Users/johncornelius/bin/pane-ask-v2"

# Exit codes from pane-ask-v2 that warrant a retry (transient).
_TRANSIENT_EXIT_CODES = {1, 7, 10}

# Exit codes that are terminal — retry is pointless.
_TERMINAL_EXIT_CODES = {3, 4, 5, 8, 9}


def dispatched_pane_ask(
    target: str,
    prompt: str,
    *,
    ssh: Optional[str] = None,
    wait: bool = False,
    timeout: int = 300,
    retries: int = 2,
    watcher_name: Optional[str] = None,
    dedup_scope: Optional[str] = None,
) -> dict:
    """Subprocess wrapper around pane-ask-v2 with doctor escalation on failure.

    Args:
        target: tmux target pane, e.g. "claude:2" or "claude-vps:3".
        prompt: text to send. Delivered via stdin (pane-ask-v2 reads "-").
        ssh: if set, passes --ssh <ssh> to pane-ask-v2 for cross-host dispatch.
        wait: if True, passes --wait (blocks until target stop_reason=end_turn).
        timeout: subprocess timeout in seconds per attempt.
        retries: max additional attempts after first failure (transient codes only).
        watcher_name: identifies this caller in doctor escalations.
        dedup_scope: 6h dedup key. Defaults to "pane-ask-{target}".

    Returns:
        On success: {"ok": True, "stdout": str, "stderr": str, "rc": 0, "attempts": int}
        On failure: {"ok": False, "rc": int, "stderr": str, "attempts": int, "escalated": bool}
    """
    watcher = watcher_name or f"swarm-pane-ask-{target}"
    scope = dedup_scope or f"pane-ask-{target}"

    cmd = [PANE_ASK_BIN]
    if ssh:
        cmd += ["--ssh", ssh]
    if wait:
        cmd.append("--wait")
    cmd += [target, "-"]

    last_rc = -1
    last_stderr = ""
    attempts = 0

    for attempt in range(retries + 1):
        attempts = attempt + 1
        if attempt > 0:
            backoff = 2 ** attempt
            time.sleep(backoff)

        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            last_rc = proc.returncode
            last_stderr = proc.stderr or ""

            if last_rc == 0:
                return {
                    "ok": True,
                    "stdout": proc.stdout or "",
                    "stderr": last_stderr,
                    "rc": 0,
                    "attempts": attempts,
                }

            if last_rc in _TERMINAL_EXIT_CODES:
                break

            if last_rc not in _TRANSIENT_EXIT_CODES:
                break

        except subprocess.TimeoutExpired:
            last_rc = -1
            last_stderr = f"timeout after {timeout}s"
            break
        except Exception as exc:
            doctor_escalate(
                watcher=watcher,
                severity="critical",
                summary=f"pane-ask-v2 to {target} raised exception: {exc}",
                context={
                    "target": target,
                    "ssh": ssh,
                    "exception": str(exc),
                    "attempts": attempts,
                },
                dedup_scope=scope,
            )
            return {
                "ok": False,
                "rc": -1,
                "stderr": str(exc),
                "attempts": attempts,
                "escalated": True,
            }

    doctor_escalate(
        watcher=watcher,
        severity="warn",
        summary=f"pane-ask-v2 to {target} failed after {attempts} attempt(s): rc={last_rc}",
        context={
            "target": target,
            "ssh": ssh,
            "rc": last_rc,
            "stderr_tail": last_stderr[-500:],
            "attempts": attempts,
        },
        dedup_scope=scope,
    )
    return {
        "ok": False,
        "rc": last_rc,
        "stderr": last_stderr,
        "attempts": attempts,
        "escalated": True,
    }


def dispatched_sdk_agent(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str = "opus",
    max_turns: int = 30,
    watcher_name: str = "swarm-sdk",
    retries: int = 1,
    dedup_scope: Optional[str] = None,
) -> dict:
    """Wrap a claude_agent_sdk query() call with retry and doctor escalation.

    If claude_agent_sdk is not installed, returns a skipped sentinel dict
    without escalating — SDK absence is a deployment concern, not a pager event.

    Args:
        system_prompt: agent system prompt.
        user_prompt: the user-turn prompt.
        model: short model name ("sonnet", "opus", "haiku").
        max_turns: passed to the SDK options.
        watcher_name: identifies this caller in doctor escalations.
        retries: max additional attempts after first failure.
        dedup_scope: 6h dedup key. Defaults to "sdk-{watcher_name}".

    Returns:
        {"ok": True, "result": <sdk output>, "attempts": int}
        {"ok": False, "error": str, "attempts": int, "escalated": bool}
        {"ok": False, "skipped": True, "reason": str}  -- SDK not installed
    """
    scope = dedup_scope or f"sdk-{watcher_name}"

    try:
        from core.mac_sdk import ClaudeAgentOptions, query
        from core.thinking import STANDARD
        from core.hooks import AGENT_HOOKS
    except ImportError:
        return {
            "ok": False,
            "skipped": True,
            "reason": "claude_agent_sdk not installed",
        }

    model_map = {
        "sonnet": "claude-sonnet-4-6",
        "opus": "claude-opus-4-7",
        "haiku": "claude-haiku-4-5",
    }
    resolved_model = model_map.get(model, model)

    last_error = ""
    attempts = 0

    for attempt in range(retries + 1):
        attempts = attempt + 1
        if attempt > 0:
            time.sleep(2 ** attempt)

        try:
            import asyncio

            async def _run():
                options = ClaudeAgentOptions(
                    model=resolved_model,
                    max_turns=max_turns,
                    system_prompt=system_prompt,
                    permission_mode="bypassPermissions",
                    cwd=str(Path.home()),
                    hooks=AGENT_HOOKS,
                    thinking=STANDARD,
                    effort="max",
                )
                result_parts = []
                async for event in query(user_prompt, options=options):
                    result_parts.append(event)
                return result_parts

            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    raise RuntimeError("closed")
                result = loop.run_until_complete(_run())
            except RuntimeError:
                result = asyncio.run(_run())

            if result is not None:
                return {"ok": True, "result": result, "attempts": attempts}

            last_error = "query returned None/empty"

        except (AttributeError, TypeError) as exc:
            last_error = f"SDK API mismatch: {exc}"
            break
        except Exception as exc:
            last_error = str(exc)

    doctor_escalate(
        watcher=watcher_name,
        severity="warn",
        summary=f"swarm SDK call failed after {attempts} attempt(s): {last_error[:120]}",
        context={
            "model": resolved_model,
            "exception": last_error,
            "system_prompt_head": system_prompt[:200],
            "attempts": attempts,
        },
        dedup_scope=scope,
    )
    return {
        "ok": False,
        "error": last_error,
        "attempts": attempts,
        "escalated": True,
    }


def escalate_swarm_failure(
    watcher_name: str,
    summary: str,
    context: Optional[dict] = None,
    severity: str = "warn",
    dedup_scope: Optional[str] = None,
) -> bool:
    """Thin wrapper over doctor_escalate for the LLM-driven Agent() flow case.

    Dispatchers can call this from Bash:
        python3 -c "
          from core.swarm_dispatch import escalate_swarm_failure
          escalate_swarm_failure('swarm-handler-rerun',
                                 'scout returned partial 3x in 10min',
                                 {'attempts': 3, 'last_error': '...'})
        "

    Args:
        watcher_name: short identifier, e.g. "swarm-research-chain".
        summary: one-line human-readable description.
        context: structured detail dict (no secret-pattern keys).
        severity: "ok" | "notice" | "warn" | "critical".
        dedup_scope: 6h dedup key. Defaults to "swarm-{watcher_name}".

    Returns:
        True if doctor_escalate dispatched, False if deduped/failed/rate-limited.
    """
    scope = dedup_scope or f"swarm-{watcher_name}"
    result = doctor_escalate(
        watcher=watcher_name,
        severity=severity,
        summary=summary,
        context=context,
        dedup_scope=scope,
    )
    return bool(result.get("dispatched"))


def _cli_escalate(args: argparse.Namespace) -> int:
    try:
        ctx = json.loads(args.context_json) if args.context_json else {}
    except json.JSONDecodeError as exc:
        print(f"error: --context-json is not valid JSON: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(
            f"would escalate: watcher={args.watcher} severity={args.severity} "
            f"summary={args.summary!r} context={ctx} dedup_scope={args.dedup_scope!r}"
        )
        return 0

    ok = escalate_swarm_failure(
        watcher_name=args.watcher,
        summary=args.summary,
        context=ctx,
        severity=args.severity,
        dedup_scope=args.dedup_scope,
    )
    print("dispatched" if ok else "deduped or failed (see doctor log)")
    return 0


def _cli_pane_ask(args: argparse.Namespace) -> int:
    result = dispatched_pane_ask(
        args.target,
        args.prompt,
        ssh=args.ssh,
        wait=args.wait,
        timeout=args.timeout,
        retries=args.retries,
        watcher_name=args.watcher,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core.swarm_dispatch",
        description="Swarm dispatch helpers with doctor escalation on failure.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    esc = sub.add_parser("escalate", help="Escalate a swarm failure to the doctor pane.")
    esc.add_argument("--watcher", required=True, help="Short watcher identifier.")
    esc.add_argument("--severity", default="warn",
                     choices=["ok", "notice", "warn", "error", "critical"],
                     help="Alert severity (default: warn).")
    esc.add_argument("--summary", required=True, help="One-line human-readable summary.")
    esc.add_argument("--context-json", default=None,
                     help="JSON object with structured context (optional).")
    esc.add_argument("--dedup-scope", default=None,
                     help="6h dedup key (defaults to swarm-<watcher>).")
    esc.add_argument("--dry-run", action="store_true",
                     help="Print what would be escalated without calling doctor_escalate.")
    esc.set_defaults(func=_cli_escalate)

    pa = sub.add_parser("pane-ask", help="Send a prompt to a pane via pane-ask-v2.")
    pa.add_argument("target", help="tmux pane target, e.g. claude:2")
    pa.add_argument("prompt", help="Prompt text to send.")
    pa.add_argument("--ssh", default=None, help="SSH host for cross-host dispatch.")
    pa.add_argument("--wait", action="store_true", help="Block until end_turn.")
    pa.add_argument("--timeout", type=int, default=300, help="Per-attempt timeout (s).")
    pa.add_argument("--retries", type=int, default=2, help="Max retry attempts.")
    pa.add_argument("--watcher", default=None, help="Override watcher name for escalations.")
    pa.set_defaults(func=_cli_pane_ask)

    return parser


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))
