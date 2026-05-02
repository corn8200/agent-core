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
