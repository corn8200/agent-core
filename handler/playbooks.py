"""Auto-remediation playbooks for known failure patterns.

Each playbook: match an anomaly → attempt fix → verify → report result.
Severity tiers:
  GREEN  — auto-fix silently, log only
  YELLOW — auto-fix, Pushover notify
  RED    — do NOT fix, escalate to approval-queue
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CIRCUIT_BREAKER_FILE = Path.home() / "logs" / "autoheal-breaker.json"
MAX_FIXES_PER_SERVICE_PER_HOUR = 3


@dataclass
class RemediationResult:
    anomaly: dict
    playbook: str
    action: str
    success: bool
    detail: str
    tier: str  # green, yellow, red


@dataclass
class CircuitBreaker:
    _state: dict = field(default_factory=dict)

    def load(self):
        if CIRCUIT_BREAKER_FILE.exists():
            try:
                self._state = json.loads(CIRCUIT_BREAKER_FILE.read_text())
            except Exception:
                self._state = {}

    def save(self):
        CIRCUIT_BREAKER_FILE.parent.mkdir(parents=True, exist_ok=True)
        CIRCUIT_BREAKER_FILE.write_text(json.dumps(self._state))

    def allow(self, service: str) -> bool:
        now = time.time()
        key = service
        entries = self._state.get(key, [])
        recent = [t for t in entries if now - t < 3600]
        self._state[key] = recent
        return len(recent) < MAX_FIXES_PER_SERVICE_PER_HOUR

    def record(self, service: str):
        key = service
        self._state.setdefault(key, []).append(time.time())
        self.save()


breaker = CircuitBreaker()


async def _run_ssh(cmd: str, timeout: int = 30) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_exec(
        "ssh", "vps", cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "timeout"
    out = (stdout or b"").decode().strip()
    err = (stderr or b"").decode().strip()
    return proc.returncode == 0, out or err


async def _run_local(cmd: str, timeout: int = 30) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "timeout"
    out = (stdout or b"").decode().strip()
    err = (stderr or b"").decode().strip()
    return proc.returncode == 0, out or err


async def _log_to_agent_cp(agent: str, kind: str, payload: dict):
    """Fire-and-forget event to agent-cp."""
    try:
        body = json.dumps({
            "host": "mac",
            "agent": agent,
            "kind": kind,
            "payload": payload,
        })
        escaped_body = body.replace('"', '\\"')
        cmd = (
            "TOKEN=$(grep APPLE_BRIDGE_TOKEN ~/.config/secrets.env | cut -d= -f2 | tr -d '\"'"
            "'"
            ") && curl -s -X POST http://127.0.0.1:8767/ingest"
            " -H 'Content-Type: application/json'"
            " -H \"Authorization: Bearer $TOKEN\""
            f' -d "{escaped_body}"'
        )
        await _run_ssh(cmd, timeout=10)
    except Exception:
        pass


# --- VPS Service Restart ---

VPS_RESTARTABLE = {
    "caddy", "sentry-mailqueue-web", "sentry-dashboard",
    "sentry-formhandler", "jobsignal-dashboard", "mailtriage",
    "agent-cp", "approval-queue-web", "email-executor",
    "apple-bridge-watcher",
}

async def fix_vps_service_down(anomaly: dict) -> RemediationResult:
    msg = anomaly["message"]
    match = re.search(r"Service down: (.+)", msg)
    if not match:
        return RemediationResult(anomaly, "vps_service_restart", "no-op", False, "couldn't parse service name", "red")

    svc = match.group(1).strip()

    if svc not in VPS_RESTARTABLE:
        return RemediationResult(anomaly, "vps_service_restart", "escalate", False,
                                 f"{svc} not in restartable whitelist", "red")

    if not breaker.allow(f"vps:{svc}"):
        return RemediationResult(anomaly, "vps_service_restart", "circuit_breaker", False,
                                 f"{svc} hit {MAX_FIXES_PER_SERVICE_PER_HOUR} restarts/hr limit", "red")

    ok, out = await _run_ssh(f"sudo systemctl restart {svc}")
    if not ok:
        return RemediationResult(anomaly, "vps_service_restart", f"restart {svc}", False, out, "yellow")

    await asyncio.sleep(3)
    ok, out = await _run_ssh(f"systemctl is-active {svc}")
    success = ok and "active" in out
    breaker.record(f"vps:{svc}")

    return RemediationResult(
        anomaly, "vps_service_restart", f"restart {svc}",
        success, f"{'recovered' if success else 'still down'}: {out}", "yellow",
    )


# --- VPS Disk Cleanup ---

async def fix_vps_disk(anomaly: dict) -> RemediationResult:
    msg = anomaly["message"]
    match = re.search(r"(\d+)%", msg)
    pct = int(match.group(1)) if match else 0

    if pct > 95:
        return RemediationResult(anomaly, "vps_disk_cleanup", "escalate", False,
                                 f"disk at {pct}% — too critical for auto-cleanup", "red")

    if not breaker.allow("vps:disk"):
        return RemediationResult(anomaly, "vps_disk_cleanup", "circuit_breaker", False,
                                 "disk cleanup hit rate limit", "red")

    cmds = [
        "sudo journalctl --vacuum-size=500M",
        "sudo apt-get clean -y",
        "sudo find /tmp -type f -mtime +7 -delete 2>/dev/null",
        "sudo find /var/log -name '*.gz' -mtime +14 -delete 2>/dev/null",
    ]
    ok, out = await _run_ssh(" && ".join(cmds), timeout=60)
    breaker.record("vps:disk")

    ok2, df_out = await _run_ssh("df -h / | tail -1")
    return RemediationResult(
        anomaly, "vps_disk_cleanup", "journal+apt+tmp cleanup",
        ok, f"cleanup {'ok' if ok else 'partial'}. disk now: {df_out}", "yellow",
    )


# --- Mac Disk Cleanup ---

async def fix_mac_disk(anomaly: dict) -> RemediationResult:
    if not breaker.allow("mac:disk"):
        return RemediationResult(anomaly, "mac_disk_cleanup", "circuit_breaker", False,
                                 "mac disk cleanup hit rate limit", "red")

    script = Path.home() / "bin" / "weekly-clean.sh"
    if not script.exists():
        return RemediationResult(anomaly, "mac_disk_cleanup", "no-op", False,
                                 "weekly-clean.sh not found", "red")

    ok, out = await _run_local(f"bash {script}", timeout=120)
    breaker.record("mac:disk")

    return RemediationResult(
        anomaly, "mac_disk_cleanup", "weekly-clean.sh",
        ok, out[-200:] if out else "done", "yellow",
    )


# --- Playbook Router ---

PLAYBOOKS = {
    ("vps", r"Service down:"): fix_vps_service_down,
    ("vps", r"VPS disk at \d+%"): fix_vps_disk,
    ("mac", r"Mac disk at \d+%"): fix_mac_disk,
}


def match_playbook(anomaly: dict):
    source = anomaly.get("source", "")
    msg = anomaly.get("message", "")
    for (src_pattern, msg_pattern), handler in PLAYBOOKS.items():
        if source == src_pattern and re.search(msg_pattern, msg):
            return handler
    return None


async def attempt_remediation(anomalies: list[dict]) -> list[RemediationResult]:
    """Try to auto-fix each anomaly that has a matching playbook.

    Returns list of results (one per anomaly that had a playbook match).
    Anomalies without playbooks are left untouched for normal alerting.
    """
    breaker.load()
    results = []

    tasks = []
    for a in anomalies:
        handler = match_playbook(a)
        if handler:
            tasks.append(handler(a))

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        clean = []
        for r in results:
            if isinstance(r, Exception):
                clean.append(RemediationResult(
                    {}, "unknown", "error", False, str(r), "red",
                ))
            else:
                clean.append(r)
        results = clean

    for r in results:
        await _log_to_agent_cp("autoheal", "remediation", {
            "playbook": r.playbook,
            "action": r.action,
            "success": r.success,
            "tier": r.tier,
            "detail": r.detail[:300],
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    return results
