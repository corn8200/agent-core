from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from core import reminder_command_adapter as command
from core.reminder_command_adapter import ReminderCommandBridge
from core.reminder_upsert import (
    LIVE_CONFIRMATION,
    ConflictError,
    ReminderRecord,
    ReminderValue,
    ValidationError,
    VerificationError,
)


FIXED_TIME = "2026-07-21T12:00:00Z"
EXECUTE_ID = "decision:execute:00000001"
UNDO_ID = "decision:undo:00000001"


def proposal(**payload_overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "list": "Claude",
        "title": "Confirm the canary action",
        "notes": "Created from a verified Duffields decision.",
        "due_at": "2026-07-22T09:30:00-04:00",
        "priority": "medium",
    }
    payload.update(payload_overrides)
    return {
        "proposal_version": "duffields-action-proposal/v1",
        "proposal_id": "proposal-canary-0001",
        "action": {
            "adapter": "reminders",
            "kind": "reminder.upsert",
            "payload": payload,
            "readback_required": True,
            "undo_supported": True,
            "undo_window_seconds": 3600,
        },
        "target": {
            "system": "apple_reminders",
            "resource_type": "reminder",
            "scope": payload["list"],
            "display": payload["title"],
        },
        "risk": {
            "level": "low",
            "reversible": True,
            "external_communication": False,
        },
    }


class FakeBackend:
    def __init__(self) -> None:
        self.records: dict[str, ReminderRecord] = {}
        self.calls: list[tuple[str, str]] = []
        self.next_identifier = 1
        self.mutation_count = 0

    def modified_at(self) -> str:
        self.mutation_count += 1
        return f"2026-07-21T12:00:{self.mutation_count:02d}.000Z"

    def find_by_marker(self, marker: str) -> list[ReminderRecord]:
        self.calls.append(("find", marker))
        return [item for item in self.records.values() if marker in item.notes.splitlines()]

    def get(self, identifier: str) -> ReminderRecord | None:
        self.calls.append(("get", identifier))
        return self.records.get(identifier)

    def create(self, value: ReminderValue, *, marker: str) -> ReminderRecord:
        self.calls.append(("create", marker))
        assert marker in value.notes.splitlines()
        identifier = f"fake-{self.next_identifier}"
        self.next_identifier += 1
        record = ReminderRecord(
            **value.__dict__,
            identifier=identifier,
            last_modified_at=self.modified_at(),
        )
        self.records[identifier] = record
        return record

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
        record = ReminderRecord(
            **value.__dict__,
            identifier=identifier,
            last_modified_at=self.modified_at(),
        )
        self.records[identifier] = record
        return record

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


def bridge(backend: FakeBackend, tmp_path: Path) -> ReminderCommandBridge:
    return ReminderCommandBridge(
        backend,
        state_root=tmp_path / "state",
        clock=lambda: FIXED_TIME,
    )


def test_execute_readback_undo_and_undo_readback(tmp_path: Path) -> None:
    backend = FakeBackend()
    subject = bridge(backend, tmp_path)
    document = proposal()

    executed = subject.dispatch(
        "execute", {"proposal": document, "operation_id": EXECUTE_ID}
    )

    assert executed["ok"] is True
    assert executed["reference"].startswith(command.REFERENCE_PREFIX)
    assert executed["details"]["status"] == "created"
    assert executed["details"]["effect_verified"] is True
    assert executed["details"]["replayed"] is False
    assert len(backend.records) == 1

    replayed = subject.dispatch(
        "execute", {"proposal": document, "operation_id": EXECUTE_ID}
    )
    assert replayed["reference"] == executed["reference"]
    assert replayed["details"]["replayed"] is True
    assert [name for name, _ in backend.calls].count("create") == 1

    readback = subject.dispatch(
        "readback", {"proposal": document, "reference": executed["reference"]}
    )
    assert readback["ok"] is True
    assert readback["observed"]["status"] == "present"
    assert readback["observed"]["reminder"]["list"] == "Claude"

    undone = subject.dispatch(
        "undo",
        {
            "proposal": document,
            "reference": executed["reference"],
            "operation_id": UNDO_ID,
        },
    )
    assert undone["ok"] is True
    assert undone["reference"].startswith(command.UNDO_REFERENCE_PREFIX)
    assert undone["details"]["status"] == "deleted_created"
    assert backend.records == {}

    undo_readback = subject.dispatch(
        "readback-undo",
        {"proposal": document, "reference": executed["reference"]},
    )
    assert undo_readback["ok"] is True
    assert undo_readback["observed"]["status"] == "absent"

    undo_replay = subject.dispatch(
        "undo",
        {
            "proposal": document,
            "reference": executed["reference"],
            "operation_id": UNDO_ID,
        },
    )
    assert undo_replay["reference"] == undone["reference"]
    assert undo_replay["details"]["replayed"] is True
    assert [name for name, _ in backend.calls].count("delete") == 1


def test_operation_state_is_private_and_contains_verified_receipt(tmp_path: Path) -> None:
    backend = FakeBackend()
    subject = bridge(backend, tmp_path)
    result = subject.execute(proposal(), operation_id=EXECUTE_ID)
    digest = result["reference"].removeprefix(command.REFERENCE_PREFIX)
    state_path = tmp_path / "state" / "operations" / f"{digest}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert state["operation_id"] == EXECUTE_ID
    assert state["execute_receipt"]["effect_verified"] is True
    assert state["execute_receipt"]["undo"]["operation"] == "delete_created"
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700


def test_independent_readback_detects_external_change(tmp_path: Path) -> None:
    backend = FakeBackend()
    subject = bridge(backend, tmp_path)
    document = proposal()
    executed = subject.execute(document, operation_id=EXECUTE_ID)
    identifier = next(iter(backend.records))
    backend.records[identifier] = replace(
        backend.records[identifier],
        title="John changed the title",
        last_modified_at="2026-07-21T13:00:00.000Z",
    )

    with pytest.raises(VerificationError, match="independent readback"):
        subject.readback(document, reference=executed["reference"])


def test_same_operation_id_cannot_bind_to_changed_proposal(tmp_path: Path) -> None:
    backend = FakeBackend()
    subject = bridge(backend, tmp_path)
    subject.execute(proposal(), operation_id=EXECUTE_ID)

    with pytest.raises(ConflictError, match="different Duffields proposal"):
        subject.execute(proposal(title="A different request"), operation_id=EXECUTE_ID)

    assert [name for name, _ in backend.calls].count("create") == 1


def test_orphan_marker_same_proposal_requires_reconciliation(tmp_path: Path) -> None:
    backend = FakeBackend()
    subject = bridge(backend, tmp_path)
    document = proposal()
    request = command.proposal_to_request(document, EXECUTE_ID)
    backend.create(request.desired(), marker=request.marker)

    with pytest.raises(ConflictError, match="reconciliation required"):
        subject.execute(document, operation_id=EXECUTE_ID)

    assert [name for name, _ in backend.calls].count("create") == 1
    assert [name for name, _ in backend.calls].count("update") == 0
    assert [name for name, _ in backend.calls].count("delete") == 0
    digest = command._operation_digest(EXECUTE_ID)
    assert not (tmp_path / "state" / "operations" / f"{digest}.json").exists()


def test_orphan_marker_changed_proposal_cannot_be_updated(tmp_path: Path) -> None:
    backend = FakeBackend()
    subject = bridge(backend, tmp_path)
    original = proposal()
    request = command.proposal_to_request(original, EXECUTE_ID)
    backend.create(request.desired(), marker=request.marker)

    with pytest.raises(ConflictError, match="reconciliation required"):
        subject.execute(
            proposal(title="Changed proposal must not update the orphan"),
            operation_id=EXECUTE_ID,
        )

    assert [name for name, _ in backend.calls].count("create") == 1
    assert [name for name, _ in backend.calls].count("update") == 0
    assert [name for name, _ in backend.calls].count("delete") == 0
    digest = command._operation_digest(EXECUTE_ID)
    assert not (tmp_path / "state" / "operations" / f"{digest}.json").exists()


def test_receipt_persistence_failure_leaves_reconciliation_only_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend()
    subject = bridge(backend, tmp_path)
    document = proposal()

    def fail_persist(state: object) -> None:
        raise OSError("simulated durable-state failure")

    monkeypatch.setattr(subject.store, "save_operation", fail_persist)
    with pytest.raises(OSError, match="durable-state failure"):
        subject.execute(document, operation_id=EXECUTE_ID)

    assert len(backend.records) == 1
    monkeypatch.undo()
    with pytest.raises(ConflictError, match="reconciliation required"):
        subject.execute(document, operation_id=EXECUTE_ID)
    assert [name for name, _ in backend.calls].count("update") == 0
    assert [name for name, _ in backend.calls].count("delete") == 0
    digest = command._operation_digest(EXECUTE_ID)
    assert not (tmp_path / "state" / "operations" / f"{digest}.json").exists()


@pytest.mark.parametrize("list_name", ["J&A Reminders", "Store", "Work"])
def test_bridge_refuses_non_canary_lists(list_name: str, tmp_path: Path) -> None:
    backend = FakeBackend()

    with pytest.raises(ValidationError, match="canary may write only"):
        bridge(backend, tmp_path).execute(
            proposal(list=list_name), operation_id=EXECUTE_ID
        )

    assert backend.calls == []


def test_bridge_rejects_unknown_payload_fields(tmp_path: Path) -> None:
    backend = FakeBackend()

    with pytest.raises(ValidationError, match="unsupported reminder payload"):
        bridge(backend, tmp_path).execute(
            proposal(completed=True), operation_id=EXECUTE_ID
        )

    assert backend.calls == []


def test_file_transport_is_atomic_private_and_live_off_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = (tmp_path / "request.json").resolve()
    response_path = (tmp_path / "response.json").resolve()
    state_root = (tmp_path / "state").resolve()
    request_path.write_text(
        json.dumps({"proposal": proposal(), "operation_id": EXECUTE_ID}),
        encoding="utf-8",
    )
    request_path.chmod(0o600)

    def forbidden_backend(*args: object, **kwargs: object) -> None:
        raise AssertionError("disabled execute must not construct EventKit")

    monkeypatch.delenv(command.LIVE_ENV, raising=False)
    monkeypatch.setattr(command, "SwiftEventKitBackend", forbidden_backend)

    result = command.main(
        [
            "execute",
            "--request",
            str(request_path),
            "--response",
            str(response_path),
            "--state-root",
            str(state_root),
        ]
    )

    assert result == 2
    response = json.loads(response_path.read_text(encoding="utf-8"))
    assert response["ok"] is False
    assert command.LIVE_ENV in response["detail"]
    assert stat.S_IMODE(response_path.stat().st_mode) == 0o600
    assert not state_root.exists()


def test_file_transport_executes_with_injected_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend()
    request_path = (tmp_path / "request.json").resolve()
    response_path = (tmp_path / "response.json").resolve()
    state_root = (tmp_path / "state").resolve()
    request_path.write_text(
        json.dumps({"proposal": proposal(), "operation_id": EXECUTE_ID}),
        encoding="utf-8",
    )
    request_path.chmod(0o600)
    monkeypatch.setenv(command.LIVE_ENV, LIVE_CONFIRMATION)
    monkeypatch.setattr(command, "SwiftEventKitBackend", lambda *args, **kwargs: backend)

    result = command.main(
        [
            "execute",
            "--request",
            str(request_path),
            "--response",
            str(response_path),
            "--state-root",
            str(state_root),
        ]
    )

    assert result == 0
    response = json.loads(response_path.read_text(encoding="utf-8"))
    assert response["ok"] is True
    assert response["details"]["effect_verified"] is True
    assert len(backend.records) == 1


def test_executable_stdin_contract_remains_non_live_by_default(tmp_path: Path) -> None:
    executable = Path(__file__).resolve().parents[1] / "bin" / "reminder-command-adapter"
    environment = dict(os.environ)
    environment.pop(command.LIVE_ENV, None)
    completed = subprocess.run(
        [str(executable), "execute", "--state-root", str((tmp_path / "state").resolve())],
        input=json.dumps({"proposal": proposal(), "operation_id": EXECUTE_ID}),
        text=True,
        capture_output=True,
        timeout=10,
        env=environment,
        check=False,
    )

    assert completed.returncode == 2
    assert json.loads(completed.stderr)["ok"] is False
    assert not (tmp_path / "state").exists()
