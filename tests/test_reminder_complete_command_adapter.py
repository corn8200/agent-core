from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from core import reminder_complete_command_adapter as complete
from core.reminder_complete_command_adapter import (
    ReminderCompleteCommandBridge,
    ReminderSnapshot,
)
from core.reminder_upsert import ConflictError, ValidationError, VerificationError


FIXED_TIME = "2026-07-28T16:00:00Z"
EXECUTE_ID = "decision:reminder-complete:0001"
UNDO_ID = "decision:reminder-complete-undo:0001"


def recurrence_rules(occurrence_count: int = 2) -> tuple[str, ...]:
    return (
        complete._canonical_json(
            {
                "days_of_month": [],
                "days_of_week": [],
                "days_of_year": [],
                "end": {
                    "end_date": None,
                    "occurrence_count": occurrence_count,
                },
                "first_day": 0,
                "frequency": 1,
                "interval": 1,
                "months_of_year": [],
                "set_positions": [],
                "weeks_of_year": [],
            }
        ),
    )


def snapshot(
    *,
    completed: bool = False,
    modified: str = "2026-07-28T15:55:00Z",
    recurring: bool = False,
    occurrence_count: int = 2,
    due_at: str = "2026-07-29T13:00:00Z",
) -> ReminderSnapshot:
    recurrence = recurrence_rules(occurrence_count) if recurring else ()
    recurrence_fingerprint = (
        "none"
        if not recurrence
        else "sha256:"
        + complete._digest([json.loads(item) for item in recurrence])
    )
    return ReminderSnapshot(
        identifier="reminder-native-0001",
        external_identifier="reminder-external-0001",
        list_id="list-health-0001",
        list_name="Health",
        title="Bring insurance card",
        due_at=due_at,
        due_state="dated",
        priority=0,
        completed=completed,
        completion_date="2026-07-28T16:00:01Z" if completed else None,
        last_modified_date=modified,
        recurrence_fingerprint=recurrence_fingerprint,
        recurrence=recurrence,
    )


def proposal(**payload_overrides: object) -> dict[str, object]:
    record = snapshot()
    payload: dict[str, object] = {
        "requested_verb": "complete",
        "list_id": record.list_id,
        "list": record.list_name,
        "native_reminder_id": record.identifier,
        "title": record.title,
        "lastModifiedDate": record.last_modified_date,
        "observed_completion_state": "incomplete",
        "observed_due_state": record.due_state,
        "observed_due_at": record.due_at or "",
        "recurrence_fingerprint": record.recurrence_fingerprint,
    }
    payload.update(payload_overrides)
    return {
        "proposal_version": "duffields-action-proposal/v1",
        "proposal_id": "reminder-complete-proposal-0001",
        "action": {
            "adapter": "reminders",
            "kind": "reminder.complete",
            "payload": payload,
            "readback_required": True,
            "undo_supported": True,
            "undo_window_seconds": 3600,
        },
        "target": {
            "system": "apple_reminders",
            "resource_type": "reminder",
            "scope": record.list_id,
            "display": record.title,
        },
        "risk": {
            "level": "low",
            "reversible": True,
            "external_communication": False,
        },
    }


class FakeBackend:
    def __init__(self, record: ReminderSnapshot | None = None) -> None:
        self.record = record or snapshot()
        self.calls: list[tuple[str, object]] = []
        self.mutations = 0
        self.generated: ReminderSnapshot | None = None

    def get(self, identifier: str) -> ReminderSnapshot | None:
        self.calls.append(("get", identifier))
        if self.record is not None and self.record.identifier == identifier:
            return self.record
        if self.generated is not None and self.generated.identifier == identifier:
            return self.generated
        return None

    def set_completed(
        self,
        identifier: str,
        *,
        expected: ReminderSnapshot,
        completed: bool,
    ) -> tuple[ReminderSnapshot, ReminderSnapshot | None]:
        self.calls.append(("set_completed", completed))
        if self.record is None or self.record.identifier != identifier:
            raise ConflictError("missing")
        if self.record.fingerprint() != expected.fingerprint():
            raise ConflictError("compare and swap failed")
        self.mutations += 1
        if completed and self.record.recurrence:
            self.generated = replace(
                self.record,
                identifier="reminder-generated-0001",
                external_identifier="reminder-generated-external-0001",
                completed=True,
                completion_date=FIXED_TIME,
                last_modified_date=f"2026-07-28T16:00:0{self.mutations}Z",
                recurrence_fingerprint="none",
                recurrence=(),
            )
            next_rules = recurrence_rules(1)
            self.record = replace(
                self.record,
                due_at="2026-08-05T13:00:00Z",
                completed=False,
                completion_date=None,
                last_modified_date=f"2026-07-28T16:00:0{self.mutations}Z",
                recurrence_fingerprint="sha256:"
                + complete._digest([json.loads(item) for item in next_rules]),
                recurrence=next_rules,
            )
        else:
            self.record = replace(
                self.record,
                completed=completed,
                completion_date=FIXED_TIME if completed else None,
                last_modified_date=f"2026-07-28T16:00:0{self.mutations}Z",
            )
        return self.record, self.generated

    def restore_recurring(
        self,
        identifier: str,
        *,
        expected: ReminderSnapshot,
        original: ReminderSnapshot,
        generated_occurrence: ReminderSnapshot,
    ) -> ReminderSnapshot:
        self.calls.append(("restore_recurring", identifier))
        if self.record is None or self.record.identifier != identifier:
            raise ConflictError("missing")
        if self.record.fingerprint() != expected.fingerprint():
            raise ConflictError("compare and swap failed")
        if (
            self.generated is None
            or self.generated.fingerprint() != generated_occurrence.fingerprint()
        ):
            raise ConflictError("generated occurrence compare and swap failed")
        self.mutations += 1
        self.generated = None
        self.record = replace(
            original,
            completion_date=None,
            last_modified_date=f"2026-07-28T16:00:0{self.mutations}Z",
        )
        return self.record


def bridge(backend: FakeBackend, tmp_path: Path) -> ReminderCompleteCommandBridge:
    return ReminderCompleteCommandBridge(
        backend,
        state_root=tmp_path / "state",
        clock=lambda: FIXED_TIME,
    )


def test_execute_readback_undo_and_replays_are_exact(tmp_path: Path) -> None:
    backend = FakeBackend()
    subject = bridge(backend, tmp_path)
    document = proposal()

    executed = subject.dispatch(
        "execute",
        {"proposal": document, "operation_id": EXECUTE_ID},
    )
    replay = subject.dispatch(
        "execute",
        {"proposal": document, "operation_id": EXECUTE_ID},
    )

    assert executed["ok"] is True
    assert executed["reference"].startswith(complete.REFERENCE_PREFIX)
    assert executed["details"]["status"] == "completed"
    assert replay["reference"] == executed["reference"]
    assert replay["details"]["replayed"] is True
    assert backend.mutations == 1

    readback = subject.dispatch(
        "readback",
        {"proposal": document, "reference": executed["reference"]},
    )
    assert readback["observed"]["status"] == "completed"
    assert readback["observed"]["native_reminder_id"] == "reminder-native-0001"
    assert (
        readback["observed"]["post_write_fingerprint"][
            "observed_completion_state"
        ]
        == "completed"
    )

    undone = subject.dispatch(
        "undo",
        {
            "proposal": document,
            "reference": executed["reference"],
            "operation_id": UNDO_ID,
        },
    )
    undo_replay = subject.dispatch(
        "undo",
        {
            "proposal": document,
            "reference": executed["reference"],
            "operation_id": UNDO_ID,
        },
    )
    assert undone["details"]["status"] == "restored_incomplete"
    assert undo_replay["reference"] == undone["reference"]
    assert undo_replay["details"]["replayed"] is True
    assert backend.mutations == 2

    undo_readback = subject.dispatch(
        "readback-undo",
        {"proposal": document, "reference": executed["reference"]},
    )
    assert undo_readback["observed"]["status"] == "restored_incomplete"
    assert backend.record is not None
    assert backend.record.completed is False
    assert backend.record.completion_date is None


def test_stale_source_version_refuses_before_mutation(tmp_path: Path) -> None:
    backend = FakeBackend(snapshot(modified="2026-07-28T15:56:00Z"))
    subject = bridge(backend, tmp_path)

    with pytest.raises(ConflictError, match="source version"):
        subject.execute(proposal(), operation_id=EXECUTE_ID)

    assert backend.mutations == 0
    assert [name for name, _ in backend.calls] == ["get"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"list_id": "another-list"},
        {"list": "Another list"},
        {"native_reminder_id": "another-reminder"},
        {"observed_completion_state": "completed"},
        {"observed_due_state": "undated"},
        {"recurrence_fingerprint": "sha256:" + "a" * 64},
        {"lastModifiedDate": "2026-07-28T14:00:00Z"},
    ],
)
def test_every_conflict_field_is_bound(
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    backend = FakeBackend()
    subject = bridge(backend, tmp_path)
    with pytest.raises((ConflictError, ValidationError)):
        subject.execute(proposal(**overrides), operation_id=EXECUTE_ID)
    assert backend.mutations == 0


def test_recurring_fingerprint_survives_completion_and_undo(tmp_path: Path) -> None:
    record = snapshot(recurring=True)
    recurrence = record.recurrence_fingerprint
    backend = FakeBackend(record)
    subject = bridge(backend, tmp_path)
    document = proposal(recurrence_fingerprint=recurrence)

    executed = subject.execute(document, operation_id=EXECUTE_ID)
    assert executed["details"]["recurring"] is True
    assert executed["details"]["effect_kind"] == "recurrence_advanced"
    readback = subject.readback(document, reference=executed["reference"])
    assert (
        readback["observed"]["advanced_master_fingerprint"][
            "observed_completion_state"
        ]
        == "incomplete"
    )
    generated = readback["observed"]["generated_occurrence_fingerprint"]
    assert generated["original_native_reminder_id"] == record.identifier
    assert generated["recurrence_fingerprint"] == "none"
    subject.undo(
        document,
        reference=executed["reference"],
        operation_id=UNDO_ID,
    )
    assert backend.record is not None
    assert backend.record.recurrence_fingerprint == recurrence
    assert backend.record.completed is False


def test_independent_readback_detects_external_change(tmp_path: Path) -> None:
    backend = FakeBackend()
    subject = bridge(backend, tmp_path)
    document = proposal()
    executed = subject.execute(document, operation_id=EXECUTE_ID)
    assert backend.record is not None
    backend.record = replace(
        backend.record,
        title="Changed outside Warboard",
        last_modified_date="2026-07-28T16:02:00Z",
    )

    with pytest.raises(VerificationError, match="independent readback"):
        subject.readback(document, reference=executed["reference"])


def test_same_operation_id_cannot_bind_changed_proposal(tmp_path: Path) -> None:
    backend = FakeBackend()
    subject = bridge(backend, tmp_path)
    subject.execute(proposal(), operation_id=EXECUTE_ID)

    with pytest.raises(ConflictError, match="different Duffields proposal"):
        subject.execute(
            proposal(recurrence_fingerprint="changed-but-valid"),
            operation_id=EXECUTE_ID,
        )
    assert backend.mutations == 1


def test_adapter_state_is_private_and_contains_both_versions(tmp_path: Path) -> None:
    backend = FakeBackend()
    subject = bridge(backend, tmp_path)
    result = subject.execute(proposal(), operation_id=EXECUTE_ID)
    digest = result["reference"].removeprefix(complete.REFERENCE_PREFIX)
    path = tmp_path / "state" / "operations" / f"{digest}.json"
    document = json.loads(path.read_text(encoding="utf-8"))

    assert document["before"]["completed"] is False
    assert document["after"]["completed"] is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700


def test_cli_requires_live_gate_for_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = tmp_path / "request.json"
    response = tmp_path / "response.json"
    request.write_text(
        json.dumps({"proposal": proposal(), "operation_id": EXECUTE_ID}),
        encoding="utf-8",
    )
    os.chmod(request, 0o600)
    monkeypatch.delenv(complete.LIVE_ENV, raising=False)

    result = complete.main(
        [
            "execute",
            "--request",
            str(request),
            "--response",
            str(response),
            "--state-root",
            str(tmp_path / "state"),
        ]
    )

    assert result == 2
    document = json.loads(response.read_text(encoding="utf-8"))
    assert document["ok"] is False
    assert complete.LIVE_ENV in document["detail"]
