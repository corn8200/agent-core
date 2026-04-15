"""Unified secret loader. Vault-first, env fallback, secrets.env.legacy last resort.

Usage:
    from core.vault import get_secret, hydrate_env

    # One-off lookup
    openai = get_secret("OPENAI_API_KEY")

    # Batch hydrate os.environ at process start (before other imports)
    hydrate_env()

Resolution order per key:
    1. os.environ (already set — never override)
    2. 1Password MachineAuto vault via `op read` (service account token)
    3. ~/.config/secrets.env.legacy (transition window, ~7 days)

Hard rule: this module NEVER sets ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN
in os.environ. The SDK scrub block (morning_brief.py lines 24-32) pops those
names so the CLI uses Max subscription billing. Repopulating them would silently
route SDK calls to pay-as-you-go API billing. The renamed sibling
ANTHROPIC_CONSOLE_KEY is fine to hydrate — the SDK doesn't look at that name.
"""

from __future__ import annotations

import functools
import os
import subprocess
from pathlib import Path
from typing import Final

_TOKEN_FILE: Final = Path.home() / ".config" / "op-service-account-token"
_LEGACY_FILE: Final = Path.home() / ".config" / "secrets.env.legacy"
_FALLBACK_FILE: Final = Path.home() / ".config" / "secrets.env"

_DANGEROUS_NAMES: Final = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"})

_cache: dict[str, str | None] = {}


@functools.cache
def _service_account_token() -> str | None:
    if _TOKEN_FILE.exists():
        return _TOKEN_FILE.read_text().strip() or None
    return None


@functools.cache
def _load_legacy() -> dict[str, str]:
    """Parse secrets.env.legacy (or secrets.env during transition). Uses bash to
    handle shell escapes, quotes, and special chars the same way `source` does."""
    path = _LEGACY_FILE if _LEGACY_FILE.exists() else _FALLBACK_FILE
    if not path.exists():
        return {}
    try:
        baseline = subprocess.run(
            ["bash", "-c", "env"], capture_output=True, text=True, timeout=5,
            env={"PATH": os.environ.get("PATH", "")},
        ).stdout
        sourced = subprocess.run(
            ["bash", "-c", f"set -a; source {path}; set +a; env"],
            capture_output=True, text=True, timeout=5,
            env={"PATH": os.environ.get("PATH", "")},
        ).stdout
    except Exception:
        return {}
    baseline_keys = {ln.partition("=")[0] for ln in baseline.splitlines() if "=" in ln}
    out: dict[str, str] = {}
    for line in sourced.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k in baseline_keys and k != "_":
            continue
        if v:
            out[k] = v
    return out


def _op_read(key: str, vault: str = "MachineAuto") -> str | None:
    token = _service_account_token()
    if not token:
        return None
    try:
        out = subprocess.run(
            ["op", "read", f"op://{vault}/{key}/password"],
            env={"OP_SERVICE_ACCOUNT_TOKEN": token, "PATH": os.environ.get("PATH", "")},
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:
        pass
    return None


def get_secret(key: str, *, vault: str = "MachineAuto") -> str | None:
    """Return secret value or None if not found. Caches per-process."""
    if key in _cache:
        return _cache[key]
    val = os.environ.get(key)
    if val:
        _cache[key] = val
        return val
    val = _op_read(key, vault=vault)
    if val:
        _cache[key] = val
        return val
    val = _load_legacy().get(key)
    _cache[key] = val
    return val


def hydrate_env(keys: list[str] | None = None, *, vault: str = "MachineAuto") -> dict[str, bool]:
    """Populate os.environ from vault for all known keys. Returns {key: found}.

    Uses `op inject` for a single-round-trip batch fetch. Falls back to
    per-key `op read` if injection fails. Never overrides keys already set in
    os.environ. Never sets dangerous Anthropic billing names.
    """
    if keys is None:
        keys = sorted(_load_legacy().keys())
    keys = [k for k in keys if k not in _DANGEROUS_NAMES]
    result: dict[str, bool] = {}

    token = _service_account_token()
    if not token or not keys:
        for k in keys:
            if k in os.environ:
                result[k] = True
                continue
            v = _load_legacy().get(k)
            if v:
                os.environ[k] = v
                result[k] = True
            else:
                result[k] = False
        return result

    template = "\n".join(f"{k}={{{{ op://{vault}/{k}/password }}}}" for k in keys) + "\n"
    try:
        out = subprocess.run(
            ["op", "inject"],
            input=template,
            env={"OP_SERVICE_ACCOUNT_TOKEN": token, "PATH": os.environ.get("PATH", "")},
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k in _DANGEROUS_NAMES:
                    continue
                if k in os.environ:
                    result[k] = True
                    continue
                if v and not v.startswith("{{"):
                    os.environ[k] = v
                    _cache[k] = v
                    result[k] = True
                else:
                    result[k] = False
            for k in keys:
                if k not in result:
                    legacy_val = _load_legacy().get(k)
                    if legacy_val and k not in os.environ:
                        os.environ[k] = legacy_val
                        result[k] = True
                    else:
                        result[k] = k in os.environ
            return result
    except Exception:
        pass

    for k in keys:
        if k in os.environ:
            result[k] = True
            continue
        v = _op_read(k, vault=vault)
        if not v:
            v = _load_legacy().get(k)
        if v:
            os.environ[k] = v
            _cache[k] = v
            result[k] = True
        else:
            result[k] = False
    return result


__all__ = ["get_secret", "hydrate_env"]
