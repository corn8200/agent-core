from __future__ import annotations

import json
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from core import reminder_upsert
from core.reminder_upsert import (
    BackendError,
    ConflictError,
    ReminderRecord,
    ReminderUpsertAdapter,
    ReminderUpsertRequest,
    ReminderValue,
    ValidationError,
    VerificationError,
)


FIXED_TIME = "2026-07-21T12:00:00Z"


def request_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": reminder_upsert.SCHEMA,
        "idempotency_key": "duffields:canary:decision-42",
        "list": "Claude",
        "title": "Confirm the canary action",
        "notes": "Created from a verified Duffields decision.",
        "due_at": "2026-07-22T09:30:00-04:00",
        "priority": "medium",
        "approval_ref": "approval:decision-42",
        "source_ref": "duffields:decision-42",
    }
    payload.update(overrides)
    return payload


def request(**overrides: object) -> ReminderUpsertRequest:
    return ReminderUpsertRequest.from_mapping(request_payload(**overrides))


class FakeBackend:
    def __init__(self) -> None:
        self.records: dict[str, ReminderRecord] = {}
        self.calls: list[tuple[str, str]] = []
        self.next_identifier = 1
        self.mutation_count = 0
        self.corrupt_readback = False

    def modified_at(self) -> str:
        self.mutation_count += 1
        return f"2026-07-21T12:00:{self.mutation_count:02d}.000Z"

    def find_by_marker(self, marker: str) -> list[ReminderRecord]:
        self.calls.append(("find", marker))
        return [
            item
            for item in self.records.values()
            if marker in item.notes.splitlines()
        ]

    def get(self, identifier: str) -> ReminderRecord | None:
        self.calls.append(("get", identifier))
        item = self.records.get(identifier)
        if item is not None and self.corrupt_readback:
            return replace(item, title=f"{item.title} (corrupt)")
        return item

    def create(self, value: ReminderValue, *, marker: str) -> ReminderRecord:
        self.calls.append(("create", marker))
        assert marker in value.notes.splitlines()
        identifier = f"fake-{self.next_identifier}"
        self.next_identifier += 1
        item = ReminderRecord(
            **value.__dict__,
            identifier=identifier,
            last_modified_at=self.modified_at(),
        )
        self.records[identifier] = item
        return item

    def update(
        self,
        identifier: str,
        value: ReminderValue,
        *,
        expected: ReminderRecord,
        marker: str,
    ) -> ReminderRecord:
        self.calls.append(("update", identifier))
        current = self.records[identifier]
        assert current.fingerprint() == expected.fingerprint()
        assert marker in current.notes.splitlines()
        item = ReminderRecord(
            **value.__dict__,
            identifier=identifier,
            last_modified_at=self.modified_at(),
        )
        self.records[identifier] = item
        return item

    def delete(
        self,
        identifier: str,
        *,
        expected: ReminderRecord,
        marker: str,
    ) -> None:
        self.calls.append(("delete", identifier))
        current = self.records[identifier]
        assert current.fingerprint() == expected.fingerprint()
        assert marker in current.notes.splitlines()
        del self.records[identifier]


def adapter(backend: FakeBackend, tmp_path: Path) -> ReminderUpsertAdapter:
    return ReminderUpsertAdapter(
        backend,
        lock_path=tmp_path / "reminder.lock",
        clock=lambda: FIXED_TIME,
    )


@pytest.mark.parametrize("list_name", ["J&A Reminders", "Store", "Home", "Work"])
def test_canary_rejects_every_non_claude_list(list_name: str, tmp_path: Path) -> None:
    backend = FakeBackend()
    with pytest.raises(ValidationError, match="canary may write only"):
        adapter(backend, tmp_path).upsert(request(list=list_name))
    assert backend.calls == []


def test_request_rejects_naive_due_and_reserved_marker() -> None:
    with pytest.raises(ValidationError, match="explicit timezone"):
        request(due_at="2026-07-22T09:30:00")
    with pytest.raises(ValidationError, match="reserved"):
        request(notes="do this [reminder-upsert:someone-else]")


def test_live_upsert_requires_approval_reference(tmp_path: Path) -> None:
    backend = FakeBackend()
    with pytest.raises(ValidationError, match="approval_ref"):
        adapter(backend, tmp_path).upsert(request(approval_ref=None))
    assert backend.calls == []


def test_create_replay_and_guarded_delete_undo(tmp_path: Path) -> None:
    backend = FakeBackend()
    subject = adapter(backend, tmp_path)

    created = subject.upsert(request())

    assert created["status"] == "created"
    assert created["effect_verified"] is True
    assert created["after"]["notes"].splitlines()[-1] == request().marker
    assert created["after"]["due_at"] == "2026-07-22T13:30:00Z"
    assert created["undo"]["operation"] == "delete_created"
    assert [name for name, _ in backend.calls].count("create") == 1

    replayed = subject.upsert(request())

    assert replayed["status"] == "unchanged"
    assert [name for name, _ in backend.calls].count("create") == 1
    assert [name for name, _ in backend.calls].count("update") == 0

    undone = subject.undo(created, approval_ref="approval:undo-42")

    assert undone["status"] == "deleted_created"
    assert undone["effect_verified"] is True
    assert backend.records == {}
    assert subject.undo(created, approval_ref="approval:undo-42")["status"] == "already_undone"


def test_update_and_guarded_restore_undo(tmp_path: Path) -> None:
    backend = FakeBackend()
    subject = adapter(backend, tmp_path)
    req = request()
    original_value = replace(
        req.desired(),
        title="Original title",
        due_at=None,
        priority=0,
    )
    original = backend.create(original_value, marker=req.marker)

    updated = subject.upsert(req)

    assert updated["status"] == "updated"
    assert updated["before"]["title"] == "Original title"
    assert updated["after"]["title"] == req.title
    assert updated["undo"]["operation"] == "restore_updated"

    undone = subject.undo(updated, approval_ref="approval:undo-42")

    assert undone["status"] == "restored_updated"
    assert backend.records[original.identifier].title == "Original title"
    assert backend.records[original.identifier].due_at is None
    assert subject.undo(updated, approval_ref="approval:undo-42")["status"] == "already_undone"


def test_undo_refuses_to_overwrite_user_edit(tmp_path: Path) -> None:
    backend = FakeBackend()
    subject = adapter(backend, tmp_path)
    receipt = subject.upsert(request())
    identifier = receipt["after"]["identifier"]
    backend.records[identifier] = replace(
        backend.records[identifier],
        title="John edited this after the automation",
    )

    with pytest.raises(ConflictError, match="John's edits"):
        subject.undo(receipt, approval_ref="approval:undo-42")

    assert identifier in backend.records


def test_undo_refuses_timestamp_only_change(tmp_path: Path) -> None:
    backend = FakeBackend()
    subject = adapter(backend, tmp_path)
    receipt = subject.upsert(request())
    identifier = receipt["after"]["identifier"]
    backend.records[identifier] = replace(
        backend.records[identifier],
        last_modified_at="2026-07-21T13:00:00.000Z",
    )

    with pytest.raises(ConflictError, match="John's edits"):
        subject.undo(receipt, approval_ref="approval:undo-42")


def test_duplicate_marker_refuses_ambiguous_write(tmp_path: Path) -> None:
    backend = FakeBackend()
    req = request()
    backend.create(req.desired(), marker=req.marker)
    duplicate = replace(
        backend.records["fake-1"],
        identifier="fake-2",
        title="Duplicate",
    )
    backend.records[duplicate.identifier] = duplicate

    with pytest.raises(ConflictError, match="matched 2 reminders"):
        adapter(backend, tmp_path).upsert(req)

    assert [name for name, _ in backend.calls].count("update") == 0


def test_completed_reminder_is_never_resurrected(tmp_path: Path) -> None:
    backend = FakeBackend()
    req = request(title="New title")
    completed = replace(req.desired(), title="Finished title", completed=True)
    backend.create(completed, marker=req.marker)

    receipt = adapter(backend, tmp_path).upsert(req)

    assert receipt["status"] == "completed"
    assert receipt["after"]["completed"] is True
    assert receipt["undo"] is None
    assert [name for name, _ in backend.calls].count("update") == 0


def test_exact_readback_mismatch_fails_verification(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.corrupt_readback = True

    with pytest.raises(VerificationError, match="exactly match"):
        adapter(backend, tmp_path).upsert(request())


def test_plan_cli_never_constructs_eventkit_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request_path = tmp_path / "request.json"
    receipt_path = tmp_path / "receipt.json"
    request_path.write_text(json.dumps(request_payload()), encoding="utf-8")

    def forbidden_backend(*args: object, **kwargs: object) -> None:
        raise AssertionError("plan mode must not construct the EventKit backend")

    monkeypatch.setattr(reminder_upsert, "SwiftEventKitBackend", forbidden_backend)

    result = reminder_upsert.main(
        ["upsert", "--request", str(request_path), "--receipt", str(receipt_path)]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["status"] == "planned"
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["effect_verified"] is False
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600


def test_read_only_backend_clears_inherited_live_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_env: dict[str, str] = {}

    def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen_env.update(kwargs["env"])
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"ok":true,"record":null}',
            stderr="",
        )

    monkeypatch.setenv("REMINDER_UPSERT_LIVE", "1")
    backend = reminder_upsert.SwiftEventKitBackend(runner=runner)

    assert backend.get("missing") is None
    assert "REMINDER_UPSERT_LIVE" not in seen_env


def test_eventkit_timeout_is_reported_as_backend_error() -> None:
    def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="swift", timeout=1)

    backend = reminder_upsert.SwiftEventKitBackend(runner=runner)

    with pytest.raises(BackendError, match="timed out"):
        backend.get("missing")


@pytest.mark.parametrize(
    "arguments,detail",
    [
        (["--receipt", "receipt.json"], "--live-confirm"),
        (
            ["--live-confirm", reminder_upsert.LIVE_CONFIRMATION],
            "durable receipt",
        ),
    ],
)
def test_live_cli_guards_run_before_backend(
    arguments: list[str],
    detail: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request_payload()), encoding="utf-8")

    def forbidden_backend(*args: object, **kwargs: object) -> None:
        raise AssertionError("failed CLI guards must not construct EventKit")

    monkeypatch.setattr(reminder_upsert, "SwiftEventKitBackend", forbidden_backend)
    expanded = [str(tmp_path / value) if value == "receipt.json" else value for value in arguments]

    result = reminder_upsert.main(
        ["upsert", "--request", str(request_path), "--apply", *expanded]
    )

    assert result == 2
    assert detail in json.loads(capsys.readouterr().err)["detail"]


def test_swift_helper_is_fail_closed_and_canary_scoped() -> None:
    source = Path(reminder_upsert.__file__).with_name("reminder_eventkit.swift").read_text(
        encoding="utf-8"
    )

    assert 'private let allowedList = "Claude"' in source
    assert 'environment["REMINDER_UPSERT_LIVE"] == "1"' in source
    assert "requestAccess(" not in source
    assert "requestFullAccessToReminders(" not in source
