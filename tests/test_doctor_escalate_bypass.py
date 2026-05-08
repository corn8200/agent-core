from core import doctor_escalate


def test_bypass_push_uses_overseer_voice_wording(monkeypatch):
    sent = {}

    monkeypatch.setattr(doctor_escalate, "_record_bypass", lambda _redis, _host: 3)
    monkeypatch.setattr(doctor_escalate, "_log_event", lambda _event: None)

    def fake_push(title, message, priority=0, *, url=None, url_title=None):
        sent.update(
            title=title,
            message=message,
            priority=priority,
            url=url,
            url_title=url_title,
        )
        return True

    monkeypatch.setattr(doctor_escalate, "_pushover_direct", fake_push)

    doctor_escalate._deliver_bypass(
        "watcher",
        "warn",
        "summary",
        "briefing",
        "rc=9 stderr=unknown target",
        None,
        None,
        "fingerprint",
        "vps",
    )

    assert sent["title"].startswith("[OVERSEER-VOICE-BYPASS-vps]")
    assert "doctor" not in sent["title"].lower()
    assert "doctor" not in sent["message"].lower()
    assert "Overseer Voice route failed" in sent["message"]
    assert "appears DOWN" not in sent["message"]
    assert sent["priority"] == 1
