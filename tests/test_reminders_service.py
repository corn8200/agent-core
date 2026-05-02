from datetime import datetime

from core.reminders_service import parse_reminders_output


def _rec(*parts: str) -> str:
    return "\x1f".join(parts) + "\x1e"


def test_parse_reminders_preserves_work_meeting_prefix():
    raw = (
        _rec("📅 Mon 9 AM - Staff sync", "Work", "", "id-meeting", "", "0")
        + _rec("📋", "Work", "", "id-section", "", "0")
    )

    parsed = parse_reminders_output(
        raw,
        now=datetime(2026, 5, 1, 12, 0, 0),
        limit_undated=None,
    )

    names = [r["name"] for r in parsed["undated"]]
    assert "📅 Mon 9 AM - Staff sync" in names
    assert "📋" not in names


def test_parse_reminders_keeps_extended_fields():
    raw = _rec(
        "🔴 Finish slide deck",
        "Work",
        "2026-05-01T13:00:00Z",
        "id-1",
        "Use Q2 numbers",
        "1",
    )

    parsed = parse_reminders_output(raw, now=datetime(2026, 5, 1, 8, 0, 0))

    item = parsed["today"][0]
    assert item["id"] == "id-1"
    assert item["notes"] == "Use Q2 numbers"
    assert item["priority"] == 1
