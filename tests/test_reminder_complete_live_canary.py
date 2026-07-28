from __future__ import annotations

import os

import pytest

from core.reminder_complete_canary import LIVE_CONFIRMATION, run_canary


@pytest.mark.skipif(
    os.environ.get("REMINDER_COMPLETE_CANARY_LIVE") != LIVE_CONFIRMATION,
    reason="live reminder completion canary is not enabled",
)
def test_live_reminder_completion_and_cleanup() -> None:
    result = run_canary()

    assert result["ok"] is True
    assert result["status"] == "REMINDER_COMPLETE_CANARY_PASS"
    assert result["production_items_remaining"] == 0
    assert [entry["recurring"] for entry in result["results"]] == [False, True]
    for entry in result["results"]:
        assert entry["execute_status"] == "completed"
        assert entry["execute_replay"] is True
        assert entry["readback_status"] == "completed"
        assert entry["undo_status"] == "restored_incomplete"
        assert entry["undo_readback_status"] == "restored_incomplete"
        assert entry["cleanup_verified"] is True
