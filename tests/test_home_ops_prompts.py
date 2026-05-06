from home_ops.prompts import _slim_work_context


def test_slim_work_context_includes_work_calendar_events():
    ctx = {
        "generated_at": "2026-05-01T12:00:00Z",
        "files": {"json": "/Users/johncornelius/logs/work-context.json"},
        "summary": {"upcoming_events": 1},
        "calendar": {
            "upcoming": [
                {"summary": "Work planning", "start": "2026-05-04T09:00:00"},
            ],
        },
        "work": {
            "reminders": [{"name": "🔴 Send update"}],
            "meeting_reminders": [],
            "priority_reminders": [{"name": "🔴 Send update"}],
        },
    }

    slim = _slim_work_context(ctx)

    assert slim["calendar_events"][0]["summary"] == "Work planning"
    assert slim["work_reminders"][0]["name"] == "🔴 Send update"
