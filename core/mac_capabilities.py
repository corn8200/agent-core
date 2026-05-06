"""Mac capability inventory + tool-arbitration helper.

Provides:
  choose_tool(intent) -> CapabilityChoice   — pure stdlib, works on VPS too
  inventory()         -> dict               — Mac-only (guards sys.platform)

Intents understood by choose_tool:
  send-imessage | send-email | schedule-event | set-reminder |
  look-up-contact | transcribe-voice-memo | query-mail |
  send-pushover | query-calendar | query-notes
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class CapabilityChoice:
    """Single best tool for an intent."""

    intent: str
    tool: str           # canonical function / helper name
    module: str         # Python import path or shell helper
    channel: str        # mcp | python | shell | osascript
    notes: str = ""
    fallback: Optional[str] = None  # tool name if primary unavailable


# ---------------------------------------------------------------------------
# Arbitration table — deterministic, zero network calls
# ---------------------------------------------------------------------------

_ROUTING: dict[str, CapabilityChoice] = {
    "send-imessage": CapabilityChoice(
        intent="send-imessage",
        tool="send_imessage_reliable",
        module="core.tools",
        channel="python",
        notes="Requires tmux relay on launchd; use relay path from FDA gotcha rule.",
        fallback="imsg",
    ),
    "send-email": CapabilityChoice(
        intent="send-email",
        tool="send_email",
        module="core.mailhub",
        channel="python",
        notes="Mailhub picks correct SMTP backend by sender address.",
        fallback="mailhub_client.MailhubClient.send",
    ),
    "schedule-event": CapabilityChoice(
        intent="schedule-event",
        tool="add_event",
        module="core.calendar_service",
        channel="python",
        notes="Wraps osascript tell app Calendar; Apple Calendar is primary.",
        fallback="osascript",
    ),
    "set-reminder": CapabilityChoice(
        intent="set-reminder",
        tool="add_reminder",
        module="core.reminders_service",
        channel="python",
        notes="Six owned lists: Home/Health/Work/Sentry AI/Claude/Someday.",
        fallback="osascript",
    ),
    "look-up-contact": CapabilityChoice(
        intent="look-up-contact",
        tool="contact_lookup",
        module="mcp__contact_lookup",
        channel="mcp",
        notes="Resolves iMessage buddy handles; pre-validates before sends.",
    ),
    "transcribe-voice-memo": CapabilityChoice(
        intent="transcribe-voice-memo",
        tool="transcribe_memo",
        module="mcp__apple-voice-memos__transcribe_memo",
        channel="mcp",
        notes="Audio at ~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/.",
        fallback="whisper",
    ),
    "query-mail": CapabilityChoice(
        intent="query-mail",
        tool="imap_search",
        module="core.mail_imap",
        channel="python",
        notes="IMAP IDLE for icloud+gmail; see mailtriage for classified results.",
        fallback="osascript_mail",
    ),
    "send-pushover": CapabilityChoice(
        intent="send-pushover",
        tool="push",
        module="core.pushover",
        channel="python",
        notes="Default one-way agent→John notification channel (2026-05-02).",
    ),
    "query-calendar": CapabilityChoice(
        intent="query-calendar",
        tool="get_events",
        module="core.calendar_service",
        channel="python",
        notes="Apple Calendar via osascript; Google Calendar linked via CalDAV.",
    ),
    "query-notes": CapabilityChoice(
        intent="query-notes",
        tool="osascript",
        module="core.safari",
        channel="osascript",
        notes="Apple Notes — read via osascript tell app Notes.",
    ),
}


def choose_tool(intent: str) -> CapabilityChoice:
    """Return the canonical tool for *intent*.

    Raises KeyError if intent is unknown — callers should handle or call
    list_intents() to validate first.
    """
    normalized = intent.lower().strip()
    choice = _ROUTING.get(normalized)
    if choice is None:
        raise KeyError(
            f"Unknown intent {intent!r}. Known: {list(_ROUTING)}"
        )
    return choice


def list_intents() -> list[str]:
    """All intents understood by choose_tool."""
    return list(_ROUTING)


# ---------------------------------------------------------------------------
# Inventory — Mac-only, platform-guarded
# ---------------------------------------------------------------------------

def _run_timeout(cmd: list[str], timeout: float = 5.0) -> str:
    """Run cmd, return stdout or empty string on failure/timeout."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _probe_applescript(app_name: str, timeout: float = 5.0) -> bool:
    """Return True if app responds to a harmless AppleScript probe."""
    script = f'tell application "{app_name}" to get name'
    out = _run_timeout(["osascript", "-e", script], timeout=timeout)
    return bool(out)


def _parse_launchagent_schedule(plist_path: Path) -> str:
    """Extract schedule from a plist (StartCalendarInterval or StartInterval)."""
    text = _run_timeout(
        ["plutil", "-convert", "json", "-o", "-", str(plist_path)],
        timeout=3.0,
    )
    if not text:
        return "on-demand"
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        return "on-demand"

    if "StartCalendarInterval" in d:
        sci = d["StartCalendarInterval"]
        if isinstance(sci, dict):
            h = sci.get("Hour", "?")
            m = sci.get("Minute", 0)
            return f"daily {h:02}:{m:02}" if isinstance(h, int) else f"cron {sci}"
        elif isinstance(sci, list):
            return f"cron ({len(sci)} intervals)"
    if "StartInterval" in d:
        secs = d["StartInterval"]
        if secs < 120:
            return f"every {secs}s"
        elif secs < 3600:
            return f"every {secs // 60}m"
        else:
            return f"every {secs // 3600}h"
    if d.get("KeepAlive"):
        return "always-alive"
    return "event/watch"


def _mcp_servers() -> list[dict]:
    """Parse MCP server list from ~/.claude/.mcp.json + ~/.codex/config.toml."""
    results: list[dict] = []
    mcp_json = Path.home() / ".claude" / ".mcp.json"
    if mcp_json.exists():
        try:
            data = json.loads(mcp_json.read_text())
            for name, conf in data.get("mcpServers", {}).items():
                results.append({
                    "name": name,
                    "source": "~/.claude/.mcp.json",
                    "command": conf.get("command", ""),
                    "args": conf.get("args", []),
                })
        except Exception:
            pass
    # codex config is TOML — parse manually (no PyYAML/toml on VPS)
    codex_cfg = Path.home() / ".codex" / "config.toml"
    if codex_cfg.exists():
        text = codex_cfg.read_text()
        in_mcp = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("[mcpServers.") or stripped.startswith("[mcp_servers."):
                name = stripped.split(".")[1].rstrip("]")
                results.append({
                    "name": f"codex:{name}",
                    "source": "~/.codex/config.toml",
                    "command": "",
                    "args": [],
                })
    return results


def _apple_data_sources() -> list[dict]:
    """Apple data sources reachable from this Mac."""
    return [
        {"name": "Mail", "access": "osascript", "app": "Mail", "notes": "icloud+gmail IMAP"},
        {"name": "Calendar", "access": "osascript", "app": "Calendar", "notes": "Apple+Google CalDAV"},
        {"name": "Contacts", "access": "osascript", "app": "Contacts", "notes": "iCloud sync"},
        {"name": "Reminders", "access": "osascript", "app": "Reminders", "notes": "6 owned lists"},
        {"name": "Notes", "access": "osascript", "app": "Notes", "notes": "iCloud sync"},
        {"name": "Messages", "access": "tmux_relay_shell", "app": "Messages",
         "notes": "chat.db TCC-protected; relay required from LaunchAgent"},
        {"name": "VoiceMemos", "access": "tmux_relay_shell", "app": "Voice Memos",
         "notes": "Audio in ~/Library/Group Containers/group.com.apple.VoiceMemos.shared/"},
        {"name": "Music", "access": "osascript", "app": "Music", "notes": "library metadata"},
        {"name": "Safari", "access": "osascript", "app": "Safari", "notes": "history+bookmarks"},
    ]


def _installed_apps(dry_run: bool = False, timeout: float = 5.0) -> list[dict]:
    """Enumerate /Applications; probe AppleScript surface unless dry_run."""
    apps_dir = Path("/Applications")
    app_dirs = sorted(p for p in apps_dir.iterdir() if p.suffix == ".app")

    # Apps known to support AppleScript (checked quickly)
    KNOWN_AS = {
        "Mail", "Safari", "Messages", "Music", "Calendar",
        "Contacts", "Reminders", "Notes", "Preview", "Finder",
        "System Events", "Terminal", "iTerm", "Claude", "Codex",
        "Google Chrome", "ChatGPT",
    }

    results: list[dict] = []
    for app_path in app_dirs:
        name = app_path.stem
        if dry_run:
            as_capable = name in KNOWN_AS
        else:
            as_capable = _probe_applescript(name, timeout=timeout) if name in KNOWN_AS else False
        results.append({
            "name": name,
            "path": str(app_path),
            "applescript": as_capable,
        })
    return results


def _launchagents() -> list[dict]:
    """List com.john.* LaunchAgents with schedule info."""
    la_dir = Path.home() / "Library" / "LaunchAgents"
    plists = sorted(la_dir.glob("com.john.*.plist"))
    results: list[dict] = []
    for p in plists:
        results.append({
            "label": p.stem,
            "path": str(p),
            "schedule": _parse_launchagent_schedule(p),
        })
    return results


def _osascript_helpers() -> list[dict]:
    """Enumerate osascript/applescript helpers on the Mac."""
    results: list[dict] = []
    # ~/Library/Application Scripts/
    app_scripts = Path.home() / "Library" / "Application Scripts"
    if app_scripts.exists():
        for p in app_scripts.rglob("*.scpt"):
            results.append({"name": p.name, "path": str(p), "type": "compiled"})
        for p in app_scripts.rglob("*.applescript"):
            results.append({"name": p.name, "path": str(p), "type": "source"})
    # ~/bin/*.applescript
    bin_dir = Path.home() / "bin"
    if bin_dir.exists():
        for p in bin_dir.glob("*.applescript"):
            results.append({"name": p.name, "path": str(p), "type": "source"})
    return results


def inventory(dry_run: bool = False) -> dict:
    """Full Mac capability inventory. Raises RuntimeError on non-Mac."""
    if sys.platform != "darwin":
        raise RuntimeError("inventory() is Mac-only (sys.platform != 'darwin')")
    return {
        "platform": "darwin",
        "dry_run": dry_run,
        "installed_apps": _installed_apps(dry_run=dry_run),
        "mcp_servers": _mcp_servers(),
        "apple_data_sources": _apple_data_sources(),
        "launchagents": _launchagents(),
        "osascript_helpers": _osascript_helpers(),
        "tool_routing": {
            intent: {
                "tool": c.tool,
                "module": c.module,
                "channel": c.channel,
            }
            for intent, c in _ROUTING.items()
        },
    }
