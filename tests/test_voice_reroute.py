from __future__ import annotations

import subprocess

from core import voice_reroute


def test_voice_target_pane_tracks_current_voice_pane():
    assert voice_reroute.VOICE_TARGET_PANE == "claude:9"


def test_voice_reroute_send_dispatches_to_current_voice_pane(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setenv("PUSHOVER_TO_VOICE", "1")
    monkeypatch.setattr(voice_reroute, "VOICE_QUEUE_LOG", tmp_path / "queue.log")
    monkeypatch.setattr(voice_reroute.socket, "gethostname", lambda: "test-host")
    monkeypatch.setattr(voice_reroute.sys, "platform", "darwin")
    monkeypatch.setattr(voice_reroute.subprocess, "run", fake_run)

    assert voice_reroute.voice_reroute_send(
        "Alarm",
        "Wake up\nnow",
        priority=1,
        url="https://example.com",
        url_title="Details",
    )

    assert calls
    cmd = calls[0]
    assert cmd[-2] == voice_reroute.VOICE_TARGET_PANE
    assert "P1" in (tmp_path / "queue.log").read_text()
    assert "Wake up / now" in (tmp_path / "queue.log").read_text()
