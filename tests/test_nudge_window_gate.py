"""Tests for nudge/engine.py::_in_any_active_window top-of-loop gate.

The gate skips gather_reminders() / get_events() when no tier is in an active
send window AND no event is imminent. Target: ~85% log reduction on the
288 runs/day schedule.
"""

import importlib.util
import asyncio
import sys
from datetime import datetime
from types import SimpleNamespace
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "worktree_nudge_engine", _HERE / "nudge" / "engine.py"
)
_engine = importlib.util.module_from_spec(_spec)
sys.modules["worktree_nudge_engine"] = _engine
_spec.loader.exec_module(_engine)

_in_any_active_window = _engine._in_any_active_window
_has_upcoming_work_meeting = _engine._has_upcoming_work_meeting
_nudge_uid = _engine._nudge_uid
_work_context_lines = _engine._work_context_lines
_format_calendar_nudge = _engine._format_calendar_nudge
_pushover_device = _engine._pushover_device


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


def test_nudge_uid_includes_start_time_for_recurring_instances():
    first = SimpleNamespace(uid="same-recurring-id", summary="Weekly sync",
                            start=datetime(2026, 5, 4, 9, 0))
    second = SimpleNamespace(uid="same-recurring-id", summary="Weekly sync",
                             start=datetime(2026, 5, 11, 9, 0))

    assert _nudge_uid(first) != _nudge_uid(second)


def test_work_context_lines_include_work_meetings_and_priorities():
    ctx = {
        "calendar": {
            "upcoming": [
                {
                    "start": "2026-05-04T09:00:00",
                    "time": "9:00 AM",
                    "summary": "Work calendar event",
                    "bucket": "today",
                }
            ]
        },
        "work": {
            "meeting_reminders": [
                {
                    "name": "📅 MON 10AM - Staff sync",
                    "due": "2026-05-04T10:00:00Z",
                    "due_bucket": "today",
                }
            ],
            "priority_reminders": [
                {
                    "name": "🔴 Send weekly update",
                    "due": "2026-05-04T11:00:00Z",
                    "due_bucket": "today",
                    "priority_marker": "P1",
                }
            ],
        },
    }

    lines = _work_context_lines(ctx, target_date=datetime(2026, 5, 4).date())

    assert lines[0] == "Work:"
    assert any("Work calendar event" in line for line in lines)
    assert any("Staff sync" in line for line in lines)
    assert any("P1 - Send weekly update" in line for line in lines)


def test_work_context_lines_use_full_calendar_events_not_only_upcoming():
    ctx = {
        "calendar": {
            "upcoming": [],
            "events": [
                {
                    "start": "2026-05-04T16:30:00",
                    "time": "4:30 PM",
                    "summary": "Late Work handoff",
                    "bucket": "tomorrow",
                }
            ],
        },
        "work": {"meeting_reminders": [], "priority_reminders": []},
    }

    lines = _work_context_lines(ctx, target_date=datetime(2026, 5, 4).date())

    assert any("Late Work handoff" in line for line in lines)


def test_work_context_lines_skip_past_full_calendar_events():
    ctx = {
        "calendar": {
            "events": [
                {
                    "start": "2026-05-04T08:00:00",
                    "time": "8:00 AM",
                    "summary": "Already happened",
                    "bucket": "today",
                    "status": "past",
                },
                {
                    "start": "2026-05-04T16:30:00",
                    "time": "4:30 PM",
                    "summary": "Still ahead",
                    "bucket": "today",
                    "status": "upcoming",
                },
            ],
        },
        "work": {"meeting_reminders": [], "priority_reminders": []},
    }

    lines = _work_context_lines(ctx, target_date=datetime(2026, 5, 4).date())

    assert not any("Already happened" in line for line in lines)
    assert any("Still ahead" in line for line in lines)


def test_has_upcoming_work_meeting_opens_gate_for_reminder_meetings():
    ctx = {
        "work": {
            "meeting_reminders": [
                {"name": "📅 Standup", "due": "2026-05-04T09:30:00"},
            ]
        }
    }

    assert _has_upcoming_work_meeting(ctx, datetime(2026, 5, 4, 9, 0), minutes=60)
    assert not _has_upcoming_work_meeting(ctx, datetime(2026, 5, 4, 7, 0), minutes=60)


def test_day_before_nudge_fires_when_work_context_is_only_signal(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 5, 3, 18, 30)

    sent = []
    logged = []
    ctx = {
        "summary": {"upcoming_events": 1},
        "calendar": {
            "upcoming": [],
            "events": [
                {
                    "start": "2026-05-04T16:30:00",
                    "time": "4:30 PM",
                    "summary": "Late Work handoff",
                    "bucket": "tomorrow",
                }
            ],
        },
        "work": {"meeting_reminders": [], "priority_reminders": []},
    }

    async def fake_refresh_work_context():
        return ctx

    async def fake_get_events(*_args, **_kwargs):
        return []

    async def fake_gather_reminders():
        return {}

    async def fake_send_nudge(message, dry_run=False, **kwargs):
        sent.append((message, kwargs))
        return True

    monkeypatch.setattr(_engine, "datetime", FrozenDateTime)
    monkeypatch.setattr(_engine, "refresh_work_context", fake_refresh_work_context)
    monkeypatch.setattr(_engine, "get_events", fake_get_events)
    monkeypatch.setattr(_engine, "gather_reminders", fake_gather_reminders)
    monkeypatch.setattr(_engine, "already_sent", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(_engine, "log_nudge", lambda *args, **_kwargs: logged.append(args))
    monkeypatch.setattr(_engine, "_send_nudge", fake_send_nudge)

    asyncio.run(_engine.run_nudges(dry_run=False))

    assert any("Late Work handoff" in message for message, _ in sent)
    assert any(kwargs.get("tier") == "day_before" for _, kwargs in sent)
    assert any(args[3] == "day_before" for args in logged)


def test_format_calendar_nudge_includes_actionable_context():
    event = SimpleNamespace(
        summary="Planning sync",
        start=datetime(2026, 5, 4, 9, 0),
        calendar="Work",
        location="Room 4",
        notes="Bring Q2 numbers",
    )

    body = _format_calendar_nudge(event, minutes_away=4)

    assert "9:00 AM - Planning sync" in body
    assert "Calendar: Work" in body
    assert "Where: Room 4" in body
    assert "Notes: Bring Q2 numbers" in body
    assert "Move now." in body


def test_send_nudge_prefers_pushover(monkeypatch):
    calls = []

    async def fake_push(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=True, detail="sent")

    async def fake_fallback(*_args, **_kwargs):
        raise AssertionError("iMessage fallback should not run when Pushover succeeds")

    monkeypatch.delenv("NUDGE_DELIVERY", raising=False)
    monkeypatch.setattr(_engine, "_send_pushover_notification", fake_push)
    monkeypatch.setattr(_engine, "_send_imessage_fallback", fake_fallback)

    asyncio.run(_engine._send_nudge("Body", tier="five_min", title="5 min: Sync"))

    assert calls == [{
        "title": "5 min: Sync",
        "message": "Body",
        "priority": 1,
        "sound": "persistent",
    }]


def test_send_nudge_falls_back_to_imessage_when_pushover_fails(monkeypatch):
    fallback = []

    async def fake_push(**_kwargs):
        return SimpleNamespace(ok=False, detail="no creds")

    async def fake_fallback(title, message):
        fallback.append((title, message))
        return True, "fallback sent"

    monkeypatch.delenv("NUDGE_DELIVERY", raising=False)
    monkeypatch.setattr(_engine, "_send_pushover_notification", fake_push)
    monkeypatch.setattr(_engine, "_send_imessage_fallback", fake_fallback)

    asyncio.run(_engine._send_nudge("Body", tier="day_before"))

    assert fallback == [("Tomorrow prep", "Body")]


def test_send_nudge_dry_run_does_not_claim_delivery(monkeypatch):
    async def fake_push(**_kwargs):
        raise AssertionError("dry-run should not call Pushover")

    monkeypatch.setattr(_engine, "_send_pushover_notification", fake_push)

    delivered = asyncio.run(_engine._send_nudge("Body", dry_run=True, tier="day_before"))

    assert delivered is False


def test_send_nudge_both_logs_success_if_pushover_sent_even_when_mirror_fails(monkeypatch):
    async def fake_push(**_kwargs):
        return SimpleNamespace(ok=True, detail="sent")

    async def fake_fallback(*_args, **_kwargs):
        return False, "mirror failed"

    monkeypatch.setenv("NUDGE_DELIVERY", "both")
    monkeypatch.setattr(_engine, "_send_pushover_notification", fake_push)
    monkeypatch.setattr(_engine, "_send_imessage_fallback", fake_fallback)

    delivered = asyncio.run(_engine._send_nudge("Body", tier="five_min"))

    assert delivered is True


def test_pushover_device_defaults_to_iphone_and_can_target_all(monkeypatch):
    monkeypatch.delenv("NUDGE_PUSHOVER_DEVICE", raising=False)
    assert _pushover_device() == "iPhone"

    monkeypatch.setenv("NUDGE_PUSHOVER_DEVICE", "all")
    assert _pushover_device() is None
