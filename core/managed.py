"""Managed Agents wrapper — drop-in alternative to claude_agent_sdk.query().

Why this exists:
    Some workflows (esp. code exec, PDF rendering, CSV analysis, anything needing
    a real container with state) benefit from Anthropic's hosted Managed Agents
    runtime over the in-process Claude Agent SDK. This module exposes a thin
    async helper that matches the shape of a single SDK pass so any pure
    text-in/text-out pass can be swapped over with a one-line change.

    For pure-LLM passes there's no advantage — that's an intentional finding of
    the 2026-04-09 benchmark (see docs/managed_vs_sdk.md). Use this for
    workflows that actually need the container.

Requires: anthropic>=0.92.0, ANTHROPIC_API_KEY in env.
Beta header: managed-agents-2026-04-01.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

BETAS = ["managed-agents-2026-04-01"]

_AGENT_CACHE: dict[tuple[str, str, str], str] = {}  # (name, model, system) -> agent_id
_ENV_CACHE: dict[str, str] = {}  # name -> environment_id

_MODEL_ALIASES = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


def _client() -> anthropic.Anthropic:
    raise RuntimeError(
        "OpJune PR2 disabled the raw API-billed Managed Agents path before "
        "2026-06-15; route through the gated broker after SDK credit cutover."
    )


def _ensure_environment(client: anthropic.Anthropic, name: str = "agent-core-default") -> str:
    """Return an environment_id, creating one if needed. Cached in-process."""
    if name in _ENV_CACHE:
        return _ENV_CACHE[name]
    for env in client.beta.environments.list(betas=BETAS):
        if env.name == name and env.archived_at is None:
            _ENV_CACHE[name] = env.id
            return env.id
    env = client.beta.environments.create(name=name, betas=BETAS)
    _ENV_CACHE[name] = env.id
    return env.id


def _ensure_agent(
    client: anthropic.Anthropic,
    name: str,
    model: str,
    system: str,
) -> str:
    """Return an agent_id, reusing by (name, model, system) triple."""
    key = (name, model, system)
    if key in _AGENT_CACHE:
        return _AGENT_CACHE[key]
    agent = client.beta.agents.create(
        name=name,
        model=_MODEL_ALIASES.get(model, model),
        system=system,
        betas=BETAS,
    )
    _AGENT_CACHE[key] = agent.id
    return agent.id


def _run_turn(
    client: anthropic.Anthropic,
    agent_id: str,
    env_id: str,
    prompt: str,
    title: str,
) -> tuple[str, dict]:
    """Blocking helper: send a single user turn, collect the assistant reply."""
    sess = client.beta.sessions.create(
        agent=agent_id,
        environment_id=env_id,
        title=title[:80] or "managed-pass",
        betas=BETAS,
    )

    text_chunks: list[str] = []
    stats = {"events": 0, "session_id": sess.id}

    # Stream first, then send — otherwise you can deadlock waiting on a turn
    # that hasn't started. Use a thread to fire the send after the stream opens.
    import threading

    send_done = threading.Event()
    send_err: list[Exception] = []

    def _send():
        try:
            client.beta.sessions.events.send(
                session_id=sess.id,
                events=[{
                    "type": "user.message",
                    "content": [{"type": "text", "text": prompt}],
                }],
                betas=BETAS,
            )
        except Exception as e:
            send_err.append(e)
        finally:
            send_done.set()

    t = threading.Thread(target=_send, daemon=True)
    t.start()

    with client.beta.sessions.events.stream(session_id=sess.id, betas=BETAS) as stream:
        for event in stream:
            stats["events"] += 1
            et = getattr(event, "type", "")
            if et == "agent.message":
                content = getattr(event, "content", []) or []
                for block in content:
                    text = getattr(block, "text", None)
                    if text:
                        text_chunks.append(text)
            elif et == "session.status_idle":
                break
            elif et.startswith("session.status_terminated") or et == "session.error":
                break

    if send_err:
        raise send_err[0]

    # Archive session to keep account clean (agents/env persist for reuse)
    try:
        client.beta.sessions.archive(sess.id, betas=BETAS)
    except Exception:
        pass

    return "".join(text_chunks).strip(), stats


async def managed_query(
    prompt: str,
    *,
    model: str = "opus",
    system: str = "You are a helpful research assistant. Respond concisely and accurately.",
    agent_name: str = "agent-core-pass",
    environment: str = "agent-core-default",
    title: Optional[str] = None,
) -> str:
    """Run a single-turn managed-agents pass. Drop-in shape for sdk_pass().

    Returns the assistant's text response. Raises on API errors.
    """
    assert (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_CONSOLE_KEY")), \
        "ANTHROPIC_CONSOLE_KEY not set (Managed Agents requires raw API billing)"

    def _work() -> str:
        client = _client()
        env_id = _ensure_environment(client, environment)
        agent_id = _ensure_agent(client, agent_name, model, system)
        text, _ = _run_turn(client, agent_id, env_id, prompt, title or agent_name)
        return text

    return await asyncio.to_thread(_work)


async def managed_query_verbose(
    prompt: str,
    *,
    model: str = "opus",
    system: str = "You are a helpful research assistant.",
    agent_name: str = "agent-core-pass",
    environment: str = "agent-core-default",
    title: Optional[str] = None,
) -> tuple[str, dict]:
    """Same as managed_query but also returns stats dict (events, session_id)."""
    assert (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_CONSOLE_KEY")), \
        "ANTHROPIC_CONSOLE_KEY not set (Managed Agents requires raw API billing)"

    def _work() -> tuple[str, dict]:
        client = _client()
        env_id = _ensure_environment(client, environment)
        agent_id = _ensure_agent(client, agent_name, model, system)
        return _run_turn(client, agent_id, env_id, prompt, title or agent_name)

    return await asyncio.to_thread(_work)
