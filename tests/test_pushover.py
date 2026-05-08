import asyncio

from core.pushover import _build_payload


def test_pushover_emergency_payload_adds_retry_and_expire():
    payload = _build_payload(
        token="token",
        user="user",
        title="Alarm",
        message="Wake up",
        priority=2,
    )

    assert payload["priority"] == 2
    assert payload["retry"] == 60
    assert payload["expire"] == 1800


def test_pushover_payload_clips_title_and_message():
    payload = _build_payload(
        token="token",
        user="user",
        title="T" * 300,
        message="M" * 1100,
        priority=9,
        sound="updown",
        url="https://example.com",
        url_title="Example",
    )

    assert payload["priority"] == 2
    assert len(payload["title"]) == 250
    assert payload["title"].endswith("...")
    assert len(payload["message"]) == 1024
    assert payload["message"].endswith("...")
    assert payload["sound"] == "updown"
    assert payload["url"] == "https://example.com"
    assert payload["url_title"] == "Example"


def test_send_pushover_reports_current_voice_reroute_target(monkeypatch):
    from core import voice_reroute
    from core.pushover import send_pushover

    monkeypatch.setattr(voice_reroute, "voice_reroute_send", lambda *_args: True)

    result = asyncio.run(send_pushover(title="Alarm", message="Wake up"))

    assert result.ok is True
    assert result.detail == "rerouted to voice (claude:9)"
