import os

_SCRUBBED_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
_SAVED_ENV = {key: os.environ.get(key) for key in _SCRUBBED_ENV_VARS}

from home_ops import engine as home_engine  # noqa: E402

for _key, _value in _SAVED_ENV.items():
    if _value is not None:
        os.environ[_key] = _value


def test_mode_lock_blocks_overlapping_same_mode_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(home_engine, "HOME", tmp_path)

    with home_engine._mode_lock("morning") as first:
        assert first is True
        with home_engine._mode_lock("morning") as second:
            assert second is False

    with home_engine._mode_lock("morning") as after_release:
        assert after_release is True
