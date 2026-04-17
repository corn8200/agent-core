"""Attachment enrichment for inbound iMessages (router-v2 Task A).

iMessage attachments (images, voice memos, documents) live in
~/Library/Messages/Attachments/ — a TCC-protected path that launchd-spawned
Python cannot read directly. All filesystem access routes through
`tmux_relay_shell` from core.tools so the relay (started under Terminal.app)
donates its Full Disk Access.

Flow:
  1. query_attachments_for_message(rowid) -> list[AttachmentInfo]
  2. stage_attachment(info) -> copies file to /tmp/ab-attach-<uuid>/<name>
  3. enrich_attachment(info, staged_path) -> textual description:
       images  -> Sonnet 4.6 vision description
       audio   -> Whisper transcription (OpenAI API, then local whisper fallback)
       other   -> "[attachment: <name> (<mime>) — unsupported]"
  4. Caller prepends the enrichment line to the InboundMessage.text so the
     router can classify on the full content.
  5. cleanup_staged(info) wipes the /tmp copy.

All expensive calls (SDK, Whisper) are async and isolated so tests can mock
them cleanly — see tests/test_attachments.py.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shlex
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from core.tools import tmux_relay_shell


# --- Constants ---------------------------------------------------------------

# File extensions grouped by handler.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".heic", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
_AUDIO_EXTS = {".m4a", ".caf", ".amr", ".mp3", ".wav", ".aac", ".aiff", ".ogg"}

# MIME prefix → bucket, used when extension is missing/ambiguous.
_MIME_IMAGE_PREFIX = "image/"
_MIME_AUDIO_PREFIX = "audio/"

_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"

_STAGE_ROOT = Path("/tmp")


# --- Data ---------------------------------------------------------------------


@dataclass
class AttachmentInfo:
    """One row from the `attachment` table of chat.db."""

    attachment_rowid: int
    filename: str           # full path inside ~/Library/Messages/Attachments/...
    transfer_name: str      # original user-facing filename
    mime_type: str
    uti: str = ""
    total_bytes: int = 0
    staged_path: Path | None = None

    @property
    def ext(self) -> str:
        return Path(self.transfer_name or self.filename).suffix.lower()

    @property
    def kind(self) -> str:
        """Return 'image', 'audio', or 'other'."""
        ext = self.ext
        if ext in _IMAGE_EXTS:
            return "image"
        if ext in _AUDIO_EXTS:
            return "audio"
        mime = (self.mime_type or "").lower()
        if mime.startswith(_MIME_IMAGE_PREFIX):
            return "image"
        if mime.startswith(_MIME_AUDIO_PREFIX):
            return "audio"
        return "other"


# --- chat.db queries (via tmux relay) -----------------------------------------


def _sqlite_via_relay_cmd(sql: str, db_path: str = "~/Library/Messages/chat.db") -> str:
    """Mirror of message_reader helper — base64 to avoid quoting hell."""
    encoded = base64.b64encode(sql.encode("utf-8")).decode("ascii")
    return (
        f"echo {encoded} | base64 -d | "
        f"sqlite3 -separator $'\\x1f' -newline $'\\x1e' {db_path}"
    )


async def query_attachments_for_message(message_rowid: int) -> list[AttachmentInfo]:
    """Return all attachments linked to a specific message ROWID.

    Joins `message_attachment_join` -> `attachment`. Returns empty list if
    the message has no attachments or the relay call fails.
    """
    sql = (
        "SELECT a.ROWID, a.filename, COALESCE(a.transfer_name,''), "
        "COALESCE(a.mime_type,''), COALESCE(a.uti,''), "
        "COALESCE(a.total_bytes,0) "
        "FROM attachment a "
        "JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID "
        f"WHERE maj.message_id = {int(message_rowid)};"
    )
    ok, out = await tmux_relay_shell(_sqlite_via_relay_cmd(sql), timeout=10.0)
    if not ok:
        return []
    raw = out.rstrip(_RECORD_SEP).rstrip("\n")
    if not raw:
        return []
    attachments: list[AttachmentInfo] = []
    for record in raw.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record or _FIELD_SEP not in record:
            continue
        parts = record.split(_FIELD_SEP)
        if len(parts) < 2:
            continue
        try:
            rowid = int(parts[0])
        except (ValueError, TypeError):
            continue
        filename = parts[1] if len(parts) > 1 else ""
        transfer_name = parts[2] if len(parts) > 2 else ""
        mime = parts[3] if len(parts) > 3 else ""
        uti = parts[4] if len(parts) > 4 else ""
        try:
            size = int(parts[5]) if len(parts) > 5 and parts[5] else 0
        except (ValueError, TypeError):
            size = 0
        # Skip zero-byte rows (iMessage inline stickers / failed transfers).
        if not filename:
            continue
        attachments.append(AttachmentInfo(
            attachment_rowid=rowid,
            filename=filename,
            transfer_name=transfer_name or Path(filename).name,
            mime_type=mime,
            uti=uti,
            total_bytes=size,
        ))
    return attachments


# --- Staging -----------------------------------------------------------------


async def stage_attachment(info: AttachmentInfo) -> Path | None:
    """Copy the attachment out of the TCC-protected Attachments tree to /tmp.

    Returns the /tmp path on success, None on failure. Uses `cp` through the
    tmux relay so FDA is inherited.
    """
    stage_dir = _STAGE_ROOT / f"ab-attach-{uuid.uuid4().hex[:12]}"
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", info.transfer_name or Path(info.filename).name)
    if not safe_name:
        safe_name = f"attachment-{info.attachment_rowid}"
    dest = stage_dir / safe_name

    # Messages DB stores filenames with a leading "~/" sometimes. Expand via
    # the relay's $HOME rather than ours.
    src = info.filename
    if src.startswith("~/"):
        src_sh = '"$HOME/"' + shlex.quote(src[2:])
    else:
        src_sh = shlex.quote(src)

    cmd = (
        f"mkdir -p {shlex.quote(str(stage_dir))} && "
        f"cp {src_sh} {shlex.quote(str(dest))} && "
        f"echo OK"
    )
    ok, out = await tmux_relay_shell(cmd, timeout=15.0)
    if not ok or "OK" not in (out or ""):
        print(f"[attach] stage failed rowid={info.attachment_rowid} "
              f"src={info.filename!r}: ok={ok} out={(out or '')[:200]!r}")
        return None
    info.staged_path = dest
    return dest


def cleanup_staged(info: AttachmentInfo) -> None:
    """Remove the staged file + its dir. Safe to call repeatedly."""
    if not info.staged_path:
        return
    try:
        stage_dir = info.staged_path.parent
        if stage_dir.exists() and str(stage_dir).startswith("/tmp/ab-attach-"):
            shutil.rmtree(stage_dir, ignore_errors=True)
    finally:
        info.staged_path = None


# --- Enrichment --------------------------------------------------------------


async def describe_image(path: Path) -> str:
    """Ask the Anthropic SDK (Sonnet 4.6) to describe an image in one line.

    Returns a short English description, or an error-marker string starting
    with "[error:" that callers can still emit so the user isn't left blind.
    """
    try:
        import anthropic  # optional dep; installed with the SDK
    except ImportError:
        return "[error: anthropic SDK not installed]"

    try:
        b64 = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    except Exception as e:
        return f"[error: could not read image: {e}]"

    media_type = _guess_media_type(path)

    def _call() -> str:
        # Explicit client — uses ANTHROPIC_API_KEY if present, or the CLI
        # OAuth token (Max subscription) via the SDK's default resolver.
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Describe this image in one concise sentence (max 25 words). "
                            "Focus on content that helps classify an iMessage: people, "
                            "objects, text visible, any indication of emergency or "
                            "task. No flowery language."
                        ),
                    },
                ],
            }],
        )
        text_parts = []
        for block in resp.content:
            if getattr(block, "type", "") == "text":
                text_parts.append(getattr(block, "text", ""))
        return " ".join(p.strip() for p in text_parts).strip() or "[image]"

    try:
        return await asyncio.to_thread(_call)
    except Exception as e:
        return f"[error: vision call failed: {type(e).__name__}: {e}]"


def _guess_media_type(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".heic": "image/heic",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }.get(ext, "image/jpeg")


async def transcribe_audio(path: Path) -> str:
    """Transcribe audio via OpenAI Whisper API first, local whisper as fallback.

    Returns the transcription text, or an error-marker string starting
    with "[error:" if both paths fail.
    """
    # Try OpenAI API if key available (core.vault)
    try:
        from core.vault import get_secret
        key = get_secret("OPENAI_API_KEY")
    except Exception:
        key = ""
    key = key or os.environ.get("OPENAI_API_KEY", "")

    if key:
        try:
            import openai  # type: ignore
        except ImportError:
            openai = None  # type: ignore

        if openai is not None:
            def _openai_call() -> str:
                client = openai.OpenAI(api_key=key)
                with open(path, "rb") as f:
                    resp = client.audio.transcriptions.create(
                        model="whisper-1",
                        file=f,
                    )
                return (getattr(resp, "text", "") or "").strip()

            try:
                text = await asyncio.to_thread(_openai_call)
                if text:
                    return text
            except Exception as e:
                print(f"[attach] OpenAI whisper failed: {e}")

    # Local whisper fallback
    local = shutil.which("whisper")
    if local:
        try:
            proc = await asyncio.create_subprocess_exec(
                local, str(path), "--model", "base", "--output_format", "txt",
                "--output_dir", str(path.parent), "--language", "en",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _out, _err = await asyncio.wait_for(proc.communicate(), timeout=120)
            txt_file = path.with_suffix(".txt")
            if txt_file.exists():
                return txt_file.read_text().strip() or "[audio]"
        except Exception as e:
            print(f"[attach] local whisper failed: {e}")

    return "[error: no whisper backend available]"


async def enrich_attachment(info: AttachmentInfo) -> str:
    """Produce a single-line enrichment string for one attachment.

    Returns "[image: <desc>]" / "[voice: <transcript>]" / "[attachment: ...]".
    Never raises — callers get a best-effort string.
    """
    if info.staged_path is None or not info.staged_path.exists():
        return f"[attachment: {info.transfer_name} — stage failed]"

    kind = info.kind
    try:
        if kind == "image":
            desc = await describe_image(info.staged_path)
            return f"[image: {desc}]"
        if kind == "audio":
            transcript = await transcribe_audio(info.staged_path)
            return f"[voice: {transcript}]"
        # Unsupported type — still tell the router what arrived
        mime = info.mime_type or "unknown mime"
        return f"[attachment: {info.transfer_name} ({mime}) — unsupported]"
    except Exception as e:
        return f"[attachment: {info.transfer_name} — enrich error: {e}]"


# --- Top-level API -----------------------------------------------------------


async def enrich_message_text(message_rowid: int, text: str) -> str:
    """Fetch, stage, describe, and clean up every attachment on a message.

    Returns the original text with attachment descriptions prepended on
    their own lines. If the message has no attachments, returns text unchanged.

    This is the single function the reader should call.
    """
    attachments = await query_attachments_for_message(message_rowid)
    if not attachments:
        return text

    enriched_lines: list[str] = []
    for info in attachments:
        staged = await stage_attachment(info)
        if staged is None:
            enriched_lines.append(
                f"[attachment: {info.transfer_name or 'unknown'} — stage failed]"
            )
            continue
        try:
            enriched_lines.append(await enrich_attachment(info))
        finally:
            cleanup_staged(info)

    if not enriched_lines:
        return text

    prefix = "\n".join(enriched_lines)
    if text:
        return f"{prefix}\n{text}"
    return prefix
