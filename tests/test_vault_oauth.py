import os
from pathlib import Path

from core import vault


def _write_account(tmp_path: Path, account: str) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".config").mkdir()
    (tmp_path / ".claude" / ".active-account").write_text(account)
    (tmp_path / ".config" / "claude-oat01-gmail").write_text("gmail-token")
    (tmp_path / ".config" / "claude-oat01-icloud").write_text("icloud-token")


def test_hydrate_claude_oauth_replaces_stale_env(monkeypatch, tmp_path):
    _write_account(tmp_path, "icloud")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "gmail-token")

    assert vault.hydrate_claude_oauth() is True
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "icloud-token"
    assert os.environ["CLAUDE_PANE_ACCOUNT"] == "icloud"


def test_hydrate_claude_oauth_refuses_missing_active_marker(monkeypatch, tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".config").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "stale-token")

    assert vault.hydrate_claude_oauth() is False
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ
