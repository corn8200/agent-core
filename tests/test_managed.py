from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from core import managed


class _Events:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, *, session_id, events, betas):
        self.sent.append({"session_id": session_id, "events": events, "betas": betas})

    def stream(self, *, session_id, betas):
        del session_id, betas
        return nullcontext(iter([
            SimpleNamespace(
                type="agent.message",
                content=[SimpleNamespace(text="pilot-ok")],
            ),
            SimpleNamespace(type="session.status_idle"),
        ]))


class _Sessions:
    def __init__(self) -> None:
        self.events = _Events()
        self.archived: list[str] = []

    def create(self, **kwargs):
        self.create_kwargs = kwargs
        return SimpleNamespace(id="session-canary")

    def archive(self, session_id, *, betas):
        self.archived.append(session_id)


class _Client:
    def __init__(self) -> None:
        self.beta = SimpleNamespace(
            environments=SimpleNamespace(
                list=lambda **kwargs: [],
                create=lambda **kwargs: SimpleNamespace(id="environment-canary"),
            ),
            agents=SimpleNamespace(
                create=lambda **kwargs: SimpleNamespace(id="agent-canary"),
            ),
            sessions=_Sessions(),
        )


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv(managed.ENABLE_ENV, raising=False)
    monkeypatch.setenv("ANTHROPIC_CONSOLE_KEY", "not-used")
    with pytest.raises(RuntimeError, match="disabled by default"):
        managed._client()


def test_enabled_requires_billing_credential(monkeypatch):
    monkeypatch.setenv(managed.ENABLE_ENV, "1")
    monkeypatch.delenv("ANTHROPIC_CONSOLE_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="is required"):
        managed._client()


def test_capability_probe_is_local_and_default_off(monkeypatch):
    monkeypatch.delenv(managed.ENABLE_ENV, raising=False)
    result = managed.managed_agents_capability_probe()
    assert result["available"] is True
    assert result["default_enabled"] is False
    assert result["network_calls"] == 0
    assert set(result["methods"]) == {
        "agents.create",
        "environments.list",
        "environments.create",
        "sessions.create",
        "sessions.events.send",
        "sessions.events.stream",
        "sessions.archive",
    }


def test_bounded_managed_query_pilot_with_fake_transport(monkeypatch):
    fake = _Client()
    managed._AGENT_CACHE.clear()
    managed._ENV_CACHE.clear()
    monkeypatch.setattr(managed, "_client", lambda: fake)

    text, stats = asyncio.run(managed.managed_query_verbose(
        "Return exactly pilot-ok",
        model="sonnet",
        agent_name="managed-pilot",
        environment="managed-pilot",
        title="disabled-by-default canary",
    ))

    assert text == "pilot-ok"
    assert stats == {"events": 2, "session_id": "session-canary"}
    assert fake.beta.sessions.archived == ["session-canary"]
    assert fake.beta.sessions.events.sent[0]["events"][0]["type"] == "user.message"
