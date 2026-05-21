from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_VPS_ACTIVE_ACCOUNT_PATH = Path("/home/ubuntu/.claude/.active-account")
DEFAULT_MAC_ACTIVE_ACCOUNT_PATH = Path("/home/ubuntu/.claude/.mac-active-account")
DEFAULT_VPS_STAMP_DIR = Path("/home/ubuntu/.claude/per-pane-account")
DEFAULT_MAC_STAMP_DIR = Path("/.claude/per-pane-account")
DEFAULT_PROTECTED_PANES_PATH = Path("/home/ubuntu/.claude/stale-creds-protected-panes")
DEFAULT_CREDENTIALS_PATH = Path.home() / ".credentials.json"


@dataclass(frozen=True)
class PaneAccountStamp:
    pane: str
    account: str
    started_at: str
    pid: int
    path: Path


@dataclass(frozen=True)
class PaneCredentialState:
    pane: str
    stamp_path: Path
    pid: int
    pid_running: bool
    account: str
    active_account: str | None
    started_at: str
    protected: bool

    @property
    def stale(self) -> bool:
        return (
            self.pid_running
            and bool(self.active_account)
            and self.account != self.active_account
        )

    @property
    def pane_label(self) -> str:
        if ":" not in self.pane:
            return self.pane
        return f"pane:{self.pane.split(':', 1)[1]}"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _coerce_started_at(value: datetime | str | None) -> str:
    if value is None:
        return _utc_now_iso()
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return str(value)


def _stamp_path(stamp_dir: Path, pane: str) -> Path:
    return stamp_dir / f"{pane}.json"


def pane_host(pane: str) -> str | None:
    if pane.startswith("claude-vps:"):
        return "vps"
    if pane.startswith("claude:"):
        return "mac"
    return None


def default_stamp_dir_for_pane(pane: str) -> Path | None:
    host = pane_host(pane)
    if host == "vps":
        return DEFAULT_VPS_STAMP_DIR
    if host == "mac":
        return DEFAULT_MAC_STAMP_DIR
    return None


def default_active_account_path_for_pane(pane: str) -> Path | None:
    host = pane_host(pane)
    if host == "vps":
        return DEFAULT_VPS_ACTIVE_ACCOUNT_PATH
    if host == "mac":
        return DEFAULT_MAC_ACTIVE_ACCOUNT_PATH
    return None


def write_pane_stamp(
    stamp_dir: Path,
    *,
    pane: str,
    account: str,
    pid: int,
    started_at: datetime | str | None = None,
) -> Path:
    path = _stamp_path(stamp_dir, pane)
    payload = {
        "account": str(account),
        "started_at": _coerce_started_at(started_at),
        "pid": int(pid),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def read_active_account(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def read_protected_panes(path: Path = DEFAULT_PROTECTED_PANES_PATH) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    protected: set[str] = set()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        protected.add(line)
    return protected


def read_pane_stamp(path: Path) -> PaneAccountStamp | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    account = payload.get("account")
    started_at = payload.get("started_at")
    pid = payload.get("pid")
    if not isinstance(account, str) or not account.strip():
        return None
    if not isinstance(started_at, str) or not started_at.strip():
        return None
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None
    pane = path.stem
    return PaneAccountStamp(
        pane=pane,
        account=account.strip(),
        started_at=started_at.strip(),
        pid=pid_int,
        path=path,
    )


def read_stamp_dir(stamp_dir: Path) -> dict[str, PaneAccountStamp]:
    try:
        paths = sorted(p for p in stamp_dir.iterdir() if p.is_file() and p.suffix == ".json")
    except OSError:
        return {}
    out: dict[str, PaneAccountStamp] = {}
    for path in paths:
        stamp = read_pane_stamp(path)
        if stamp is not None:
            out[stamp.pane] = stamp
    return out


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_protected_pane(pane: str, protected_panes: set[str] | None = None) -> bool:
    protected = protected_panes or set()
    if pane in protected:
        return True
    if pane.startswith("claude:") and pane.endswith(":1"):
        return True
    if pane.startswith("claude-vps:") and pane.endswith(":1"):
        return True
    return False


def collect_pane_states(
    stamp_dir: Path,
    *,
    active_account: str | None,
    protected_panes: set[str] | None = None,
    pid_exists_fn: Any = pid_exists,
) -> list[PaneCredentialState]:
    states: list[PaneCredentialState] = []
    for stamp in read_stamp_dir(stamp_dir).values():
        running = bool(pid_exists_fn(stamp.pid))
        states.append(
            PaneCredentialState(
                pane=stamp.pane,
                stamp_path=stamp.path,
                pid=stamp.pid,
                pid_running=running,
                account=stamp.account,
                active_account=active_account,
                started_at=stamp.started_at,
                protected=is_protected_pane(stamp.pane, protected_panes),
            )
        )
    return sorted(states, key=lambda item: item.pane)


def stale_count(states: list[PaneCredentialState]) -> int:
    return sum(1 for state in states if state.stale)


def statusline_fragment(states: list[PaneCredentialState]) -> str | None:
    count = stale_count(states)
    if count <= 0:
        return None
    return f"panes-stale:{count}"


def swap_guard_log_line(
    pane: str,
    state: PaneCredentialState | None,
) -> str:
    label = f"pane:{pane.split(':', 1)[1]}" if ":" in pane else pane
    if state is None or not state.stale:
        return f"[SWAP-SKIPPED-{label} creds-ok]"
    active = state.active_account or "unknown"
    return (
        f"[SWAP-SKIPPED-{label} stale-creds account={state.account} "
        f"active={active} manual-restart-required]"
    )


def doctor_payload_for_state(
    state: PaneCredentialState,
    *,
    watcher: str = "stale-creds-watcher",
) -> dict[str, Any]:
    context = {
        "pane": state.pane,
        "pane_label": state.pane_label,
        "pane_pid": state.pid,
        "started_at": state.started_at,
        "spawn_account": state.account,
        "active_account": state.active_account,
        "protected": state.protected,
    }
    if state.protected:
        return {
            "watcher": watcher,
            "severity": "warn",
            "summary": (
                f"{state.pane} stale credentials - manual restart required. "
                f"Active account is {state.active_account or 'unknown'} but pane spawned on {state.account}. "
                "Kill and respawn to refresh creds."
            ),
            "context": context,
        }
    return {
        "watcher": watcher,
        "severity": "notice",
        "summary": (
            f"[STALE-CREDS-AUTO-FIXED-{state.pane_label}] account was {state.account}, "
            f"now {state.active_account or 'unknown'}"
        ),
        "context": context,
    }


def watcher_actions(states: list[PaneCredentialState]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for state in states:
        if not state.pid_running:
            actions.append(
                {
                    "pane": state.pane,
                    "action": "cleanup_stamp",
                    "stamp_path": str(state.stamp_path),
                    "pid": state.pid,
                }
            )
            continue
        if not state.stale:
            continue
        actions.append(
            {
                "pane": state.pane,
                "action": "manual_restart_required" if state.protected else "auto_restart_eligible",
                "pid": state.pid,
                "doctor_payload": doctor_payload_for_state(state),
            }
        )
    return actions


def commander_self_check(
    *,
    credentials_path: Path = DEFAULT_CREDENTIALS_PATH,
    process_started_at: datetime,
) -> tuple[bool, str | None]:
    try:
        modified_at = datetime.fromtimestamp(credentials_path.stat().st_mtime, tz=UTC)
    except OSError:
        return True, None
    if modified_at > process_started_at.astimezone(UTC):
        return (
            False,
            "stale creds detected: credentials.json updated after process start, parent loop will respawn",
        )
    return True, None
