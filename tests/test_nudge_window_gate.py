"""Tests for nudge/engine.py::_in_any_active_window top-of-loop gate.

The gate skips gather_reminders() / get_events() when no tier is in an active
send window AND no event is imminent. Target: ~85% log reduction on the
288 runs/day schedule.
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "worktree_nudge_engine", _HERE / "nudge" / "engine.py"
)
_engine = importlib.util.module_from_spec(_spec)
sys.modules["worktree_nudge_engine"] = _engine
_spec.loader.exec_module(_engine)

_in_any_active_window = _engine._in_any_active_window


def _dt(weekday_label: str, hour: int, minute: int = 0) -> datetime:
    """Return a concrete datetime for a given weekday + time.

    weekday_label: monday..sunday (case-insensitive).
    """
    base = {
        "monday": datetime(2026, 4, 13, 0, 0),
        "tuesday": datetime(2026, 4, 14, 0, 0),
        "wednesday": datetime(2026, 4, 15, 0, 0),
        "thursday": datetime(2026, 4, 16, 0, 0),
        "friday": datetime(2026, 4, 17, 0, 0),
        "saturday": datetime(2026, 4, 18, 0, 0),
        "sunday": datetime(2026, 4, 19, 0, 0),
    }[weekday_label.lower()]
    return base.replace(hour=hour, minute=minute)


class TestActiveWindowGate:
    def test_sunday_7pm_week_ahead_window_open(self):
        assert _in_any_active_window(_dt("sunday", 19)) is True
        assert _in_any_active_window(_dt("sunday", 19, 30)) is True
        assert _in_any_active_window(_dt("sunday", 20, 0)) is True

    def test_week_ahead_does_not_open_on_other_days(self):
        # Sunday-only — Tuesday 7pm should still be day_before (6-8pm).
        # But Wednesday 3am must be closed.
        assert _in_any_active_window(_dt("wednesday", 3)) is False
        assert _in_any_active_window(_dt("tuesday", 19)) is True  # day_before

    def test_day_before_window_18_to_20(self):
        assert _in_any_active_window(_dt("monday", 18)) is True
        assert _in_any_active_window(_dt("monday", 19)) is True
        assert _in_any_active_window(_dt("monday", 20)) is True
        assert _in_any_active_window(_dt("monday", 17, 59)) is False
        assert _in_any_active_window(_dt("monday", 21)) is False

    def test_morning_preview_window_7_to_8(self):
        assert _in_any_active_window(_dt("thursday", 7)) is True
        assert _in_any_active_window(_dt("thursday", 8)) is True
        assert _in_any_active_window(_dt("thursday", 6, 59)) is False
        assert _in_any_active_window(_dt("thursday", 9)) is False

    def test_closed_hours_return_false(self):
        # 3 AM Wednesday — no tier is open, no events hint.
        assert _in_any_active_window(_dt("wednesday", 3)) is False
        # 1 PM Tuesday — between morning and day_before, no tier.
        assert _in_any_active_window(_dt("tuesday", 13)) is False
        # 10 PM Friday — past day_before window.
        assert _in_any_active_window(_dt("friday", 22)) is False

    def test_upcoming_events_hint_forces_open(self):
        # 3 AM Wednesday — closed unless upcoming_events=True.
        closed = _in_any_active_window(_dt("wednesday", 3))
        open_ = _in_any_active_window(_dt("wednesday", 3), upcoming_events=True)
        assert closed is False
        assert open_ is True

    def test_expected_skip_ratio_is_meaningful(self):
        """Sanity: run over every 5-min tick in a full week, confirm the
        gate would skip a large majority when no events are imminent."""
        total = 0
        skipped = 0
        for day in range(7):
            base = datetime(2026, 4, 13 + day, 0, 0)
            # 288 five-min ticks per day.
            for tick in range(288):
                t = base.replace(
                    hour=(tick * 5) // 60, minute=(tick * 5) % 60
                )
                total += 1
                if not _in_any_active_window(t):
                    skipped += 1
        # Windows open: day_before 18-20 inclusive (3h × 7d = 63h of 168),
        # morning 7-8 inclusive (2h × 7d = 14h of 168), Sun 19-20 subsumed
        # by day_before. Combined open hours ≈ 35/168 ≈ 20.8%, so
        # bare-gate skip ≈ 79%. `_has_upcoming_events` adds the rest of
        # the headline "~85% log reduction" in the real system. Here we
        # just lock in that the gate itself drops >75%.
        ratio = skipped / total
        assert ratio > 0.75, f"skip ratio only {ratio:.2%}"
