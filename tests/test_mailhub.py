"""Tests for core.mailhub token resolution chain."""

from __future__ import annotations

import core.mailhub as mailhub
import pytest


@pytest.fixture(autouse=True)
def reset_token_state(monkeypatch):
    """Each test starts with a clean cache + env so resolution chain is deterministic."""

    monkeypatch.setattr(mailhub, "_token_cache", None)
    monkeypatch.delenv("MAILHUB_TOKEN", raising=False)
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    yield


def test_resolves_from_env_var_when_present(monkeypatch):
    monkeypatch.setenv("MAILHUB_TOKEN", "env-token-abc")
    assert mailhub._resolve_token() == "env-token-abc"
    assert mailhub._token_cache == ("env-token-abc", "env:MAILHUB_TOKEN")


def test_falls_through_to_first_existing_env_file(tmp_path, monkeypatch):
    primary = tmp_path / "first.env"
    secondary = tmp_path / "second.env"
    secondary.write_text("MAILHUB_TOKEN=from-second-file\n")
    monkeypatch.setattr(mailhub, "_TOKEN_ENV_FILES", (primary, secondary))
    monkeypatch.setattr(mailhub, "_read_token_from_1password", lambda: None)

    token = mailhub._resolve_token()

    assert token == "from-second-file"
    assert mailhub._token_cache == ("from-second-file", f"file:{secondary}")


def test_first_existing_file_wins_over_later_files(tmp_path, monkeypatch):
    primary = tmp_path / "first.env"
    secondary = tmp_path / "second.env"
    primary.write_text("MAILHUB_TOKEN=primary-wins\n")
    secondary.write_text("MAILHUB_TOKEN=should-not-be-read\n")
    monkeypatch.setattr(mailhub, "_TOKEN_ENV_FILES", (primary, secondary))

    assert mailhub._resolve_token() == "primary-wins"


def test_strips_quotes_and_comments_from_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / "quoted.env"
    env_file.write_text(
        "# comment line\n"
        "\n"
        'MAILHUB_TOKEN="quoted-value"\n'
        "OTHER_VAR=ignored\n"
    )
    monkeypatch.setattr(mailhub, "_TOKEN_ENV_FILES", (env_file,))
    monkeypatch.setattr(mailhub, "_read_token_from_1password", lambda: None)

    assert mailhub._resolve_token() == "quoted-value"


def test_falls_back_to_1password_when_no_files_match(tmp_path, monkeypatch):
    nonexistent = tmp_path / "missing.env"
    monkeypatch.setattr(mailhub, "_TOKEN_ENV_FILES", (nonexistent,))
    monkeypatch.setattr(mailhub, "_read_token_from_1password", lambda: "from-1password")

    token = mailhub._resolve_token()

    assert token == "from-1password"
    assert mailhub._token_cache == (
        "from-1password",
        f"1password:{mailhub._OP_REF}",
    )


def test_returns_none_when_chain_exhausted(tmp_path, monkeypatch):
    monkeypatch.setattr(mailhub, "_TOKEN_ENV_FILES", (tmp_path / "absent.env",))
    monkeypatch.setattr(mailhub, "_read_token_from_1password", lambda: None)

    assert mailhub._resolve_token() is None


def test_resolve_token_or_raise_raises_with_full_chain(tmp_path, monkeypatch):
    files = (tmp_path / "a.env", tmp_path / "b.env")
    monkeypatch.setattr(mailhub, "_TOKEN_ENV_FILES", files)
    monkeypatch.setattr(mailhub, "_read_token_from_1password", lambda: None)

    with pytest.raises(mailhub.MailhubAuthError) as excinfo:
        mailhub._resolve_token_or_raise()

    err = excinfo.value
    assert err.status_code == 401
    assert err.sources_tried == [
        "env:MAILHUB_TOKEN",
        f"file:{files[0]}",
        f"file:{files[1]}",
        f"1password:{mailhub._OP_REF}",
    ]
    msg = str(err)
    assert "no mailhub token resolved" in msg
    assert "env:MAILHUB_TOKEN" in msg
    assert mailhub._OP_REF in msg


def test_mailhubautherror_subclasses_mailhuberror():
    """Existing `except MailhubError:` blocks should still catch the new error."""

    err = mailhub.MailhubAuthError(["env:MAILHUB_TOKEN"])
    assert isinstance(err, mailhub.MailhubError)


def test_token_cache_avoids_repeated_resolution(tmp_path, monkeypatch):
    """Calls after the first should not re-walk the chain."""

    env_file = tmp_path / "cached.env"
    env_file.write_text("MAILHUB_TOKEN=cached-token\n")
    monkeypatch.setattr(mailhub, "_TOKEN_ENV_FILES", (env_file,))

    call_count = {"n": 0}

    def fake_op() -> str | None:
        call_count["n"] += 1
        return None

    monkeypatch.setattr(mailhub, "_read_token_from_1password", fake_op)

    assert mailhub._resolve_token() == "cached-token"
    env_file.unlink()
    assert mailhub._resolve_token() == "cached-token"
    assert mailhub._resolve_token() == "cached-token"
    assert call_count["n"] == 0


def test_force_refresh_skips_cache(tmp_path, monkeypatch):
    env_file = tmp_path / "refresh.env"
    env_file.write_text("MAILHUB_TOKEN=v1\n")
    monkeypatch.setattr(mailhub, "_TOKEN_ENV_FILES", (env_file,))
    monkeypatch.setattr(mailhub, "_read_token_from_1password", lambda: None)

    assert mailhub._resolve_token() == "v1"
    env_file.write_text("MAILHUB_TOKEN=v2\n")
    assert mailhub._resolve_token() == "v1"
    assert mailhub._resolve_token(force_refresh=True) == "v2"


def test_corrupt_env_file_falls_through(tmp_path, monkeypatch):
    """A file that can't be parsed shouldn't crash the chain."""

    bad = tmp_path / "bad.env"
    bad.write_bytes(b"\xff\xfe\x00invalid")
    good = tmp_path / "good.env"
    good.write_text("MAILHUB_TOKEN=fallback-ok\n")
    monkeypatch.setattr(mailhub, "_TOKEN_ENV_FILES", (bad, good))
    monkeypatch.setattr(mailhub, "_read_token_from_1password", lambda: None)

    assert mailhub._resolve_token() == "fallback-ok"


def test_empty_token_value_falls_through(tmp_path, monkeypatch):
    """MAILHUB_TOKEN= should be treated as missing."""

    empty_file = tmp_path / "empty.env"
    empty_file.write_text("MAILHUB_TOKEN=\n")
    backup = tmp_path / "backup.env"
    backup.write_text("MAILHUB_TOKEN=actual-value\n")
    monkeypatch.setattr(mailhub, "_TOKEN_ENV_FILES", (empty_file, backup))
    monkeypatch.setattr(mailhub, "_read_token_from_1password", lambda: None)

    assert mailhub._resolve_token() == "actual-value"
