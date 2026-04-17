"""Unit tests for core.recall — graceful degradation + formatting.

These tests do NOT hit OpenAI or pgvector. They monkeypatch `core.vector.search`
to control the return shape. Live-path smoke testing is handled by
`python -m core.recall smoke` (CLI).
"""
from __future__ import annotations

import importlib
import sys
import time

import pytest


@pytest.fixture(autouse=True)
def _reset_recall():
    """Reload core.recall to reset the in-process circuit breaker."""
    if "core.recall" in sys.modules:
        importlib.reload(sys.modules["core.recall"])
    yield
    if "core.recall" in sys.modules:
        importlib.reload(sys.modules["core.recall"])


def _fake_hits(n: int = 3) -> list[dict]:
    return [
        {
            "source_type": "memory",
            "source_id": f"mem_{i}",
            "chunk_idx": 0,
            "content": f"content body for hit {i} " + "x" * 200,
            "metadata": {},
            "similarity": 0.6 - i * 0.05,
        }
        for i in range(n)
    ]


def test_get_context_returns_block_on_hits(monkeypatch):
    from core import recall as R

    monkeypatch.setattr(R, "_CIRCUIT", {})
    monkeypatch.setattr("core.vector.search", lambda **kw: _fake_hits(2))
    block = R.get_context("test query", kind="agent")
    assert block.startswith("## Relevant prior context")
    assert "source:memory" in block
    assert "mem_0" in block
    assert "---" in block


def test_get_context_empty_on_no_hits(monkeypatch):
    from core import recall as R

    monkeypatch.setattr(R, "_CIRCUIT", {})
    monkeypatch.setattr("core.vector.search", lambda **kw: [])
    assert R.get_context("query", kind="agent") == ""


def test_get_context_empty_on_exception(monkeypatch):
    from core import recall as R

    monkeypatch.setattr(R, "_CIRCUIT", {})

    def boom(**kw):
        raise RuntimeError("pg down")

    monkeypatch.setattr("core.vector.search", boom)
    assert R.get_context("query", kind="agent") == ""
    # circuit tripped after failure
    assert "agent" in R._CIRCUIT


def test_get_context_empty_on_empty_query():
    from core import recall as R

    assert R.get_context("", kind="agent") == ""
    assert R.get_context("   ", kind="agent") == ""


def test_get_context_unknown_kind_returns_empty(monkeypatch):
    from core import recall as R

    monkeypatch.setattr("core.vector.search", lambda **kw: _fake_hits(3))
    assert R.get_context("q", kind="bogus") == ""


def test_circuit_breaker_mutes_kind(monkeypatch):
    from core import recall as R

    monkeypatch.setattr(R, "_CIRCUIT", {})

    def boom(**kw):
        raise RuntimeError("timeout")

    monkeypatch.setattr("core.vector.search", boom)
    assert R.get_context("q", kind="agent") == ""
    # subsequent call short-circuits without even trying
    called = {"n": 0}

    def count(**kw):
        called["n"] += 1
        return _fake_hits(1)

    monkeypatch.setattr("core.vector.search", count)
    assert R.get_context("q2", kind="agent") == ""
    assert called["n"] == 0  # circuit was open, search not invoked


def test_circuit_expires(monkeypatch):
    from core import recall as R

    monkeypatch.setattr(R, "_CIRCUIT", {"agent": time.time() - 1})
    monkeypatch.setattr("core.vector.search", lambda **kw: _fake_hits(1))
    block = R.get_context("q", kind="agent")
    assert block != ""
    assert "agent" not in R._CIRCUIT


def test_over_budget_trips_circuit_but_returns_hits(monkeypatch):
    from core import recall as R

    monkeypatch.setattr(R, "_CIRCUIT", {})
    monkeypatch.setattr(R, "BUDGET_MS", 1)  # impossible budget

    def slow(**kw):
        time.sleep(0.01)  # 10ms > 1ms budget
        return _fake_hits(1)

    monkeypatch.setattr("core.vector.search", slow)
    block = R.get_context("q", kind="agent")
    assert block != ""  # hits already computed, returned anyway
    assert "agent" in R._CIRCUIT  # but circuit tripped


def test_content_trimmed_to_trim_chars(monkeypatch):
    from core import recall as R

    big = "x" * 2000
    hit = {
        "source_type": "memory",
        "source_id": "big",
        "chunk_idx": 0,
        "content": big,
        "metadata": {},
        "similarity": 0.7,
    }
    monkeypatch.setattr(R, "_CIRCUIT", {})
    monkeypatch.setattr("core.vector.search", lambda **kw: [hit])
    block = R.get_context("q", kind="agent")
    assert big not in block  # trimmed
    assert "…" in block
    # content line should be ≤ TRIM_CHARS + 1
    for line in block.splitlines():
        if line.startswith("x"):
            assert len(line) <= R.TRIM_CHARS + 2


def test_agent_override_titan(monkeypatch):
    from core import recall as R

    seen: dict = {}

    def spy(**kw):
        seen.update(kw)
        return _fake_hits(1)

    monkeypatch.setattr("core.vector.search", spy)
    R.get_context("q", kind="agent", agent_name="Titan")
    # Titan override: rerank=True, limit=10, min_similarity=0.20
    assert seen["rerank"] is True
    assert seen["limit"] == 10
    assert seen["min_similarity"] == 0.20


def test_agent_default_no_override(monkeypatch):
    from core import recall as R

    seen: dict = {}

    def spy(**kw):
        seen.update(kw)
        return _fake_hits(1)

    monkeypatch.setattr("core.vector.search", spy)
    R.get_context("q", kind="agent", agent_name="Scout")
    # Scout has no override → falls through to kind="agent" default
    assert seen["rerank"] is False
    assert seen["limit"] == 4


def test_extra_types_extend_without_duplicates(monkeypatch):
    from core import recall as R

    seen: dict = {}

    def spy(**kw):
        seen.update(kw)
        return []

    monkeypatch.setattr("core.vector.search", spy)
    R.get_context("q", kind="agent", extra_types=["email", "memory"])
    # default agent types = (memory, note); + email; dedupe memory
    assert "email" in seen["source_types"]
    assert seen["source_types"].count("memory") == 1


def test_limit_override(monkeypatch):
    from core import recall as R

    seen: dict = {}

    def spy(**kw):
        seen.update(kw)
        return []

    monkeypatch.setattr("core.vector.search", spy)
    R.get_context("q", kind="agent", limit=99)
    assert seen["limit"] == 99


def test_kind_defaults_coverage():
    """Every KIND_DEFAULTS key has a matching DEMO_QUERIES entry for smoke."""
    from core import recall as R

    assert set(R.KIND_DEFAULTS) == set(R.DEMO_QUERIES)
