"""Transport-layer tests for core/imessage_triage/classify.py.

These tests specifically verify:
  1. core.mac_sdk.query IS the transport (called on every classify)
  2. httpx.post is NOT called (no direct API path)
  3. The module imports cleanly with no env-var errors
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_query(response_json: str):
    """Async generator that yields one AssistantMessage with TextBlock."""
    from core.mac_sdk import AssistantMessage, TextBlock

    async def _gen(prompt, options=None, **kwargs):
        msg = MagicMock(spec=AssistantMessage)
        block = MagicMock(spec=TextBlock)
        block.text = response_json
        msg.content = [block]
        yield msg

    return _gen


# ---------------------------------------------------------------------------
# Critical transport assertions
# ---------------------------------------------------------------------------


def test_mac_sdk_query_is_called(monkeypatch):
    """classify_thread MUST go through core.mac_sdk.query — not httpx."""
    from core.imessage_triage import classify as classify_mod

    sdk_calls = []

    async def spy_query(prompt, options=None, **kwargs):
        sdk_calls.append(prompt)
        from core.mac_sdk import AssistantMessage, TextBlock
        msg = MagicMock(spec=AssistantMessage)
        block = MagicMock(spec=TextBlock)
        block.text = '{"category": "social", "urgency": 2}'
        msg.content = [block]
        yield msg

    monkeypatch.setattr(classify_mod, "query", spy_query)

    result = asyncio.run(classify_mod.classify_thread("+13045551234", ["[Them] Hey, how's it going?"]))

    assert sdk_calls, "core.mac_sdk.query was never called — transport is wrong"
    assert result["category"] == "social"
    assert result["urgency"] == 2


def test_httpx_post_is_never_called(monkeypatch):
    """httpx.post must NOT be called from classify_thread."""
    from core.imessage_triage import classify as classify_mod

    fake = _make_fake_query('{"category": "noise", "urgency": 1}')
    monkeypatch.setattr(classify_mod, "query", fake)

    httpx_calls = []
    with patch("httpx.post", side_effect=lambda *a, **kw: httpx_calls.append((a, kw))):
        asyncio.run(classify_mod.classify_thread("+13045551234", ["[Them] Spam message"]))

    assert not httpx_calls, f"httpx.post was called {len(httpx_calls)} time(s) — API billing leak"


def test_no_anthropic_api_key_usage(monkeypatch):
    """classify_thread must not call get_secret for ANTHROPIC_API_KEY."""
    from core.imessage_triage import classify as classify_mod

    fake = _make_fake_query('{"category": "noise", "urgency": 1}')
    monkeypatch.setattr(classify_mod, "query", fake)

    secret_calls = []

    def spy_get_secret(key, **kwargs):
        secret_calls.append(key)
        return "fake"

    # patch in case get_secret is still imported anywhere in classify
    with patch("core.vault.get_secret", side_effect=spy_get_secret):
        asyncio.run(classify_mod.classify_thread("+13045551234", ["test"]))

    api_key_calls = [k for k in secret_calls if "ANTHROPIC_API_KEY" in k]
    assert not api_key_calls, f"get_secret('ANTHROPIC_API_KEY') called — billing leak: {api_key_calls}"


def test_module_imports_cleanly():
    """from core.imessage_triage import classify must exit 0 with no env-var errors."""
    import importlib
    import core.imessage_triage.classify as m
    importlib.reload(m)  # force re-import even if cached
    assert hasattr(m, "classify_thread")
    assert hasattr(m, "VALID_CATEGORIES")
    assert hasattr(m, "_parse_response")


def test_classify_uses_mac_sdk_query_not_direct_anthropic_url():
    """The module must not contain the string 'api.anthropic.com' or ANTHROPIC_MESSAGES_URL."""
    classify_path = Path(__file__).parent.parent / "core" / "imessage_triage" / "classify.py"
    source = classify_path.read_text()
    assert "api.anthropic.com" not in source, "Direct Anthropic URL found in classify.py — must use mac_sdk"
    assert "ANTHROPIC_MESSAGES_URL" not in source, "ANTHROPIC_MESSAGES_URL constant found — remove it"
    assert "ANTHROPIC_API_KEY" not in source, "ANTHROPIC_API_KEY reference found in classify.py — remove it"


def test_classify_has_no_httpx_import():
    """classify.py must not import httpx (banned for API calls)."""
    classify_path = Path(__file__).parent.parent / "core" / "imessage_triage" / "classify.py"
    source = classify_path.read_text()
    assert "import httpx" not in source, "httpx is imported in classify.py — remove it"


def test_classify_has_no_pay_per_token_rationalization():
    """The 'intentional pay-per-token' lie must be gone."""
    classify_path = Path(__file__).parent.parent / "core" / "imessage_triage" / "classify.py"
    source = classify_path.read_text()
    assert "pay-per-token" not in source, "pay-per-token rationalization still present in classify.py"
    assert "intentional" not in source.lower() or "pay" not in source.lower(), \
        "Suspicious rationalization text still present in classify.py"
