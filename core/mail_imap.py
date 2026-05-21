"""mail_imap — imaplib wrapper for iCloud + Gmail with search-by-sender,
search-by-subject, and date-range filters.

Built for #583 to replace osascript Mail.app search (slow, brittle -1719,
TCC-protected from launchd). IMAP works from any process — no Mail.app
dependency, no FDA, no AppleScript flake.

Usage:

    from core.mail_imap import MailIMAP

    with MailIMAP("icloud") as imap:
        recent = imap.recent(limit=20, days=7)
        from_ashley = imap.search_by_sender("", days=30)
        about_doctor = imap.search_by_subject("doctor", days=14)

The connection is opened lazily on first query and reused across queries
in the same context. For batch lookups across both accounts:

    with MailIMAP.batch(["icloud", "gmail-burner"]) as imaps:
        for account, imap in imaps.items():
            ...

Returns dict shape: {msg_id, subject, from, from_addr, date, snippet, account}.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import logging
import re
import socket
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from typing import Iterator

from core.vault import get_secret

logger = logging.getLogger(__name__)

ACCOUNTS = {
    "icloud": {
        "host_key": "ICLOUD_IMAP_HOST",
        "host_default": "imap.mail.me.com",
        "user_key": "ICLOUD_EMAIL",
        "pass_key": "ICLOUD_APP_PASSWORD",
    },
    "gmail-burner": {
        "host_key": None,
        "host_default": "imap.gmail.com",
        "user_key": "GMAIL_USER",
        "pass_key": "GMAIL_APP_PASSWORD",
    },
}

DEFAULT_TIMEOUT_S = 20
DEFAULT_FOLDER = "INBOX"
SNIPPET_LEN = 240


class MailIMAPError(RuntimeError):
    pass


def _decode_mime(s: str | None) -> str:
    """Decode RFC 2047 encoded-word headers (=?UTF-8?B?...?=) into plain str."""
    if not s:
        return ""
    try:
        parts = decode_header(s)
    except Exception:
        return s
    out = []
    for raw, charset in parts:
        if isinstance(raw, bytes):
            try:
                out.append(raw.decode(charset or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                out.append(raw.decode("utf-8", errors="replace"))
        else:
            out.append(raw)
    return "".join(out).strip()


def _parse_addr(raw_from: str) -> tuple[str, str]:
    """Split 'Name <addr>' into (name, addr)."""
    if not raw_from:
        return ("", "")
    name, addr = email.utils.parseaddr(raw_from)
    return (_decode_mime(name), (addr or "").lower())


def _format_imap_date(d: datetime) -> str:
    """IMAP SINCE format: DD-Mon-YYYY (e.g. 25-Apr-2026)."""
    return d.strftime("%d-%b-%Y")


def _extract_snippet(raw_text: bytes | str) -> str:
    """First N chars of plaintext body, collapsed whitespace, MIME-stripped."""
    if isinstance(raw_text, bytes):
        try:
            text = raw_text.decode("utf-8", errors="replace")
        except Exception:
            text = raw_text.decode("latin-1", errors="replace")
    else:
        text = raw_text or ""
    # Strip soft line breaks + collapse whitespace
    text = re.sub(r"=\r?\n", "", text)  # quoted-printable soft breaks
    text = re.sub(r"\s+", " ", text).strip()
    return text[:SNIPPET_LEN]


class MailIMAP:
    """One IMAP connection per account, lazy-opened, held for batch queries."""

    def __init__(self, account: str, *, folder: str = DEFAULT_FOLDER, timeout_s: int = DEFAULT_TIMEOUT_S):
        if account not in ACCOUNTS:
            raise ValueError(f"unknown account {account!r}; known: {list(ACCOUNTS)}")
        self.account = account
        self.folder = folder
        self.timeout_s = timeout_s
        self._conn: imaplib.IMAP4_SSL | None = None
        self._selected = False

    # --- lifecycle ---

    def _connect(self) -> imaplib.IMAP4_SSL:
        cfg = ACCOUNTS[self.account]
        host = (get_secret(cfg["host_key"]) if cfg.get("host_key") else None) or cfg["host_default"]
        user = get_secret(cfg["user_key"])
        pwd = get_secret(cfg["pass_key"])
        if not user or not pwd:
            raise MailIMAPError(f"missing creds for {self.account}: user_key={cfg['user_key']} pass_key={cfg['pass_key']}")
        socket.setdefaulttimeout(self.timeout_s)
        try:
            conn = imaplib.IMAP4_SSL(host, port=993, timeout=self.timeout_s)
            conn.login(user, pwd)
        except (imaplib.IMAP4.error, socket.error) as exc:
            raise MailIMAPError(f"{self.account} IMAP connect/login failed: {exc}") from exc
        self._conn = conn
        return conn

    def _ensure_selected(self) -> imaplib.IMAP4_SSL:
        conn = self._conn or self._connect()
        if not self._selected:
            typ, _ = conn.select(self.folder, readonly=True)
            if typ != "OK":
                raise MailIMAPError(f"select {self.folder!r} failed: {typ}")
            self._selected = True
        return conn

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            if self._selected:
                self._conn.close()
            self._conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
        finally:
            self._conn = None
            self._selected = False

    def __enter__(self) -> "MailIMAP":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --- queries ---

    def recent(self, *, limit: int = 20, days: int | None = 7) -> list[dict]:
        """Most recent N messages, optionally bounded by days."""
        return self._search([("SINCE", _format_imap_date(datetime.now(tz=timezone.utc) - timedelta(days=days)))] if days else [], limit=limit)

    def search_by_sender(self, sender: str, *, days: int = 30, limit: int = 50) -> list[dict]:
        """Messages from sender (token match against From: header). Case-insensitive.

        GOTCHA: iCloud IMAP tokenizes the From header on whitespace AND `.`, so
        `search_by_sender("mail.anthropic.com")` returns nothing while
        `search_by_sender("anthropic")` matches `no-reply@mail.anthropic.com`.
        Pass the longest unbroken token (display-name fragment, mailbox local-part,
        or single domain label). Gmail IMAP is more lenient but the same rule is
        the safest cross-account default.
        """
        if not sender:
            return []
        terms = [("FROM", sender)]
        if days:
            terms.append(("SINCE", _format_imap_date(datetime.now(tz=timezone.utc) - timedelta(days=days))))
        return self._search(terms, limit=limit)

    def search_by_subject(self, query: str, *, days: int = 14, limit: int = 50) -> list[dict]:
        """Messages with subject containing query."""
        if not query:
            return []
        terms = [("SUBJECT", query)]
        if days:
            terms.append(("SINCE", _format_imap_date(datetime.now(tz=timezone.utc) - timedelta(days=days))))
        return self._search(terms, limit=limit)

    # --- internal ---

    def _search(self, terms: list[tuple[str, str]], *, limit: int) -> list[dict]:
        conn = self._ensure_selected()
        # Build IMAP SEARCH expression — quote any term value containing spaces.
        if terms:
            expr_parts = []
            for k, v in terms:
                expr_parts.append(k)
                expr_parts.append(f'"{v}"' if (" " in v or not v.isascii()) else v)
            typ, data = conn.search(None, *expr_parts)
        else:
            typ, data = conn.search(None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return []
        ids = data[0].split()
        # Take the LAST `limit` ids — IMAP returns oldest first
        ids = ids[-limit:]
        if not ids:
            return []
        # Fetch envelope + first part of body in one round-trip
        fetch_set = b",".join(ids)
        # BODY.PEEK avoids marking as read
        typ, fetched = conn.fetch(fetch_set, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)] BODY.PEEK[1]<0.500>)")
        if typ != "OK":
            return []
        results = self._parse_fetch(fetched)
        # Sort newest-first by parsed date
        results.sort(key=lambda r: r.get("_date_sortkey") or 0, reverse=True)
        for r in results:
            r.pop("_date_sortkey", None)
        return results

    def _parse_fetch(self, raw: list) -> list[dict]:
        out = []
        # IMAP fetch returns alternating tuples + closing parens.
        # Each msg comes as (b"<id> (BODY[HEADER.FIELDS (...)] {n}", b"<headers>") followed by
        # (b" BODY[1]<0.500> {n}", b"<body>") and a b")" terminator.
        # Easier path: walk pairs that have a tuple form.
        msg_chunks: dict[bytes, dict] = {}
        current_id: bytes | None = None
        for item in raw:
            if isinstance(item, tuple):
                prelude, payload = item
                m = re.match(rb"(\d+)", prelude)
                if not m:
                    continue
                msg_id = m.group(1)
                current_id = msg_id
                bucket = msg_chunks.setdefault(msg_id, {})
                if b"HEADER" in prelude:
                    bucket["headers"] = payload or b""
                elif b"BODY[1]" in prelude:
                    bucket["body"] = payload or b""
        for msg_id, bucket in msg_chunks.items():
            headers = email.message_from_bytes(bucket.get("headers") or b"")
            subject = _decode_mime(headers.get("Subject", ""))
            from_name, from_addr = _parse_addr(headers.get("From", ""))
            raw_date = headers.get("Date", "")
            try:
                parsed = email.utils.parsedate_to_datetime(raw_date)
                date_iso = parsed.astimezone(timezone.utc).isoformat()
                sortkey = parsed.timestamp()
            except (TypeError, ValueError):
                date_iso = raw_date
                sortkey = 0.0
            message_id = headers.get("Message-ID", "").strip()
            out.append({
                "msg_id": msg_id.decode(),
                "subject": subject[:200],
                "from": from_name or from_addr,
                "from_addr": from_addr,
                "date": date_iso,
                "rfc_message_id": message_id,
                "snippet": _extract_snippet(bucket.get("body") or b""),
                "account": self.account,
                "_date_sortkey": sortkey,
            })
        return out

    # --- batch helper ---

    @classmethod
    @contextmanager
    def batch(cls, accounts: list[str], **kwargs) -> Iterator[dict[str, "MailIMAP"]]:
        """Open multiple connections; yield {account: MailIMAP}; close all on exit."""
        opened: dict[str, MailIMAP] = {}
        try:
            for a in accounts:
                opened[a] = cls(a, **kwargs)
            yield opened
        finally:
            for imap in opened.values():
                try:
                    imap.close()
                except Exception:
                    pass
