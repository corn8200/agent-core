"""Custom MCP tools available to all SDK agents via in-process server.

Includes:
  - Core tools: SSH, iMessage, email, osascript, Pushover
  - Swarm context tools: shared key-value store for inter-agent memory
"""

import asyncio
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool, create_sdk_mcp_server


# --- Shared Swarm Context (in-process key-value store) ---

class SwarmContext:
    """Thread-safe shared state for agents within a swarm run.

    Agents call swarm_context_write/read/list MCP tools which operate
    on this store. Each Swarm run gets its own SwarmContext instance.
    """

    def __init__(self):
        self._store: dict[str, str] = {}

    def write(self, key: str, value: str):
        self._store[key] = value

    def read(self, key: str) -> str | None:
        return self._store.get(key)

    def list_keys(self) -> list[str]:
        return list(self._store.keys())

    def dump(self) -> dict[str, str]:
        return dict(self._store)

    def clear(self):
        self._store.clear()


# Module-level default context (used by standalone agents outside swarms)
_default_context = SwarmContext()


def _get_active_context() -> SwarmContext:
    """Get the currently active swarm context."""
    return _default_context


def set_active_context(ctx: SwarmContext):
    """Set the active swarm context (called by Swarm engine per run)."""
    global _default_context
    _default_context = ctx


# --- tmux relay for GUI-dependent commands (iMessage, etc.) ---

_TMUX = "/opt/homebrew/bin/tmux"
_RELAY_SESSIONS = ["research-listener", "claude", "main"]


async def _tmux_relay_osascript(script: str, timeout: float = 30.0) -> tuple[bool, str]:
    """Run osascript via tmux new-window to get GUI session access.

    Returns (success, output). Falls back gracefully if no tmux session exists.
    """
    tag = uuid.uuid4().hex[:8]
    script_file = Path(f"/tmp/imsg-{tag}.scpt")
    result_file = Path(f"/tmp/imsg-{tag}.result")
    script_file.write_text(script)

    # Find a live tmux session
    target = None
    for sess in _RELAY_SESSIONS:
        ret = subprocess.run([_TMUX, "has-session", "-t", sess],
                             capture_output=True, timeout=3)
        if ret.returncode == 0:
            target = sess
            break

    if not target:
        script_file.unlink(missing_ok=True)
        return False, "no tmux session for relay"

    # Run osascript in a temporary tmux window (inherits Terminal.app GUI context)
    bash_cmd = (
        f"osascript {script_file} > {result_file} 2>&1; "
        f"rm -f {script_file}; exit 0"
    )
    # Use "-a -t <session>:" so tmux appends a new window instead of trying
    # to reuse a fixed index (fixed 2026-04-09: "index N in use" failures).
    # -d keeps the new window from stealing focus from the active Claude pane.
    proc = await asyncio.create_subprocess_exec(
        _TMUX, "new-window", "-a", "-d", "-t", f"{target}:", "-n", f"relay-{tag}",
        "bash", "-c", bash_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    # Wait for result file (osascript runs async in the tmux window)
    for _ in range(int(timeout * 5)):
        if result_file.exists():
            output = result_file.read_text().strip()
            result_file.unlink(missing_ok=True)
            return True, output
        await asyncio.sleep(0.2)

    result_file.unlink(missing_ok=True)
    script_file.unlink(missing_ok=True)
    return False, "tmux relay timed out"


async def _tmux_relay_shell(shell_cmd: str, timeout: float = 15.0) -> tuple[bool, str]:
    """Run a bash command via tmux new-window to inherit Terminal.app's FDA.

    launchd-spawned children do not inherit Full Disk Access, so accessing
    protected paths like `~/Library/Group Containers/...` or `~/Library/Messages`
    fails silently. Routing through a tmux session that was started from
    Terminal.app (which has FDA) gives the command full access.
    """
    tag = uuid.uuid4().hex[:8]
    result_file = Path(f"/tmp/shellrelay-{tag}.out")

    target = None
    for sess in _RELAY_SESSIONS:
        ret = subprocess.run([_TMUX, "has-session", "-t", sess],
                             capture_output=True, timeout=3)
        if ret.returncode == 0:
            target = sess
            break

    if not target:
        return False, "no tmux session for relay"

    bash_cmd = f"({shell_cmd}) > {result_file} 2>&1; exit 0"
    proc = await asyncio.create_subprocess_exec(
        _TMUX, "new-window", "-a", "-d", "-t", f"{target}:", "-n", f"shrelay-{tag}",
        "bash", "-c", bash_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    for _ in range(int(timeout * 5)):
        if result_file.exists():
            output = result_file.read_text()
            result_file.unlink(missing_ok=True)
            return True, output
        await asyncio.sleep(0.2)

    result_file.unlink(missing_ok=True)
    return False, "tmux shell relay timed out"


async def send_imessage_reliable(buddy: str, message: str) -> tuple[bool, str]:
    """Send iMessage via tmux relay (works from LaunchAgents). Falls back to Pushover."""
    # Pre-warm Messages.app so the first send doesn't cold-start inside the 30s window
    escaped_msg = message.replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))
    script = (
        f'tell application "Messages"\n'
        f'  if not running then launch\n'
        f'  set targetService to 1st account whose service type is iMessage\n'
        f'  set targetBuddy to participant "{buddy}" of targetService\n'
        f'  send "{escaped_msg}" to targetBuddy\n'
        f'  return "ok"\n'
        f'end tell'
    )
    ok, output = await _tmux_relay_osascript(script, timeout=30.0)
    if ok:
        return True, f"iMessage sent to {buddy} via tmux relay"

    # Fallback: Pushover
    from core.constants import PUSHOVER_USER, PUSHOVER_TOKEN
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-o", "/dev/null",
            "-F", f"token={PUSHOVER_TOKEN}",
            "-F", f"user={PUSHOVER_USER}",
            "-F", f"title=Message for {buddy}",
            "-F", f"message={message[:1000]}",
            "https://api.pushover.net/1/messages.json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10)
        return True, f"Pushover sent (iMessage relay failed: {output})"
    except Exception as e:
        return False, f"All delivery failed: {e}"


@tool(
    "ssh_command",
    "Run a command on a remote host via SSH. Returns stdout.",
    {"host": str, "command": str},
)
async def ssh_command(args: dict[str, Any]) -> dict:
    host = args["host"]
    cmd = args["command"]
    proc = await asyncio.create_subprocess_exec(
        "ssh", host, cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode().strip()
    if proc.returncode != 0:
        output += f"\n[STDERR] {stderr.decode().strip()}"
    return {"content": [{"type": "text", "text": output}]}


@tool(
    "send_imessage",
    "Send an iMessage via tmux relay (works from any context). Falls back to Pushover.",
    {"buddy": str, "message": str},
)
async def send_imessage(args: dict[str, Any]) -> dict:
    ok, result = await send_imessage_reliable(args["buddy"], args["message"])
    return {"content": [{"type": "text", "text": result}]}


@tool(
    "send_business_email",
    "Send a business email from info@sentryaithermal.com via sentry-mailqueue on VPS.",
    {"to": str, "subject": str, "body": str},
)
async def send_business_email(args: dict[str, Any]) -> dict:
    to_addr = args["to"]
    subject = args["subject"].replace("'", "'\\''")
    body = args["body"].replace("'", "'\\''")
    queue_cmd = (
        f"cd /srv/apps/sentry-mailqueue && "
        f".venv/bin/python queue_cli.py --to '{to_addr}' --subject '{subject}' --body '{body}' && "
        f".venv/bin/python queue_cli.py --send-now"
    )
    proc = await asyncio.create_subprocess_exec(
        "ssh", "vps", queue_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode().strip()
    if proc.returncode != 0:
        return {"content": [{"type": "text", "text": f"Email failed: {stderr.decode()}"}]}
    return {"content": [{"type": "text", "text": f"Business email sent to {to_addr}: {output}"}]}


@tool(
    "osascript_run",
    "Run an AppleScript snippet and return the result. For Apple data access. Has 15s timeout.",
    {"script": str},
)
async def osascript_run(args: dict[str, Any]) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", args["script"],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        return {"content": [{"type": "text", "text": "osascript timed out (15s) — may not work from LaunchAgent context"}]}
    output = stdout.decode().strip()
    if proc.returncode != 0:
        output = f"osascript error: {stderr.decode().strip()}"
    return {"content": [{"type": "text", "text": output}]}


@tool(
    "moshi_push",
    "Send a push notification via Pushover (Moshi).",
    {"title": str, "message": str},
)
async def moshi_push(args: dict[str, Any]) -> dict:
    title = args["title"]
    message = args["message"]
    # Uses the notify-moshi.sh script if available, otherwise curl
    proc = await asyncio.create_subprocess_exec(
        "bash", "-c",
        f'if [ -x ~/bin/notify-moshi.sh ]; then ~/bin/notify-moshi.sh "{title}" "{message}"; '
        f'else echo "notify-moshi.sh not found"; fi',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return {"content": [{"type": "text", "text": stdout.decode().strip() or "Push sent"}]}


@tool(
    "swarm_context_write",
    "Store a key-value pair in the shared swarm context. Other agents in this swarm can read it.",
    {"key": str, "value": str},
)
async def swarm_context_write(args: dict[str, Any]) -> dict:
    ctx = _get_active_context()
    ctx.write(args["key"], args["value"])
    return {"content": [{"type": "text", "text": f"Stored '{args['key']}' in swarm context"}]}


@tool(
    "swarm_context_read",
    "Read a value from the shared swarm context by key. Returns empty string if key not found.",
    {"key": str},
)
async def swarm_context_read(args: dict[str, Any]) -> dict:
    ctx = _get_active_context()
    value = ctx.read(args["key"])
    if value is None:
        return {"content": [{"type": "text", "text": f"Key '{args['key']}' not found in swarm context"}]}
    return {"content": [{"type": "text", "text": value}]}


@tool(
    "swarm_context_list",
    "List all keys in the shared swarm context.",
    {},
)
async def swarm_context_list(args: dict[str, Any]) -> dict:
    ctx = _get_active_context()
    keys = ctx.list_keys()
    if not keys:
        return {"content": [{"type": "text", "text": "Swarm context is empty"}]}
    return {"content": [{"type": "text", "text": "Keys: " + ", ".join(keys)}]}


def create_core_server():
    """Create the in-process MCP server with all core tools + swarm context."""
    return create_sdk_mcp_server(
        name="core-tools",
        version="1.1.0",
        tools=[
            ssh_command,
            send_imessage,
            send_business_email,
            osascript_run,
            moshi_push,
            swarm_context_write,
            swarm_context_read,
            swarm_context_list,
        ],
    )
