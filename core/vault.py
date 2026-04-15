"""Unified secret loader. Vault-first, env fallback, secrets.env.legacy last resort.

Usage:
    from core.vault import get_secret, hydrate_env

    # One-off lookup
    openai = get_secret("OPENAI_API_KEY")

    # Batch hydrate os.environ at process start (before other imports)
    hydrate_env()

Resolution order per key:
    1. os.environ (already set — never override)
    2. 1Password MachineAuto vault via `op read` / `op inject` (service account token)
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

# Never auto-hydrated into os.environ. ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN
# would trigger pay-as-you-go billing on SDK calls. ANTHROPIC_CONSOLE_KEY* are
# real sk-ant API keys stored under renamed names; injecting them into general
# env defeats the scrub-block defense-in-depth that downstream scripts rely on.
# Scripts that explicitly need these must call get_secret("...") directly.
_DANGEROUS_NAMES: Final = frozenset({
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_CONSOLE_KEY",
    "ANTHROPIC_CONSOLE_KEY_MAC",
    "ANTHROPIC_CONSOLE_KEY_VPS",
})

_BASH_NOISE: Final = frozenset({
    "_", "PWD", "OLDPWD", "SHLVL", "LINES", "COLUMNS",
    "HISTSIZE", "HISTFILE", "HISTFILESIZE", "HOSTNAME", "IFS",
    "PS1", "PS2", "PS3", "PS4", "BASH", "BASH_VERSION", "BASH_VERSINFO",
    "BASHOPTS", "SHELLOPTS", "MACHTYPE", "OSTYPE", "HOSTTYPE",
    "EUID", "UID", "PPID", "RANDOM", "LINENO", "SECONDS",
    "OPTERR", "OPTIND", "MAILCHECK", "TERM", "SHELL",
})

_cache: dict[str, str | None] = {}


@functools.cache
def _service_account_token() -> str | None:
    if _TOKEN_FILE.exists():
        return _TOKEN_FILE.read_text().strip() or None
    return None


@functools.cache
def _load_legacy() -> dict[str, str]:
    """Parse secrets.env.legacy (or secrets.env during transition). Uses bash
    to source the file exactly as shell scripts do, handling quotes, shell
    escapes, and special chars. Filters bash runtime variables and empty values.
    """
    path = _LEGACY_FILE if _LEGACY_FILE.exists() else _FALLBACK_FILE
    if not path.exists():
        return {}
    clean_env = {"PATH": os.environ.get("PATH", "")}
    try:
        baseline = subprocess.run(
            ["bash", "-c", "env"], capture_output=True, text=True, timeout=5,
            env=clean_env,
        ).stdout
        sourced = subprocess.run(
            ["bash", "-c", f"set -a; source {path}; set +a; env"],
            capture_output=True, text=True, timeout=5, env=clean_env,
        ).stdout
    except Exception:
        return {}
    baseline_keys = {ln.partition("=")[0] for ln in baseline.splitlines() if "=" in ln}
    out: dict[str, str] = {}
    for line in sourced.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k in baseline_keys:
            continue
        if k in _BASH_NOISE:
            continue
        if not v:
            continue
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
    """Populate os.environ from vault for known keys. Returns {key: found}.

    Uses `op inject` for a single-round-trip batch fetch. Falls back to
    per-key `op read` + legacy file if injection fails. Never overrides keys
    already set in os.environ. Never sets dangerous Anthropic billing names.
    """
    if keys is None:
        keys = sorted(_load_legacy().keys())
    keys = [k for k in keys if k not in _DANGEROUS_NAMES and k not in _BASH_NOISE]
    result: dict[str, bool] = {}

    token = _service_account_token()
    if not token or not keys:
        legacy = _load_legacy()
        for k in keys:
            if k in os.environ:
                result[k] = True
                continue
            v = legacy.get(k)
            if v:
                os.environ[k] = v
                _cache[k] = v
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
                if k in _DANGEROUS_NAMES or k in _BASH_NOISE:
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
            legacy = _load_legacy()
            for k in keys:
                if result.get(k):
                    continue
                v = legacy.get(k)
                if v and k not in os.environ:
                    os.environ[k] = v
                    _cache[k] = v
                    result[k] = True
                else:
                    result[k] = k in os.environ
            return result
    except Exception:
        pass

    legacy = _load_legacy()
    for k in keys:
        if k in os.environ:
            result[k] = True
            continue
        v = _op_read(k, vault=vault) or legacy.get(k)
        if v:
            os.environ[k] = v
            _cache[k] = v
            result[k] = True
        else:
            result[k] = False
    return result


__all__ = ["get_secret", "hydrate_env"]
