"""Synthetic live canary for exact mail archive, readback, replay, and undo."""

from __future__ import annotations

import argparse
import datetime as dt
import email.policy
import email.utils
import json
import os
import re
import sys
import uuid
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Mapping, Sequence

from .mail_archive_command_adapter import (
    LIVE_CONFIRMATION,
    LIVE_ENV,
    IMAPArchiveBackend,
    MailArchiveCommandBridge,
    MailArchiveError,
    MailArchiveVerificationError,
    _canonical_json,
    _current_uidvalidity,
    _provider_for_account,
    _search_message_id,
)


CANARY_ENV = "MAIL_ARCHIVE_CANARY_LIVE"
CANARY_CONFIRMATION = "I_UNDERSTAND_THIS_APPENDS_AND_DELETES_SYNTHETIC_MAIL"
DEFAULT_STATE_ROOT = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Duffields"
    / "mail-archive-canary"
)


def _synthetic_message(message_id: str) -> bytes:
    message = EmailMessage(policy=email.policy.SMTP)
    message["Message-ID"] = message_id
    message["Date"] = email.utils.format_datetime(dt.datetime.now(dt.timezone.utc))
    message["From"] = "Duffields Canary <canary@localhost.invalid>"
    message["To"] = "Duffields Canary <canary@localhost.invalid>"
    message["Subject"] = "Duffields synthetic archive canary"
    message.set_content(
        "Synthetic Warboard archive canary. This message is safe to delete."
    )
    return message.as_bytes()


def _appenduid(data: list[Any]) -> int | None:
    text = " ".join(
        item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
        for item in data
    )
    match = re.search(r"APPENDUID\s+[0-9]+\s+([0-9]+)", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _append_synthetic(
    backend: IMAPArchiveBackend,
    *,
    account: str,
    message_id: str,
) -> dict[str, Any]:
    with backend._open(account) as conn:
        typ, data = conn.append(
            "INBOX",
            None,
            imap_internaldate(dt.datetime.now(dt.timezone.utc)),
            _synthetic_message(message_id),
        )
        if typ != "OK":
            raise MailArchiveError("synthetic canary APPEND failed")
        backend._select(conn, "INBOX", readonly=True)
        uidvalidity = _current_uidvalidity(conn)
        uid = _appenduid(data)
        if uid is None:
            uid = _search_message_id(conn, message_id)
        if uid is None:
            raise MailArchiveVerificationError(
                "synthetic canary APPEND could not resolve its exact UID"
            )
        row = backend._fetch_collection_row(
            conn,
            account=account,
            provider=_provider_for_account(account),
            uidvalidity=uidvalidity,
            uid=uid,
        )
        if row is None or row.get("rfc_message_id") != message_id:
            raise MailArchiveVerificationError(
                "synthetic canary APPEND readback did not match"
            )
        return row


def imap_internaldate(value: dt.datetime) -> str:
    return value.astimezone().strftime('"%d-%b-%Y %H:%M:%S %z"')


def _proposal(row: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    provider = str(row["provider"])
    destination = str(row["destination_mailbox"])
    payload = {
        "requested_verb": "archive",
        "provider": provider,
        "subject": str(row["subject"]),
        "account": str(row["account"]),
        "origin_mailbox": str(row["origin_mailbox"]),
        "uidvalidity": str(row["uidvalidity"]),
        "uid": str(row["uid"]),
        "rfc_message_id": str(row["rfc_message_id"]),
        "x_gm_msgid": str(row.get("x_gm_msgid") or ""),
        "observed_labels": list(row.get("observed_labels") or []),
        "archive_method": "gmail_label" if provider == "gmail" else "move",
        "destination_mailbox": destination,
    }
    return {
        "proposal_version": "duffields-action-proposal/v1",
        "proposal_id": f"mail-canary-{run_id}",
        "action": {
            "adapter": "mail",
            "kind": "mail.archive",
            "payload": payload,
            "readback_required": True,
            "undo_supported": True,
            "undo_window_seconds": 3600,
        },
        "target": {
            "system": "mail",
            "resource_type": "message",
            "scope": payload["account"],
            "display": payload["subject"],
        },
        "risk": {
            "level": "low",
            "reversible": True,
            "external_communication": False,
        },
    }


def _delete_exact(
    backend: IMAPArchiveBackend,
    *,
    account: str,
    mailbox: str,
    uidvalidity: int,
    uid: int,
    message_id: str,
) -> bool:
    with backend._open(account) as conn:
        backend._select(conn, mailbox)
        if _current_uidvalidity(conn) != uidvalidity:
            return False
        fetched = backend._fetch_uid_message(
            conn,
            uid,
            fields="(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
        )
        if fetched is None:
            return True
        if fetched.get("message_id") != message_id:
            return False
        backend._delete_uid(conn, mailbox, uid)
        backend._select(conn, mailbox, readonly=True)
        return (
            backend._fetch_uid_message(
                conn,
                uid,
                fields="(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
            )
            is None
        )


def run_canary(
    *,
    account: str = "icloud",
    state_root: Path = DEFAULT_STATE_ROOT,
) -> dict[str, Any]:
    if account != "icloud":
        raise MailArchiveError(
            "the destructive-cleanup canary is deliberately limited to iCloud"
        )
    run_id = uuid.uuid4().hex[:16]
    message_id = f"<duffields-warboard-canary-{run_id}@localhost.invalid>"
    backend = IMAPArchiveBackend()
    row: dict[str, Any] | None = None
    cleanup_targets: list[dict[str, Any]] = []
    cleanup_ok = False
    try:
        row = _append_synthetic(backend, account=account, message_id=message_id)
        cleanup_targets.append(
            {
                "mailbox": "INBOX",
                "uidvalidity": row["uidvalidity"],
                "uid": row["uid"],
            }
        )
        proposal = _proposal(row, run_id)
        root = state_root / run_id
        bridge = MailArchiveCommandBridge(backend, state_root=root)
        executed = bridge.execute(
            proposal,
            operation_id=f"warboard:mail:execute:{run_id}",
        )
        archived = executed.get("state", {}).get("archive", {})
        cleanup_targets.append(
            {
                "mailbox": "Archive",
                "uidvalidity": archived.get("uidvalidity"),
                "uid": archived.get("uid"),
            }
        )
        readback = bridge.readback(proposal, reference=executed["reference"])
        replayed = bridge.execute(
            proposal,
            operation_id=f"warboard:mail:execute:{run_id}",
        )
        undone = bridge.undo(
            proposal,
            reference=executed["reference"],
            operation_id=f"warboard:mail:undo:{run_id}",
        )
        undo_readback = bridge.readback_undo(
            proposal,
            reference=executed["reference"],
        )
        restored = undone.get("state", {}).get("origin", {})
        cleanup_targets.append(
            {
                "mailbox": "INBOX",
                "uidvalidity": restored.get("uidvalidity"),
                "uid": restored.get("uid"),
            }
        )
        cleanup_ok = _delete_exact(
            backend,
            account=account,
            mailbox="INBOX",
            uidvalidity=int(restored["uidvalidity"]),
            uid=int(restored["uid"]),
            message_id=message_id,
        )
        return {
            "ok": True,
            "schema": "mail-archive-canary/v1",
            "account": account,
            "mail_readback": (
                readback.get("ok") is True
                and readback.get("observed", {}).get("origin_absent") is True
                and readback.get("observed", {}).get("destination_present") is True
            ),
            "mail_replay": replayed.get("details", {}).get("replayed") is True,
            "mail_undo_readback": (
                undo_readback.get("ok") is True
                and undo_readback.get("observed", {}).get("origin_present") is True
                and undo_readback.get("observed", {}).get("destination_absent") is True
            ),
            "all_restored": cleanup_ok,
        }
    finally:
        if row is not None and not cleanup_ok:
            # Best effort is confined to exact UIDs observed during this run.
            for target in reversed(cleanup_targets):
                try:
                    if target.get("uidvalidity") and target.get("uid"):
                        _delete_exact(
                            backend,
                            account=account,
                            mailbox=str(target["mailbox"]),
                            uidvalidity=int(target["uidvalidity"]),
                            uid=int(target["uid"]),
                            message_id=message_id,
                        )
                except Exception:
                    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mail-archive-canary")
    parser.add_argument("--account", default="icloud", choices=("icloud",))
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if os.environ.get(CANARY_ENV) != CANARY_CONFIRMATION:
            raise MailArchiveError(
                f"live canary requires {CANARY_ENV}={CANARY_CONFIRMATION!r}"
            )
        os.environ[LIVE_ENV] = LIVE_CONFIRMATION
        result = run_canary(account=args.account, state_root=args.state_root)
        exit_code = 0 if all(
            result.get(field) is True
            for field in (
                "mail_readback",
                "mail_replay",
                "mail_undo_readback",
                "all_restored",
            )
        ) else 2
    except (OSError, ValueError, MailArchiveError, KeyError) as exc:
        result = {
            "ok": False,
            "schema": "mail-archive-canary/v1",
            "error": {"code": type(exc).__name__, "message": str(exc)[:1000]},
            "mail_readback": False,
            "mail_replay": False,
            "mail_undo_readback": False,
            "all_restored": False,
        }
        exit_code = 2
    stream = sys.stdout if exit_code == 0 else sys.stderr
    print(_canonical_json(result), file=stream)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
