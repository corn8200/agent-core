"""usage_cache — centralized reader for ~/.claude/pane-commander/usage-cache.json.

This is the ONE blessed way to read the usage cache. Direct reads
(`json.load(open("...usage-cache.json"))`) are policy violations and will
be flagged by the pre-commit hook at
~/claude-config/hooks/usage-cache-direct-read-ban.sh.

WHY THIS EXISTS (#885 disconnect, 2026-04-28):
The cache holds slots for every per-account scraper that ever ran. When
account X is no longer active (post-/swap), scrapers self-disable and stamp
their slot dormant — preserving last-known data under `last_known` but
clearing top-level pct/wall fields. A naive reader that does
`cache["gmail-20x"]["seven_day_pct"]` gets None when dormant (correct), but
a naive reader that checks `cache["gmail-20x"]["hit_wall"]` gets KeyError
or a phantom truthy value from `last_known` if it knows where to look —
neither is what the operator wanted.

`read_active()` returns ONLY non-dormant slots — the live truth. Callers
that need historical/forensic data use `read_all(include_dormant=True)`.

API:
    from core.usage_cache import read_active, read_all

    active = read_active()
    # → {"icloud-20x": {...probe data...}}

    historical = read_all(include_dormant=True)
    # → {"gmail-20x": {dormant: True, last_known: {...}}, "icloud-20x": {...}}

    pct = headroom_pct("icloud-20x")
    # → 0.71 (or None if missing/dormant)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CACHE_PATH = Path.home() / ".claude" / "pane-commander" / "usage-cache.json"


def _resolve_cache_path(path: str | os.PathLike | None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get("USAGE_CACHE_PATH")
    if env:
        return Path(env)
    return DEFAULT_CACHE_PATH


def _load_raw(path: str | os.PathLike | None = None) -> dict[str, Any]:
    p = _resolve_cache_path(path)
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    data.pop("_schema", None)
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def is_dormant(slot: dict[str, Any]) -> bool:
    """A slot is dormant if its scraper self-disabled (account != host active)."""
    return bool(slot.get("dormant"))


def read_all(
    path: str | os.PathLike | None = None,
    include_dormant: bool = False,
) -> dict[str, dict[str, Any]]:
    """Return all cache slots. Default excludes dormant — use include_dormant=True
    for forensic/historical reads.
    """
    raw = _load_raw(path)
    if include_dormant:
        return raw
    return {k: v for k, v in raw.items() if not is_dormant(v)}


def read_active(
    path: str | os.PathLike | None = None,
) -> dict[str, dict[str, Any]]:
    """Return only non-dormant slots — the live truth.

    Use this for ANY decision-making code (headroom checks, gate logic,
    statusline rendering, dispatcher gating). Dormant slots are scrapers'
    last-known state for accounts no pane is currently using; treating
    them as live signal causes the #885 disconnect class of bugs.
    """
    return read_all(path, include_dormant=False)


def headroom_pct(account_full: str, path: str | os.PathLike | None = None) -> float | None:
    """Return primary_pct for an account if it's active and has a fresh probe.

    None when: account missing, dormant, or read_ok=False. Callers gating on
    headroom MUST treat None as "no signal — fail open" (not "blocked").
    """
    slot = read_active(path).get(account_full)
    if not slot:
        return None
    if not slot.get("read_ok"):
        return None
    pct = slot.get("primary_pct")
    if isinstance(pct, (int, float)):
        return float(pct)
    return None
