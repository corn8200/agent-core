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
    """Structural poka-yoke. AST-scan the engine for `portal_url("/work")` and
    `portal_url("/home")` calls. The only allowed sites are the two top-level
    constant assignments. Anywhere else = copy-paste regression.

    Quote style does not matter (single, double, triple) because the AST
    represents string literals as values, not source text.
    """
    import ast
    text = ENGINE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(text)

    # Build parent map so we can identify enclosing assignment for each call.
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent  # type: ignore[attr-defined]

    allowed_targets = {
        ("_WORK_MEETING_NUDGE_URL", "/work"),
        ("_CALENDAR_NUDGE_URL", "/home"),
    }

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "portal_url"):
            continue
        if not (len(node.args) == 1 and isinstance(node.args[0], ast.Constant)):
            continue
        path = node.args[0].value
        if path not in {"/work", "/home"}:
            continue

        # Walk up to find the enclosing module-level assignment.
        cur = getattr(node, "parent", None)
        target_name: str | None = None
        while cur is not None:
            if isinstance(cur, ast.Assign) and len(cur.targets) == 1:
                tgt = cur.targets[0]
                if isinstance(tgt, ast.Name):
                    # Must be at module scope (parent of the Assign is Module).
                    if isinstance(getattr(cur, "parent", None), ast.Module):
                        target_name = tgt.id
                break
            cur = getattr(cur, "parent", None)

        if (target_name, path) not in allowed_targets:
            offenders.append(
                f"line {node.lineno}: portal_url({path!r}) — "
                f"enclosing assignment {target_name!r} not in allow-list"
            )

    assert not offenders, (
        "inline portal_url('/work' or '/home') found outside allowed "
        "constant assignments:\n  " + "\n  ".join(offenders)
    )


def test_only_work_meeting_branch_uses_work_url():
    """Confirm only the work-meeting branch references the work URL constant.

    AST-walks the parent chain from each `_WORK_MEETING_NUDGE_URL` reference
    to find the *innermost* enclosing FunctionDef. Robust against nested
    functions, decorators, and method scopes — not just flat module-level
    `def`s.
    """
    import ast
    text = ENGINE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(text)

    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent  # type: ignore[attr-defined]

    def innermost_func(node: ast.AST) -> str | None:
        cur = getattr(node, "parent", None)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur.name
            cur = getattr(cur, "parent", None)
        return None

    sites: list[tuple[int, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "_WORK_MEETING_NUDGE_URL":
            # Skip the constant's own LHS assignment.
            parent = getattr(node, "parent", None)
            if (
                isinstance(parent, ast.Assign)
                and len(parent.targets) == 1
                and parent.targets[0] is node
            ):
                continue
            sites.append((node.lineno, innermost_func(node)))

    assert sites, "expected at least one _WORK_MEETING_NUDGE_URL reference"
    for line_no, func_name in sites:
        assert func_name == "_send_work_meeting_nudges", (
            f"_WORK_MEETING_NUDGE_URL referenced on line {line_no} from "
            f"function {func_name!r} — only _send_work_meeting_nudges may "
            f"reference the work URL. Calendar branches must use "
            f"_CALENDAR_NUDGE_URL."
        )
