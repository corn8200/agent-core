from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from core.work_context import _atomic_write, build_context


def test_work_context_classifies_work_prefixes_and_priorities():
    context = build_context(
        events=[
            {
                "summary": "Planning call",
                "start": "2026-05-01T15:00:00",
                "end": "2026-05-01T15:30:00",
                "calendar": "Work",
                "uid": "event-1",
            }
        ],
        reminders={
            "overdue": [],
            "today": [
                {
                    "name": "📅 Fri 4 PM - Vendor review",
                    "list": "Work",
                    "due": "2026-05-01T16:00:00Z",
                    "id": "rem-1",
                },
                {
                    "name": "🔴 Send weekly update",
                    "list": "Work",
                    "due": "2026-05-01T17:00:00Z",
                    "id": "rem-2",
                },
            ],
            "this_week": [],
            "later": [],
            "undated": [],
        },
        now=datetime(2026, 5, 1, 12, 0, 0),
    )

    assert context["summary"]["upcoming_events"] == 1
    assert context["summary"]["work_reminders"] == 2
    assert context["summary"]["meeting_reminders"] == 1
    assert context["summary"]["priority_reminders"] == 1
    assert context["work"]["meeting_reminders"][0]["kind"] == "meeting"
    assert context["work"]["priority_reminders"][0]["priority_marker"] == "P1"


def test_work_context_regeneration_drops_missing_events():
    first = build_context(
        events=[
            {
                "summary": "Canceled call",
                "start": "2026-05-01T15:00:00",
                "end": "2026-05-01T15:30:00",
                "calendar": "Work",
                "uid": "event-canceled",
            }
        ],
        reminders={"overdue": [], "today": [], "this_week": [], "later": [], "undated": []},
        now=datetime(2026, 5, 1, 12, 0, 0),
    )
    second = build_context(
        events=[],
        reminders={"overdue": [], "today": [], "this_week": [], "later": [], "undated": []},
        now=datetime(2026, 5, 1, 12, 5, 0),
    )

    assert first["calendar"]["events"][0]["id"] == "event-canceled"
    assert second["calendar"]["events"] == []


def test_work_context_excludes_non_work_calendar_and_reminders():
    context = build_context(
        events=[
            {
                "summary": "Family event",
                "start": "2026-05-01T15:00:00",
                "end": "2026-05-01T15:30:00",
                "calendar": "Family",
                "uid": "family-1",
            }
        ],
        reminders={
            "overdue": [],
            "today": [
                {"name": "Treat weeds", "list": "Home", "due": "2026-05-01T17:00:00Z"},
            ],
            "this_week": [],
            "later": [],
            "undated": [],
        },
        now=datetime(2026, 5, 1, 12, 0, 0),
    )

    assert context["calendar"]["events"] == []
    assert context["reminders"]["all"] == []
    assert context["summary"]["reminders"] == 0


def test_atomic_write_allows_overlapping_writers(tmp_path):
    target = tmp_path / "work-context.json"

    def write_once(i: int):
        _atomic_write(target, f'{{"writer": {i}}}')

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write_once, range(40)))

    assert target.read_text().startswith('{"writer": ')
    assert not list(tmp_path.glob(".*.tmp"))
