"""Custom MCP tools available to all SDK agents via in-process server.

Includes:
  - Core tools: SSH, iMessage, email, osascript, Pushover
  - Swarm context tools: shared key-value store for inter-agent memory
"""

import asyncio
import difflib
import json
import re
import shlex
import subprocess
import uuid
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool, create_sdk_mcp_server  # allow-direct-sdk


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
_RELAY_SESSIONS = ["claude", "main"]


async def _kill_tmux_session(session: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            _TMUX, "kill-session", "-t", session,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
    except Exception:
        pass


async def _tmux_relay_osascript(script: str, timeout: float = 30.0) -> tuple[bool, str]:
    """Run osascript via a hidden one-shot tmux session to get GUI session access.

    Returns (success, output). Falls back gracefully if no tmux session exists.
    """
    tag = uuid.uuid4().hex[:8]
    script_file = Path(f"/tmp/imsg-{tag}.scpt")
    result_file = Path(f"/tmp/imsg-{tag}.result")
    script_file.write_text(script)

    # Find a live tmux session. Use async subprocess with wait_for so a
    # wedged tmux server (hung has-session call) cannot block the event loop.
    target = None
    for sess in _RELAY_SESSIONS:
        try:
            p = await asyncio.create_subprocess_exec(
                _TMUX, "has-session", "-t", sess,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                rc = await asyncio.wait_for(p.wait(), timeout=2)
            except asyncio.TimeoutError:
                p.kill()
                await p.wait()
                rc = 1
            if rc == 0:
                target = sess
                break
        except Exception:
            continue

    if not target:
        script_file.unlink(missing_ok=True)
        return False, "no tmux session for relay"

    # Run osascript in a private one-shot tmux session. This keeps Terminal.app
    # FDA inheritance without inserting relay-* windows into the human claude session.
    relay_session = f"imsg-relay-{tag}"
    bash_cmd = (
        f"osascript {script_file} > {result_file} 2>&1; "
        f"rm -f {script_file}; exit 0"
    )
    proc = await asyncio.create_subprocess_exec(
        _TMUX, "new-session", "-d", "-s", relay_session, "-n", "relay",
        "-c", str(Path.home()),
        f"/bin/bash -c {shlex.quote(bash_cmd)}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        result_file.unlink(missing_ok=True)
        script_file.unlink(missing_ok=True)
        return False, f"tmux new-session returned {proc.returncode}: {stderr.decode(errors='replace')[:200]}"

    try:
        for _ in range(int(timeout * 5)):
            if result_file.exists():
                output = result_file.read_text().strip()
                result_file.unlink(missing_ok=True)
                return True, output
            await asyncio.sleep(0.2)
        return False, "tmux relay timed out"
    finally:
        result_file.unlink(missing_ok=True)
        script_file.unlink(missing_ok=True)
        await _kill_tmux_session(relay_session)


async def tmux_relay_shell(shell_cmd: str, timeout: float = 15.0) -> tuple[bool, str]:
    """Run a bash command via a hidden one-shot tmux session to inherit Terminal.app's FDA.

    launchd-spawned children do not inherit Full Disk Access, so accessing
    protected paths like `~/Library/Group Containers/...` or `~/Library/Messages`
    fails silently. Routing through a tmux session that was started from
    Terminal.app (which has FDA) gives the command full access.

    WARNING: shell_cmd is executed by bash inside the relay session; caller
    is responsible for quoting untrusted input. Do NOT pass user-controlled
    strings without escaping — this function intentionally supports shell
    features (pipes, redirects, globs) and makes no attempt to sandbox.
    """
    tag = uuid.uuid4().hex[:8]
    result_file = Path(f"/tmp/shellrelay-{tag}.out")

    target = None
    checked = []
    for sess in _RELAY_SESSIONS:
        checked.append(sess)
        try:
            p = await asyncio.create_subprocess_exec(
                _TMUX, "has-session", "-t", sess,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                rc = await asyncio.wait_for(p.wait(), timeout=2)
            except asyncio.TimeoutError:
                p.kill()
                await p.wait()
                rc = 1
            if rc == 0:
                target = sess
                break
        except Exception:
            continue

    if not target:
        # launchd-spawned tmux cannot inherit FDA on its own, so auto-creating
        # a fresh session would be useless for the TCC-protected paths this
        # helper exists to reach. Warn loudly and bail.
        detail = (
            f"no tmux session for relay (checked: {', '.join(checked)}). "
            f"Start a tmux session from Terminal.app named 'claude' or 'main' "
            f"so FDA is inherited by the relay."
        )
        print(f"[tmux_relay_shell] WARNING: {detail}", flush=True)
        return False, detail

    # Atomic publish: write to .tmp, then mv to final. Use a private one-shot
    # tmux session so FDA relay work never inserts shrelay-* windows into claude.
    tmp_file = Path(f"{result_file}.tmp")
    relay_session = f"shellrelay-{tag}"
    bash_cmd = f"({shell_cmd}) > {tmp_file} 2>&1; mv {tmp_file} {result_file}; exit 0"
    proc = await asyncio.create_subprocess_exec(
        _TMUX, "new-session", "-d", "-s", relay_session, "-n", "relay",
        "-c", str(Path.home()),
        f"/bin/bash -c {shlex.quote(bash_cmd)}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, _stderr = await proc.communicate()

    if proc.returncode != 0:
        print(
            f"[tmux_relay_shell] new-session failed rc={proc.returncode} "
            f"session={target} cmd={shell_cmd[:100]!r} "
            f"stderr={_stderr.decode(errors='replace').strip()[:200]}",
            flush=True,
        )
        result_file.unlink(missing_ok=True)
        tmp_file.unlink(missing_ok=True)
        return False, f"tmux new-session returned {proc.returncode}"

    try:
        for _ in range(int(timeout * 5)):
            if result_file.exists():
                output = result_file.read_text()
                result_file.unlink(missing_ok=True)
                return True, output
            await asyncio.sleep(0.2)

        print(
            f"[tmux_relay_shell] result file never appeared "
            f"session={target} cmd={shell_cmd[:100]!r} timeout={timeout}s",
            flush=True,
        )
        return False, "tmux shell relay timed out"
    finally:
        result_file.unlink(missing_ok=True)
        tmp_file.unlink(missing_ok=True)
        await _kill_tmux_session(relay_session)


async def tmux_relay_healthy() -> tuple[bool, str]:
    """Probe whether the tmux relay can reach TCC-protected paths.

    Runs `ls ~/Library/Messages/chat.db` through the relay. Returns
    (True, reason) if the chat.db path comes back without a TCC or
    missing-file error, else (False, diagnostic).
    """
    ok, output = await tmux_relay_shell(
        "ls ~/Library/Messages/chat.db 2>&1", timeout=10.0
    )
    if not ok:
        return False, f"relay unavailable: {output}"
    out = output.strip()
    if "Operation not permitted" in out:
        return False, f"relay lacks FDA: {out}"
    if "No such file" in out:
        return False, f"chat.db missing from relay view: {out}"
    if "chat.db" in out:
        return True, "relay has FDA"
    return False, f"unexpected probe output: {out[:200]}"


_PHONE_HANDLE_RE = re.compile(r"^\+?\d[\d\-\s().]{6,}$")
_EMAIL_HANDLE_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _looks_like_imessage_handle(s: str) -> bool:
    """True if `s` is already a deliverable iMessage handle (phone or email)."""
    s = s.strip()
    return bool(_PHONE_HANDLE_RE.match(s) or _EMAIL_HANDLE_RE.match(s))


def _normalize_phone(raw: str) -> str:
    """Strip spaces/parens/dashes from a phone string; keep leading +."""
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        return raw.strip()
    if raw.strip().startswith("+"):
        return "+" + digits
    # Default to US (+1) if 10 digits and no country code
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


async def _osascript(script: str, timeout: float = 15.0) -> tuple[bool, str]:
    """Run osascript directly via subprocess. Contacts/Calendar automation
    works from any context that has Automation TCC; no tmux relay needed.
    Returns (ok, stdout_stripped). On non-zero exit returns (False, stderr_or_stdout).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return False, "osascript timed out"
    except Exception as e:
        return False, f"osascript spawn failed: {e}"
    if proc.returncode != 0:
        return False, (err or out).decode("utf-8", "replace").strip()
    return True, out.decode("utf-8", "replace").strip()


async def contacts_lookup(name: str, fields: list[str] | None = None) -> dict:
    """General Contacts.app lookup. Returns a matched contact with requested fields.

    Not limited to iMessage — callers pass `fields` to pull birthday, address,
    organization, notes, etc. Matching logic mirrors resolve_imessage_buddy:
    case-insensitive exact on full name first, fallback to substring. Zero hits
    → fuzzy difflib suggestions. >1 hits → ambiguous result.

    `fields` accepts: "phones", "emails", "birthday", "addresses", "organization",
    "job_title", "note", "urls", "nickname". Default pulls phones + emails.

    Returns:
      {"ok": True, "name": "Joe Burkhardt", "fields": {"phones":[...], ...}}
      {"ok": False, "reason": "not_found", "suggestions": [...]}
      {"ok": False, "reason": "ambiguous", "matches": [{"name":..., "phones":[...]}, ...]}
      {"ok": False, "reason": "lookup_failed", "error": "..."}
    """
    s = (name or "").strip()
    if not s:
        return {"ok": False, "reason": "empty", "input": name}
    requested = set(fields or ["phones", "emails"])

    field_scripts = {
        "phones":       'try\n    repeat with x in phones of p\n      set out to out & "phones=" & (value of x as text) & (ASCII character 31)\n    end repeat\n  end try\n  ',
        "emails":       'try\n    repeat with x in emails of p\n      set out to out & "emails=" & (value of x as text) & (ASCII character 31)\n    end repeat\n  end try\n  ',
        "birthday":     'try\n    set bd to birth date of p\n    if bd is not missing value then set out to out & "birthday=" & (bd as text) & (ASCII character 31)\n  end try\n  ',
        "addresses":    'try\n    repeat with x in addresses of p\n      set out to out & "addresses=" & (formatted address of x) & (ASCII character 31)\n    end repeat\n  end try\n  ',
        "organization": 'try\n    if organization of p is not missing value then set out to out & "organization=" & (organization of p as text) & (ASCII character 31)\n  end try\n  ',
        "job_title":    'try\n    if job title of p is not missing value then set out to out & "job_title=" & (job title of p as text) & (ASCII character 31)\n  end try\n  ',
        "note":         'try\n    if note of p is not missing value then set out to out & "note=" & (note of p as text) & (ASCII character 31)\n  end try\n  ',
        "urls":         'try\n    repeat with x in urls of p\n      set out to out & "urls=" & (value of x as text) & (ASCII character 31)\n    end repeat\n  end try\n  ',
        "nickname":     'try\n    if nickname of p is not missing value then set out to out & "nickname=" & (nickname of p as text) & (ASCII character 31)\n  end try\n  ',
    }
    inner = "".join(field_scripts[f] for f in requested if f in field_scripts)
    safe = s.replace('\\', '\\\\').replace('"', '\\"')
    script = (
        'launch application "Contacts"\n'
        'tell application "Contacts"\n'
        '  if not running then launch\n'
        '  set out to ""\n'
        f'  set matches to (every person whose name is "{safe}")\n'
        '  if (count of matches) is 0 then\n'
        f'    set matches to (every person whose name contains "{safe}")\n'
        '  end if\n'
        '  repeat with p in matches\n'
        '    set out to out & (name of p as text) & (ASCII character 31)\n'
        f'    {inner}'
        '    set out to out & (ASCII character 30)\n'
        '  end repeat\n'
        '  return out\n'
        'end tell\n'
    )
    ok, output = await _osascript(script, timeout=20.0)
    if not ok:
        return {"ok": False, "reason": "lookup_failed", "error": output, "input": name}

    parsed: list[dict] = []
    for record in output.split("\x1e"):
        record = record.strip("\n\r\t ")
        if not record:
            continue
        parts = [p for p in record.split("\x1f") if p]
        if not parts:
            continue
        entry: dict = {"name": parts[0]}
        for f in requested:
            entry[f] = []
        for field in parts[1:]:
            if "=" not in field:
                continue
            key, _, val = field.partition("=")
            if key == "phones":
                entry.setdefault("phones", []).append(_normalize_phone(val))
            elif key in ("emails", "addresses", "urls"):
                entry.setdefault(key, []).append(val.strip())
            else:
                entry[key] = val.strip()
        parsed.append(entry)

    if not parsed:
        suggestions = await _fuzzy_contact_suggestions(s)
        return {"ok": False, "reason": "not_found", "input": name, "suggestions": suggestions}

    low = s.lower()
    exact = [p for p in parsed if p["name"].lower() == low]
    pool = exact or parsed
    if len(pool) > 1:
        return {"ok": False, "reason": "ambiguous", "input": name, "matches": pool}
    hit = pool[0]
    return {"ok": True, "name": hit["name"], "fields": {k: v for k, v in hit.items() if k != "name"}}


async def resolve_imessage_buddy(name: str) -> dict:
    """Resolve a display name to a deliverable iMessage handle via Contacts.app.

    Prevents the silent-failure mode where Messages.app accepts a misspelled
    name as a participant, creates a phantom handle, and sends nothing.

    Returns a dict:
      {"ok": True, "handle": "+12404056533", "display": "Joe Burkhardt",
       "passthrough": False}
        exact match, one contact, one usable phone/email
      {"ok": True, "handle": "+12404056533", "display": "+12404056533",
       "passthrough": True}
        input already looked like a phone/email — pass through unchecked
      {"ok": False, "reason": "not_found", "input": name,
       "suggestions": ["Joe Burkhardt", "Joe French"]}
      {"ok": False, "reason": "ambiguous", "input": name,
       "matches": [{"name": "Joe Burkhardt", "handles": [...]}, ...]}
      {"ok": False, "reason": "no_handle", "input": name, "display": "..."}
        contact exists but has no phone or email
      {"ok": False, "reason": "lookup_failed", "error": "..."}
    """
    s = (name or "").strip()
    if not s:
        return {"ok": False, "reason": "empty", "input": name}
    if _looks_like_imessage_handle(s):
        handle = _normalize_phone(s) if _PHONE_HANDLE_RE.match(s) else s
        return {"ok": True, "handle": handle, "display": handle, "passthrough": True}

    lookup = await contacts_lookup(s, fields=["phones", "emails"])
    if not lookup.get("ok"):
        reason = lookup.get("reason")
        if reason == "ambiguous":
            return {
                "ok": False,
                "reason": "ambiguous",
                "input": name,
                "matches": [
                    {"name": m["name"], "handles": (m.get("phones") or []) + (m.get("emails") or [])}
                    for m in lookup.get("matches", [])
                ],
            }
        return {**lookup, "input": name}

    fields = lookup.get("fields", {})
    phones = fields.get("phones") or []
    emails = fields.get("emails") or []
    handle = phones[0] if phones else (emails[0] if emails else None)
    if not handle:
        return {"ok": False, "reason": "no_handle", "input": name, "display": lookup["name"]}
    return {"ok": True, "handle": handle, "display": lookup["name"], "passthrough": False}


async def _fuzzy_contact_suggestions(query: str, cutoff: float = 0.55, n: int = 5) -> list[str]:
    """Pull all contact names and difflib-rank closest matches to `query`."""
    script = (
        'launch application "Contacts"\n'
        'tell application "Contacts"\n'
        '  if not running then launch\n'
        '  set out to ""\n'
        '  repeat with p in every person\n'
        '    set out to out & (name of p as text) & (ASCII character 30)\n'
        '  end repeat\n'
        '  return out\n'
        'end tell\n'
    )
    ok, output = await _osascript(script, timeout=20.0)
    if not ok:
        return []
    names = [n.strip() for n in output.split("\x1e") if n.strip()]
    if not names:
        return []
    return difflib.get_close_matches(query, names, n=n, cutoff=cutoff)


async def _chatdb_service_for_handle(handle: str) -> str | None:
    """Query chat.db for the most-recent successful outbound service used with handle.

    Returns 'iMessage', 'SMS', 'RCS', or None (no history / relay unavailable).
    'RCS' means the SMS service type should be used in osascript (Mac Continuity
    routes both SMS and RCS through the same SMS account).

    Tries the handle as-is, then with a leading +1 if it looks like a 10-digit US
    number without one, to handle normalization mismatches.
    """
    handles_to_try = [handle]
    digits_only = re.sub(r"\D", "", handle)
    if len(digits_only) == 10:
        handles_to_try.append(f"+1{digits_only}")
    elif len(digits_only) == 11 and digits_only.startswith("1") and not handle.startswith("+"):
        handles_to_try.append(f"+{digits_only}")

    for h in handles_to_try:
        safe = h.replace("'", "''")
        sql = (
            f"SELECT m.service, MAX(m.date) AS last "
            f"FROM message m JOIN handle h ON m.handle_id = h.ROWID "
            f"WHERE h.id = '{safe}' AND m.is_from_me = 1 AND m.error = 0 "
            f"GROUP BY m.service ORDER BY last DESC LIMIT 1;"
        )
        ok, output = await tmux_relay_shell(
            f"sqlite3 ~/Library/Messages/chat.db {shlex.quote(sql)}", timeout=8.0
        )
        if not ok:
            return None
        row = output.strip()
        if row:
            service = row.split("|")[0].strip()
            return service
    return None


async def _chatdb_verify_send(handle: str, text: str, after_mac_ts: int, timeout: float = 8.0) -> tuple[str, int] | None:
    """Poll chat.db for the outbound row matching this send.

    after_mac_ts is Mac absolute time in nanoseconds (date column units).
    Returns (service, error_code) once a row appears, or None on timeout.

    Note: chat.db stores message body in attributedBody blob (not text column)
    for both iMessage and RCS/SMS on modern macOS. We match by timestamp and
    is_from_me only — the timestamp window is tight (pre_ts captured just
    before osascript fires) so false matches are not a practical concern.
    """
    handles_to_try = [handle]
    digits_only = re.sub(r"\D", "", handle)
    if len(digits_only) == 10:
        handles_to_try.append(f"+1{digits_only}")
    elif len(digits_only) == 11 and digits_only.startswith("1") and not handle.startswith("+"):
        handles_to_try.append(f"+{digits_only}")

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(1.5)
        for h in handles_to_try:
            safe_h = h.replace("'", "''")
            sql = (
                f"SELECT m.service, m.error FROM message m "
                f"JOIN handle h ON m.handle_id = h.ROWID "
                f"WHERE h.id = '{safe_h}' AND m.is_from_me = 1 "
                f"AND m.date > {after_mac_ts} "
                f"ORDER BY m.date DESC LIMIT 1;"
            )
            ok, output = await tmux_relay_shell(
                f"sqlite3 ~/Library/Messages/chat.db {shlex.quote(sql)}", timeout=6.0
            )
            if not ok:
                continue
            row = output.strip()
            if row:
                parts = row.split("|")
                try:
                    return parts[0].strip(), int(parts[1].strip())
                except (IndexError, ValueError):
                    pass
    return None


def _mac_now_ns() -> int:
    """Current time as Mac absolute nanoseconds (chat.db date column units)."""
    import time
    return int((time.time() - 978307200) * 1e9)


async def _send_via_osascript_service(buddy: str, escaped_msg: str, service_type: str) -> tuple[bool, str]:
    """Send via the named osascript service type ('iMessage' or 'SMS')."""
    if service_type == "iMessage":
        script = (
            f'launch application "Messages"\n'
            f'tell application "Messages"\n'
            f'  if not running then launch\n'
            f'  set targetService to first service whose service type is iMessage\n'
            f'  set targetBuddy to buddy "{buddy}" of targetService\n'
            f'  send "{escaped_msg}" to targetBuddy\n'
            f'  return "ok"\n'
            f'end tell'
        )
    else:
        script = (
            f'launch application "Messages"\n'
            f'tell application "Messages"\n'
            f'  if not running then launch\n'
            f'  set targetService to first service whose service type is SMS\n'
            f'  set targetBuddy to buddy "{buddy}" of targetService\n'
            f'  send "{escaped_msg}" to targetBuddy\n'
            f'  return "ok"\n'
            f'end tell'
        )
    return await _tmux_relay_osascript(script, timeout=30.0)


_IMESSAGE_ERROR_CODES = {1, 22, 102, 1032}


async def send_imessage_reliable(buddy: str, message: str, _approved: bool = False, _require_approval: bool = False) -> tuple[bool, str]:
    """Send iMessage or SMS via tmux relay, with chat.db delivery verification.

    Structurally incapable of returning (True, ...) unless chat.db confirms
    a row with error=0 for this send. Auto-routes to SMS when iMessage is not
    viable (Layer 1: prior history shows SMS/RCS; Layer 2: post-send error code).

    Third-party recipients route through core.outbox for APPROVE/DENY preview
    unless _approved=True (reply-router promotion path) or the recipient is one
    of John's own self-identifiers.

    If `buddy` is a display name (not phone/email), it is resolved against
    Contacts.app first. Unresolvable or ambiguous names short-circuit with a
    helpful error + fuzzy suggestions rather than silently sending to a
    phantom handle.
    """
    display = buddy
    if not _approved and not _looks_like_imessage_handle(buddy):
        from core.outbox import _is_self
        if not _is_self(buddy):
            resolved = await resolve_imessage_buddy(buddy)
            if not resolved.get("ok"):
                reason = resolved.get("reason", "unknown")
                if reason == "not_found":
                    sug = resolved.get("suggestions") or []
                    hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
                    return False, f"No Contacts match for '{buddy}'.{hint}"
                if reason == "ambiguous":
                    opts = "; ".join(
                        f"{m['name']} ({', '.join(m['handles']) or 'no handle'})"
                        for m in resolved.get("matches", [])
                    )
                    return False, f"Ambiguous contact '{buddy}'. Candidates: {opts}"
                if reason == "no_handle":
                    return False, f"Contact '{resolved.get('display', buddy)}' has no phone or email."
                return False, f"Contact lookup failed for '{buddy}': {resolved.get('error','')}"
            buddy = resolved["handle"]
            display = resolved.get("display", buddy)

    if _require_approval and not _approved:
        from core.outbox import _is_self, queue_or_send
        if not _is_self(buddy):
            result = await queue_or_send(
                channel="imessage",
                recipient=f"{display} <{buddy}>" if display and display != buddy else buddy,
                subject=None,
                body=message,
                source="send_imessage_reliable",
                send_fn=send_imessage_reliable,
                send_fn_module="core.tools",
                send_fn_name="send_imessage_reliable",
                send_kwargs={"buddy": buddy, "message": message},
            )
            status = result.get("status")
            if status == "queued":
                return False, f"NOT SENT — outbox queued {result['uuid']} awaiting John's APPROVE/DENY"
            if status == "preview_undeliverable":
                return False, f"NOT SENT — preview channel dead ({result.get('hint','')}). imsg_err={result.get('imsg_err')} push_err={result.get('push_err')}"
            if status == "sent_direct":
                inner = result.get("result") or (True, "sent")
                return inner if isinstance(inner, tuple) else (True, str(inner))

    escaped_msg = message.replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))

    # Layer 1 — pre-flight: check chat.db for prior send history on this handle.
    # If the most-recent successful service is SMS or RCS, skip iMessage entirely.
    prior_service = await _chatdb_service_for_handle(buddy)
    if prior_service in ("SMS", "RCS"):
        first_service = "SMS"
    else:
        first_service = "iMessage"

    pre_ts = _mac_now_ns()

    ok, output = await _send_via_osascript_service(buddy, escaped_msg, first_service)
    if not ok:
        if first_service == "iMessage":
            ok2, output2 = await _send_via_osascript_service(buddy, escaped_msg, "SMS")
            if not ok2:
                return False, f"iMessage relay failed ({output}); SMS relay also failed ({output2})"
            attempted_service = "SMS"
            osascript_ok = True
        else:
            return False, f"SMS relay failed: {output}"
    else:
        attempted_service = first_service
        osascript_ok = True

    # Layer 2 — post-send verification: query chat.db for the outbound row.
    row = await _chatdb_verify_send(buddy, message, pre_ts, timeout=8.0)
    if row is None:
        return False, f"no chat.db record after 8s — send did not register (osascript said ok={osascript_ok})"

    actual_service, error_code = row

    if error_code == 0:
        svc_label = "iMessage" if actual_service == "iMessage" else "SMS"
        return True, f"Delivered via {svc_label} to {buddy}"

    # iMessage error — try SMS fallback if we haven't already
    if actual_service == "iMessage" and error_code in _IMESSAGE_ERROR_CODES and attempted_service == "iMessage":
        pre_ts2 = _mac_now_ns()
        ok3, output3 = await _send_via_osascript_service(buddy, escaped_msg, "SMS")
        if not ok3:
            return False, f"iMessage failed (error={error_code}), SMS fallback relay also failed: {output3}"
        row2 = await _chatdb_verify_send(buddy, message, pre_ts2, timeout=8.0)
        if row2 is None:
            return False, f"iMessage failed (error={error_code}), SMS fallback: no chat.db record after 8s"
        svc2, err2 = row2
        if err2 == 0:
            return True, f"Delivered via SMS to {buddy} (iMessage error={error_code}, auto-fallback)"
        return False, f"iMessage failed (error={error_code}), SMS fallback also failed (error={err2})"

    return False, f"send failed on {actual_service} (error={error_code})"

async def send_imessage(args: dict[str, Any]) -> dict:
    try:
        agent = args.get("agent", "unknown")
        tier = args.get("tier", "normal")
        from core.message_bus import send_message
        ok, result = await send_message(
            message=args["message"],
            agent=agent,
            recipient=args["buddy"],
            tier=tier,
        )
        return {"content": [{"type": "text", "text": result}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"send_imessage error: {e}"}]}


@tool(
    "send_business_email",
    "Send a business email from info@sentryaithermal.com via sentry-mailqueue on VPS.",
    {"to": str, "subject": str, "body": str},
)
async def send_business_email(args: dict[str, Any]) -> dict:
    try:
        to_addr = args["to"]
        subject = args["subject"]
        body = args["body"]
        if args.get("_require_approval") and not args.get("_approved"):
            from core.outbox import _is_self, queue_or_send
            if not _is_self(to_addr):
                outbox_kwargs = {k: v for k, v in args.items() if k != "_approved"}
                result = await queue_or_send(
                    channel="email_business",
                    recipient=to_addr,
                    subject=subject,
                    body=body,
                    source="send_business_email",
                    send_fn=send_business_email,
                    send_fn_module="core.tools",
                    send_fn_name="send_business_email",
                    send_kwargs={"args": outbox_kwargs},
                )
                status = result.get("status")
                if status == "queued":
                    return {"content": [{"type": "text", "text": f"NOT SENT — outbox queued {result['uuid']} awaiting John's APPROVE/DENY"}]}
                if status == "preview_undeliverable":
                    return {"content": [{"type": "text", "text": f"NOT SENT — preview channel dead. imsg_err={result.get('imsg_err')} push_err={result.get('push_err')}. {result.get('hint','')}"}]}
                if status == "sent_direct":
                    return result.get("result") or {"content": [{"type": "text", "text": "sent"}]}
        queue_cmd = (
            f"cd /srv/apps/sentry-mailqueue && "
            f".venv/bin/python queue_cli.py --to {shlex.quote(to_addr)} --subject {shlex.quote(subject)} --body {shlex.quote(body)} && "
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
    except Exception as e:
        return {"content": [{"type": "text", "text": f"send_business_email error: {e}"}]}


@tool(
    "send_personal_email",
    "Send a personal or system email from notify@jcornelius.net (Cornelius Family). Use for briefs, alerts, family communications.",
    {"to": {"type": "string", "description": "recipient email address"},
     "subject": {"type": "string", "description": "email subject"},
     "body": {"type": "string", "description": "email body (plain text or HTML)"},
     "html": {"type": "boolean", "description": "treat body as HTML", "default": False}},
)
async def send_personal_email(args: dict[str, Any]) -> dict:
    import base64
    to = args["to"]
    subject = args["subject"]
    body = args["body"]
    if args.get("_require_approval") and not args.get("_approved"):
        from core.outbox import _is_self, queue_or_send
        if not _is_self(to):
            outbox_kwargs = {k: v for k, v in args.items() if k != "_approved"}
            result = await queue_or_send(
                channel="email_personal",
                recipient=to,
                subject=subject,
                body=body,
                source="send_personal_email",
                send_fn=send_personal_email,
                send_fn_module="core.tools",
                send_fn_name="send_personal_email",
                send_kwargs={"args": outbox_kwargs},
            )
            status = result.get("status")
            if status == "queued":
                return {"content": [{"type": "text", "text": f"NOT SENT — outbox queued {result['uuid']} awaiting John's APPROVE/DENY"}]}
            if status == "preview_undeliverable":
                return {"content": [{"type": "text", "text": f"NOT SENT — preview channel dead. imsg_err={result.get('imsg_err')} push_err={result.get('push_err')}. {result.get('hint','')}"}]}
            if status == "sent_direct":
                return result.get("result") or {"content": [{"type": "text", "text": "sent"}]}
    html_flag = "--html" if args.get("html") else ""
    b64 = base64.b64encode(body.encode()).decode()
    cmd = f"send-email --from notify@jcornelius.net --to {shlex.quote(to)} --subject {shlex.quote(subject)} --body-b64 {b64} {html_flag}".strip()
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "vps", cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return {"content": [{"type": "text", "text": f"Email failed: {stderr.decode().strip()}"}]}
        return {"content": [{"type": "text", "text": f"Personal email sent to {to}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"send_personal_email error: {e}"}]}


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
    "contact_lookup",
    "Look up a person in Contacts.app by display name. Returns phones, emails, "
    "birthday, address, organization, etc. — whatever `fields` you ask for. "
    "Use this before sending iMessage/email to a display name, or when asked "
    "to 'look up X's birthday/address/number.' Zero matches returns fuzzy "
    "suggestions; multiple matches returns all candidates for disambiguation.",
    {"name": str, "fields": str},
)
async def contact_lookup(args: dict[str, Any]) -> dict:
    name = args.get("name", "").strip()
    raw_fields = args.get("fields", "") or ""
    fields = [f.strip() for f in raw_fields.split(",") if f.strip()] or None
    try:
        result = await contacts_lookup(name, fields=fields)
    except Exception as e:
        return {"content": [{"type": "text", "text": f"contact_lookup error: {e}"}]}
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}]}


@tool(
    "moshi_push",
    "Send a push notification via Pushover (Moshi).",
    {"title": str, "message": str},
)
async def moshi_push(args: dict[str, Any]) -> dict:
    try:
        title = shlex.quote(args["title"])
        message = shlex.quote(args["message"])
        # Uses the notify-moshi.sh script if available, otherwise curl
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c",
            f'if [ -x ~/bin/notify-moshi.sh ]; then ~/bin/notify-moshi.sh {title} {message}; '
            f'else echo "notify-moshi.sh not found"; fi',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return {"content": [{"type": "text", "text": stdout.decode().strip() or "Push sent"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"moshi_push error: {e}"}]}


@tool(
    "swarm_context_write",
    "Store a key-value pair in the shared swarm context. Other agents in this swarm can read it.",
    {"key": str, "value": str},
)
async def swarm_context_write(args: dict[str, Any]) -> dict:
    try:
        ctx = _get_active_context()
        ctx.write(args["key"], args["value"])
        return {"content": [{"type": "text", "text": f"Stored '{args['key']}' in swarm context"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"swarm_context_write error: {e}"}]}


@tool(
    "swarm_context_read",
    "Read a value from the shared swarm context by key. Returns empty string if key not found.",
    {"key": str},
)
async def swarm_context_read(args: dict[str, Any]) -> dict:
    try:
        ctx = _get_active_context()
        value = ctx.read(args["key"])
        if value is None:
            return {"content": [{"type": "text", "text": f"Key '{args['key']}' not found in swarm context"}]}
        return {"content": [{"type": "text", "text": value}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"swarm_context_read error: {e}"}]}


@tool(
    "swarm_context_list",
    "List all keys in the shared swarm context.",
    {},
)
async def swarm_context_list(args: dict[str, Any]) -> dict:
    try:
        ctx = _get_active_context()
        keys = ctx.list_keys()
        if not keys:
            return {"content": [{"type": "text", "text": "Swarm context is empty"}]}
        return {"content": [{"type": "text", "text": "Keys: " + ", ".join(keys)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"swarm_context_list error: {e}"}]}


@tool(
    "get_schedule",
    "Get today's schedule with events, reminders, free slots, and current status.",
    {},
)
async def get_schedule(args: dict[str, Any]) -> dict:
    try:
        from core.calendar_service import get_schedule_view
        view = await get_schedule_view()
        return {"content": [{"type": "text", "text": json.dumps(view.to_dict(), indent=2)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"get_schedule error: {e}"}]}


@tool(
    "get_week_view",
    "Get 7-day calendar lookahead with per-day summaries, free hours, and key events.",
    {},
)
async def get_week_view_tool(args: dict[str, Any]) -> dict:
    try:
        from core.calendar_service import get_week_view
        view = await get_week_view()
        return {"content": [{"type": "text", "text": json.dumps(view.to_dict(), indent=2)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"get_week_view error: {e}"}]}


@tool(
    "check_calendar",
    "Check calendar availability for a time range. Returns free slots and conflicts.",
    {"start": str, "end": str},
)
async def check_calendar(args: dict[str, Any]) -> dict:
    try:
        from datetime import datetime as dt
        from core.calendar_service import check_availability, detect_conflicts
        start = dt.fromisoformat(args["start"])
        end = dt.fromisoformat(args["end"])
        free, conflicts = await asyncio.gather(
            check_availability(start, end),
            detect_conflicts(start, end),
        )
        result = {
            "free_slots": [s.to_dict() for s in free],
            "conflicts": [e.to_dict() for e in conflicts],
        }
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"check_calendar error: {e}"}]}


@tool(
    "create_calendar_event",
    "Create a new calendar event. Defaults to Google CalDAV calendar for sync.",
    {"summary": str, "start": str, "end": str, "calendar": str, "location": str, "notes": str},
)
async def create_calendar_event(args: dict[str, Any]) -> dict:
    try:
        from datetime import datetime as dt
        from core.calendar_service import create_event
        start = dt.fromisoformat(args["start"])
        end = dt.fromisoformat(args["end"])
        ok = await create_event(
            summary=args["summary"],
            start=start,
            end=end,
            calendar=args.get("calendar", "notify@jcornelius.net"),
            location=args.get("location", ""),
            notes=args.get("notes", ""),
        )
        status = "Event created" if ok else "Failed to create event"
        return {"content": [{"type": "text", "text": status}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"create_calendar_event error: {e}"}]}


# --- Apple Reminders ---
# John's 6 owned lists (see CLAUDE.md "Apple Reminders — Canonical Lists").
# J&A Reminders and Store are SHARED with wife — NEVER write to them.
REMINDER_LISTS = {"Home", "Health", "Work", "Sentry AI", "Claude", "Someday"}
REMINDER_LIST_GUIDE = (
    "Home=personal/domestic/family/chores; "
    "Health=medical only (appts, shots, Rx); "
    "Work=day job (TAMKO); "
    "Sentry AI=business/federal/tax/SAM.gov/client work; "
    "Claude=tech action items Claude creates for manual execution (Touch ID etc); "
    "Someday=no-date aspirational backlog"
)


@tool(
    "add_reminder",
    "Add an Apple Reminder to one of John's owned lists. "
    f"list MUST be one of: Home, Health, Work, Sentry AI, Claude, Someday. {REMINDER_LIST_GUIDE}. "
    "due is ISO 8601 (e.g. 2026-04-18T09:00:00) — if omitted, defaults to tomorrow 9am "
    "so the item surfaces in Apple's built-in Today smart view. "
    "NEVER use list 'J&A Reminders' or 'Store' (shared with wife).",
    {"name": str, "list": str, "body": str, "due": str},
)
async def add_reminder(args: dict[str, Any]) -> dict:
    try:
        from datetime import datetime, timedelta

        name = (args.get("name") or "").strip()
        list_name = (args.get("list") or "").strip()
        body = args.get("body") or ""
        due = (args.get("due") or "").strip()

        if not name:
            return {"content": [{"type": "text", "text": "add_reminder error: name is required"}]}
        if list_name not in REMINDER_LISTS:
            return {"content": [{"type": "text", "text": (
                f"add_reminder error: list must be one of {sorted(REMINDER_LISTS)} — got '{list_name}'. "
                f"NEVER write to 'J&A Reminders' or 'Store' (shared w/ wife). Domain guide: {REMINDER_LIST_GUIDE}"
            )}]}

        if due:
            try:
                due_dt = datetime.fromisoformat(due)
            except ValueError:
                return {"content": [{"type": "text", "text": (
                    f"add_reminder error: due must be ISO 8601 (e.g. 2026-04-18T09:00:00), got '{due}'"
                )}]}
        else:
            tomorrow_9am = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
            due_dt = tomorrow_9am

        due_applescript = due_dt.strftime('%B %d, %Y %I:%M:%S %p')

        def _esc(s: str) -> str:
            return s.replace('\\', '\\\\').replace('"', '\\"')

        props = f'name:"{_esc(name)}", due date:date "{due_applescript}"'
        if body:
            props += f', body:"{_esc(body)}"'

        script = (
            f'launch application "Reminders"\n'
            f'tell application "Reminders"\n'
            f'  tell list "{_esc(list_name)}"\n'
            f'    make new reminder with properties {{{props}}}\n'
            f'  end tell\n'
            f'end tell\n'
        )

        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        if proc.returncode != 0:
            return {"content": [{"type": "text", "text": f"add_reminder failed: {stderr.decode().strip()}"}]}
        due_display = due_dt.strftime('%a %b %d at %I:%M %p').replace(' 0', ' ')
        return {"content": [{"type": "text", "text": f"Reminder added → {list_name}: '{name}' (due {due_display})"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"add_reminder error: {e}"}]}


def create_core_server():
    """Create the in-process MCP server with all core tools + swarm context."""
    return create_sdk_mcp_server(
        name="core-tools",
        version="1.3.0",
        tools=[
            ssh_command,
            send_imessage,
            send_business_email,
            send_personal_email,
            osascript_run,
            contact_lookup,
            moshi_push,
            get_schedule,
            get_week_view_tool,
            check_calendar,
            create_calendar_event,
            add_reminder,
            swarm_context_write,
            swarm_context_read,
            swarm_context_list,
        ],
    )
