"""Exact, receipt-backed Apple Reminders completion adapter for Warboard."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .reminder_upsert import (
    BackendError,
    ConflictError,
    ReminderUpsertError,
    ValidationError,
    VerificationError,
    _canonical_json,
    _digest,
    _exclusive_lock,
    _utc_now,
    _write_json_atomic,
)


STATE_SCHEMA = "reminder-complete-command-state/v1"
UNDO_STATE_SCHEMA = "reminder-complete-command-undo-state/v1"
REFERENCE_PREFIX = "reminder-complete-operation:"
UNDO_REFERENCE_PREFIX = "reminder-complete-undo-operation:"
LIVE_ENV = "REMINDER_COMPLETE_COMMAND_ADAPTER_LIVE"
LIVE_CONFIRMATION = "I_UNDERSTAND_THIS_COMPLETES_APPLE_REMINDERS"
STATE_ROOT_ENV = "REMINDER_COMPLETE_COMMAND_ADAPTER_STATE_ROOT"
DEFAULT_STATE_ROOT = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Duffields"
    / "reminder-complete-adapter"
)
MAX_REQUEST_BYTES = 128 * 1024
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PAYLOAD_KEYS = frozenset(
    {
        "requested_verb",
        "list_id",
        "list",
        "native_reminder_id",
        "title",
        "lastModifiedDate",
        "observed_completion_state",
        "observed_due_state",
        "observed_due_at",
        "recurrence_fingerprint",
    }
)


class ReminderCompleteError(ReminderUpsertError):
    """Base error for the exact reminder-completion bridge."""


def _mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field} must be a JSON object")
    return value


def _clean(value: Any, *, field: str, limit: int = 240) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    rendered = value.strip()
    if not rendered or len(rendered) > limit:
        raise ValidationError(f"{field} must contain 1-{limit} characters")
    return rendered


def _bounded_text(value: Any, *, field: str, limit: int = 240) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValidationError(f"{field} must be a string of at most {limit} characters")
    return value


def _operation_id(value: Any) -> str:
    rendered = _clean(value, field="operation_id", limit=160)
    if not _OPERATION_ID_RE.fullmatch(rendered):
        raise ValidationError("operation_id must use safe identifier characters")
    return rendered


def _operation_digest(operation_id: str) -> str:
    return _digest({"operation_id": operation_id})


def _proposal_digest(proposal: Mapping[str, Any]) -> str:
    return _digest(dict(proposal))


def _parse_reference(reference: Any, *, prefix: str) -> str:
    rendered = _clean(reference, field="reference", limit=256)
    if not rendered.startswith(prefix):
        raise ValidationError(f"reference must start with {prefix!r}")
    digest = rendered[len(prefix) :]
    if not _DIGEST_RE.fullmatch(digest):
        raise ValidationError("reference has an invalid operation digest")
    return digest


def _exact(value: Mapping[str, Any], expected: frozenset[str], *, field: str) -> None:
    unexpected = set(value) - expected
    missing = expected - set(value)
    if missing or unexpected:
        raise ValidationError(
            f"{field} fields do not match the closed contract"
        )


@dataclass(frozen=True)
class ReminderConflict:
    list_id: str
    list_name: str
    native_reminder_id: str
    title: str
    last_modified_date: str
    observed_completion_state: str
    observed_due_state: str
    observed_due_at: str
    recurrence_fingerprint: str

    @classmethod
    def from_proposal(cls, proposal: Mapping[str, Any]) -> "ReminderConflict":
        if proposal.get("proposal_version") != "duffields-action-proposal/v1":
            raise ValidationError("unsupported Duffields proposal_version")
        action = _mapping(proposal.get("action"), field="proposal.action")
        if action.get("kind") != "reminder.complete":
            raise ValidationError("proposal.action.kind must be reminder.complete")
        if action.get("readback_required") is not True:
            raise ValidationError("reminder completion requires independent readback")
        if action.get("undo_supported") is not True:
            raise ValidationError("reminder completion requires guarded undo")
        payload = _mapping(action.get("payload"), field="proposal.action.payload")
        _exact(payload, _PAYLOAD_KEYS, field="proposal.action.payload")
        if payload.get("requested_verb") != "complete":
            raise ValidationError("requested_verb must be complete")
        completion = _clean(
            payload.get("observed_completion_state"),
            field="observed_completion_state",
            limit=16,
        )
        if completion != "incomplete":
            raise ConflictError("only an observed incomplete reminder can be completed")
        due_state = _clean(
            payload.get("observed_due_state"),
            field="observed_due_state",
            limit=16,
        )
        if due_state not in {"dated", "undated"}:
            raise ValidationError("observed_due_state must be dated or undated")
        due_at = _bounded_text(
            payload.get("observed_due_at"),
            field="observed_due_at",
            limit=80,
        )
        if due_state == "dated" and not due_at:
            raise ValidationError("dated reminders require observed_due_at")
        if due_state == "undated" and due_at:
            raise ValidationError("undated reminders cannot have observed_due_at")
        recurrence = _clean(
            payload.get("recurrence_fingerprint"),
            field="recurrence_fingerprint",
        )
        modified = _clean(
            payload.get("lastModifiedDate"),
            field="lastModifiedDate",
            limit=80,
        )
        list_id = _clean(payload.get("list_id"), field="list_id", limit=160)
        list_name = _clean(payload.get("list"), field="list", limit=160)
        native_id = _clean(
            payload.get("native_reminder_id"),
            field="native_reminder_id",
        )
        title = _clean(payload.get("title"), field="title")

        target = _mapping(proposal.get("target"), field="proposal.target")
        if (
            target.get("system") != "apple_reminders"
            or target.get("resource_type") != "reminder"
        ):
            raise ValidationError("proposal target must be one Apple reminder")
        if target.get("scope") not in {list_id, list_name}:
            raise ValidationError("target.scope must match the exact reminder list")
        if target.get("display") != title:
            raise ValidationError("target.display must match the reminder title")
        risk = _mapping(proposal.get("risk"), field="proposal.risk")
        if (
            risk.get("level") != "low"
            or risk.get("reversible") is not True
            or risk.get("external_communication") is not False
        ):
            raise ValidationError(
                "reminder completion permits only low-risk reversible actions"
            )
        return cls(
            list_id=list_id,
            list_name=list_name,
            native_reminder_id=native_id,
            title=title,
            last_modified_date=modified,
            observed_completion_state=completion,
            observed_due_state=due_state,
            observed_due_at=due_at,
            recurrence_fingerprint=recurrence,
        )


@dataclass(frozen=True)
class ReminderSnapshot:
    identifier: str
    external_identifier: str
    list_id: str
    list_name: str
    title: str
    due_at: str | None
    due_state: str
    priority: int
    completed: bool
    completion_date: str | None
    last_modified_date: str
    recurrence_fingerprint: str
    recurrence: tuple[str, ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ReminderSnapshot":
        expected = {
            "identifier",
            "external_identifier",
            "list_id",
            "list",
            "title",
            "due_at",
            "due_state",
            "priority",
            "completed",
            "completion_date",
            "lastModifiedDate",
            "recurrence_fingerprint",
            "recurrence",
        }
        optional = {"due_at", "completion_date"}
        if set(raw) - expected or (expected - optional) - set(raw):
            raise BackendError("EventKit reminder record fields are not closed")
        priority = raw.get("priority")
        completed = raw.get("completed")
        due_at = raw.get("due_at")
        completion_date = raw.get("completion_date")
        recurrence_raw = raw.get("recurrence")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise BackendError("EventKit priority must be an integer")
        if not isinstance(completed, bool):
            raise BackendError("EventKit completed must be boolean")
        if due_at is not None and not isinstance(due_at, str):
            raise BackendError("EventKit due_at must be a string or null")
        if completion_date is not None and not isinstance(completion_date, str):
            raise BackendError("EventKit completion_date must be a string or null")
        if (
            not isinstance(recurrence_raw, list)
            or len(recurrence_raw) > 16
            or any(not isinstance(item, Mapping) for item in recurrence_raw)
        ):
            raise BackendError("EventKit recurrence must be a bounded list of objects")
        recurrence = tuple(_canonical_json(dict(item)) for item in recurrence_raw)
        if sum(len(item) for item in recurrence) > 32 * 1024:
            raise BackendError("EventKit recurrence exceeded 32 KiB")
        due_state = _clean(raw.get("due_state"), field="record.due_state", limit=16)
        if due_state not in {"dated", "undated"}:
            raise BackendError("EventKit due_state is invalid")
        recurrence_fingerprint = _clean(
            raw.get("recurrence_fingerprint"),
            field="record.recurrence_fingerprint",
        )
        expected_recurrence_fingerprint = (
            "none"
            if not recurrence
            else "sha256:"
            + _digest([json.loads(item) for item in recurrence])
        )
        if recurrence_fingerprint != expected_recurrence_fingerprint:
            raise BackendError("EventKit recurrence fingerprint does not match its rules")
        return cls(
            identifier=_clean(raw.get("identifier"), field="record.identifier"),
            external_identifier=_bounded_text(
                raw.get("external_identifier"),
                field="record.external_identifier",
            ),
            list_id=_clean(raw.get("list_id"), field="record.list_id", limit=160),
            list_name=_clean(raw.get("list"), field="record.list", limit=160),
            title=_clean(raw.get("title"), field="record.title"),
            due_at=due_at,
            due_state=due_state,
            priority=priority,
            completed=completed,
            completion_date=completion_date,
            last_modified_date=_clean(
                raw.get("lastModifiedDate"),
                field="record.lastModifiedDate",
                limit=80,
            ),
            recurrence_fingerprint=recurrence_fingerprint,
            recurrence=recurrence,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "external_identifier": self.external_identifier,
            "list_id": self.list_id,
            "list": self.list_name,
            "title": self.title,
            "due_at": self.due_at,
            "due_state": self.due_state,
            "priority": self.priority,
            "completed": self.completed,
            "completion_date": self.completion_date,
            "lastModifiedDate": self.last_modified_date,
            "recurrence_fingerprint": self.recurrence_fingerprint,
            "recurrence": [json.loads(item) for item in self.recurrence],
        }

    def fingerprint(self) -> str:
        return _digest(self.to_dict())

    def matches_conflict(self, expected: ReminderConflict) -> bool:
        return (
            self.identifier == expected.native_reminder_id
            and self.list_id == expected.list_id
            and self.list_name == expected.list_name
            and self.title == expected.title
            and self.last_modified_date == expected.last_modified_date
            and self.completed is False
            and expected.observed_completion_state == "incomplete"
            and self.due_state == expected.observed_due_state
            and (self.due_at or "") == expected.observed_due_at
            and self.recurrence_fingerprint == expected.recurrence_fingerprint
        )


def state_effect_kind(state: Mapping[str, Any]) -> str:
    effect_kind = state.get("effect_kind")
    if effect_kind not in {"completed", "recurrence_advanced"}:
        raise ReminderCompleteError("adapter state has an invalid effect_kind")
    return str(effect_kind)


class ReminderCompleteBackend(Protocol):
    def get(self, identifier: str) -> ReminderSnapshot | None: ...

    def set_completed(
        self,
        identifier: str,
        *,
        expected: ReminderSnapshot,
        completed: bool,
    ) -> tuple[ReminderSnapshot, ReminderSnapshot | None]: ...

    def restore_recurring(
        self,
        identifier: str,
        *,
        expected: ReminderSnapshot,
        original: ReminderSnapshot,
        generated_occurrence: ReminderSnapshot,
    ) -> ReminderSnapshot: ...


class SwiftEventKitCompleteBackend:
    def __init__(
        self,
        script_path: Path | None = None,
        *,
        allow_mutation: bool = False,
        timeout: int = 45,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.script_path = script_path or Path(__file__).with_name(
            "reminder_complete_eventkit.swift"
        )
        self.allow_mutation = allow_mutation
        self.timeout = timeout
        self.runner = runner

    def _call(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        environment = os.environ.copy()
        environment.pop("REMINDER_COMPLETE_LIVE", None)
        if self.allow_mutation:
            environment["REMINDER_COMPLETE_LIVE"] = "1"
        try:
            result = self.runner(
                ["/usr/bin/swift", str(self.script_path), operation],
                input=_canonical_json(payload),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"EventKit {operation} timed out") from exc
        except OSError as exc:
            raise BackendError(f"EventKit {operation} could not start: {exc}") from exc
        if len(result.stdout.encode("utf-8")) > 256 * 1024:
            raise BackendError("EventKit response exceeded 256 KiB")
        try:
            response = json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError as exc:
            raise BackendError("EventKit returned invalid JSON") from exc
        if not isinstance(response, dict):
            raise BackendError("EventKit response must be a JSON object")
        if result.returncode != 0 or response.get("ok") is not True:
            error = response.get("error")
            if isinstance(error, Mapping):
                code = str(error.get("code") or "eventkit_error")
                detail = str(error.get("message") or code)
            else:
                code = "eventkit_error"
                detail = str(error or result.stderr.strip() or code)
            if code in {
                "compare_and_swap_failed",
                "identity_mismatch",
                "not_found",
                "state_conflict",
            }:
                raise ConflictError(detail)
            raise BackendError(detail)
        return response

    def get(self, identifier: str) -> ReminderSnapshot | None:
        response = self._call("get", {"identifier": identifier})
        raw = response.get("record")
        if raw is None:
            return None
        return ReminderSnapshot.from_mapping(_mapping(raw, field="EventKit record"))

    def set_completed(
        self,
        identifier: str,
        *,
        expected: ReminderSnapshot,
        completed: bool,
    ) -> tuple[ReminderSnapshot, ReminderSnapshot | None]:
        response = self._call(
            "set-completed",
            {
                "identifier": identifier,
                "expected": expected.to_dict(),
                "completed": completed,
            },
        )
        before = ReminderSnapshot.from_mapping(
            _mapping(response.get("before"), field="EventKit before record")
        )
        if before.fingerprint() != expected.fingerprint():
            raise ConflictError("EventKit mutation did not bind the expected source version")
        after = ReminderSnapshot.from_mapping(
            _mapping(response.get("record"), field="EventKit after record")
        )
        generated_raw = response.get("generated_occurrence")
        generated = (
            None
            if generated_raw is None
            else ReminderSnapshot.from_mapping(
                _mapping(
                    generated_raw,
                    field="EventKit generated occurrence",
                )
            )
        )
        return after, generated

    def restore_recurring(
        self,
        identifier: str,
        *,
        expected: ReminderSnapshot,
        original: ReminderSnapshot,
        generated_occurrence: ReminderSnapshot,
    ) -> ReminderSnapshot:
        response = self._call(
            "restore-recurring",
            {
                "identifier": identifier,
                "expected": expected.to_dict(),
                "restore": original.to_dict(),
                "generated_occurrence": generated_occurrence.to_dict(),
            },
        )
        before = ReminderSnapshot.from_mapping(
            _mapping(response.get("before"), field="EventKit before record")
        )
        if before.fingerprint() != expected.fingerprint():
            raise ConflictError(
                "EventKit recurring undo did not bind the expected source version"
            )
        return ReminderSnapshot.from_mapping(
            _mapping(response.get("record"), field="EventKit restored record")
        )


class AdapterStateStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser()
        if not self.root.is_absolute():
            raise ValidationError("adapter state root must be absolute")
        self.operations = self.root / "operations"
        self.undo = self.root / "undo"
        self.locks = self.root / "locks"

    def prepare(self) -> None:
        if self.root.exists() and self.root.is_symlink():
            raise ValidationError("adapter state root cannot be a symlink")
        for path in (self.root, self.operations, self.undo, self.locks):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = os.lstat(path)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise ValidationError("adapter state path must be a user-owned directory")
            if stat.S_IMODE(info.st_mode) & 0o077:
                os.chmod(path, 0o700)

    def mutation_lock(self):
        self.prepare()
        return _exclusive_lock(self.locks / "bridge.lock")

    def operation_path(self, digest: str) -> Path:
        return self.operations / f"{digest}.json"

    def undo_path(self, digest: str) -> Path:
        return self.undo / f"{digest}.json"

    def load_operation_id(self, operation_id: str) -> dict[str, Any] | None:
        digest = _operation_digest(operation_id)
        state = self._load(self.operation_path(digest), STATE_SCHEMA)
        if state is not None and (
            state.get("operation_id") != operation_id
            or state.get("operation_digest") != digest
        ):
            raise ConflictError("operation digest is bound to another operation")
        return state

    def load_reference(self, reference: Any) -> dict[str, Any]:
        digest = _parse_reference(reference, prefix=REFERENCE_PREFIX)
        state = self._load(self.operation_path(digest), STATE_SCHEMA)
        if state is None or state.get("reference") != reference:
            raise ValidationError("reminder completion reference was not found")
        return state

    def load_undo_reference(self, reference: Any) -> dict[str, Any]:
        digest = _parse_reference(reference, prefix=UNDO_REFERENCE_PREFIX)
        state = self._load(self.undo_path(digest), UNDO_STATE_SCHEMA)
        if state is None or state.get("reference") != reference:
            raise ValidationError("reminder completion undo reference was not found")
        return state

    def save_operation(self, state: Mapping[str, Any]) -> None:
        digest = _clean(
            state.get("operation_digest"),
            field="operation_digest",
            limit=64,
        )
        if not _DIGEST_RE.fullmatch(digest):
            raise ReminderCompleteError("operation digest is invalid")
        self.prepare()
        _write_json_atomic(self.operation_path(digest), state)

    def save_undo(self, state: Mapping[str, Any]) -> None:
        digest = _clean(
            state.get("operation_digest"),
            field="operation_digest",
            limit=64,
        )
        if not _DIGEST_RE.fullmatch(digest):
            raise ReminderCompleteError("undo operation digest is invalid")
        self.prepare()
        _write_json_atomic(self.undo_path(digest), state)

    @staticmethod
    def _load(path: Path, schema: str) -> dict[str, Any] | None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > MAX_REQUEST_BYTES
        ):
            raise ReminderCompleteError("adapter state file is unsafe")
        try:
            document = json.loads(path.read_bytes())
        except json.JSONDecodeError as exc:
            raise ReminderCompleteError("adapter state is invalid JSON") from exc
        if not isinstance(document, dict) or document.get("schema") != schema:
            raise ReminderCompleteError("adapter state schema is invalid")
        return document


class ReminderCompleteCommandBridge:
    def __init__(
        self,
        backend: ReminderCompleteBackend,
        *,
        state_root: Path = DEFAULT_STATE_ROOT,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.backend = backend
        self.store = AdapterStateStore(state_root)
        self.clock = clock

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
        self,
        proposal: Mapping[str, Any],
        *,
        operation_id: str,
    ) -> dict[str, Any]:
        conflict = ReminderConflict.from_proposal(proposal)
        operation_id = _operation_id(operation_id)
        operation_digest = _operation_digest(operation_id)
        proposal_digest = _proposal_digest(proposal)
        reference = f"{REFERENCE_PREFIX}{operation_digest}"
        with self.store.mutation_lock():
            existing = self.store.load_operation_id(operation_id)
            if existing is not None:
                self._validate_binding(existing, proposal_digest)
                return self._execute_response(existing, replayed=True)
            before = self.backend.get(conflict.native_reminder_id)
            if before is None:
                raise ConflictError("exact reminder was not found")
            if not before.matches_conflict(conflict):
                raise ConflictError(
                    "reminder identity or source version changed before completion"
                )
            after, generated_occurrence = self.backend.set_completed(
                before.identifier,
                expected=before,
                completed=True,
            )
            effect_kind = "completed"
            try:
                self._verify_transition(before, after, completed=True)
                if generated_occurrence is not None:
                    raise VerificationError(
                        "ordinary completion reported an unexpected generated occurrence"
                    )
            except VerificationError:
                self._verify_recurring_advance(before, after)
                if generated_occurrence is None:
                    raise VerificationError(
                        "recurring completion has no generated occurrence receipt"
                    )
                self._verify_generated_occurrence(before, generated_occurrence)
                effect_kind = "recurrence_advanced"
            state = {
                "schema": STATE_SCHEMA,
                "created_at": self.clock(),
                "operation_id": operation_id,
                "operation_digest": operation_digest,
                "proposal_digest": proposal_digest,
                "reference": reference,
                "before": before.to_dict(),
                "after": after.to_dict(),
                "effect_kind": effect_kind,
                "generated_occurrence": (
                    generated_occurrence.to_dict()
                    if generated_occurrence is not None
                    else None
                ),
                "latest_undo_reference": None,
            }
            self.store.save_operation(state)
            return self._execute_response(state, replayed=False)

    def readback(
        self,
        proposal: Mapping[str, Any],
        *,
        reference: Any,
    ) -> dict[str, Any]:
        state = self.store.load_reference(reference)
        self._validate_binding(state, _proposal_digest(proposal))
        expected = ReminderSnapshot.from_mapping(
            _mapping(state.get("after"), field="state.after")
        )
        current = self.backend.get(expected.identifier)
        if current is None or current.fingerprint() != expected.fingerprint():
            raise VerificationError(
                "independent readback does not match the completed reminder receipt"
            )
        generated = self._state_generated_occurrence(state)
        if generated is not None:
            current_generated = self.backend.get(generated.identifier)
            if (
                current_generated is None
                or current_generated.fingerprint() != generated.fingerprint()
            ):
                raise VerificationError(
                    "independent readback does not match the generated recurring occurrence"
                )
        effect_kind = state_effect_kind(state)
        effect_fingerprints: dict[str, Any]
        if effect_kind == "recurrence_advanced":
            if generated is None:
                raise VerificationError(
                    "recurring completion receipt has no generated occurrence"
                )
            generated_fingerprint = self._source_fingerprint(generated)
            generated_fingerprint["original_native_reminder_id"] = (
                ReminderSnapshot.from_mapping(
                    _mapping(state.get("before"), field="state.before")
                ).identifier
            )
            effect_fingerprints = {
                "advanced_master_fingerprint": self._source_fingerprint(current),
                "generated_occurrence_fingerprint": generated_fingerprint,
            }
        else:
            effect_fingerprints = {
                "post_write_fingerprint": self._source_fingerprint(current)
            }
        return {
            "ok": True,
            "observed": {
                "status": "completed",
                "effect_kind": effect_kind,
                "reference": state["reference"],
                "native_reminder_id": current.identifier,
                "fingerprint": current.fingerprint(),
                "recurring": current.recurrence_fingerprint != "none",
                **effect_fingerprints,
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
        operation_id = _operation_id(operation_id)
        undo_digest = _operation_digest(operation_id)
        proposal_digest = _proposal_digest(proposal)
        undo_reference = f"{UNDO_REFERENCE_PREFIX}{undo_digest}"
        with self.store.mutation_lock():
            original = self.store.load_reference(reference)
            self._validate_binding(original, proposal_digest)
            existing = self.store._load(
                self.store.undo_path(undo_digest),
                UNDO_STATE_SCHEMA,
            )
            if existing is not None:
                if (
                    existing.get("operation_id") != operation_id
                    or existing.get("original_reference") != reference
                    or existing.get("proposal_digest") != proposal_digest
                ):
                    raise ConflictError("undo operation_id is bound to another request")
                self._link_undo(original, undo_reference)
                return self._undo_response(existing, replayed=True)
            completed = ReminderSnapshot.from_mapping(
                _mapping(original.get("after"), field="state.after")
            )
            before = ReminderSnapshot.from_mapping(
                _mapping(original.get("before"), field="state.before")
            )
            current = self.backend.get(completed.identifier)
            if current is None or current.fingerprint() != completed.fingerprint():
                raise ConflictError("completed reminder changed before undo")
            effect_kind = state_effect_kind(original)
            if effect_kind == "recurrence_advanced":
                generated = self._state_generated_occurrence(original)
                if generated is None:
                    raise ReminderCompleteError(
                        "recurring completion receipt has no generated occurrence"
                    )
                current_generated = self.backend.get(generated.identifier)
                if (
                    current_generated is None
                    or current_generated.fingerprint() != generated.fingerprint()
                ):
                    raise ConflictError(
                        "generated recurring occurrence changed before undo"
                    )
                restored = self.backend.restore_recurring(
                    current.identifier,
                    expected=current,
                    original=before,
                    generated_occurrence=generated,
                )
            else:
                restored, generated = self.backend.set_completed(
                    current.identifier,
                    expected=current,
                    completed=before.completed,
                )
                if generated is not None:
                    raise VerificationError(
                        "ordinary undo reported an unexpected generated occurrence"
                    )
                self._verify_transition(
                    current,
                    restored,
                    completed=before.completed,
                )
            self._verify_restoration(before, restored)
            state = {
                "schema": UNDO_STATE_SCHEMA,
                "created_at": self.clock(),
                "operation_id": operation_id,
                "operation_digest": undo_digest,
                "proposal_digest": proposal_digest,
                "reference": undo_reference,
                "original_reference": reference,
                "before": current.to_dict(),
                "after": restored.to_dict(),
                "effect_kind": effect_kind,
                "removed_generated_occurrence_id": (
                    generated.identifier
                    if effect_kind == "recurrence_advanced"
                    else None
                ),
            }
            self.store.save_undo(state)
            self._link_undo(original, undo_reference)
            return self._undo_response(state, replayed=False)

    def readback_undo(
        self,
        proposal: Mapping[str, Any],
        *,
        reference: Any,
    ) -> dict[str, Any]:
        original = self.store.load_reference(reference)
        self._validate_binding(original, _proposal_digest(proposal))
        undo_reference = original.get("latest_undo_reference")
        if not isinstance(undo_reference, str) or not undo_reference:
            raise ValidationError("reminder completion has no recorded undo")
        undo = self.store.load_undo_reference(undo_reference)
        expected = ReminderSnapshot.from_mapping(
            _mapping(undo.get("after"), field="undo.after")
        )
        current = self.backend.get(expected.identifier)
        if current is None or current.fingerprint() != expected.fingerprint():
            raise VerificationError(
                "independent undo readback does not match the restored reminder"
            )
        removed_identifier = undo.get("removed_generated_occurrence_id")
        if removed_identifier is not None:
            removed_identifier = _clean(
                removed_identifier,
                field="removed_generated_occurrence_id",
            )
            if self.backend.get(removed_identifier) is not None:
                raise VerificationError(
                    "generated recurring occurrence remains after undo"
                )
        return {
            "ok": True,
            "observed": {
                "status": "restored_incomplete",
                "reference": reference,
                "native_reminder_id": current.identifier,
                "fingerprint": current.fingerprint(),
                "restored_fingerprint": self._source_fingerprint(current),
            },
        }

    @staticmethod
    def _validate_binding(state: Mapping[str, Any], proposal_digest: str) -> None:
        if state.get("proposal_digest") != proposal_digest:
            raise ConflictError("operation is bound to a different Duffields proposal")

    @staticmethod
    def _verify_transition(
        before: ReminderSnapshot,
        after: ReminderSnapshot,
        *,
        completed: bool,
    ) -> None:
        stable_before = before.to_dict()
        stable_after = after.to_dict()
        for name in ("completed", "completion_date", "lastModifiedDate"):
            stable_before.pop(name)
            stable_after.pop(name)
        if stable_before != stable_after or after.completed is not completed:
            raise VerificationError("completion changed fields outside the exact reminder state")

    @staticmethod
    def _verify_restoration(
        original: ReminderSnapshot,
        restored: ReminderSnapshot,
    ) -> None:
        original_value = original.to_dict()
        restored_value = restored.to_dict()
        for name in ("completion_date", "lastModifiedDate"):
            original_value.pop(name)
            restored_value.pop(name)
        if original_value != restored_value:
            raise VerificationError("undo did not restore the prior reminder state")

    @staticmethod
    def _verify_recurring_advance(
        before: ReminderSnapshot,
        after: ReminderSnapshot,
    ) -> None:
        stable_before = before.to_dict()
        stable_after = after.to_dict()
        for name in (
            "due_at",
            "completed",
            "completion_date",
            "lastModifiedDate",
            "recurrence_fingerprint",
            "recurrence",
        ):
            stable_before.pop(name)
            stable_after.pop(name)
        try:
            before_due = dt.datetime.fromisoformat(
                (before.due_at or "").replace("Z", "+00:00")
            )
            after_due = dt.datetime.fromisoformat(
                (after.due_at or "").replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise VerificationError(
                "recurring completion did not preserve a comparable due date"
            ) from exc
        if (
            not before.recurrence
            or stable_before != stable_after
            or before.completed
            or after.completed
            or after_due <= before_due
        ):
            raise VerificationError(
                "completion did not advance the exact recurring reminder"
            )

    @staticmethod
    def _verify_generated_occurrence(
        original: ReminderSnapshot,
        generated: ReminderSnapshot,
    ) -> None:
        if (
            generated.identifier == original.identifier
            or generated.external_identifier == original.external_identifier
            or generated.list_id != original.list_id
            or generated.list_name != original.list_name
            or generated.title != original.title
            or generated.due_at != original.due_at
            or generated.due_state != original.due_state
            or generated.priority != original.priority
            or not generated.completed
            or generated.completion_date is None
            or generated.recurrence
            or generated.recurrence_fingerprint != "none"
        ):
            raise VerificationError(
                "generated recurring occurrence is not bound to the completed occurrence"
            )

    @staticmethod
    def _state_generated_occurrence(
        state: Mapping[str, Any],
    ) -> ReminderSnapshot | None:
        raw = state.get("generated_occurrence")
        if raw is None:
            return None
        return ReminderSnapshot.from_mapping(
            _mapping(raw, field="state.generated_occurrence")
        )

    @staticmethod
    def _source_fingerprint(snapshot: ReminderSnapshot) -> dict[str, Any]:
        return {
            "list_id": snapshot.list_id,
            "native_reminder_id": snapshot.identifier,
            "observed_completion_state": (
                "completed" if snapshot.completed else "incomplete"
            ),
            "observed_due_state": snapshot.due_state,
            "observed_due_at": snapshot.due_at or "",
            "lastModifiedDate": snapshot.last_modified_date,
            "recurrence_fingerprint": snapshot.recurrence_fingerprint,
        }

    def _link_undo(
        self,
        original: Mapping[str, Any],
        undo_reference: str,
    ) -> None:
        updated = dict(original)
        existing = updated.get("latest_undo_reference")
        if existing not in {None, undo_reference}:
            raise ConflictError("reminder completion is bound to another undo")
        updated["latest_undo_reference"] = undo_reference
        self.store.save_operation(updated)

    @staticmethod
    def _execute_response(
        state: Mapping[str, Any],
        *,
        replayed: bool,
    ) -> dict[str, Any]:
        after = ReminderSnapshot.from_mapping(
            _mapping(state.get("after"), field="state.after")
        )
        return {
            "ok": True,
            "reference": state["reference"],
            "details": {
                "status": "completed",
                "effect_verified": True,
                "effect_kind": state_effect_kind(state),
                "native_reminder_id": after.identifier,
                "after_fingerprint": after.fingerprint(),
                "recurring": after.recurrence_fingerprint != "none",
                "replayed": replayed,
            },
        }

    @staticmethod
    def _undo_response(
        state: Mapping[str, Any],
        *,
        replayed: bool,
    ) -> dict[str, Any]:
        after = ReminderSnapshot.from_mapping(
            _mapping(state.get("after"), field="undo.after")
        )
        return {
            "ok": True,
            "reference": state["reference"],
            "details": {
                "status": "restored_incomplete",
                "effect_verified": True,
                "effect_kind": state_effect_kind(state),
                "native_reminder_id": after.identifier,
                "after_fingerprint": after.fingerprint(),
                "original_reference": state["original_reference"],
                "replayed": replayed,
            },
        }


def _read_request(path: Path | None) -> dict[str, Any]:
    if path is None:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    else:
        if not path.is_absolute():
            raise ValidationError("--request must be absolute")
        info = os.lstat(path)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > MAX_REQUEST_BYTES
        ):
            raise ValidationError("--request must be a private user-owned file")
        raw = path.read_bytes()
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise ValidationError("request must contain 1-131072 bytes")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError("request is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ValidationError("request must be one JSON object")
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reminder-complete-command-adapter")
    parser.add_argument(
        "operation",
        choices=("execute", "readback", "undo", "readback-undo"),
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
            raise ValidationError("--response must be absolute")
        document = _read_request(args.request)
        mutating = args.operation in {"execute", "undo"}
        if mutating and os.environ.get(LIVE_ENV) != LIVE_CONFIRMATION:
            raise ValidationError(
                f"{args.operation} requires {LIVE_ENV}={LIVE_CONFIRMATION!r}"
            )
        root = args.state_root or Path(
            os.environ.get(STATE_ROOT_ENV, str(DEFAULT_STATE_ROOT))
        )
        backend = SwiftEventKitCompleteBackend(
            args.swift_helper,
            allow_mutation=mutating,
        )
        result = ReminderCompleteCommandBridge(
            backend,
            state_root=root,
        ).dispatch(args.operation, document)
        exit_code = 0
    except (
        OSError,
        ValueError,
        sqlite3.Error,
        ReminderUpsertError,
        json.JSONDecodeError,
    ) as exc:
        result = {
            "ok": False,
            "error": type(exc).__name__,
            "detail": str(exc)[:1000],
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
