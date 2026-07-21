"""Duffields CommandAdapter bridge for the guarded Reminders canary.

The process accepts one fixed operation name and JSON over stdin, or over
explicit private request/response files for a shell-free tmux TCC relay.
Mutations are disabled unless the relay supplies the exact live environment
gate. Proposal values are never accepted through argv.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .reminder_upsert import (
    LIVE_CONFIRMATION,
    RECEIPT_SCHEMA,
    SCHEMA,
    UNDO_RECEIPT_SCHEMA,
    ConflictError,
    ReminderBackend,
    ReminderRecord,
    ReminderUpsertAdapter,
    ReminderUpsertError,
    ReminderUpsertRequest,
    ReminderValue,
    SwiftEventKitBackend,
    ValidationError,
    VerificationError,
    _canonical_json,
    _digest,
    _exclusive_lock,
    _same_value,
    _utc_now,
    _write_json_atomic,
)


STATE_SCHEMA = "reminder-command-adapter-state/v1"
UNDO_STATE_SCHEMA = "reminder-command-adapter-undo-state/v1"
REFERENCE_PREFIX = "reminder-upsert-operation:"
UNDO_REFERENCE_PREFIX = "reminder-upsert-undo-operation:"
LIVE_ENV = "REMINDER_COMMAND_ADAPTER_LIVE"
STATE_ROOT_ENV = "REMINDER_COMMAND_ADAPTER_STATE_ROOT"
DEFAULT_STATE_ROOT = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Duffields"
    / "reminder-adapter"
)
MAX_REQUEST_BYTES = 128 * 1024
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PAYLOAD_KEYS = frozenset({"list", "title", "notes", "due_at", "priority"})


class CommandBridgeError(ReminderUpsertError):
    pass


def _mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field} must be a JSON object")
    return value


def _operation_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("operation_id must be a string")
    rendered = value.strip()
    if not _OPERATION_ID_RE.fullmatch(rendered):
        raise ValidationError(
            "operation_id must be 8-160 safe identifier characters"
        )
    return rendered


def _operation_digest(operation_id: str) -> str:
    return _digest({"operation_id": operation_id})


def _proposal_digest(proposal: Mapping[str, Any]) -> str:
    return _digest(dict(proposal))


def _reject_constant(value: str) -> None:
    raise ValidationError(f"non-finite JSON number is forbidden: {value}")


def _parse_reference(reference: Any, *, prefix: str) -> str:
    if not isinstance(reference, str):
        raise ValidationError("reference must be a string")
    rendered = reference.strip()
    if not rendered.startswith(prefix):
        raise ValidationError(f"reference must start with {prefix!r}")
    digest = rendered[len(prefix) :]
    if not _DIGEST_RE.fullmatch(digest):
        raise ValidationError("reference has an invalid operation digest")
    return digest


def proposal_to_request(
    proposal: Mapping[str, Any], operation_id: str
) -> ReminderUpsertRequest:
    """Translate the narrow Duffields proposal view into the canary schema."""

    if proposal.get("proposal_version") != "duffields-action-proposal/v1":
        raise ValidationError("unsupported Duffields proposal_version")
    proposal_id_value = proposal.get("proposal_id")
    if not isinstance(proposal_id_value, str):
        raise ValidationError("proposal_id must be a string")
    proposal_id = proposal_id_value.strip()
    if not proposal_id or len(proposal_id) > 200:
        raise ValidationError("proposal_id is required and must be at most 200 characters")

    action = _mapping(proposal.get("action"), field="proposal.action")
    if action.get("kind") != "reminder.upsert":
        raise ValidationError("proposal.action.kind must be 'reminder.upsert'")
    if action.get("readback_required") is not True:
        raise ValidationError("reminder canary requires independent readback")
    if action.get("undo_supported") is not True:
        raise ValidationError("reminder canary requires guarded undo")
    payload = _mapping(action.get("payload"), field="proposal.action.payload")
    unknown = sorted(set(payload) - _PAYLOAD_KEYS)
    if unknown:
        raise ValidationError(f"unsupported reminder payload fields: {unknown}")

    target = _mapping(proposal.get("target"), field="proposal.target")
    if (
        target.get("system") != "apple_reminders"
        or target.get("resource_type") != "reminder"
    ):
        raise ValidationError("proposal target must be an Apple reminder")
    if target.get("scope") != payload.get("list"):
        raise ValidationError("proposal target scope must match the reminder list")
    if target.get("display") != payload.get("title"):
        raise ValidationError("proposal target display must match the reminder title")
    risk = _mapping(proposal.get("risk"), field="proposal.risk")
    if (
        risk.get("level") != "low"
        or risk.get("reversible") is not True
        or risk.get("external_communication") is not False
    ):
        raise ValidationError("reminder canary permits only low-risk reversible actions")

    operation_id = _operation_id(operation_id)
    digest = _operation_digest(operation_id)
    request = {
        "schema": SCHEMA,
        "idempotency_key": f"duffields:{digest}",
        "list": payload.get("list"),
        "title": payload.get("title"),
        "notes": payload.get("notes", ""),
        "due_at": payload.get("due_at"),
        "priority": payload.get("priority", "none"),
        "approval_ref": f"duffields-operation:{digest}",
        "source_ref": f"duffields-proposal:{proposal_id}",
    }
    return ReminderUpsertRequest.from_mapping(request)


class AdapterStateStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser()
        if not self.root.is_absolute():
            raise ValidationError("adapter state root must be an absolute path")

    def prepare(self) -> None:
        for path in (
            self.root,
            self.root / "operations",
            self.root / "undo",
            self.root / "locks",
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
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
            raise CommandBridgeError("operation state digest does not match its path")
        return state

    def load_operation_id(self, operation_id: str) -> dict[str, Any] | None:
        digest = _operation_digest(operation_id)
        state = self.load_operation_digest(digest)
        if state is not None and state.get("operation_id") != operation_id:
            raise ConflictError("operation digest is bound to a different operation_id")
        return state

    def load_reference(self, reference: Any) -> dict[str, Any]:
        digest = _parse_reference(reference, prefix=REFERENCE_PREFIX)
        state = self.load_operation_digest(digest)
        if state is None or state.get("reference") != reference:
            raise ValidationError("reminder operation reference was not found")
        return state

    def load_undo_reference(self, reference: Any) -> dict[str, Any]:
        digest = _parse_reference(reference, prefix=UNDO_REFERENCE_PREFIX)
        state = self._load(self.undo_path(digest), schema=UNDO_STATE_SCHEMA)
        if (
            state is None
            or state.get("operation_digest") != digest
            or state.get("reference") != reference
        ):
            raise ValidationError("reminder undo reference was not found")
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
            raise CommandBridgeError("adapter state operation digest must be a string")
        digest = value
        if not _DIGEST_RE.fullmatch(digest):
            raise CommandBridgeError("adapter state operation digest is invalid")
        return digest

    @staticmethod
    def _load(path: Path, *, schema: str) -> dict[str, Any] | None:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        if len(raw) > MAX_REQUEST_BYTES:
            raise CommandBridgeError(f"adapter state exceeds {MAX_REQUEST_BYTES} bytes")
        try:
            document = json.loads(raw, parse_constant=_reject_constant)
        except json.JSONDecodeError as exc:
            raise CommandBridgeError(f"adapter state is invalid JSON: {path}") from exc
        if not isinstance(document, dict) or document.get("schema") != schema:
            raise CommandBridgeError(f"adapter state has an invalid schema: {path}")
        return document


class ReminderCommandBridge:
    def __init__(
        self,
        backend: ReminderBackend,
        *,
        state_root: Path = DEFAULT_STATE_ROOT,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.backend = backend
        self.store = AdapterStateStore(state_root)
        self.clock = clock
        self.adapter = ReminderUpsertAdapter(
            backend,
            lock_path=self.store.root / "eventkit.lock",
            clock=clock,
        )

    def dispatch(self, operation: str, document: Mapping[str, Any]) -> dict[str, Any]:
        proposal = _mapping(document.get("proposal"), field="proposal")
        if operation == "execute":
            return self.execute(
                proposal,
                operation_id=_operation_id(document.get("operation_id")),
            )
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
        raise ValidationError(f"unsupported command adapter operation: {operation!r}")

    def execute(
        self, proposal: Mapping[str, Any], *, operation_id: str
    ) -> dict[str, Any]:
        request = proposal_to_request(proposal, operation_id)
        operation_digest = _operation_digest(operation_id)
        proposal_digest = _proposal_digest(proposal)
        reference = f"{REFERENCE_PREFIX}{operation_digest}"
        with self.store.mutation_lock():
            existing = self.store.load_operation_id(operation_id)
            if existing is not None:
                self._validate_binding(existing, proposal_digest, request)
                return self._execute_response(existing, replayed=True)

            state: dict[str, Any] | None = None

            def persist_verified(receipt: Mapping[str, Any]) -> None:
                nonlocal state
                if (
                    receipt.get("schema") != RECEIPT_SCHEMA
                    or receipt.get("effect_verified") is not True
                    or receipt.get("status") != "created"
                ):
                    raise VerificationError(
                        "unbound canary execute did not return a verified create receipt"
                    )
                state = {
                    "schema": STATE_SCHEMA,
                    "created_at": self.clock(),
                    "operation_id": operation_id,
                    "operation_digest": operation_digest,
                    "proposal_digest": proposal_digest,
                    "request": request.public_dict(),
                    "reference": reference,
                    "execute_receipt": dict(receipt),
                    "latest_undo_reference": None,
                }
                self.store.save_operation(state)

            self.adapter.create_unbound(
                request,
                persist_verified=persist_verified,
            )
            if state is None:
                raise VerificationError("verified create receipt was not persisted")
            return self._execute_response(state, replayed=False)

    def readback(self, proposal: Mapping[str, Any], *, reference: Any) -> dict[str, Any]:
        state = self.store.load_reference(reference)
        self._validate_proposal(state, proposal)
        receipt = _mapping(state.get("execute_receipt"), field="execute_receipt")
        expected = ReminderRecord.from_mapping(
            _mapping(receipt.get("after"), field="execute_receipt.after")
        )
        current = self.backend.get(expected.identifier)
        if current is None:
            raise VerificationError("executed reminder is missing during independent readback")
        if current.fingerprint() != expected.fingerprint():
            raise VerificationError("independent readback does not match the execute receipt")
        return {
            "ok": True,
            "observed": {
                "status": "present",
                "reference": state["reference"],
                "reminder": current.to_dict(),
                "fingerprint": current.fingerprint(),
            },
        }

    def undo(
        self,
        proposal: Mapping[str, Any],
        *,
        reference: Any,
        operation_id: str,
    ) -> dict[str, Any]:
        _parse_reference(reference, prefix=REFERENCE_PREFIX)
        undo_digest = _operation_digest(operation_id)
        proposal_digest = _proposal_digest(proposal)
        undo_reference = f"{UNDO_REFERENCE_PREFIX}{undo_digest}"
        with self.store.mutation_lock():
            original = self.store.load_reference(reference)
            self._validate_proposal(original, proposal)
            existing = self.store._load(
                self.store.undo_path(undo_digest), schema=UNDO_STATE_SCHEMA
            )
            if existing is not None:
                if (
                    existing.get("operation_id") != operation_id
                    or existing.get("original_reference") != reference
                    or existing.get("proposal_digest") != proposal_digest
                ):
                    raise ConflictError("undo operation_id is bound to a different request")
                self._link_undo(original, undo_reference)
                return self._undo_response(existing, replayed=True)

            execute_receipt = _mapping(
                original.get("execute_receipt"), field="execute_receipt"
            )
            undo_receipt = self.adapter.undo(
                execute_receipt,
                approval_ref=f"duffields-undo-operation:{undo_digest}",
            )
            if (
                undo_receipt.get("schema") != UNDO_RECEIPT_SCHEMA
                or undo_receipt.get("effect_verified") is not True
            ):
                raise VerificationError("undo did not return a verified adapter receipt")
            state = {
                "schema": UNDO_STATE_SCHEMA,
                "created_at": self.clock(),
                "operation_id": operation_id,
                "operation_digest": undo_digest,
                "proposal_digest": proposal_digest,
                "reference": undo_reference,
                "original_reference": reference,
                "undo_receipt": undo_receipt,
            }
            self.store.save_undo(state)
            self._link_undo(original, undo_reference)
            return self._undo_response(state, replayed=False)

    def readback_undo(
        self, proposal: Mapping[str, Any], *, reference: Any
    ) -> dict[str, Any]:
        original = self.store.load_reference(reference)
        self._validate_proposal(original, proposal)
        undo_reference = original.get("latest_undo_reference")
        if not undo_reference:
            raise VerificationError("no verified undo receipt is linked to this operation")
        undo_state = self.store.load_undo_reference(undo_reference)
        if undo_state.get("original_reference") != reference:
            raise ConflictError("linked undo receipt belongs to a different operation")
        undo_receipt = _mapping(undo_state.get("undo_receipt"), field="undo_receipt")
        if undo_receipt.get("effect_verified") is not True:
            raise VerificationError("linked undo receipt is not verified")

        execute_receipt = _mapping(
            original.get("execute_receipt"), field="execute_receipt"
        )
        undo_plan = execute_receipt.get("undo")
        if not isinstance(undo_plan, Mapping):
            expected = ReminderRecord.from_mapping(
                _mapping(execute_receipt.get("after"), field="execute_receipt.after")
            )
            current = self.backend.get(expected.identifier)
            if current is None or current.fingerprint() != expected.fingerprint():
                raise VerificationError("no-op undo readback no longer matches execute state")
            observed = {"status": "unchanged", "reminder": current.to_dict()}
        elif undo_plan.get("operation") == "delete_created":
            identifier = str(undo_plan.get("reminder_id") or "")
            if self.backend.get(identifier) is not None:
                raise VerificationError("created reminder remains present after undo")
            observed = {"status": "absent", "reminder_id": identifier}
        elif undo_plan.get("operation") == "restore_updated":
            expected = ReminderRecord.from_mapping(
                _mapping(execute_receipt.get("before"), field="execute_receipt.before")
            )
            current = self.backend.get(expected.identifier)
            expected_value = ReminderValue(
                list_name=expected.list_name,
                title=expected.title,
                notes=expected.notes,
                due_at=expected.due_at,
                priority=expected.priority,
                completed=expected.completed,
            )
            if current is None or not _same_value(current, expected_value):
                raise VerificationError("updated reminder was not exactly restored after undo")
            observed = {"status": "restored", "reminder": current.to_dict()}
        else:
            raise ValidationError("execute receipt contains an unsupported undo operation")
        return {
            "ok": True,
            "observed": {
                **observed,
                "reference": reference,
                "undo_reference": undo_reference,
            },
        }

    def _validate_binding(
        self,
        state: Mapping[str, Any],
        proposal_digest: str,
        request: ReminderUpsertRequest,
    ) -> None:
        if state.get("proposal_digest") != proposal_digest:
            raise ConflictError("operation_id is bound to a different Duffields proposal")
        if state.get("request") != request.public_dict():
            raise ConflictError("operation_id is bound to a different reminder request")

    @staticmethod
    def _validate_proposal(state: Mapping[str, Any], proposal: Mapping[str, Any]) -> None:
        if state.get("proposal_digest") != _proposal_digest(proposal):
            raise ConflictError("reference is bound to a different Duffields proposal")

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
        if (
            receipt.get("schema") != RECEIPT_SCHEMA
            or receipt.get("effect_verified") is not True
            or receipt.get("status") != "created"
        ):
            raise CommandBridgeError("persisted execute receipt is not a verified create")
        return {
            "ok": True,
            "reference": state["reference"],
            "details": {
                "status": receipt.get("status"),
                "effect_verified": receipt.get("effect_verified"),
                "receipt_sha256": _digest(receipt),
                "replayed": replayed,
            },
        }

    @staticmethod
    def _undo_response(state: Mapping[str, Any], *, replayed: bool) -> dict[str, Any]:
        receipt = _mapping(state.get("undo_receipt"), field="undo_receipt")
        if (
            receipt.get("schema") != UNDO_RECEIPT_SCHEMA
            or receipt.get("effect_verified") is not True
        ):
            raise CommandBridgeError("persisted undo receipt is not verified")
        return {
            "ok": True,
            "reference": state["reference"],
            "details": {
                "status": receipt.get("status"),
                "effect_verified": receipt.get("effect_verified"),
                "receipt_sha256": _digest(receipt),
                "original_reference": state.get("original_reference"),
                "replayed": replayed,
            },
        }


def _read_request(path: Path | None) -> dict[str, Any]:
    if path is None:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    else:
        if not path.is_absolute():
            raise ValidationError("--request must be an absolute path")
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValidationError("--request must be a regular file owned by this user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ValidationError("--request must not grant group or other permissions")
        if info.st_size > MAX_REQUEST_BYTES:
            raise ValidationError("request exceeds 128 KiB")
        raw = path.read_bytes()
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValidationError("request exceeds 128 KiB")
    try:
        document = json.loads(raw, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ValidationError("request is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ValidationError("request must be one JSON object")
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reminder-command-adapter")
    parser.add_argument(
        "operation", choices=("execute", "readback", "undo", "readback-undo")
    )
    parser.add_argument("--request", type=Path)
    parser.add_argument("--response", type=Path)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--swift-helper", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    response_path: Path | None = args.response
    try:
        if response_path is not None and not response_path.is_absolute():
            raise ValidationError("--response must be an absolute path")
        document = _read_request(args.request)
        mutating = args.operation in {"execute", "undo"}
        if mutating and os.environ.get(LIVE_ENV) != LIVE_CONFIRMATION:
            raise ValidationError(
                f"{args.operation} requires {LIVE_ENV}={LIVE_CONFIRMATION!r}"
            )
        root_value = args.state_root or Path(
            os.environ.get(STATE_ROOT_ENV, str(DEFAULT_STATE_ROOT))
        )
        backend = SwiftEventKitBackend(
            args.swift_helper,
            allow_mutation=mutating,
        )
        result = ReminderCommandBridge(backend, state_root=root_value).dispatch(
            args.operation, document
        )
        exit_code = 0
    except (OSError, ValueError, ReminderUpsertError, json.JSONDecodeError) as exc:
        result = {
            "ok": False,
            "error": type(exc).__name__,
            "detail": str(exc),
        }
        exit_code = 2

    if response_path is not None:
        try:
            _write_json_atomic(response_path, result)
        except OSError as exc:
            print(
                _canonical_json(
                    {"ok": False, "error": type(exc).__name__, "detail": str(exc)}
                ),
                file=sys.stderr,
            )
            return 2
    else:
        stream = sys.stdout if exit_code == 0 else sys.stderr
        print(_canonical_json(result), file=stream)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
