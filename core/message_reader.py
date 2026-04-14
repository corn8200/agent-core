"""Unified inbound iMessage reader — polls chat.db via tmux relay.

One reader, multiple subscribers. Replaces research-chain/main.py as the
single listener process. Uses tmux_relay_shell for FDA-protected chat.db access.
"""

import asyncio
import base64
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from core.message_db import log_inbound
from core.tools import tmux_relay_shell


# Leading-garbage-tolerant match for bus attribution: "[AgentName]".
# The attributedBody hex extractor sometimes pastes a junk char before the
# real text, so we allow any non-letter prefix followed by "[Word]".
_BOT_ATTRIBUTION_RE = re.compile(r"^[^A-Za-z0-9]?\[[A-Z][A-Za-z0-9 _-]{1,30}\]")


def _looks_like_bot_attribution(text: str) -> bool:
    if not text:
        return False
    return bool(_BOT_ATTRIBUTION_RE.match(text.lstrip()))


# Control-character delimiters that cannot appear in iMessage text bodies.
# 0x1f = ASCII Unit Separator (between fields)
# 0x1e = ASCII Record Separator (between rows)
# Using these instead of "|||"/newline makes parsing immune to message
# bodies containing pipes, parens, or embedded newlines (e.g. forwarded
# multi-line content like "(shared from GROUNDTRUTH)\nsome text").
_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"


def _sqlite_via_relay_cmd(sql: str, db_path: str = "~/Library/Messages/chat.db") -> str:
    """Build a shell command that pipes base64-encoded SQL into sqlite3.

    Avoids shell quoting pitfalls — SQL can contain arbitrary single/double
    quotes without escaping. The relay runs the result under bash.
    """
    encoded = base64.b64encode(sql.encode("utf-8")).decode("ascii")
    return (
        f"echo {encoded} | base64 -d | "
        f"sqlite3 -separator $'\\x1f' -newline $'\\x1e' {db_path}"
    )


STATE_FILE = Path.home() / ".imessage_bus_state"
LEGACY_STATE = Path.home() / ".research_chain_state"
SELF_CHATS = ("corn82@icloud.com", "+13042684985")
DB_PATH = "~/Library/Messages/chat.db"


@dataclass
class InboundMessage:
    rowid: int
    chat_identifier: str
    text: str
    timestamp: str
    is_from_me: bool


def extract_text_from_attributed_body(hex_str: str) -> str | None:
    """Extract plain text from hex-encoded attributedBody (typedstream format).

    Ported from research-chain/main.py — proven extraction logic.
    """
    if not hex_str:
        return None
    try:
        blob = bytes.fromhex(hex_str)
        runs = []
        current = bytearray()
        for b in blob:
            if (0x20 <= b <= 0x7E) or b in (0x0A, 0x0D, 0x09):
                current.append(b)
            else:
                if len(current) > 1:
                    runs.append(bytes(current).decode("ascii"))
                current = bytearray()
        if len(current) > 1:
            runs.append(bytes(current).decode("ascii"))

        skip = {
            "streamtyped", "NSAttributedString", "NSMutableAttributedString",
            "NSObject", "NSString", "NSMutableString", "NSDictionary",
            "NSMutableDictionary", "NSParagraphStyle", "NSMutableParagraphStyle",
            "NSFont", "NSColor", "NSNumber", "NSValue", "NSUUID",
        }
        for run in runs:
            cleaned = run.strip("+").strip()
            if (cleaned and len(cleaned) > 3
                    and cleaned not in skip
                    and not cleaned.startswith("__kIM")
                    and not cleaned.startswith("$")):
                return cleaned
        return None
    except Exception:
        return None


class MessageReader:
    """Polls chat.db via tmux_relay_shell, publishes new messages to subscribers."""

    def __init__(self, poll_interval: int = 5):
        self._subscribers: list[Callable[[InboundMessage], Awaitable[None]]] = []
        self._poll_interval = poll_interval
        self._monitored_chats = set(SELF_CHATS)

    def subscribe(self, callback: Callable[[InboundMessage], Awaitable[None]]):
        """Register a handler for inbound messages."""
        self._subscribers.append(callback)

    def _get_last_rowid(self) -> int:
        # Migration: if our state file doesn't exist but legacy does, copy it
        if not STATE_FILE.exists() and LEGACY_STATE.exists():
            try:
                val = LEGACY_STATE.read_text().strip()
                STATE_FILE.write_text(val)
                print(f"[reader] Migrated state from {LEGACY_STATE}: rowid={val}")
            except Exception:
                pass
        try:
            return int(STATE_FILE.read_text().strip())
        except (FileNotFoundError, ValueError):
            return 0

    def _save_rowid(self, rowid: int):
        STATE_FILE.write_text(str(rowid))

    async def _poll_once(self) -> list[InboundMessage]:
        """Query chat.db for new messages via tmux relay."""
        last = self._get_last_rowid()
        chat_list = ", ".join(f"'{c}'" for c in self._monitored_chats)

        # Self-chats: accept BOTH directions. When you text yourself from
        # another device (iPhone/Watch), on this Mac the message arrives as
        # is_from_me=0 — filtering on is_from_me=1 would lose those commands.
        query = (
            f"SELECT m.ROWID, m.text, hex(m.attributedBody), m.is_from_me, "
            f"c.chat_identifier, "
            f"datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime') "
            f"FROM message m "
            f"JOIN chat_message_join cmj ON m.ROWID = cmj.message_id "
            f"JOIN chat c ON cmj.chat_id = c.ROWID "
            f"WHERE m.ROWID > {last} "
            f"AND c.chat_identifier IN ({chat_list}) "
            f"ORDER BY m.ROWID ASC;"
        )

        ok, output = await tmux_relay_shell(
            _sqlite_via_relay_cmd(query),
            timeout=10.0,
        )

        if not ok:
            print(f"[reader] relay failed: {output[:200]}")
            return []

        # sqlite3 writes parse errors to stdout with exit 0 — catch them.
        if output.startswith("Error:") or "\nError:" in output:
            print(f"[reader] sqlite error: {output[:200]}")
            return []

        messages = []
        # Strip a trailing record separator (sqlite3 emits one after the last row).
        raw = output.rstrip(_RECORD_SEP).rstrip("\n")
        if not raw:
            return []
        for record in raw.split(_RECORD_SEP):
            record = record.strip("\n")
            if not record or _FIELD_SEP not in record:
                continue
            parts = record.split(_FIELD_SEP)
            if len(parts) < 4:
                continue

            try:
                rowid = int(parts[0])
            except (ValueError, TypeError):
                # Defensive: if the first field isn't an int, the row is
                # malformed (should be impossible with control-char delimiters,
                # but skip + log rather than crash the whole poll loop).
                print(f"[reader] Skipping malformed row (bad ROWID): {parts[0][:60]!r}")
                continue
            text = parts[1] if len(parts) > 1 else ""
            hex_body = parts[2] if len(parts) > 2 else ""
            is_from_me = parts[3] == "1" if len(parts) > 3 else True
            chat_id = parts[4] if len(parts) > 4 else ""
            timestamp = parts[5] if len(parts) > 5 else datetime.now().isoformat()

            # Extract text from attributedBody if needed
            if not text and hex_body:
                text = extract_text_from_attributed_body(hex_body)

            if not text:
                # Save rowid to skip empty messages
                self._save_rowid(rowid)
                continue

            # Drop the bot's own outbound messages. The message bus prepends
            # an [AgentName] attribution tag — if we see one in a
            # from-me message in a self-chat, it's a reply we just sent and
            # must not be re-routed (feedback loop).
            if is_from_me and _looks_like_bot_attribution(text):
                self._save_rowid(rowid)
                continue

            messages.append(InboundMessage(
                rowid=rowid,
                chat_identifier=chat_id,
                text=text,
                timestamp=timestamp,
                is_from_me=is_from_me,
            ))

        return messages

    async def poll_loop(self):
        """Main loop. Reads chat.db via tmux relay, fans out to subscribers."""
        print(f"[reader] Started — polling every {self._poll_interval}s")
        print(f"[reader] Monitoring: {', '.join(self._monitored_chats)}")
        print(f"[reader] State file: {STATE_FILE}")

        poll_count = 0
        while True:
            try:
                messages = await self._poll_once()
                poll_count += 1

                if poll_count <= 3 or messages:
                    print(f"[reader] Poll #{poll_count}: {len(messages)} messages")

                for msg in messages:
                    print(f"[reader] ROWID {msg.rowid}: {msg.text[:80]}")

                    # Log to bus DB
                    log_inbound(
                        chat_db_rowid=msg.rowid,
                        chat_identifier=msg.chat_identifier,
                        text=msg.text,
                        received_at=msg.timestamp,
                    )

                    # Fan out to subscribers
                    for callback in self._subscribers:
                        try:
                            await callback(msg)
                        except Exception as e:
                            print(f"[reader] Subscriber error: {e}")

                    # Save after processing each message
                    self._save_rowid(msg.rowid)

            except Exception as e:
                print(f"[reader] Error: {e}")

            await asyncio.sleep(self._poll_interval)


async def get_recent_messages(chat_identifier: str, limit: int = 5) -> list[dict]:
    """Get recent messages from a chat for context injection into the router."""
    safe_chat = chat_identifier.replace("'", "''")
    query = (
        f"SELECT m.ROWID, m.text, m.is_from_me, "
        f"datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime') as ts "
        f"FROM message m "
        f"JOIN chat_message_join cmj ON m.ROWID = cmj.message_id "
        f"JOIN chat c ON cmj.chat_id = c.ROWID "
        f"WHERE c.chat_identifier = '{safe_chat}' "
        f"ORDER BY m.ROWID DESC LIMIT {limit};"
    )
    ok, output = await tmux_relay_shell(
        _sqlite_via_relay_cmd(query),
        timeout=10.0,
    )
    if not ok:
        return []

    messages = []
    raw = output.rstrip(_RECORD_SEP).rstrip("\n")
    if not raw:
        return []
    for record in raw.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record or _FIELD_SEP not in record:
            continue
        parts = record.split(_FIELD_SEP)
        if len(parts) < 3:
            continue
        try:
            int(parts[0])
        except (ValueError, TypeError):
            continue
        messages.append({
            "text": parts[1],
            "from_me": parts[2] == "1",
            "timestamp": parts[3] if len(parts) > 3 else "",
        })
    messages.reverse()  # chronological order
    return messages
