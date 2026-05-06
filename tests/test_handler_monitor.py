import asyncio
import importlib
import sys
from datetime import datetime


def _load_monitor(monkeypatch):
    sys.modules.pop("handler.monitor", None)
    import core.vault as vault

    monkeypatch.setattr(vault, "hydrate_env", lambda *args, **kwargs: {})
    return importlib.import_module("handler.monitor")


def test_publish_stack_alert_builds_handler_anomaly_payload(monkeypatch):
    monitor = _load_monitor(monkeypatch)
    events = []

    def fake_event(agent, kind, payload=None, **_kwargs):
        events.append((agent, kind, payload))
        return 123

    monkeypatch.setattr(monitor.cp, "event", fake_event)

    ok = monitor._publish_stack_alert(
        "Handler: 1 issue",
        "Service down: cp-api",
        anomalies=[{"severity": "high", "source": "vps", "message": "Service down: cp-api"}],
        diagnosis="cp-api is down",
        fingerprint="abc123",
    )

    assert ok is True
    assert len(events) == 1
    agent, kind, payload = events[0]
    assert agent == "handler-agent"
    assert kind == "anomaly"
    assert payload["kind"] == "anomaly"
    assert payload["priority"] == 2
    assert payload["sources"] == ("ui",)
    assert payload["fingerprint"] == "abc123"
    assert payload["diagnosis"] == "cp-api is down"
    assert payload["anomalies"] == [
        {"severity": "high", "source": "vps", "message": "Service down: cp-api"}
    ]


def test_deliver_actionable_alert_uses_stack_first_without_outbound(monkeypatch):
    monitor = _load_monitor(monkeypatch)
    anomaly = {"severity": "high", "source": "vps", "message": "Service down: cp-api"}
    stack_calls = []
    pushes = []
    emails = []
    saved = []

    def fake_stack(*args, **kwargs):
        stack_calls.append((args, kwargs))
        return True

    async def fake_push(title, message):
        pushes.append((title, message))

    async def fake_email(anomalies, data):
        emails.append((anomalies, data))

    monkeypatch.setattr(monitor, "_publish_stack_alert", fake_stack)
    monkeypatch.setattr(monitor, "send_pushover", fake_push)
    monkeypatch.setattr(monitor, "send_alert_email", fake_email)
    monkeypatch.setattr(monitor, "save_state", lambda state: saved.append(state))

    delivered = asyncio.run(monitor._deliver_actionable_alert(
        title="Handler: 1 issue",
        message="Service down: cp-api",
        anomalies=[anomaly],
        data={"vps": {"raw": "sample"}},
        diagnosis="cp-api is down",
        fingerprint="abc123",
        state={"alert_count": 4},
        now=datetime(2026, 5, 3, 12, 0),
        high=[anomaly],
        should_email=True,
        skip_reason=None,
    ))

    assert delivered is True
    assert len(stack_calls) == 1
    assert pushes == [("Handler: 1 issue", "Service down: cp-api")]
    assert emails == [([anomaly], {"vps": {"raw": "sample"}})]
    assert saved == [{
        "last_alert_hash": "abc123",
        "last_alert_ts": "2026-05-03T12:00:00",
        "alert_count": 5,
        "anomalies": [anomaly],
    }]


def test_deliver_actionable_alert_keeps_quiet_path_stack_silent(monkeypatch):
    monitor = _load_monitor(monkeypatch)
    anomaly = {"severity": "high", "source": "vps", "message": "Service down: cp-api"}
    pushes = []

    def fake_stack(*_args, **_kwargs):
        raise AssertionError("quiet/dedup path should not publish Stack rows")

    async def fake_push(title, message):
        pushes.append((title, message))

    async def fake_email(*_args, **_kwargs):
        raise AssertionError("email should remain skipped on quiet/dedup path")

    monkeypatch.setattr(monitor, "_publish_stack_alert", fake_stack)
    monkeypatch.setattr(monitor, "send_pushover", fake_push)
    monkeypatch.setattr(monitor, "send_alert_email", fake_email)
    monkeypatch.setattr(
        monitor,
        "save_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("state should not save")),
    )

    delivered = asyncio.run(monitor._deliver_actionable_alert(
        title="Handler: 1 issue",
        message="Service down: cp-api",
        anomalies=[anomaly],
        data={},
        diagnosis="",
        fingerprint="abc123",
        state={"alert_count": 4},
        now=datetime(2026, 5, 3, 23, 0),
        high=[anomaly],
        should_email=False,
        skip_reason="quiet hours (22:00-7:00)",
    ))

    assert delivered is False
    assert pushes == [("Handler: 1 issue", "Service down: cp-api")]


def test_deliver_actionable_alert_falls_back_to_existing_push_email(monkeypatch):
    monitor = _load_monitor(monkeypatch)
    anomaly = {"severity": "high", "source": "vps", "message": "Service down: cp-api"}
    pushes = []
    emails = []
    saved = []

    async def fake_push(title, message):
        pushes.append((title, message))

    async def fake_email(anomalies, data):
        emails.append((anomalies, data))

    monkeypatch.setattr(monitor, "_publish_stack_alert", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(monitor, "send_pushover", fake_push)
    monkeypatch.setattr(monitor, "send_alert_email", fake_email)
    monkeypatch.setattr(monitor, "save_state", lambda state: saved.append(state))

    delivered = asyncio.run(monitor._deliver_actionable_alert(
        title="Handler: 1 issue",
        message="Service down: cp-api",
        anomalies=[anomaly],
        data={"vps": {"raw": "sample"}},
        diagnosis="",
        fingerprint="abc123",
        state={"alert_count": 4},
        now=datetime(2026, 5, 3, 12, 0),
        high=[anomaly],
        should_email=True,
        skip_reason=None,
    ))

    assert delivered is False
    assert pushes == [("Handler: 1 issue", "Service down: cp-api")]
    assert emails == [([anomaly], {"vps": {"raw": "sample"}})]
    assert saved == [{
        "last_alert_hash": "abc123",
        "last_alert_ts": "2026-05-03T12:00:00",
        "alert_count": 5,
        "anomalies": [anomaly],
    }]


def test_quick_check_posts_silent_heartbeat_when_clear(monkeypatch):
    monitor = _load_monitor(monkeypatch)
    heartbeats = []

    async def fake_gather_all(force=False):
        assert force is False
        return {"vps": {"raw": ""}}

    async def fake_heartbeat(*, ok=True):
        heartbeats.append(ok)
        return True

    async def fail_push(*_args, **_kwargs):
        raise AssertionError("clear check should not push")

    monkeypatch.setattr(monitor, "gather_all", fake_gather_all)
    monkeypatch.setattr(monitor, "post_handler_heartbeat", fake_heartbeat)
    monkeypatch.setattr(monitor, "send_pushover", fail_push)
    monkeypatch.setattr(monitor, "_publish_stack_alert", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("clear check should not publish Stack")))

    asyncio.run(monitor.quick_check(dry_run=False))

    assert heartbeats == [True]
