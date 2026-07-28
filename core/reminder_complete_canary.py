"""Self-cleaning live canary for exact Warboard reminder completion."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from .reminder_complete_command_adapter import (
    ReminderCompleteCommandBridge,
    ReminderSnapshot,
    SwiftEventKitCompleteBackend,
)
from .reminder_upsert import ReminderUpsertError, _digest


LIVE_CONFIRMATION = "I_UNDERSTAND_THIS_CREATES_AND_DELETES_CANARY_REMINDERS"


def _proposal(record: ReminderSnapshot, *, identifier: str, now: dt.datetime) -> dict[str, Any]:
    created = now.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    expires = (now + dt.timedelta(hours=1)).astimezone(dt.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    return {
        "proposal_version": "duffields-action-proposal/v1",
        "proposal_id": identifier,
        "created_at": created,
        "expires_at": expires,
        "decision_nonce": "canary" + "0" * 26,
        "source": {
            "run_id": f"warboard-reminder-canary/{now.date().isoformat()}",
            "item_id": identifier,
            "evidence": ["dedicated self-cleaning EventKit canary"],
        },
        "presentation": {
            "title": "Reminder completion canary",
            "summary": "Complete one dedicated canary reminder.",
            "consequence": "The canary restores and deletes the reminder.",
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
        "action": {
            "adapter": "reminders",
            "kind": "reminder.complete",
            "payload": {
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
            },
            "readback_required": True,
            "undo_supported": True,
            "undo_window_seconds": 900,
        },
        "decision_defaults": {"later_seconds": 86400},
        "choices": ["do_it", "later", "no"],
        "visibility": "inline",
    }


def _records(document: Mapping[str, Any]) -> list[ReminderSnapshot]:
    raw = document.get("records")
    if not isinstance(raw, list):
        raise RuntimeError("canary find response has no records")
    return [
        ReminderSnapshot.from_mapping(item)
        for item in raw
        if isinstance(item, Mapping)
    ]


def run_one(
    *,
    recurring: bool,
    backend: SwiftEventKitCompleteBackend,
    state_root: Path,
    now: dt.datetime,
) -> dict[str, Any]:
    token = uuid.uuid4().hex
    marker = f"[warboard-reminder-complete-canary:{token}]"
    proposal_id = f"reminder-complete-canary-{token}"
    created = False
    cleaned = False
    try:
        response = backend._call(
            "canary-create",
            {"marker": marker, "recurring": recurring},
        )
        raw_record = response.get("record")
        if not isinstance(raw_record, Mapping):
            raise RuntimeError("canary create response has no reminder")
        before = ReminderSnapshot.from_mapping(raw_record)
        created = True
        if (before.recurrence_fingerprint != "none") is not recurring:
            raise RuntimeError("canary recurrence readback does not match the request")
        proposal = _proposal(before, identifier=proposal_id, now=now)
        bridge = ReminderCompleteCommandBridge(
            backend,
            state_root=state_root,
            clock=lambda: now.astimezone(dt.timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
        )
        executed = bridge.execute(
            proposal,
            operation_id=f"canary:execute:{token}",
        )
        readback = bridge.readback(proposal, reference=executed["reference"])
        replay = bridge.execute(
            proposal,
            operation_id=f"canary:execute:{token}",
        )
        undone = bridge.undo(
            proposal,
            reference=executed["reference"],
            operation_id=f"canary:undo:{token}",
        )
        undo_readback = bridge.readback_undo(
            proposal,
            reference=executed["reference"],
        )
        matches = _records(
            backend._call(
                "canary-find",
                {"marker": marker, "recurring": recurring},
            )
        )
        if len(matches) != 1:
            diagnostics = [
                {
                    "same_identifier": record.identifier == before.identifier,
                    "same_external_identifier": (
                        record.external_identifier == before.external_identifier
                    ),
                    "completed": record.completed,
                    "due_matches_original": record.due_at == before.due_at,
                    "recurrence_matches_original": (
                        record.recurrence_fingerprint
                        == before.recurrence_fingerprint
                    ),
                }
                for record in matches
            ]
            raise RuntimeError(
                "canary undo did not restore exactly one marker-bound reminder; "
                f"found {len(matches)} with structural states "
                f"{json.dumps(diagnostics, sort_keys=True)}"
            )
        restored = matches[0]
        if restored.identifier != before.identifier or restored.completed:
            raise RuntimeError("canary undo did not restore the exact reminder")
        deletion = backend._call(
            "canary-delete",
            {"marker": marker, "recurring": recurring},
        )
        cleaned = True
        if int(deletion.get("deleted") or 0) < 1:
            raise RuntimeError("canary cleanup deleted no reminder")
        remaining = _records(
            backend._call(
                "canary-find",
                {"marker": marker, "recurring": recurring},
            )
        )
        if remaining:
            raise RuntimeError("canary marker remains after cleanup")
        return {
            "ok": True,
            "recurring": recurring,
            "execute_status": executed["details"]["status"],
            "execute_replay": replay["details"]["replayed"],
            "readback_status": readback["observed"]["status"],
            "undo_status": undone["details"]["status"],
            "undo_readback_status": undo_readback["observed"]["status"],
            "identity_digest": _digest(
                {
                    "list_id": before.list_id,
                    "native_reminder_id": before.identifier,
                    "recurrence_fingerprint": before.recurrence_fingerprint,
                }
            ),
            "cleanup_verified": True,
        }
    finally:
        if created and not cleaned:
            try:
                backend._call(
                    "canary-delete",
                    {"marker": marker, "recurring": recurring},
                )
            except Exception:
                pass


def run_canary() -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    backend = SwiftEventKitCompleteBackend(allow_mutation=True, timeout=60)
    stale_cutoff = (now - dt.timedelta(minutes=10)).isoformat().replace(
        "+00:00", "Z"
    )
    stale_cleanup = backend._call(
        "canary-clean-stale",
        {"before": stale_cutoff},
    )
    with tempfile.TemporaryDirectory(
        prefix="warboard-reminder-complete-canary-"
    ) as temporary:
        root = Path(temporary)
        results = [
            run_one(
                recurring=False,
                backend=backend,
                state_root=root / "ordinary",
                now=now,
            ),
            run_one(
                recurring=True,
                backend=backend,
                state_root=root / "recurring",
                now=now,
            ),
        ]
    return {
        "ok": all(result["ok"] for result in results),
        "status": "REMINDER_COMPLETE_CANARY_PASS",
        "results": results,
        "stale_synthetic_cleaned": int(stale_cleanup.get("deleted") or 0),
        "production_items_remaining": 0,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-confirm", required=True)
    args = parser.parse_args(argv)
    if args.live_confirm != LIVE_CONFIRMATION:
        print(json.dumps({"ok": False, "error": "live confirmation is invalid"}))
        return 2
    try:
        result = run_canary()
    except (OSError, ValueError, RuntimeError, ReminderUpsertError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": type(exc).__name__,
                    "detail": str(exc)[:1000],
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
