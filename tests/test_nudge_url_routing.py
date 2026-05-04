"""Regression guard: nudge URL routing.

Calendar/summary nudges (week_ahead, day_before, morning_preview, calendar
fifteen/five-min) MUST link to /home. Only Apple-Reminders work-meeting
nudges (`_send_work_meeting_nudges`) link to /work. Filed 2026-05-04 after
a Family-calendar HVAC appointment ("Mcrea heating and air") fired a Pushover
with an "Open work" link pointing at the work queue.

This file is the structural guard. If a future refactor copy-pastes a
`portal_url("/work")` or `portal_url("/home")` literal into engine.py
outside the constant initializations, the inline-literal test fails.
"""
from pathlib import Path

import pytest

ENGINE_PATH = Path(__file__).resolve().parent.parent / "nudge" / "engine.py"


def _load_engine():
    """Import nudge.engine. The engine has heavy import-time side effects
    (Apple Calendar / work-context); skip if any dep is missing."""
    import sys
    sys.path.insert(0, str(ENGINE_PATH.parent.parent))
    try:
        from nudge import engine  # type: ignore
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"nudge.engine import failed (env-specific): {exc!r}")
    return engine


def test_url_constants_are_distinct_and_correct():
    engine = _load_engine()
    assert engine._WORK_MEETING_NUDGE_URL.endswith("/work"), engine._WORK_MEETING_NUDGE_URL
    assert engine._CALENDAR_NUDGE_URL.endswith("/home"), engine._CALENDAR_NUDGE_URL
    assert engine._WORK_MEETING_NUDGE_URL != engine._CALENDAR_NUDGE_URL
    assert engine._WORK_MEETING_NUDGE_URL_TITLE == "Open work"
    assert engine._CALENDAR_NUDGE_URL_TITLE == "Open cockpit"


def test_no_inline_portal_url_work_or_home_outside_constants():
    """Structural poka-yoke. Strip the two constant initializations from the
    source text; if anything else still references portal_url("/work") or
    portal_url("/home"), a copy-paste regression has happened."""
    text = ENGINE_PATH.read_text(encoding="utf-8")

    # Allowed: the two constant init lines exactly once each.
    work_init = '_WORK_MEETING_NUDGE_URL = portal_url("/work")'
    home_init = '_CALENDAR_NUDGE_URL = portal_url("/home")'
    assert text.count(work_init) == 1, (
        "expected exactly one _WORK_MEETING_NUDGE_URL = portal_url(\"/work\") line"
    )
    assert text.count(home_init) == 1, (
        "expected exactly one _CALENDAR_NUDGE_URL = portal_url(\"/home\") line"
    )

    stripped = text.replace(work_init, "", 1).replace(home_init, "", 1)
    assert 'portal_url("/work")' not in stripped, (
        'Inline portal_url("/work") found in nudge/engine.py — '
        'use _WORK_MEETING_NUDGE_URL instead.'
    )
    assert 'portal_url("/home")' not in stripped, (
        'Inline portal_url("/home") found in nudge/engine.py — '
        'use _CALENDAR_NUDGE_URL instead.'
    )


def test_only_work_meeting_branch_uses_work_url():
    """Confirm only the work-meeting branch references the work URL constant.

    Uses AST enclosing-function detection — if a future refactor moves a
    work URL into a calendar branch (`_send_calendar_nudges`, week_ahead,
    etc.), this test fails loud."""
    import ast
    text = ENGINE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(text)

    def enclosing_func(target_line: int) -> str | None:
        best: tuple[int, str] | None = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.lineno <= target_line and target_line <= (node.end_lineno or 0):
                    if best is None or node.lineno > best[0]:
                        best = (node.lineno, node.name)
        return best[1] if best else None

    sites = [
        i + 1
        for i, ln in enumerate(text.splitlines())
        if "url=_WORK_MEETING_NUDGE_URL" in ln
    ]
    assert sites, "expected at least one url=_WORK_MEETING_NUDGE_URL site"
    for line_no in sites:
        func = enclosing_func(line_no)
        assert func == "_send_work_meeting_nudges", (
            f"url=_WORK_MEETING_NUDGE_URL on line {line_no} is inside "
            f"{func!r} — only _send_work_meeting_nudges may reference the "
            f"work URL. Calendar branches must use _CALENDAR_NUDGE_URL."
        )
