"""endpoints — single source of truth for control-plane URLs.

Load order (first hit wins):
  1. ~/claude-config/swarm/endpoints.yaml (canonical)
  2. ENDPOINTS_FILE env var override (testing / VPS)

Usage:
    from core.endpoints import get
    url = get("swarm.dispatch")
    url = get("agent_cp.health")
"""

import os
import threading
from pathlib import Path

try:
    import yaml
except ImportError:
    raise ImportError("pyyaml is required: pip install pyyaml")

_CANONICAL = Path.home() / "claude-config" / "swarm" / "endpoints.yaml"

_lock = threading.Lock()
_cache: dict | None = None
_cache_mtime: float = 0.0


def _resolve_path() -> Path:
    override = os.environ.get("ENDPOINTS_FILE")
    if override:
        return Path(override)
    return _CANONICAL


def _load() -> dict:
    global _cache, _cache_mtime
    path = _resolve_path()
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        raise FileNotFoundError(
            f"endpoints.yaml not found at {path}. "
            "Set ENDPOINTS_FILE env var to override."
        )
    with _lock:
        if _cache is not None and mtime == _cache_mtime:
            return _cache
        with path.open() as fh:
            data = yaml.safe_load(fh)
        _cache = data
        _cache_mtime = mtime
        return _cache


def get(key: str) -> str:
    """Return the URL for a dot-separated key path.

    get("swarm.dispatch") -> the dispatch URL from endpoints.yaml

    Raises KeyError if the path does not exist in the registry.
    """
    data = _load()
    parts = key.split(".")
    node = data
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"unknown endpoint '{key}'")
        node = node[part]
    if not isinstance(node, str):
        raise KeyError(f"endpoint '{key}' is a namespace, not a URL")
    return node


def all_keys(prefix: str = "") -> list[str]:
    """Return all leaf key paths, optionally filtered by prefix."""
    data = _load()

    def _walk(node: dict, path: str) -> list[str]:
        keys = []
        for k, v in node.items():
            full = f"{path}.{k}" if path else k
            if isinstance(v, dict):
                keys.extend(_walk(v, full))
            elif isinstance(v, str):
                keys.append(full)
        return keys

    keys = _walk(data, "")
    if prefix:
        keys = [k for k in keys if k.startswith(prefix)]
    return keys
