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
in os.environ. The SDK scrub block (~/Projects/anthropic-update-watcher/watcher.py:182-183) pops those
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
_OP_RUNNER: Final = Path.home() / ".config" / "op-service-account-run.sh"
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

def _should_skip(name: str) -> bool:
    if name in _DANGEROUS_NAMES:
        return True
    return False

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
def _is_launchd_context() -> bool:
    """True when running under launchd (LaunchAgent/LaunchDaemon).

    launchd-spawned processes can't safely invoke `op` — the 1Password CLI
    probes for desktop-app integration via the kTCCServiceSystemPolicyAppData
    TCC service, which LaunchAgents don't inherit, triggering repeated
    "op would like to access data from other apps" prompts that hang the
    process. Interactive Terminal/iTerm/SSH sessions have that grant and
    op runs fine.
    """
    if os.environ.get("VAULT_FORCE_OP") == "1":
        return False
    if os.environ.get("VAULT_SKIP_OP") == "1":
        return True
    if os.environ.get("TERM_PROGRAM"):  # Terminal, iTerm, VS Code
        return False
    if os.environ.get("SSH_TTY") or os.environ.get("SSH_CONNECTION"):
        return False
    try:
        if os.isatty(0) or os.isatty(1):
            return False
    except Exception:
        pass
    return True


_MACHINE_CACHE: Final = Path.home() / ".config" / "machine-secrets.cache"


@functools.cache
def _load_machine_cache() -> dict[str, str]:
    if not _MACHINE_CACHE.exists():
        return {}
    out: dict[str, str] = {}
    try:
        for line in _MACHINE_CACHE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if v:
                out[k] = v
    except Exception:
        return {}
    return out


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


def _op_read(key: str, vault: str = "MachineAutoBiz") -> str | None:
    if _is_launchd_context():
        return None
    token = _service_account_token()
    if not token:
        return None
    if not _OP_RUNNER.is_file():
        return None
    # API_CREDENTIAL category items store the secret under `credential`, not
    # `password`. Try both so new-style items (e.g. AGENT_CP_TOKEN, created
    # 2026-04-23) resolve without needing a second write.
    for field in ("password", "credential"):
        try:
            out = subprocess.run(
                [str(_OP_RUNNER), "read", f"op://{vault}/{key}/{field}"],
                env={
                    "OP_SERVICE_ACCOUNT_TOKEN": token,
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": str(Path.home()),
                },
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0:
                val = out.stdout.strip()
                if val:
                    return val
        except Exception:
            pass
    return None


def get_secret(key: str, *, vault: str = "MachineAutoBiz") -> str | None:
    """Return secret value or None if not found. Caches per-process."""
    if key in _cache:
        return _cache[key]
    val = os.environ.get(key)
    if val:
        _cache[key] = val
        return val
    val = _load_machine_cache().get(key)
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


def hydrate_env(keys: list[str] | None = None, *, vault: str = "MachineAutoBiz") -> dict[str, bool]:
    """Populate os.environ from vault for known keys. Returns {key: found}.

    Uses `op inject` for a single-round-trip batch fetch. Falls back to
    per-key `op read` + legacy file if injection fails. Never overrides keys
    already set in os.environ. Never sets dangerous Anthropic billing names.
    """
    if keys is None:
        keys = sorted(_load_legacy().keys())
    keys = [k for k in keys if not _should_skip(k) and k not in _BASH_NOISE]
    result: dict[str, bool] = {}

    # Always populate CLAUDE_CODE_OAUTH_TOKEN from per-host oat01 cache.
    # Independent of vault: this is the canonical on-disk source and is
    # load-bearing for any LaunchAgent that spawns `claude -p`.
    hydrate_claude_oauth()

    token = _service_account_token()
    launchd = _is_launchd_context()
    # In launchd context, NEVER invoke op — it will hang on a TCC prompt.
    # Fall through to machine cache + legacy file, which together cover every
    # key any LaunchAgent needs.
    if not token or not keys or launchd or not _OP_RUNNER.is_file():
        machine_cache = _load_machine_cache()
        legacy = _load_legacy()
        for k in keys:
            if k in os.environ:
                result[k] = True
                continue
            v = machine_cache.get(k) or legacy.get(k)
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
            [str(_OP_RUNNER), "inject"],
            input=template,
            env={
                "OP_SERVICE_ACCOUNT_TOKEN": token,
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(Path.home()),
            },
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if _should_skip(k) or k in _BASH_NOISE:
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
            machine_cache = _load_machine_cache()
            legacy = _load_legacy()
            for k in keys:
                if result.get(k):
                    continue
                v = machine_cache.get(k) or legacy.get(k)
                if v and k not in os.environ:
                    os.environ[k] = v
                    _cache[k] = v
                    result[k] = True
                else:
                    result[k] = k in os.environ
            return result
    except Exception:
        pass

    machine_cache = _load_machine_cache()
    legacy = _load_legacy()
    for k in keys:
        if k in os.environ:
            result[k] = True
            continue
        v = machine_cache.get(k) or _op_read(k, vault=vault) or legacy.get(k)
        if v:
            os.environ[k] = v
            _cache[k] = v
            result[k] = True
        else:
            result[k] = False
    return result


def hydrate_claude_oauth() -> bool:
    """Load CLAUDE_CODE_OAUTH_TOKEN from per-host oat01 cache.

    Mirrors the shell-init logic at ~/.config/claude-oat01-shell-init.sh.
    LaunchAgent-spawned Python doesn't inherit the env var from .zshenv, so
    this helper reads the active-account marker and exports the matching
    token file into os.environ.

    Returns True if the active account token is loaded. If the process still has
    the other account's token from before a swap, replace it; stale env wins over
    .credentials.json in Claude CLI and must not survive account swaps.
    """
    active_marker = Path.home() / ".claude" / ".active-account"
    try:
        account = active_marker.read_text().strip()
    except Exception:
        account = ""
    if account not in {"gmail", "icloud"}:
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        return False
    token_file = Path.home() / ".config" / f"claude-oat01-{account}"
    try:
        if token_file.is_file():
            tok = token_file.read_text().strip()
            if tok:
                if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") != tok:
                    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = tok
                os.environ["CLAUDE_PANE_ACCOUNT"] = account
                return True
    except Exception:
        pass
    return False


__all__ = ["get_secret", "hydrate_env", "hydrate_claude_oauth"]
