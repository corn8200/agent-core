"""Duffields command bridge for guarded single-message mail archive."""

from __future__ import annotations

import argparse
import datetime as dt
import email
import email.utils
import hashlib
import html
import imaplib
import json
import os
import re
import socket
import stat
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from core.mail_imap import _decode_mime, _extract_snippet, _parse_addr
from core.vault import get_secret


STATE_SCHEMA = "mail-archive-command-adapter-state/v1"
UNDO_STATE_SCHEMA = "mail-archive-command-adapter-undo-state/v1"
RECEIPT_SCHEMA = "mail-archive-execute-receipt/v1"
UNDO_RECEIPT_SCHEMA = "mail-archive-undo-receipt/v1"
PROBE_SCHEMA = "mail-archive-probe/v1"
REFERENCE_PREFIX = "mail-archive-operation:"
UNDO_REFERENCE_PREFIX = "mail-archive-undo-operation:"
LIVE_ENV = "MAIL_ARCHIVE_COMMAND_ADAPTER_LIVE"
LIVE_CONFIRMATION = "I_UNDERSTAND_THIS_ARCHIVES_EXACT_MAIL"
STATE_ROOT_ENV = "MAIL_ARCHIVE_COMMAND_ADAPTER_STATE_ROOT"
DEFAULT_STATE_ROOT = (
    Path.home() / "Library" / "Application Support" / "Duffields" / "mail-archive"
)
MAX_REQUEST_BYTES = 128 * 1024
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:@/-]{0,127}$")
_UID_RE = re.compile(r"^[1-9][0-9]{0,18}$")
_UIDVALIDITY_RE = re.compile(r"^[1-9][0-9]{0,18}$")
_MESSAGE_ID_RE = re.compile(r"^<[^<>\s]+>$")
_GMAIL_MSGID_RE = re.compile(r"^[0-9]{1,20}$")
_INBOX_LABELS = frozenset({"inbox", "\\inbox"})


class MailArchiveError(RuntimeError):
    code = "mail_archive_invalid"
    http_status = 400

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.details = dict(details or {})


class MailArchiveConflictError(MailArchiveError):
    code = "mail_archive_conflict"
    http_status = 409


class MailArchiveNotFoundError(MailArchiveError):
    code = "mail_archive_not_found"
    http_status = 404


class MailArchiveVerificationError(MailArchiveError):
    code = "mail_archive_unverified"
    http_status = 502


@dataclass(frozen=True)
class MailArchiveRequest:
    """Closed request model for a single exact message archive."""

    account: str
    provider_name: str
    subject: str
    origin_mailbox: str
    uidvalidity: int
    uid: int
    message_id: str
    archive_mailbox: str
    archive_method: str
    gmail_msgid: str | None = None
    labels: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MailArchiveRequest":
        _exact(
            value,
            {
                "requested_verb",
                "provider",
                "subject",
                "account",
                "origin_mailbox",
                "uidvalidity",
                "uid",
                "rfc_message_id",
                "x_gm_msgid",
                "observed_labels",
                "archive_method",
                "destination_mailbox",
            },
            field="mail archive request",
        )
        if value.get("requested_verb") != "archive":
            raise MailArchiveError("requested_verb must be archive")
        account = _account_name(value.get("account"))
        provider = _bounded(value.get("provider"), field="provider", limit=20)
        if provider not in {"gmail", "icloud"}:
            raise MailArchiveError("provider must be gmail or icloud")
        if provider != _provider_for_account(account):
            raise MailArchiveError("provider does not match the configured account")
        subject = _bounded(value.get("subject"), field="subject", limit=240)
        origin_mailbox = _bounded(
            value.get("origin_mailbox"), field="origin_mailbox", limit=128
        )
        uidvalidity = _positive_int_text(
            value.get("uidvalidity"), field="uidvalidity"
        )
        uid = _positive_int_text(value.get("uid"), field="uid")
        message_id = _message_id(value.get("rfc_message_id"))
        archive_method = _bounded(
            value.get("archive_method"), field="archive_method", limit=32
        )
        destination_raw = value.get("destination_mailbox")
        if not isinstance(destination_raw, str):
            raise MailArchiveError("destination_mailbox must be a string")
        archive_mailbox = destination_raw.strip()
        gmail_msgid = None
        labels: tuple[str, ...] = ()
        if provider == "gmail":
            if archive_method != "gmail_label" or archive_mailbox:
                raise MailArchiveError(
                    "Gmail archive requires gmail_label and no destination mailbox"
                )
            gmail_msgid = _gmail_msgid(value.get("x_gm_msgid"))
            labels = _normalize_labels(_label_list(value.get("observed_labels")))
            if not any(_is_label(label, "INBOX") for label in labels):
                raise MailArchiveError("gmail archive request requires the Inbox label")
        else:
            if archive_method != "move" or not archive_mailbox:
                raise MailArchiveError(
                    "iCloud archive requires move and a destination mailbox"
                )
            if value.get("x_gm_msgid") not in (None, ""):
                raise MailArchiveError("x_gm_msgid is only valid for Gmail requests")
            if value.get("observed_labels") not in (None, [], ()):
                raise MailArchiveError(
                    "observed_labels are only valid for Gmail requests"
                )
        return cls(
            account=account,
            provider_name=provider,
            subject=subject,
            origin_mailbox=origin_mailbox,
            uidvalidity=uidvalidity,
            uid=uid,
            message_id=message_id,
            archive_mailbox=archive_mailbox,
            archive_method=archive_method,
            gmail_msgid=gmail_msgid,
            labels=labels,
        )

    @property
    def provider(self) -> str:
        return self.provider_name

    @property
    def all_mailbox(self) -> str:
        return "[Gmail]/All Mail"

    @property
    def public_dict(self) -> dict[str, Any]:
        return {
            "requested_verb": "archive",
            "provider": self.provider,
            "subject": self.subject,
            "account": self.account,
            "origin_mailbox": self.origin_mailbox,
            "uidvalidity": str(self.uidvalidity),
            "uid": str(self.uid),
            "rfc_message_id": self.message_id,
            "x_gm_msgid": self.gmail_msgid or "",
            "observed_labels": list(self.labels),
            "archive_method": self.archive_method,
            "destination_mailbox": self.archive_mailbox,
        }


@runtime_checkable
class MailArchiveBackend(Protocol):
    def execute(self, request: MailArchiveRequest, *, operation_id: str) -> dict[str, Any]:
        ...

    def readback(
        self,
        request: MailArchiveRequest,
        *,
        execute_receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        ...

    def undo(
        self,
        request: MailArchiveRequest,
        *,
        execute_receipt: Mapping[str, Any],
        approval_ref: str,
    ) -> dict[str, Any]:
        ...

    def readback_undo(
        self,
        request: MailArchiveRequest,
        *,
        execute_receipt: Mapping[str, Any],
        undo_receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        ...

    def probe(self, account: str, mailbox: str | None = None) -> dict[str, Any]:
        ...


class AdapterStateStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser()
        if not self.root.is_absolute():
            raise MailArchiveError("adapter state root must be an absolute path")

    def prepare(self) -> None:
        if self.root.exists() and self.root.is_symlink():
            raise MailArchiveError("adapter state root cannot be a symlink")
        for path in (
            self.root,
            self.root / "operations",
            self.root / "undo",
            self.root / "locks",
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = os.lstat(path)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise MailArchiveError(
                    "adapter state paths must be user-owned directories"
                )
            if stat.S_IMODE(info.st_mode) & 0o077:
                path.chmod(0o700)

    def mutation_lock(self):
        self.prepare()
        return _exclusive_lock(self.root / "locks" / "bridge.lock")

    def operation_path(self, digest: str) -> Path:
        return self.root / "operations" / f"{digest}.json"

    def undo_path(self, digest: str) -> Path:
        return self.root / "undo" / f"{digest}.json"

    def load_operation_digest(self, digest: str) -> dict[str, Any] | None:
        digest = self._validated_digest(digest)
        state = self._load(self.operation_path(digest), schema=STATE_SCHEMA)
        if state is not None and state.get("operation_digest") != digest:
            raise MailArchiveError("operation state digest does not match its path")
        return state

    def load_operation_id(self, operation_id: str) -> dict[str, Any] | None:
        digest = _operation_digest(operation_id)
        state = self.load_operation_digest(digest)
        if state is not None and state.get("operation_id") != operation_id:
            raise MailArchiveConflictError("operation digest is bound to a different operation_id")
        return state

    def load_reference(self, reference: Any) -> dict[str, Any]:
        digest = _parse_reference(reference, prefix=REFERENCE_PREFIX)
        state = self.load_operation_digest(digest)
        if state is None or state.get("reference") != reference:
            raise MailArchiveError("mail archive reference was not found")
        return state

    def load_undo_reference(self, reference: Any) -> dict[str, Any]:
        digest = _parse_reference(reference, prefix=UNDO_REFERENCE_PREFIX)
        state = self._load(self.undo_path(digest), schema=UNDO_STATE_SCHEMA)
        if (
            state is None
            or state.get("operation_digest") != digest
            or state.get("reference") != reference
        ):
            raise MailArchiveError("mail archive undo reference was not found")
        return state

    def save_operation(self, state: Mapping[str, Any]) -> None:
        digest = self._validated_digest(state.get("operation_digest"))
        _write_json_atomic(self.operation_path(digest), state)

    def save_undo(self, state: Mapping[str, Any]) -> None:
        digest = self._validated_digest(state.get("operation_digest"))
        _write_json_atomic(self.undo_path(digest), state)

    @staticmethod
    def _validated_digest(value: Any) -> str:
        if not isinstance(value, str):
            raise MailArchiveError("adapter state operation digest must be a string")
        digest = value
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise MailArchiveError("adapter state operation digest is invalid")
        return digest

    @staticmethod
    def _load(path: Path, *, schema: str) -> dict[str, Any] | None:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        if len(raw) > MAX_REQUEST_BYTES:
            raise MailArchiveError(f"adapter state exceeds {MAX_REQUEST_BYTES} bytes")
        try:
            document = json.loads(raw, parse_constant=_reject_constant)
        except json.JSONDecodeError as exc:
            raise MailArchiveError(f"adapter state is invalid JSON: {path}") from exc
        if not isinstance(document, dict) or document.get("schema") != schema:
            raise MailArchiveError(f"adapter state has an invalid schema: {path}")
        return document


class MailArchiveCommandBridge:
    def __init__(
        self,
        backend: MailArchiveBackend,
        *,
        state_root: Path = DEFAULT_STATE_ROOT,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.backend = backend
        self.store = AdapterStateStore(state_root)
        self.clock = clock or _utc_now

    def dispatch(self, operation: str, document: Mapping[str, Any]) -> dict[str, Any]:
        if operation == "probe":
            return self.probe(document)
        proposal = _mapping(document.get("proposal"), field="proposal")
        if operation == "execute":
            return self.execute(proposal, operation_id=_operation_id(document.get("operation_id")))
        if operation == "readback":
            return self.readback(proposal, reference=document.get("reference"))
        if operation == "undo":
            return self.undo(
                proposal,
                reference=document.get("reference"),
                operation_id=_operation_id(document.get("operation_id")),
            )
        if operation == "readback-undo":
            return self.readback_undo(proposal, reference=document.get("reference"))
        raise MailArchiveError(f"unsupported command adapter operation: {operation!r}")

    def probe(self, document: Mapping[str, Any]) -> dict[str, Any]:
        account = _account_name(document.get("account"))
        mailbox = None
        if document.get("mailbox") is not None:
            mailbox = _bounded(document.get("mailbox"), field="mailbox", limit=128)
        return self.backend.probe(account, mailbox)

    def execute(self, proposal: Mapping[str, Any], *, operation_id: str) -> dict[str, Any]:
        request = proposal_to_request(proposal, operation_id)
        operation_digest = _operation_digest(operation_id)
        proposal_digest = _proposal_digest(proposal)
        reference = f"{REFERENCE_PREFIX}{operation_digest}"
        with self.store.mutation_lock():
            existing = self.store.load_operation_id(operation_id)
            if existing is not None:
                self._validate_binding(existing, proposal_digest, request)
                return self._execute_response(existing, replayed=True)

            receipt = self.backend.execute(request, operation_id=operation_id)
            self._validate_execute_receipt(receipt, request)
            state = {
                "schema": STATE_SCHEMA,
                "created_at": self.clock(),
                "operation_id": operation_id,
                "operation_digest": operation_digest,
                "proposal_digest": proposal_digest,
                "request": request.public_dict,
                "reference": reference,
                "execute_receipt": dict(receipt),
                "latest_undo_reference": None,
            }
            self.store.save_operation(state)
            return self._execute_response(state, replayed=False)

    def readback(self, proposal: Mapping[str, Any], *, reference: Any) -> dict[str, Any]:
        state = self.store.load_reference(reference)
        request = proposal_to_request(proposal, state["operation_id"])
        self._validate_binding(state, _proposal_digest(proposal), request)
        receipt = _mapping(state.get("execute_receipt"), field="execute_receipt")
        observed = self.backend.readback(request, execute_receipt=receipt)
        return {"ok": True, "reference": state["reference"], "observed": observed}

    def undo(
        self,
        proposal: Mapping[str, Any],
        *,
        reference: Any,
        operation_id: str,
    ) -> dict[str, Any]:
        undo_digest = _operation_digest(operation_id)
        proposal_digest = _proposal_digest(proposal)
        undo_reference = f"{UNDO_REFERENCE_PREFIX}{undo_digest}"
        with self.store.mutation_lock():
            original = self.store.load_reference(reference)
            request = proposal_to_request(proposal, original["operation_id"])
            self._validate_binding(original, proposal_digest, request)
            existing = self.store._load(self.store.undo_path(undo_digest), schema=UNDO_STATE_SCHEMA)
            if existing is not None:
                if (
                    existing.get("operation_id") != operation_id
                    or existing.get("original_reference") != reference
                    or existing.get("proposal_digest") != proposal_digest
                ):
                    raise MailArchiveConflictError("undo operation_id is bound to a different request")
                self._link_undo(original, undo_reference)
                return self._undo_response(existing, replayed=True)

            execute_receipt = _mapping(original.get("execute_receipt"), field="execute_receipt")
            undo_receipt = self.backend.undo(
                request,
                execute_receipt=execute_receipt,
                approval_ref=f"duffields-undo-operation:{undo_digest}",
            )
            self._validate_undo_receipt(undo_receipt, request)
            state = {
                "schema": UNDO_STATE_SCHEMA,
                "created_at": self.clock(),
                "operation_id": operation_id,
                "operation_digest": undo_digest,
                "proposal_digest": proposal_digest,
                "reference": undo_reference,
                "original_reference": reference,
                "undo_receipt": dict(undo_receipt),
            }
            self.store.save_undo(state)
            self._link_undo(original, undo_reference)
            return self._undo_response(state, replayed=False)

    def readback_undo(
        self, proposal: Mapping[str, Any], *, reference: Any
    ) -> dict[str, Any]:
        original = self.store.load_reference(reference)
        request = proposal_to_request(proposal, original["operation_id"])
        self._validate_binding(original, _proposal_digest(proposal), request)
        undo_reference = original.get("latest_undo_reference")
        if not undo_reference:
            raise MailArchiveVerificationError("no verified undo receipt is linked to this operation")
        undo_state = self.store.load_undo_reference(undo_reference)
        if undo_state.get("original_reference") != reference:
            raise MailArchiveConflictError("linked undo receipt belongs to a different operation")
        undo_receipt = _mapping(undo_state.get("undo_receipt"), field="undo_receipt")
        execute_receipt = _mapping(original.get("execute_receipt"), field="execute_receipt")
        observed = self.backend.readback_undo(
            request,
            execute_receipt=execute_receipt,
            undo_receipt=undo_receipt,
        )
        return {
            "ok": True,
            "reference": reference,
            "undo_reference": undo_reference,
            "observed": observed,
        }

    @staticmethod
    def _validate_execute_receipt(
        receipt: Mapping[str, Any], request: MailArchiveRequest
    ) -> None:
        if receipt.get("schema") != RECEIPT_SCHEMA:
            raise MailArchiveVerificationError("persisted execute receipt has an invalid schema")
        if receipt.get("account") != request.account or receipt.get("provider") != request.provider:
            raise MailArchiveVerificationError("execute receipt provider does not match the request")

    @staticmethod
    def _validate_undo_receipt(
        receipt: Mapping[str, Any], request: MailArchiveRequest
    ) -> None:
        if receipt.get("schema") != UNDO_RECEIPT_SCHEMA:
            raise MailArchiveVerificationError("persisted undo receipt has an invalid schema")
        if receipt.get("account") != request.account or receipt.get("provider") != request.provider:
            raise MailArchiveVerificationError("undo receipt provider does not match the request")

    def _validate_binding(
        self,
        state: Mapping[str, Any],
        proposal_digest: str,
        request: MailArchiveRequest,
    ) -> None:
        if state.get("proposal_digest") != proposal_digest:
            raise MailArchiveConflictError("operation_id is bound to a different Duffields proposal")
        if state.get("request") != request.public_dict:
            raise MailArchiveConflictError("operation_id is bound to a different mail archive request")

    def _link_undo(self, original: Mapping[str, Any], undo_reference: str) -> None:
        if original.get("latest_undo_reference") == undo_reference:
            return
        updated = dict(original)
        updated["latest_undo_reference"] = undo_reference
        updated["updated_at"] = self.clock()
        self.store.save_operation(updated)

    @staticmethod
    def _execute_response(state: Mapping[str, Any], *, replayed: bool) -> dict[str, Any]:
        receipt = _mapping(state.get("execute_receipt"), field="execute_receipt")
        return {
            "ok": True,
            "reference": state["reference"],
            "details": {
                "status": receipt.get("status"),
                "effect_verified": receipt.get("effect_verified"),
                "provider": receipt.get("provider"),
                "replayed": replayed,
            },
            "state": receipt.get("after"),
        }

    @staticmethod
    def _undo_response(state: Mapping[str, Any], *, replayed: bool) -> dict[str, Any]:
        receipt = _mapping(state.get("undo_receipt"), field="undo_receipt")
        return {
            "ok": True,
            "reference": state["reference"],
            "details": {
                "status": receipt.get("status"),
                "effect_verified": receipt.get("effect_verified"),
                "provider": receipt.get("provider"),
                "replayed": replayed,
                "original_reference": state.get("original_reference"),
            },
            "state": receipt.get("after"),
        }


def proposal_to_request(
    proposal: Mapping[str, Any], operation_id: str
) -> MailArchiveRequest:
    if proposal.get("proposal_version") != "duffields-action-proposal/v1":
        raise MailArchiveError("unsupported Duffields proposal_version")
    proposal_id = _bounded(proposal.get("proposal_id"), field="proposal_id", limit=200)
    action = _mapping(proposal.get("action"), field="proposal.action")
    if action.get("kind") != "mail.archive":
        raise MailArchiveError("proposal.action.kind must be 'mail.archive'")
    if action.get("adapter") != "mail":
        raise MailArchiveError("proposal.action.adapter must be 'mail'")
    if action.get("readback_required") is not True:
        raise MailArchiveError("mail archive request requires independent readback")
    if action.get("undo_supported") is not True:
        raise MailArchiveError("mail archive request requires guarded undo")
    payload = _mapping(action.get("payload"), field="proposal.action.payload")
    _exact(
        payload,
        {
            "requested_verb",
            "provider",
            "subject",
            "account",
            "origin_mailbox",
            "uidvalidity",
            "uid",
            "rfc_message_id",
            "x_gm_msgid",
            "observed_labels",
            "archive_method",
            "destination_mailbox",
        },
        field="proposal.action.payload",
    )
    request = MailArchiveRequest.from_mapping(payload)
    target = _mapping(proposal.get("target"), field="proposal.target")
    if (
        target.get("system") != "mail"
        or target.get("resource_type") != "message"
        or target.get("scope") != request.account
        or target.get("display") != request.subject
    ):
        raise MailArchiveError("proposal target does not match the exact mail message")
    risk = _mapping(proposal.get("risk"), field="proposal.risk")
    if (
        risk.get("level") != "low"
        or risk.get("reversible") is not True
        or risk.get("external_communication") is not False
    ):
        raise MailArchiveError(
            "mail archive permits only low-risk reversible actions"
        )
    _ = _operation_id(operation_id)
    _ = proposal_id
    return request


def _read_request(path: Path | None) -> dict[str, Any]:
    if path is None:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    else:
        raw = path.read_bytes()
    if len(raw) > MAX_REQUEST_BYTES:
        raise MailArchiveError(f"request exceeds {MAX_REQUEST_BYTES} bytes")
    try:
        document = json.loads(raw, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise MailArchiveError("request is not valid JSON") from exc
    if not isinstance(document, dict):
        raise MailArchiveError("request must be one JSON object")
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mail-archive-command-adapter")
    parser.add_argument(
        "operation",
        choices=("collect", "probe", "execute", "readback", "undo", "readback-undo"),
    )
    parser.add_argument("--request", type=Path)
    parser.add_argument("--response", type=Path)
    parser.add_argument("--state-root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    response_path: Path | None = args.response
    try:
        if response_path is not None and not response_path.is_absolute():
            raise MailArchiveError("--response must be absolute")
        document = _read_request(args.request)
        mutating = args.operation in {"execute", "undo"}
        if mutating and os.environ.get(LIVE_ENV) != LIVE_CONFIRMATION:
            raise MailArchiveError(
                f"{args.operation} requires {LIVE_ENV}={LIVE_CONFIRMATION!r}"
            )
        backend = IMAPArchiveBackend()
        if args.operation == "collect":
            result = backend.collect(
                days=_bounded_int(document.get("days", 7), field="days", low=1, high=31),
                limit_per_account=_bounded_int(
                    document.get("limit_per_account", 40),
                    field="limit_per_account",
                    low=1,
                    high=100,
                ),
            )
        else:
            root = args.state_root or Path(
                os.environ.get(STATE_ROOT_ENV, str(DEFAULT_STATE_ROOT))
            )
            result = MailArchiveCommandBridge(
                backend,
                state_root=root,
            ).dispatch(args.operation, document)
        exit_code = 0
    except (OSError, ValueError, MailArchiveError, json.JSONDecodeError) as exc:
        code = (
            "source_version_conflict"
            if isinstance(exc, MailArchiveConflictError)
            else getattr(exc, "code", type(exc).__name__)
        )
        result = {
            "ok": False,
            "error": {"code": code, "message": str(exc)[:1000]},
        }
        exit_code = 2
    if response_path is not None:
        try:
            _write_json_atomic(response_path, result)
        except OSError as exc:
            print(
                _canonical_json(
                    {"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)}}
                ),
                file=sys.stderr,
            )
            return 2
    else:
        stream = sys.stdout if exit_code == 0 else sys.stderr
        print(_canonical_json(result), file=stream)
    return exit_code


class IMAPArchiveBackend:
    """Live IMAP implementation for iCloud and Gmail archive/undo."""

    def __init__(
        self,
        *,
        timeout_s: int = 20,
        account_configs: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self.timeout_s = timeout_s
        self.account_configs = dict(account_configs or _default_account_configs())

    def probe(self, account: str, mailbox: str | None = None) -> dict[str, Any]:
        with self._open(account) as conn:
            caps = _capability_set(conn)
            selected = None
            uidvalidity = None
            if mailbox:
                selected = self._select(conn, mailbox, readonly=True)
                uidvalidity = selected["uidvalidity"]
            return {
                "ok": True,
                "schema": PROBE_SCHEMA,
                "account": account,
                "mailbox": mailbox,
                "capabilities": sorted(caps),
                "supports_uidplus": "UIDPLUS" in caps,
                "supports_gmail": "X-GM-EXT-1" in caps,
                "selected": selected,
                "uidvalidity": uidvalidity,
            }

    def collect(self, *, days: int, limit_per_account: int) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        read_accounts: list[str] = []
        failures: dict[str, str] = {}
        for account in ("icloud", "gmail-burner"):
            try:
                rows.extend(
                    self._collect_account(
                        account,
                        days=days,
                        limit=limit_per_account,
                    )
                )
                read_accounts.append(account)
            except (OSError, ValueError, MailArchiveError, imaplib.IMAP4.error) as exc:
                failures[account] = str(exc)[:240]
        rows.sort(key=lambda row: str(row.get("date") or ""), reverse=True)
        return {
            "ok": bool(read_accounts),
            "rows": rows,
            "coverage": {
                "read_accounts": read_accounts,
                "folders": ["INBOX"],
                "unavailable_accounts": sorted(failures),
                "complete": not failures,
                "identity_mode": "exact",
                "detail": "; ".join(
                    f"{account}: {detail}"
                    for account, detail in sorted(failures.items())
                ),
            },
        }

    def _collect_account(
        self,
        account: str,
        *,
        days: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        with self._open(account) as conn:
            self._select(conn, "INBOX", readonly=True)
            uidvalidity = _current_uidvalidity(conn)
            if uidvalidity <= 0:
                raise MailArchiveVerificationError(
                    f"{account} did not expose UIDVALIDITY"
                )
            since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime(
                "%d-%b-%Y"
            )
            typ, data = conn.uid("SEARCH", None, "SINCE", since)
            if typ != "OK":
                raise MailArchiveError(f"{account} recent UID search failed")
            raw_uids = data[0].split() if data and data[0] else []
            uids = [int(raw) for raw in raw_uids[-limit:]]
            provider = _provider_for_account(account)
            rows: list[dict[str, Any]] = []
            for uid in reversed(uids):
                row = self._fetch_collection_row(
                    conn,
                    account=account,
                    provider=provider,
                    uidvalidity=uidvalidity,
                    uid=uid,
                )
                if row is not None:
                    rows.append(row)
            return rows

    def _fetch_collection_row(
        self,
        conn: imaplib.IMAP4_SSL,
        *,
        account: str,
        provider: str,
        uidvalidity: int,
        uid: int,
    ) -> dict[str, Any] | None:
        gmail_fields = "X-GM-MSGID X-GM-LABELS " if provider == "gmail" else ""
        fields = (
            f"(UID {gmail_fields}"
            "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)] "
            "BODY.PEEK[1.MIME] BODY.PEEK[1]<0.2000>)"
        )
        typ, data = conn.uid("FETCH", str(uid), fields)
        if typ != "OK" or not data:
            return None
        identity = _parse_fetch_message(data)
        if identity is None or int(identity["uid"]) != uid:
            return None
        headers = b""
        mime_headers = b""
        body = b""
        for item in data:
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            prelude, payload = item
            prelude_bytes = (
                prelude
                if isinstance(prelude, bytes)
                else str(prelude).encode("utf-8", errors="replace")
            )
            payload_bytes = (
                payload
                if isinstance(payload, bytes)
                else str(payload).encode("utf-8", errors="replace")
            )
            upper = prelude_bytes.upper()
            if b"HEADER.FIELDS" in upper:
                headers = payload_bytes
            elif b"BODY[1.MIME]" in upper:
                mime_headers = payload_bytes
            elif b"BODY[1]" in upper:
                body = payload_bytes
        message = email.message_from_bytes(headers)
        message_id = str(message.get("Message-ID") or identity["message_id"] or "").strip()
        if not _MESSAGE_ID_RE.fullmatch(message_id):
            return None
        subject = _decode_mime(message.get("Subject", ""))[:240]
        from_name, from_addr = _parse_addr(str(message.get("From") or ""))
        raw_date = str(message.get("Date") or "")
        try:
            parsed_date = email.utils.parsedate_to_datetime(raw_date)
            if parsed_date.tzinfo is None:
                parsed_date = parsed_date.replace(tzinfo=dt.timezone.utc)
            date_value = parsed_date.astimezone(dt.timezone.utc).isoformat()
        except (TypeError, ValueError):
            date_value = raw_date[:80]
        labels = _normalize_labels(identity.get("labels") or ())
        gmail_msgid = str(identity.get("gmail_msgid") or "")
        if provider == "gmail":
            if not _GMAIL_MSGID_RE.fullmatch(gmail_msgid):
                return None
            if not _is_label_set(labels, "INBOX"):
                labels = _normalize_labels((*labels, "INBOX"))
        return {
            "id": f"{account}:{uidvalidity}:{uid}:{message_id}",
            "provider": provider,
            "account": account,
            "origin_mailbox": "INBOX",
            "uidvalidity": str(uidvalidity),
            "uid": str(uid),
            "rfc_message_id": message_id,
            "x_gm_msgid": gmail_msgid if provider == "gmail" else "",
            "observed_labels": list(labels) if provider == "gmail" else [],
            "destination_mailbox": "" if provider == "gmail" else "Archive",
            "subject": subject or "(no subject)",
            "from": (from_name or from_addr)[:160],
            "from_addr": from_addr[:160],
            "date": date_value,
            "snippet": _display_snippet(body, mime_headers),
        }

    def execute(self, request: MailArchiveRequest, *, operation_id: str) -> dict[str, Any]:
        with self._open(request.account) as conn:
            if request.provider == "gmail":
                return self._gmail_execute(conn, request, operation_id=operation_id)
            return self._icloud_execute(conn, request, operation_id=operation_id)

    def readback(
        self,
        request: MailArchiveRequest,
        *,
        execute_receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        if request.provider == "gmail":
            return self._gmail_readback(request, execute_receipt=execute_receipt)
        return self._icloud_readback(request, execute_receipt=execute_receipt)

    def undo(
        self,
        request: MailArchiveRequest,
        *,
        execute_receipt: Mapping[str, Any],
        approval_ref: str,
    ) -> dict[str, Any]:
        if request.provider == "gmail":
            return self._gmail_undo(
                request, execute_receipt=execute_receipt, approval_ref=approval_ref
            )
        return self._icloud_undo(
            request, execute_receipt=execute_receipt, approval_ref=approval_ref
        )

    def readback_undo(
        self,
        request: MailArchiveRequest,
        *,
        execute_receipt: Mapping[str, Any],
        undo_receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        if request.provider == "gmail":
            return self._gmail_readback_undo(
                request,
                execute_receipt=execute_receipt,
                undo_receipt=undo_receipt,
            )
        return self._icloud_readback_undo(
            request,
            execute_receipt=execute_receipt,
            undo_receipt=undo_receipt,
        )

    def _icloud_execute(
        self, conn: imaplib.IMAP4_SSL, request: MailArchiveRequest, *, operation_id: str
    ) -> dict[str, Any]:
        self._require_capability(conn, "UIDPLUS")
        origin = self._select(conn, request.origin_mailbox)
        before = self._fetch_standard_identity(conn, request)
        if before["uidvalidity"] != request.uidvalidity or before["uid"] != request.uid:
            raise MailArchiveConflictError(
                "message identity changed before archive",
                details={"expected_uidvalidity": request.uidvalidity, "expected_uid": request.uid},
            )
        if before["message_id"] != request.message_id:
            raise MailArchiveConflictError("RFC Message-ID changed before archive")
        copyuid = self._copy_message(conn, request.uid, request.archive_mailbox)
        archive_identity = dict(before)
        archive_identity.update(
            {
                "mailbox": request.archive_mailbox,
                "uidvalidity": copyuid["dest_uidvalidity"],
                "uid": copyuid["dest_uid"],
            }
        )
        try:
            self._delete_uid(conn, request.origin_mailbox, request.uid)
        except (OSError, MailArchiveError, imaplib.IMAP4.error) as exc:
            self._select(conn, request.archive_mailbox, readonly=True)
            copied = self._fetch_uid_message(
                conn,
                copyuid["dest_uid"],
                fields="(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
            )
            if copied is None or copied.get("message_id") != request.message_id:
                raise MailArchiveVerificationError(
                    "archive copy could not be reconciled after origin removal failed"
                ) from exc
            return {
                "ok": False,
                "schema": RECEIPT_SCHEMA,
                "provider": request.provider,
                "account": request.account,
                "operation_id": operation_id,
                "status": "uncertain",
                "effect_verified": False,
                "before": before,
                "after": {
                    "origin": before,
                    "archive": {
                        "account": request.account,
                        "provider": request.provider,
                        **archive_identity,
                    },
                },
                "undo": {
                    "operation": "reconcile_copy_and_origin",
                    "origin_mailbox": request.origin_mailbox,
                    "archive_mailbox": request.archive_mailbox,
                    "archive_uidvalidity": copyuid["dest_uidvalidity"],
                    "archive_uid": copyuid["dest_uid"],
                    "message_id": request.message_id,
                },
            }
        after = self._icloud_readback_identity(conn, request, archive_identity=archive_identity)
        return {
            "ok": True,
            "schema": RECEIPT_SCHEMA,
            "provider": request.provider,
            "account": request.account,
            "operation_id": operation_id,
            "status": "archived",
            "effect_verified": True,
            "before": before,
            "after": after,
            "undo": {
                "operation": "copy_back_and_delete_archive",
                "origin_mailbox": request.origin_mailbox,
                "archive_mailbox": request.archive_mailbox,
                "archive_uidvalidity": copyuid["dest_uidvalidity"],
                "archive_uid": copyuid["dest_uid"],
                "message_id": request.message_id,
            },
        }

    def _gmail_execute(
        self, conn: imaplib.IMAP4_SSL, request: MailArchiveRequest, *, operation_id: str
    ) -> dict[str, Any]:
        self._require_capability(conn, "X-GM-EXT-1")
        self._select(conn, request.origin_mailbox)
        before = self._fetch_gmail_identity(conn, request)
        if before["uidvalidity"] != request.uidvalidity or before["uid"] != request.uid:
            raise MailArchiveConflictError("gmail UID or UIDVALIDITY changed before archive")
        if before["message_id"] != request.message_id or before["gmail_msgid"] != request.gmail_msgid:
            raise MailArchiveConflictError("gmail message identity changed before archive")
        if tuple(request.labels) != tuple(before["labels"]):
            raise MailArchiveConflictError("gmail observed labels changed before archive")
        self._gmail_set_labels(conn, request.uid, remove=(r"\Inbox",))
        after = self._fetch_gmail_identity(conn, request, uid_hint=int(before["uid"]))
        if _is_label_set(after["labels"], "Inbox"):
            raise MailArchiveVerificationError("gmail archive did not remove Inbox")
        if not _is_label_set(after["labels"], "All Mail"):
            # Gmail may omit the implicit All Mail system label from X-GM-LABELS.
            # Presence in the selected All Mail mailbox is the authoritative proof.
            pass
        expected_after = _normalize_labels(
            [
                label
                for label in request.labels
                if not _is_label(label, "INBOX")
            ]
        )
        if tuple(after["labels"]) != expected_after:
            raise MailArchiveVerificationError(
                "Gmail archive changed labels beyond removing Inbox"
            )
        return {
            "ok": True,
            "schema": RECEIPT_SCHEMA,
            "provider": request.provider,
            "account": request.account,
            "operation_id": operation_id,
            "status": "archived",
            "effect_verified": True,
            "before": before,
            "after": after,
            "undo": {
                "operation": "restore_inbox_label",
                "uid": request.uid,
                "gmail_msgid": request.gmail_msgid,
                "labels_before": list(before["labels"]),
                "labels_after": list(after["labels"]),
            },
        }

    def _icloud_readback(
        self,
        request: MailArchiveRequest,
        *,
        execute_receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        after = _mapping(execute_receipt.get("after"), field="execute_receipt.after")
        expected = _mapping(after.get("archive"), field="execute_receipt.after.archive")
        with self._open(request.account) as conn:
            current = self._icloud_readback_identity(conn, request, archive_identity=expected)
        destination = _mapping(current.get("archive"), field="archive identity")
        return {
            "origin_absent": current.get("origin") is None,
            "destination_present": True,
            "destination_identity": {
                "account": request.account,
                "mailbox": request.archive_mailbox,
                "uidvalidity": str(destination["uidvalidity"]),
                "uid": str(destination["uid"]),
                "rfc_message_id": destination["message_id"],
            },
        }

    def _gmail_readback(
        self,
        request: MailArchiveRequest,
        *,
        execute_receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        expected = _mapping(execute_receipt.get("after"), field="execute_receipt.after")
        with self._open(request.account) as conn:
            current = self._fetch_gmail_identity(conn, request, uid_hint=int(expected["uid"]))
        if _is_label_set(current["labels"], "Inbox"):
            raise MailArchiveVerificationError("gmail readback still sees Inbox")
        if not _is_label_set(current["labels"], "All Mail"):
            raise MailArchiveVerificationError("gmail readback lost All Mail")
        expected_labels = _normalize_labels(
            [
                label
                for label in request.labels
                if not _is_label(label, "INBOX")
            ]
        )
        if tuple(current["labels"]) != expected_labels:
            raise MailArchiveConflictError(
                "Gmail labels changed after the archived state was recorded"
            )
        if current["message_id"] != request.message_id:
            raise MailArchiveVerificationError("Gmail Message-ID changed during readback")
        return {
            "x_gm_msgid": current["gmail_msgid"],
            "all_mail_present": True,
            "inbox_absent": True,
        }

    def _icloud_undo(
        self,
        request: MailArchiveRequest,
        *,
        execute_receipt: Mapping[str, Any],
        approval_ref: str,
    ) -> dict[str, Any]:
        _ = approval_ref
        before = _mapping(execute_receipt.get("after"), field="execute_receipt.after")
        archive_identity = _mapping(
            before.get("archive"), field="execute_receipt.after.archive"
        )
        self._require_capability_from_receipt(execute_receipt, "UIDPLUS")
        with self._open(request.account) as conn:
            self._select(conn, request.archive_mailbox)
            current_archive = self._fetch_standard_identity(
                conn,
                request,
                mailbox=request.archive_mailbox,
                uid_hint=int(archive_identity["uid"]),
                uidvalidity_hint=int(archive_identity["uidvalidity"]),
            )
            if current_archive["message_id"] != request.message_id:
                raise MailArchiveConflictError("archived message changed before undo")
            copyuid = self._copy_message(conn, current_archive["uid"], request.origin_mailbox)
            self._delete_uid(conn, request.archive_mailbox, current_archive["uid"])
            restored = self._icloud_readback_identity(
                conn,
                request,
                archive_identity=None,
                origin_uid=copyuid["dest_uid"],
            )
        return {
            "ok": True,
            "schema": UNDO_RECEIPT_SCHEMA,
            "provider": request.provider,
            "account": request.account,
            "status": "restored",
            "effect_verified": True,
            "before": before,
            "after": restored,
        }

    def _gmail_undo(
        self,
        request: MailArchiveRequest,
        *,
        execute_receipt: Mapping[str, Any],
        approval_ref: str,
    ) -> dict[str, Any]:
        _ = approval_ref
        before = _mapping(execute_receipt.get("after"), field="execute_receipt.after")
        with self._open(request.account) as conn:
            self._select(conn, request.all_mailbox)
            current = self._fetch_gmail_identity(conn, request, uid_hint=int(before["uid"]))
            if current["gmail_msgid"] != request.gmail_msgid:
                raise MailArchiveConflictError("gmail message changed before undo")
            expected_after = _normalize_labels(
                [
                    label
                    for label in request.labels
                    if not _is_label(label, "INBOX")
                ]
            )
            if tuple(current["labels"]) != expected_after:
                raise MailArchiveConflictError(
                    "Gmail labels changed before guarded undo"
                )
            self._gmail_set_labels(conn, int(current["uid"]), add=(r"\Inbox",))
            restored = self._fetch_gmail_identity(
                conn,
                request,
                uid_hint=int(current["uid"]),
            )
        if not _is_label_set(restored["labels"], "Inbox"):
            raise MailArchiveVerificationError("gmail undo did not restore Inbox")
        if tuple(restored["labels"]) != request.labels:
            raise MailArchiveVerificationError(
                "Gmail undo did not restore the exact prior labels"
            )
        return {
            "ok": True,
            "schema": UNDO_RECEIPT_SCHEMA,
            "provider": request.provider,
            "account": request.account,
            "status": "restored",
            "effect_verified": True,
            "before": before,
            "after": restored,
        }

    def _icloud_readback_undo(
        self,
        request: MailArchiveRequest,
        *,
        execute_receipt: Mapping[str, Any],
        undo_receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        before = _mapping(execute_receipt.get("before"), field="execute_receipt.before")
        executed_after = _mapping(
            execute_receipt.get("after"), field="execute_receipt.after"
        )
        destination = _mapping(
            executed_after.get("archive"),
            field="execute_receipt.after.archive",
        )
        restored_after = _mapping(
            undo_receipt.get("after"), field="undo_receipt.after"
        )
        restored_identity = _mapping(
            restored_after.get("origin"),
            field="undo_receipt.after.origin",
        )
        with self._open(request.account) as conn:
            restored = self._fetch_standard_identity(
                conn,
                request,
                mailbox=request.origin_mailbox,
                uid_hint=int(restored_identity["uid"]),
                uidvalidity_hint=int(restored_identity["uidvalidity"]),
            )
            if restored["message_id"] != request.message_id:
                raise MailArchiveVerificationError("restored message has the wrong Message-ID")
            self._select(conn, request.archive_mailbox, readonly=True)
            if _current_uidvalidity(conn) != int(destination["uidvalidity"]):
                raise MailArchiveConflictError(
                    "archive UIDVALIDITY changed before undo readback"
                )
            archive_present = self._fetch_uid_message(
                conn,
                int(destination["uid"]),
                fields="(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
            )
            if archive_present is not None:
                raise MailArchiveVerificationError("archived copy still present after undo")
        return {
            "origin_present": True,
            "destination_absent": True,
            "origin_identity": {
                "account": request.account,
                "mailbox": request.origin_mailbox,
                "uidvalidity": str(restored["uidvalidity"]),
                "uid": str(restored["uid"]),
                "rfc_message_id": restored["message_id"],
            },
        }

    def _gmail_readback_undo(
        self,
        request: MailArchiveRequest,
        *,
        execute_receipt: Mapping[str, Any],
        undo_receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        before = _mapping(execute_receipt.get("before"), field="execute_receipt.before")
        with self._open(request.account) as conn:
            restored = self._fetch_gmail_identity(
                conn,
                request,
                uid_hint=int(undo_receipt["after"]["uid"]),
            )
        if not _is_label_set(restored["labels"], "Inbox"):
            raise MailArchiveVerificationError("gmail undo readback did not restore Inbox")
        if not _is_label_set(restored["labels"], "All Mail"):
            raise MailArchiveVerificationError("gmail undo readback lost All Mail")
        if tuple(restored["labels"]) != request.labels:
            raise MailArchiveVerificationError(
                "Gmail undo readback did not restore exact prior labels"
            )
        return {
            "x_gm_msgid": restored["gmail_msgid"],
            "restored_labels": list(restored["labels"]),
        }

    def _open(self, account: str):
        cfg = self.account_configs.get(account)
        if cfg is None:
            raise MailArchiveError(f"unknown mail account {account!r}")
        host = (get_secret(cfg["host_key"]) if cfg.get("host_key") else None) or cfg["host_default"]
        user = get_secret(cfg["user_key"])
        password = get_secret(cfg["pass_key"])
        if not user or not password:
            raise MailArchiveError(f"missing creds for {account}")
        socket.setdefaulttimeout(self.timeout_s)
        try:
            conn = imaplib.IMAP4_SSL(host, port=993, timeout=self.timeout_s)
            conn.login(user, password)
        except (imaplib.IMAP4.error, socket.error) as exc:
            raise MailArchiveError(f"{account} IMAP connect/login failed: {exc}") from exc

        class _Conn:
            def __init__(self, inner: imaplib.IMAP4_SSL) -> None:
                self.inner = inner

            def __enter__(self) -> imaplib.IMAP4_SSL:
                return self.inner

            def __exit__(self, *exc_info) -> None:
                try:
                    self.inner.close()
                except Exception:
                    pass
                try:
                    self.inner.logout()
                except Exception:
                    pass

        return _Conn(conn)

    def _select(
        self, conn: imaplib.IMAP4_SSL, mailbox: str, *, readonly: bool = False
    ) -> dict[str, Any]:
        typ, data = conn.select(mailbox, readonly=readonly)
        if typ != "OK":
            raise MailArchiveError(f"select {mailbox!r} failed: {typ}")
        uidvalidity = _extract_uidvalidity(conn, data)
        setattr(conn, "_duffields_uidvalidity", uidvalidity)
        setattr(conn, "_duffields_selected_mailbox", mailbox)
        return {"mailbox": mailbox, "uidvalidity": uidvalidity}

    def _fetch_standard_identity(
        self,
        conn: imaplib.IMAP4_SSL,
        request: MailArchiveRequest,
        *,
        mailbox: str | None = None,
        uid_hint: int | None = None,
        uidvalidity_hint: int | None = None,
        origin_uid: int | None = None,
    ) -> dict[str, Any]:
        mailbox = mailbox or request.origin_mailbox
        self._select(conn, mailbox)
        if uidvalidity_hint is not None:
            current_uidvalidity = _current_uidvalidity(conn)
            if current_uidvalidity != uidvalidity_hint:
                raise MailArchiveConflictError("UIDVALIDITY changed before archive")
        fetched = self._fetch_uid_message(conn, uid_hint or request.uid, fields="(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        if fetched is None:
            raise MailArchiveNotFoundError("message not found in source mailbox")
        return {
            "account": request.account,
            "provider": request.provider,
            "mailbox": mailbox,
            "uidvalidity": _current_uidvalidity(conn),
            "uid": int(fetched["uid"]),
            "message_id": fetched["message_id"],
        }

    def _fetch_gmail_identity(
        self,
        conn: imaplib.IMAP4_SSL,
        request: MailArchiveRequest,
        *,
        uid_hint: int | None = None,
    ) -> dict[str, Any]:
        mailbox = request.origin_mailbox if uid_hint is None else request.all_mailbox
        self._select(conn, mailbox)
        if uid_hint is None:
            fetched = self._fetch_uid_message(
                conn,
                request.uid,
                fields="(UID X-GM-MSGID X-GM-LABELS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
            )
            if fetched is None:
                raise MailArchiveNotFoundError("gmail message not found in source mailbox")
            return {
                "account": request.account,
                "provider": request.provider,
                "mailbox": mailbox,
                "uidvalidity": _current_uidvalidity(conn),
                "uid": int(fetched["uid"]),
                "message_id": fetched["message_id"],
                "gmail_msgid": fetched["gmail_msgid"],
                "labels": tuple(fetched["labels"]),
            }
        fetched = self._find_by_gmail_msgid(conn, request.gmail_msgid or "", mailbox=mailbox)
        if fetched is None:
            raise MailArchiveNotFoundError("gmail message was not found during readback")
        return {
            "account": request.account,
            "provider": request.provider,
            "mailbox": mailbox,
            "uidvalidity": _current_uidvalidity(conn),
            "uid": int(fetched["uid"]),
            "message_id": fetched["message_id"],
            "gmail_msgid": fetched["gmail_msgid"],
            "labels": tuple(fetched["labels"]),
        }

    def _icloud_readback_identity(
        self,
        conn: imaplib.IMAP4_SSL,
        request: MailArchiveRequest,
        *,
        archive_identity: Mapping[str, Any] | None,
        origin_uid: int | None = None,
    ) -> dict[str, Any]:
        if archive_identity is not None:
            self._select(conn, request.origin_mailbox, readonly=True)
            if _current_uidvalidity(conn) != request.uidvalidity:
                raise MailArchiveConflictError(
                    "origin UIDVALIDITY changed during archive readback"
                )
            origin = self._fetch_uid_message(
                conn,
                request.uid,
                fields="(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
            )
            if origin is not None:
                raise MailArchiveVerificationError(
                    "origin identity is still present after archive"
                )
            self._select(conn, request.archive_mailbox, readonly=True)
            if _current_uidvalidity(conn) != int(archive_identity["uidvalidity"]):
                raise MailArchiveConflictError(
                    "destination UIDVALIDITY changed during archive readback"
                )
            fetched = self._fetch_uid_message(
                conn,
                int(archive_identity["uid"]),
                fields="(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
            )
            if fetched is None:
                raise MailArchiveVerificationError("archived copy missing after execute")
            if fetched["message_id"] != request.message_id:
                raise MailArchiveVerificationError(
                    "archived copy has the wrong RFC Message-ID"
                )
            return {
                "origin": None,
                "archive": {
                    "account": request.account,
                    "provider": request.provider,
                    "mailbox": request.archive_mailbox,
                    "uidvalidity": _current_uidvalidity(conn),
                    "uid": int(fetched["uid"]),
                    "message_id": fetched["message_id"],
                },
            }
        self._select(conn, request.origin_mailbox)
        fetched = self._fetch_uid_message(
            conn,
            origin_uid or request.uid,
            fields="(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
        )
        if fetched is None:
            raise MailArchiveVerificationError("restored message missing after undo")
        return {
            "origin": {
                "account": request.account,
                "provider": request.provider,
                "mailbox": request.origin_mailbox,
                "uidvalidity": _current_uidvalidity(conn),
                "uid": int(fetched["uid"]),
                "message_id": fetched["message_id"],
            },
            "archive": None,
        }

    def _copy_message(self, conn: imaplib.IMAP4_SSL, uid: int, mailbox: str) -> dict[str, int]:
        typ, data = conn.uid("COPY", str(uid), mailbox)
        if typ != "OK":
            raise MailArchiveError(f"copy to {mailbox!r} failed: {typ}")
        uidvalidity, dest_uid = _parse_copyuid(data)
        if uidvalidity is None or dest_uid is None:
            raise MailArchiveVerificationError("UIDPLUS COPYUID response is required")
        return {"dest_uidvalidity": uidvalidity, "dest_uid": dest_uid}

    def _delete_uid(self, conn: imaplib.IMAP4_SSL, mailbox: str, uid: int) -> None:
        self._select(conn, mailbox)
        typ, _ = conn.uid("STORE", str(uid), "+FLAGS.SILENT", r"(\Deleted)")
        if typ != "OK":
            raise MailArchiveError(f"marking UID {uid} deleted in {mailbox!r} failed")
        typ, _ = conn.uid("EXPUNGE", str(uid))
        if typ != "OK":
            raise MailArchiveError(f"expunging UID {uid} in {mailbox!r} failed")

    def _gmail_set_labels(
        self,
        conn: imaplib.IMAP4_SSL,
        uid: int,
        *,
        remove: Sequence[str] = (),
        add: Sequence[str] = (),
    ) -> None:
        if remove:
            typ, _ = conn.uid("STORE", str(uid), "-X-GM-LABELS.SILENT", "(" + " ".join(remove) + ")")
            if typ != "OK":
                raise MailArchiveError("removing Gmail labels failed")
        if add:
            typ, _ = conn.uid("STORE", str(uid), "+X-GM-LABELS.SILENT", "(" + " ".join(add) + ")")
            if typ != "OK":
                raise MailArchiveError("adding Gmail labels failed")

    def _find_by_message_id(self, conn: imaplib.IMAP4_SSL, mailbox: str, message_id: str) -> dict[str, Any] | None:
        self._select(conn, mailbox)
        uid = _search_message_id(conn, message_id)
        if uid is None:
            return None
        fetched = self._fetch_uid_message(
            conn,
            uid,
            fields="(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
        )
        if fetched is None:
            return None
        return {
            "mailbox": mailbox,
            "uidvalidity": _current_uidvalidity(conn),
            "uid": int(fetched["uid"]),
            "message_id": fetched["message_id"],
        }

    def _find_by_gmail_msgid(
        self, conn: imaplib.IMAP4_SSL, gm_msgid: str, *, mailbox: str
    ) -> dict[str, Any] | None:
        self._select(conn, mailbox)
        uid = _search_gmail_msgid(conn, gm_msgid)
        if uid is None:
            return None
        fetched = self._fetch_uid_message(
            conn,
            uid,
            fields="(UID X-GM-MSGID X-GM-LABELS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
        )
        if fetched is None:
            return None
        return {
            "mailbox": mailbox,
            "uidvalidity": _current_uidvalidity(conn),
            "uid": int(fetched["uid"]),
            "message_id": fetched["message_id"],
            "gmail_msgid": fetched["gmail_msgid"],
            "labels": tuple(fetched["labels"]),
        }

    def _require_capability(self, conn: imaplib.IMAP4_SSL, capability: str) -> None:
        caps = _capability_set(conn)
        if capability not in caps:
            raise MailArchiveError(f"IMAP capability {capability!r} is required")

    @staticmethod
    def _require_capability_from_receipt(receipt: Mapping[str, Any], capability: str) -> None:
        if capability == "UIDPLUS" and receipt.get("undo", {}).get("operation") != "copy_back_and_delete_archive":
            raise MailArchiveError("undo receipt does not support UIDPLUS-style archive restore")

    @staticmethod
    def _fetch_uid_message(
        conn: imaplib.IMAP4_SSL,
        uid: int,
        *,
        fields: str,
    ) -> dict[str, Any] | None:
        typ, data = conn.uid("FETCH", str(uid), fields)
        if typ != "OK" or not data:
            return None
        return _parse_fetch_message(data)


def _default_account_configs() -> dict[str, dict[str, str]]:
    gmail_cfg = {
        "host_key": None,
        "host_default": "imap.gmail.com",
        "user_key": "CORN82_GMAIL_USER",
        "pass_key": "CORN82_GMAIL_APP_PASSWORD",
    }
    return {
        "icloud": {
            "host_key": "ICLOUD_IMAP_HOST",
            "host_default": "imap.mail.me.com",
            "user_key": "ICLOUD_EMAIL",
            "pass_key": "ICLOUD_APP_PASSWORD",
        },
        "gmail": dict(gmail_cfg),
        "gmail-burner": dict(gmail_cfg),
    }


def _display_snippet(raw_body: bytes, mime_headers: bytes) -> str:
    fallback = _extract_snippet(raw_body, mime_headers)
    try:
        part = email.message_from_bytes(mime_headers + b"\r\n" + raw_body)
        decoded = part.get_payload(decode=True)
        if isinstance(decoded, bytes):
            charset = part.get_content_charset() or "utf-8"
            try:
                text = decoded.decode(charset, errors="replace")
            except (LookupError, UnicodeError):
                text = decoded.decode("utf-8", errors="replace")
        else:
            payload = part.get_payload()
            text = payload if isinstance(payload, str) else fallback
    except Exception:
        text = fallback
    text = re.sub(
        r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
        " ",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"=\r?\n", "", text)
    return re.sub(r"\s+", " ", text).strip()[:240]


def _parse_fetch_message(data: list[Any]) -> dict[str, Any] | None:
    uid = None
    message_id = ""
    gmail_msgid = ""
    labels: list[str] = []
    for item in data:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        prelude, payload = item
        if isinstance(prelude, bytes):
            prelude_text = prelude.decode("utf-8", errors="replace")
        else:
            prelude_text = str(prelude)
        if uid is None:
            match = re.search(r"\bUID\s+(\d+)\b", prelude_text, re.IGNORECASE)
            if match:
                uid = int(match.group(1))
        blob = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
        match = re.search(r"Message-ID:\s*(<[^>]+>)", blob, re.IGNORECASE)
        if match:
            message_id = match.group(1).strip()
        if "X-GM-MSGID" in prelude_text.upper():
            match = re.search(r"X-GM-MSGID\s+(\d+)", prelude_text, re.IGNORECASE)
            if match:
                gmail_msgid = match.group(1)
        if "X-GM-LABELS" in prelude_text.upper():
            labels = _parse_labels(prelude_text)
    if uid is None:
        return None
    return {
        "uid": uid,
        "message_id": message_id,
        "gmail_msgid": gmail_msgid,
        "labels": labels,
    }


def _parse_labels(blob: str) -> list[str]:
    match = re.search(r"X-GM-LABELS\s*\((.*?)\)", blob, re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    tokens = re.findall(r'"(?:\\.|[^"])*"|[^\s]+', match.group(1).strip())
    return [_canonical_label(token) for token in tokens if token]


def _search_message_id(conn: imaplib.IMAP4_SSL, message_id: str) -> int | None:
    typ, data = conn.uid("SEARCH", "HEADER", "Message-ID", message_id)
    if typ != "OK" or not data or not data[0]:
        return None
    return int(data[0].split()[0])


def _search_gmail_msgid(conn: imaplib.IMAP4_SSL, gm_msgid: str) -> int | None:
    typ, data = conn.uid("SEARCH", "X-GM-MSGID", gm_msgid)
    if typ != "OK" or not data or not data[0]:
        return None
    return int(data[0].split()[0])


def _parse_copyuid(data: list[Any]) -> tuple[int | None, int | None]:
    text = " ".join(
        item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
        for item in data
    )
    match = re.search(r"COPYUID\s+([0-9]+)\s+[0-9:,]+\s+([0-9]+)", text)
    if not match:
        return (None, None)
    return (int(match.group(1)), int(match.group(2)))


def _capability_set(conn: imaplib.IMAP4_SSL) -> set[str]:
    typ, data = conn.capability()
    if typ != "OK" or not data:
        return set()
    caps: set[str] = set()
    for item in data:
        raw = item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
        caps.update(token.upper() for token in raw.split())
    return caps


def _current_uidvalidity(conn: imaplib.IMAP4_SSL) -> int:
    resp = conn.response("UIDVALIDITY")
    if resp and len(resp) > 1 and resp[1]:
        first = resp[1][0]
        raw = first.decode("utf-8", errors="replace") if isinstance(first, bytes) else str(first)
        match = re.search(r"([0-9]+)", raw)
        if match:
            return int(match.group(1))
    cached = getattr(conn, "_duffields_uidvalidity", 0)
    return int(cached) if isinstance(cached, int) else 0


def _extract_uidvalidity(conn: imaplib.IMAP4_SSL, data: list[Any]) -> int:
    uidvalidity = _current_uidvalidity(conn)
    if uidvalidity:
        return uidvalidity
    for item in data:
        raw = item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
        match = re.search(r"UIDVALIDITY\s+([0-9]+)", raw, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 0


def _provider_for_account(account: str) -> str:
    if account in {"gmail", "gmail-burner"}:
        return "gmail"
    return "icloud"


def _is_label(label: str, expected: str) -> bool:
    return _canonical_label(label).lower() == _canonical_label(expected).lower()


def _is_label_set(labels: Sequence[str], expected: str) -> bool:
    return any(_is_label(label, expected) for label in labels)


def _normalize_labels(labels: Sequence[str]) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for label in labels:
        normalized = _canonical_label(label)
        if normalized:
            seen[normalized.lower()] = normalized
    return tuple(sorted(seen.values(), key=lambda item: item.lower()))


def _canonical_label(label: str) -> str:
    rendered = str(label or "").strip()
    if len(rendered) >= 2 and rendered[0] == rendered[-1] == '"':
        rendered = rendered[1:-1].replace(r"\"", '"').replace(r"\\", "\\")
    aliases = {
        r"\inbox": "INBOX",
        "inbox": "INBOX",
        r"\all": "ALL",
        "all": "ALL",
        "all mail": "ALL",
    }
    return aliases.get(rendered.lower(), rendered)


def _label_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise MailArchiveError("labels must be a JSON array")
    labels: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise MailArchiveError(f"labels[{index}] must be a nonempty string")
        labels.append(item.strip())
    return labels


def _account_name(value: Any) -> str:
    rendered = str(value or "")
    if not _SAFE_NAME_RE.fullmatch(rendered):
        raise MailArchiveError("account has an invalid shape")
    return rendered


def _operation_id(value: Any) -> str:
    rendered = str(value or "")
    if not _OPERATION_ID_RE.fullmatch(rendered):
        raise MailArchiveError("operation_id has an invalid shape")
    return rendered


def _bounded(value: Any, *, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise MailArchiveError(f"{field} must be a string")
    rendered = value.strip()
    if not rendered or len(rendered) > limit:
        raise MailArchiveError(f"{field} must contain 1 to {limit} characters")
    return rendered


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MailArchiveError(f"{field} must be a positive integer")
    return value


def _positive_int_text(value: Any, *, field: str) -> int:
    rendered = str(value or "")
    pattern = _UIDVALIDITY_RE if field == "uidvalidity" else _UID_RE
    if not pattern.fullmatch(rendered):
        raise MailArchiveError(f"{field} must be a positive integer string")
    return int(rendered)


def _bounded_int(value: Any, *, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise MailArchiveError(f"{field} must be an integer from {low} to {high}")
    return value


def _message_id(value: Any) -> str:
    rendered = _bounded(value, field="message_id", limit=255)
    if not _MESSAGE_ID_RE.fullmatch(rendered):
        raise MailArchiveError("message_id must be an RFC Message-ID")
    return rendered


def _gmail_msgid(value: Any) -> str:
    rendered = _bounded(value, field="gmail_msgid", limit=20)
    if not _GMAIL_MSGID_RE.fullmatch(rendered):
        raise MailArchiveError("gmail_msgid must be a Gmail X-GM-MSGID")
    return rendered


def _mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MailArchiveError(f"{field} must be a JSON object")
    return value


def _exact(value: Mapping[str, Any], expected: set[str], *, field: str) -> None:
    if not isinstance(value, Mapping):
        raise MailArchiveError(f"{field} must be an object")
    supplied = set(value)
    if supplied != expected:
        missing = sorted(expected - supplied)
        extra = sorted(supplied - expected)
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if extra:
            detail.append(f"unexpected {', '.join(extra)}")
        raise MailArchiveError(f"{field} has invalid fields: {'; '.join(detail)}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _operation_digest(operation_id: str) -> str:
    return _digest({"operation_id": operation_id})


def _proposal_digest(proposal: Mapping[str, Any]) -> str:
    return _digest(
        {
            key: proposal.get(key)
            for key in (
                "proposal_version",
                "proposal_id",
                "action",
                "target",
                "risk",
            )
        }
    )


def _parse_reference(reference: Any, *, prefix: str) -> str:
    if not isinstance(reference, str):
        raise MailArchiveError("reference must be a string")
    rendered = reference.strip()
    if not rendered.startswith(prefix):
        raise MailArchiveError(f"reference must start with {prefix!r}")
    digest = rendered[len(prefix) :]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise MailArchiveError("reference has an invalid operation digest")
    return digest


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a+b") as handle:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _write_json_atomic(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    tmp.write_text(_canonical_json(document), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _reject_constant(value: str) -> None:
    raise MailArchiveError(f"non-finite JSON number is forbidden: {value}")


def _read_json_file(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    document = json.loads(raw, parse_constant=_reject_constant)
    if not isinstance(document, dict):
        raise MailArchiveError("request must be a JSON object")
    return document


if __name__ == "__main__":
    raise SystemExit(main())
