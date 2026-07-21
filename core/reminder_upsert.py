"""Idempotent, receipt-backed Apple Reminders canary adapter.

The library is backend-neutral so its reconciliation and undo behavior can be
tested without touching Reminders.  The CLI is plan-only unless every live
write guard is supplied explicitly.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence


SCHEMA = "reminder-upsert-request/v1"
RECEIPT_SCHEMA = "reminder-upsert-receipt/v1"
UNDO_RECEIPT_SCHEMA = "reminder-upsert-undo-receipt/v1"
CANARY_LISTS = frozenset({"Claude"})
LIVE_CONFIRMATION = "I_UNDERSTAND_THIS_WRITES_APPLE_REMINDERS"
MARKER_PREFIX = "[reminder-upsert:"
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_PRIORITY_TO_EVENTKIT = {"none": 0, "low": 9, "medium": 5, "high": 1}


class ReminderUpsertError(RuntimeError):
    """Base error for a rejected or unverified reminder operation."""


class ValidationError(ReminderUpsertError):
    pass


class ConflictError(ReminderUpsertError):
    pass


class VerificationError(ReminderUpsertError):
    pass


class BackendError(ReminderUpsertError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_due(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("due_at must be an ISO 8601 string or null")
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"due_at must be ISO 8601: {text!r}") from exc
    if parsed.tzinfo is None:
        raise ValidationError("due_at requires an explicit timezone offset")
    return (
        parsed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _clean_text(value: Any, *, field: str, required: bool, limit: int) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ValidationError(f"{field} must be a string")
    if required and not text:
        raise ValidationError(f"{field} is required")
    if len(text) > limit:
        raise ValidationError(f"{field} exceeds {limit} characters")
    return text


def marker_for(key: str) -> str:
    if not _KEY_RE.fullmatch(key):
        raise ValidationError(
            "idempotency_key must be 1-128 safe identifier characters"
        )
    return f"{MARKER_PREFIX}{key}]"


@dataclass(frozen=True)
class ReminderValue:
    list_name: str
    title: str
    notes: str
    due_at: str | None
    priority: int
    completed: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ReminderValue":
        list_name = raw.get("list", raw.get("list_name"))
        title = raw.get("title")
        notes = raw.get("notes")
        priority = raw.get("priority", 0)
        completed = raw.get("completed", False)
        if not isinstance(list_name, str):
            raise BackendError("backend reminder list must be a string")
        if not isinstance(title, str) or not isinstance(notes, str):
            raise BackendError("backend reminder title and notes must be strings")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise BackendError("backend reminder priority must be an integer")
        if not isinstance(completed, bool):
            raise BackendError("backend reminder completed flag must be boolean")
        return cls(
            list_name=list_name,
            title=title,
            notes=notes,
            due_at=_canonical_due(raw.get("due_at")),
            priority=priority,
            completed=completed,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "list": self.list_name,
            "title": self.title,
            "notes": self.notes,
            "due_at": self.due_at,
            "priority": self.priority,
            "completed": self.completed,
        }


@dataclass(frozen=True)
class ReminderRecord(ReminderValue):
    identifier: str = ""
    last_modified_at: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ReminderRecord":
        value = ReminderValue.from_mapping(raw)
        identifier = raw.get("identifier", raw.get("reminder_id"))
        if not isinstance(identifier, str) or not identifier.strip():
            raise BackendError("backend reminder record has no identifier")
        identifier = identifier.strip()
        last_modified = raw.get("last_modified_at")
        if last_modified is not None and not isinstance(last_modified, str):
            raise BackendError("backend reminder last_modified_at must be a string or null")
        return cls(
            **value.__dict__,
            identifier=identifier,
            last_modified_at=last_modified or None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            **super().to_dict(),
            "last_modified_at": self.last_modified_at,
        }

    def fingerprint(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True)
class ReminderUpsertRequest:
    idempotency_key: str
    list_name: str
    title: str
    notes: str
    due_at: str | None
    priority: int
    approval_ref: str | None
    source_ref: str | None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ReminderUpsertRequest":
        if raw.get("schema") != SCHEMA:
            raise ValidationError(f"request schema must be {SCHEMA!r}")
        key = _clean_text(
            raw.get("idempotency_key"), field="idempotency_key", required=True, limit=128
        )
        marker_for(key)
        list_name = _clean_text(raw.get("list"), field="list", required=True, limit=128)
        title = _clean_text(raw.get("title"), field="title", required=True, limit=500)
        notes = _clean_text(raw.get("notes"), field="notes", required=False, limit=8_000)
        if MARKER_PREFIX in notes:
            raise ValidationError("notes contain the reserved reminder-upsert marker prefix")
        priority_raw = raw.get("priority", "none")
        if not isinstance(priority_raw, str):
            raise ValidationError("priority must be a string")
        priority_name = priority_raw.strip().lower()
        if priority_name not in _PRIORITY_TO_EVENTKIT:
            raise ValidationError("priority must be none, low, medium, or high")
        approval_ref = _clean_text(
            raw.get("approval_ref"), field="approval_ref", required=False, limit=500
        ) or None
        source_ref = _clean_text(
            raw.get("source_ref"), field="source_ref", required=False, limit=1_000
        ) or None
        return cls(
            idempotency_key=key,
            list_name=list_name,
            title=title,
            notes=notes,
            due_at=_canonical_due(raw.get("due_at")),
            priority=_PRIORITY_TO_EVENTKIT[priority_name],
            approval_ref=approval_ref,
            source_ref=source_ref,
        )

    @property
    def marker(self) -> str:
        return marker_for(self.idempotency_key)

    def desired(self) -> ReminderValue:
        stored_notes = f"{self.notes}\n\n{self.marker}" if self.notes else self.marker
        return ReminderValue(
            list_name=self.list_name,
            title=self.title,
            notes=stored_notes,
            due_at=self.due_at,
            priority=self.priority,
            completed=False,
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "idempotency_key": self.idempotency_key,
            "list": self.list_name,
            "title": self.title,
            "notes": self.notes,
            "due_at": self.due_at,
            "priority": next(
                name for name, value in _PRIORITY_TO_EVENTKIT.items() if value == self.priority
            ),
            "approval_ref": self.approval_ref,
            "source_ref": self.source_ref,
        }


class ReminderBackend(Protocol):
    def find_by_marker(self, marker: str) -> list[ReminderRecord]: ...

    def get(self, identifier: str) -> ReminderRecord | None: ...

    def create(self, value: ReminderValue, *, marker: str) -> ReminderRecord: ...

    def update(
        self,
        identifier: str,
        value: ReminderValue,
        *,
        expected: ReminderRecord,
        marker: str,
    ) -> ReminderRecord: ...

    def delete(self, identifier: str, *, expected: ReminderRecord, marker: str) -> None: ...


class SwiftEventKitBackend:
    """EventKit backend implemented by the adjacent fail-closed Swift helper."""

    def __init__(
        self,
        script_path: Path | None = None,
        *,
        allow_mutation: bool = False,
        timeout: int = 45,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.script_path = script_path or Path(__file__).with_name("reminder_eventkit.swift")
        self.allow_mutation = allow_mutation
        self.timeout = timeout
        self.runner = runner

    def _call(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        env = os.environ.copy()
        env.pop("REMINDER_UPSERT_LIVE", None)
        if self.allow_mutation:
            env["REMINDER_UPSERT_LIVE"] = "1"
        try:
            proc = self.runner(
                ["/usr/bin/swift", str(self.script_path), operation],
                input=_canonical_json(payload),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"EventKit {operation} timed out") from exc
        except OSError as exc:
            raise BackendError(f"EventKit {operation} could not start: {exc}") from exc
        if len(proc.stdout.encode("utf-8")) > 256 * 1024:
            raise BackendError(f"EventKit {operation} response exceeded 256 KiB")
        try:
            response = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except json.JSONDecodeError as exc:
            raise BackendError(
                f"EventKit helper returned invalid JSON (rc={proc.returncode})"
            ) from exc
        if not isinstance(response, dict):
            raise BackendError(f"EventKit {operation} response must be a JSON object")
        if proc.returncode != 0 or response.get("ok") is not True:
            detail = response.get("error") or proc.stderr.strip() or "unknown EventKit error"
            if isinstance(detail, Mapping):
                detail = detail.get("message") or _canonical_json(detail)
            raise BackendError(f"EventKit {operation} failed: {detail}")
        return response

    def find_by_marker(self, marker: str) -> list[ReminderRecord]:
        response = self._call("find", {"marker": marker})
        records = response.get("records")
        if not isinstance(records, list) or not all(
            isinstance(item, Mapping) for item in records
        ):
            raise BackendError("EventKit find response has invalid records")
        return [ReminderRecord.from_mapping(item) for item in records]

    def get(self, identifier: str) -> ReminderRecord | None:
        response = self._call("get", {"identifier": identifier})
        raw = response.get("record")
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise BackendError("EventKit get response has an invalid reminder record")
        return ReminderRecord.from_mapping(raw)

    def create(self, value: ReminderValue, *, marker: str) -> ReminderRecord:
        response = self._call("create", {"value": value.to_dict(), "marker": marker})
        return self._required_record(response, operation="create")

    def update(
        self,
        identifier: str,
        value: ReminderValue,
        *,
        expected: ReminderRecord,
        marker: str,
    ) -> ReminderRecord:
        response = self._call(
            "update",
            {
                "identifier": identifier,
                "value": value.to_dict(),
                "expected": expected.to_dict(),
                "marker": marker,
            },
        )
        return self._required_record(response, operation="update")

    def delete(self, identifier: str, *, expected: ReminderRecord, marker: str) -> None:
        self._call(
            "delete",
            {"identifier": identifier, "expected": expected.to_dict(), "marker": marker},
        )

    @staticmethod
    def _required_record(
        response: Mapping[str, Any], *, operation: str
    ) -> ReminderRecord:
        record = response.get("record")
        if not isinstance(record, Mapping):
            raise BackendError(f"EventKit {operation} response has no reminder record")
        return ReminderRecord.from_mapping(record)


def _same_value(record: ReminderRecord, value: ReminderValue) -> bool:
    return ReminderValue(
        list_name=record.list_name,
        title=record.title,
        notes=record.notes,
        due_at=record.due_at,
        priority=record.priority,
        completed=record.completed,
    ) == value


def _verified_readback(
    backend: ReminderBackend,
    identifier: str,
    expected: ReminderValue,
) -> ReminderRecord:
    readback = backend.get(identifier)
    if readback is None:
        raise VerificationError(f"reminder {identifier!r} was not readable after write")
    if readback.identifier != identifier:
        raise VerificationError("reminder readback returned a different identifier")
    if not _same_value(readback, expected):
        raise VerificationError(
            "reminder readback did not exactly match requested title/list/notes/due/priority"
        )
    return readback


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        path.chmod(0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ReminderUpsertAdapter:
    def __init__(
        self,
        backend: ReminderBackend,
        *,
        allowed_lists: Sequence[str] = tuple(CANARY_LISTS),
        lock_path: Path | None = None,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.backend = backend
        self.allowed_lists = frozenset(allowed_lists)
        self.lock_path = lock_path or (
            Path.home() / ".local/state/agent-core/reminder-upsert.lock"
        )
        self.clock = clock

    def _validate_live(self, request: ReminderUpsertRequest) -> None:
        if request.list_name not in self.allowed_lists:
            raise ValidationError(
                f"canary may write only to {sorted(self.allowed_lists)}; got {request.list_name!r}"
            )
        if not request.approval_ref:
            raise ValidationError("approval_ref is required for a live reminder write")

    def plan(self, request: ReminderUpsertRequest) -> dict[str, Any]:
        if request.list_name not in self.allowed_lists:
            raise ValidationError(
                f"canary may write only to {sorted(self.allowed_lists)}; got {request.list_name!r}"
            )
        return {
            "schema": RECEIPT_SCHEMA,
            "generated_at": self.clock(),
            "operation": "upsert",
            "status": "planned",
            "effect_verified": False,
            "request": request.public_dict(),
            "side_effect_ref": None,
            "before": None,
            "after": request.desired().to_dict(),
            "after_fingerprint": None,
            "undo": None,
            "detail": "plan only; EventKit backend was not opened",
        }

    def upsert(self, request: ReminderUpsertRequest) -> dict[str, Any]:
        self._validate_live(request)
        with _exclusive_lock(self.lock_path):
            return self._upsert_locked(request)

    def create_unbound(
        self,
        request: ReminderUpsertRequest,
        *,
        persist_verified: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Create only when no marker exists, persisting before releasing the lock.

        This is the crash-safe initial-canary primitive. A marker without a
        durable operation binding is uncertain state and must be reconciled;
        it is never treated as an idempotent replay or an update candidate.
        """

        self._validate_live(request)
        with _exclusive_lock(self.lock_path):
            matches = self.backend.find_by_marker(request.marker)
            if matches:
                raise ConflictError(
                    "unbound idempotency marker already exists; reconciliation required"
                )
            desired = request.desired()
            created = self.backend.create(desired, marker=request.marker)
            after = _verified_readback(self.backend, created.identifier, desired)
            undo = {
                "schema": "reminder-upsert-undo/v1",
                "operation": "delete_created",
                "reminder_id": after.identifier,
                "marker": request.marker,
                "expected_after_fingerprint": after.fingerprint(),
                "restore": None,
            }
            receipt = self._receipt(
                request,
                status="created",
                before=None,
                after=after,
                undo=undo,
            )
            if persist_verified is not None:
                persist_verified(receipt)
            return receipt

    def _upsert_locked(self, request: ReminderUpsertRequest) -> dict[str, Any]:
        desired = request.desired()
        matches = self.backend.find_by_marker(request.marker)
        if len(matches) > 1:
            raise ConflictError(
                f"idempotency marker matched {len(matches)} reminders; refusing an ambiguous write"
            )

        before = matches[0] if matches else None
        if before is not None and before.completed:
            return self._receipt(
                request,
                status="completed",
                before=before,
                after=before,
                undo=None,
                detail="existing reminder is completed; no resurrection or update performed",
            )

        if before is None:
            created = self.backend.create(desired, marker=request.marker)
            after = _verified_readback(self.backend, created.identifier, desired)
            undo = {
                "schema": "reminder-upsert-undo/v1",
                "operation": "delete_created",
                "reminder_id": after.identifier,
                "marker": request.marker,
                "expected_after_fingerprint": after.fingerprint(),
                "restore": None,
            }
            return self._receipt(
                request,
                status="created",
                before=None,
                after=after,
                undo=undo,
            )

        if _same_value(before, desired):
            readback = _verified_readback(self.backend, before.identifier, desired)
            return self._receipt(
                request,
                status="unchanged",
                before=before,
                after=readback,
                undo=None,
                detail="idempotent replay; no mutation performed",
            )

        updated = self.backend.update(
            before.identifier,
            desired,
            expected=before,
            marker=request.marker,
        )
        after = _verified_readback(self.backend, updated.identifier, desired)
        undo = {
            "schema": "reminder-upsert-undo/v1",
            "operation": "restore_updated",
            "reminder_id": after.identifier,
            "marker": request.marker,
            "expected_after_fingerprint": after.fingerprint(),
            "restore": before.to_dict(),
        }
        return self._receipt(
            request,
            status="updated",
            before=before,
            after=after,
            undo=undo,
        )

    def _receipt(
        self,
        request: ReminderUpsertRequest,
        *,
        status: str,
        before: ReminderRecord | None,
        after: ReminderRecord,
        undo: Mapping[str, Any] | None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema": RECEIPT_SCHEMA,
            "generated_at": self.clock(),
            "operation": "upsert",
            "status": status,
            "effect_verified": True,
            "request": request.public_dict(),
            "side_effect_ref": f"eventkit-reminder:{after.identifier}",
            "before": before.to_dict() if before else None,
            "after": after.to_dict(),
            "after_fingerprint": after.fingerprint(),
            "undo": dict(undo) if undo else None,
            "detail": detail,
        }

    def undo(self, receipt: Mapping[str, Any], *, approval_ref: str) -> dict[str, Any]:
        if receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("effect_verified") is not True:
            raise ValidationError("undo requires a verified reminder-upsert receipt")
        approval_ref = _clean_text(
            approval_ref, field="approval_ref", required=True, limit=500
        )
        undo = receipt.get("undo")
        if not isinstance(undo, Mapping):
            return self._undo_receipt(
                receipt,
                approval_ref=approval_ref,
                status="nothing_to_undo",
                effect_verified=True,
                reminder=None,
            )
        with _exclusive_lock(self.lock_path):
            return self._undo_locked(receipt, undo, approval_ref=approval_ref)

    def _undo_locked(
        self,
        receipt: Mapping[str, Any],
        undo: Mapping[str, Any],
        *,
        approval_ref: str,
    ) -> dict[str, Any]:
        identifier = str(undo.get("reminder_id") or "")
        marker = str(undo.get("marker") or "")
        expected_fingerprint = str(undo.get("expected_after_fingerprint") or "")
        operation = str(undo.get("operation") or "")
        if not identifier or not marker or not expected_fingerprint:
            raise ValidationError("undo payload is incomplete")

        current = self.backend.get(identifier)
        restore_raw = undo.get("restore")
        if operation == "delete_created":
            if current is None:
                return self._undo_receipt(
                    receipt,
                    approval_ref=approval_ref,
                    status="already_undone",
                    effect_verified=True,
                    reminder=None,
                )
            if current.fingerprint() != expected_fingerprint:
                raise ConflictError("reminder changed after create; refusing to delete John's edits")
            self.backend.delete(identifier, expected=current, marker=marker)
            if self.backend.get(identifier) is not None:
                raise VerificationError("reminder remained readable after undo delete")
            return self._undo_receipt(
                receipt,
                approval_ref=approval_ref,
                status="deleted_created",
                effect_verified=True,
                reminder=None,
            )

        if operation != "restore_updated" or not isinstance(restore_raw, Mapping):
            raise ValidationError(f"unsupported undo operation {operation!r}")
        restore_record = ReminderRecord.from_mapping(restore_raw)
        if current is None:
            raise ConflictError("updated reminder is missing; refusing to recreate it during undo")
        restore_value = ReminderValue(
            list_name=restore_record.list_name,
            title=restore_record.title,
            notes=restore_record.notes,
            due_at=restore_record.due_at,
            priority=restore_record.priority,
            completed=restore_record.completed,
        )
        if _same_value(current, restore_value):
            return self._undo_receipt(
                receipt,
                approval_ref=approval_ref,
                status="already_undone",
                effect_verified=True,
                reminder=current,
            )
        if current.fingerprint() != expected_fingerprint:
            raise ConflictError("reminder changed after update; refusing to overwrite John's edits")
        restored = self.backend.update(
            identifier,
            restore_value,
            expected=current,
            marker=marker,
        )
        readback = _verified_readback(self.backend, restored.identifier, restore_value)
        return self._undo_receipt(
            receipt,
            approval_ref=approval_ref,
            status="restored_updated",
            effect_verified=True,
            reminder=readback,
        )

    def _undo_receipt(
        self,
        original: Mapping[str, Any],
        *,
        approval_ref: str,
        status: str,
        effect_verified: bool,
        reminder: ReminderRecord | None,
    ) -> dict[str, Any]:
        return {
            "schema": UNDO_RECEIPT_SCHEMA,
            "generated_at": self.clock(),
            "operation": "undo",
            "status": status,
            "effect_verified": effect_verified,
            "approval_ref": approval_ref,
            "original_receipt_sha256": _digest(original),
            "reminder": reminder.to_dict() if reminder else None,
        }


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValidationError(f"{path} must contain one JSON object")
    return data


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(0o600)
        os.replace(temp_path, path)
        temp_path = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def _planned_undo(receipt: Mapping[str, Any], *, clock: Callable[[], str]) -> dict[str, Any]:
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise ValidationError("not a reminder-upsert receipt")
    return {
        "schema": UNDO_RECEIPT_SCHEMA,
        "generated_at": clock(),
        "operation": "undo",
        "status": "planned",
        "effect_verified": False,
        "original_receipt_sha256": _digest(receipt),
        "undo": receipt.get("undo"),
        "detail": "plan only; EventKit backend was not opened",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m core.reminder_upsert")
    sub = parser.add_subparsers(dest="command", required=True)

    upsert = sub.add_parser("upsert", help="plan or apply one idempotent reminder upsert")
    upsert.add_argument("--request", type=Path, required=True)
    upsert.add_argument("--receipt", type=Path)
    upsert.add_argument("--apply", action="store_true")
    upsert.add_argument("--live-confirm")
    upsert.add_argument("--lock-path", type=Path)
    upsert.add_argument("--swift-helper", type=Path)

    undo = sub.add_parser("undo", help="plan or apply the guarded undo in a receipt")
    undo.add_argument("--receipt", type=Path, required=True)
    undo.add_argument("--undo-receipt", type=Path)
    undo.add_argument("--approval-ref")
    undo.add_argument("--apply", action="store_true")
    undo.add_argument("--live-confirm")
    undo.add_argument("--lock-path", type=Path)
    undo.add_argument("--swift-helper", type=Path)
    return parser


def _require_live_cli(args: argparse.Namespace, *, output: Path | None) -> None:
    if args.live_confirm != LIVE_CONFIRMATION:
        raise ValidationError(f"--live-confirm must equal {LIVE_CONFIRMATION!r}")
    if output is None:
        raise ValidationError("live operations require a durable receipt output path")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "upsert":
            request = ReminderUpsertRequest.from_mapping(_read_json(args.request))
            if not args.apply:
                result = ReminderUpsertAdapter(
                    backend=None,  # type: ignore[arg-type]
                    lock_path=args.lock_path,
                ).plan(request)
            else:
                _require_live_cli(args, output=args.receipt)
                backend = SwiftEventKitBackend(args.swift_helper, allow_mutation=True)
                result = ReminderUpsertAdapter(backend, lock_path=args.lock_path).upsert(request)
            output = args.receipt
        else:
            original = _read_json(args.receipt)
            if not args.apply:
                result = _planned_undo(original, clock=_utc_now)
            else:
                _require_live_cli(args, output=args.undo_receipt)
                backend = SwiftEventKitBackend(args.swift_helper, allow_mutation=True)
                result = ReminderUpsertAdapter(backend, lock_path=args.lock_path).undo(
                    original,
                    approval_ref=args.approval_ref or "",
                )
            output = args.undo_receipt
        if output is not None:
            _write_json_atomic(output, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, ReminderUpsertError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__, "detail": str(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
