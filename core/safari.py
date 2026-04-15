"""Interactive Safari control — osascript + JavaScript injection.

For John's "fucking around" mode: he's actively using Safari, Claude reads
what's on screen, clicks things, fills fields. NOT for headless automation —
use `core.browser` (Playwright WebKit) for that.

Requirements (one-time setup, done in Phase 0):
  1. Safari → Settings → Advanced → "Show features for web developers" ✓
  2. Safari → Develop menu → "Allow JavaScript from Apple Events" ✓

Hard limits:
  - Can't interact with cross-origin iframes from injected JS.
  - Bot detection on some sites (LinkedIn, Google) will flag automated clicks.
  - `do JavaScript` returns a string — complex objects need JSON.stringify.
  - Safari must be running and have at least one window open.

For anything that needs persistent cookies or bot-detection bypass, fall
back to Playwright WebKit via `core.browser.with_site()`.
"""
from __future__ import annotations

import asyncio
import json
import shlex
from typing import Any

OSASCRIPT_TIMEOUT = 15


async def _osascript(script: str, *, timeout: float = OSASCRIPT_TIMEOUT) -> str:
    """Run an AppleScript snippet and return stdout. Raises RuntimeError on error."""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"osascript timed out after {timeout}s")
    if proc.returncode != 0:
        raise RuntimeError(f"osascript error: {stderr.decode().strip()}")
    return stdout.decode().rstrip("\n")


def _js_script(js: str, *, window_idx: int = 1, tab_idx: int | None = None) -> str:
    """Build an AppleScript that executes `js` in the chosen tab."""
    if tab_idx is None:
        tab_ref = f"current tab of window {window_idx}"
    else:
        tab_ref = f"tab {tab_idx} of window {window_idx}"
    js_escaped = js.replace("\\", "\\\\").replace('"', '\\"')
    return f'tell application "Safari" to do JavaScript "{js_escaped}" in {tab_ref}'


async def safari_activate() -> None:
    """Bring Safari to the foreground. Required before some JS operations."""
    await _osascript('tell application "Safari" to activate')


async def safari_is_running() -> bool:
    """True if Safari.app is currently running."""
    result = await _osascript('''tell application "System Events"
        return (name of processes) contains "Safari"
    end tell''')
    return result.strip().lower() == "true"


async def safari_ensure_window(fallback_url: str = "about:blank") -> None:
    """Make sure Safari is running and has at least one window open.

    Safari sometimes runs without any visible windows; every helper that
    references `window 1` will raise `-1719 Invalid index` in that state.
    Call this at the start of any flow that expects window 1 to exist.
    """
    await _osascript(f'''tell application "Safari"
        activate
        if (count of windows) = 0 then
            make new document with properties {{URL:"{fallback_url}"}}
        end if
    end tell''')


async def safari_current_url(window_idx: int = 1) -> str:
    """Return the URL of the active tab in the given window."""
    return await _osascript(
        f'tell application "Safari" to return URL of current tab of window {window_idx}'
    )


async def safari_current_title(window_idx: int = 1) -> str:
    """Return the title of the active tab."""
    return await _osascript(
        f'tell application "Safari" to return name of current tab of window {window_idx}'
    )


async def safari_tab_list() -> list[dict[str, Any]]:
    """Return all tabs across all windows as a flat list.

    Each entry: {window_idx, tab_idx, title, url}
    """
    script = '''set outputList to {}
tell application "Safari"
    set winCount to count of windows
    repeat with w from 1 to winCount
        try
            set tabCount to count of tabs of window w
            repeat with t from 1 to tabCount
                try
                    set tabTitle to name of tab t of window w
                    set tabURL to URL of tab t of window w
                    set end of outputList to ((w as string) & "\\t" & (t as string) & "\\t" & tabTitle & "\\t" & tabURL)
                end try
            end repeat
        end try
    end repeat
end tell
set AppleScript's text item delimiters to linefeed
return outputList as string'''
    raw = await _osascript(script)
    tabs: list[dict[str, Any]] = []
    for line in raw.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            w, t, title, url = parts
            tabs.append({
                "window_idx": int(w),
                "tab_idx": int(t),
                "title": title,
                "url": url,
            })
    return tabs


async def safari_page_text(
    window_idx: int = 1,
    tab_idx: int | None = None,
) -> str:
    """Return document.body.innerText of the chosen tab (active if tab_idx None)."""
    return await _osascript(
        _js_script("document.body.innerText", window_idx=window_idx, tab_idx=tab_idx)
    )


async def safari_page_html(
    window_idx: int = 1,
    tab_idx: int | None = None,
) -> str:
    """Return document.documentElement.outerHTML of the chosen tab."""
    return await _osascript(
        _js_script(
            "document.documentElement.outerHTML",
            window_idx=window_idx,
            tab_idx=tab_idx,
        )
    )


async def safari_selected_text(
    window_idx: int = 1,
    tab_idx: int | None = None,
) -> str:
    """Return currently selected text, if any."""
    return await _osascript(
        _js_script(
            "document.getSelection().toString()",
            window_idx=window_idx,
            tab_idx=tab_idx,
        )
    )


async def safari_eval(
    js: str,
    *,
    window_idx: int = 1,
    tab_idx: int | None = None,
) -> str:
    """Execute arbitrary JavaScript in the tab and return the result as a string.

    For complex return values, have the JS call JSON.stringify(...) itself:

        data = await safari_eval("JSON.stringify({url: location.href, h: document.title})")
        parsed = json.loads(data)
    """
    return await _osascript(_js_script(js, window_idx=window_idx, tab_idx=tab_idx))


async def safari_click_by_text(
    link_text: str,
    *,
    window_idx: int = 1,
    tab_idx: int | None = None,
) -> bool:
    """Click the first link or button whose innerText contains `link_text`.

    Returns True if an element was found and clicked, False if nothing matched.
    """
    needle = link_text.replace("\\", "\\\\").replace("'", "\\'")
    js = (
        "(function(){"
        "var needle = '" + needle + "';"
        "var els = Array.from(document.querySelectorAll('a,button,[role=button],[role=link]'));"
        "var el = els.find(function(e){return (e.innerText||'').indexOf(needle) !== -1;});"
        "if (el) { el.click(); return 'clicked'; }"
        "return 'not_found';"
        "})()"
    )
    result = await _osascript(
        _js_script(js, window_idx=window_idx, tab_idx=tab_idx)
    )
    return result.strip() == "clicked"


async def safari_fill(
    selector: str,
    value: str,
    *,
    window_idx: int = 1,
    tab_idx: int | None = None,
) -> bool:
    """Fill an <input>/<textarea> matched by CSS selector.

    Fires `input` and `change` events so React/Vue/etc controlled components
    see the update. Returns True if the element was found.

    NOTE: value can come from vault via `get_secret("SOMETHING")`. This string
    is embedded into a JS string literal — we escape backslash, single-quote,
    and newlines; hostile content shouldn't reach here, but this is not a
    sandbox.
    """
    sel = selector.replace("\\", "\\\\").replace("'", "\\'")
    val_clean = (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "")
    )
    js = (
        "(function(){"
        "var el = document.querySelector('" + sel + "');"
        "if (!el) return 'not_found';"
        "var proto = Object.getPrototypeOf(el);"
        "var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;"
        "setter.call(el, '" + val_clean + "');"
        "el.dispatchEvent(new Event('input', {bubbles:true}));"
        "el.dispatchEvent(new Event('change', {bubbles:true}));"
        "return 'filled';"
        "})()"
    )
    result = await _osascript(
        _js_script(js, window_idx=window_idx, tab_idx=tab_idx)
    )
    return result.strip() == "filled"


async def safari_new_tab(url: str) -> None:
    """Open a URL in a new Safari tab (front window)."""
    url_escaped = url.replace('"', '\\"')
    await _osascript(
        f'tell application "Safari" to tell window 1 to set current tab to (make new tab with properties {{URL:"{url_escaped}"}})'
    )


async def safari_navigate(
    url: str,
    *,
    window_idx: int = 1,
    tab_idx: int | None = None,
) -> None:
    """Navigate the current (or specified) tab to a URL."""
    url_escaped = url.replace('"', '\\"')
    if tab_idx is None:
        script = f'tell application "Safari" to set URL of current tab of window {window_idx} to "{url_escaped}"'
    else:
        script = f'tell application "Safari" to set URL of tab {tab_idx} of window {window_idx} to "{url_escaped}"'
    await _osascript(script)


async def safari_wait_for_url(
    url_contains: str,
    *,
    timeout: float = 30,
    poll: float = 0.5,
    window_idx: int = 1,
) -> bool:
    """Poll until the active tab URL contains `url_contains`, or timeout."""
    elapsed = 0.0
    while elapsed < timeout:
        try:
            url = await safari_current_url(window_idx=window_idx)
            if url_contains in url:
                return True
        except Exception:
            pass
        await asyncio.sleep(poll)
        elapsed += poll
    return False


async def safari_screenshot(output_path: str) -> str:
    """Screenshot Safari's frontmost window to `output_path` (PNG).

    Uses `screencapture -l <windowID>` via osascript lookup of the window ID.
    Returns the path on success, empty string if Safari isn't frontmost.
    """
    script = '''tell application "System Events" to tell process "Safari"
        try
            return id of window 1
        on error
            return "0"
        end try
    end tell'''
    wid = await _osascript(script)
    wid = wid.strip()
    if not wid or wid == "0":
        return ""
    safe_path = shlex.quote(output_path)
    proc = await asyncio.create_subprocess_shell(
        f"screencapture -l {wid} {safe_path}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    if proc.returncode != 0:
        return ""
    return output_path


async def safari_dump_state(
    *,
    window_idx: int = 1,
    tab_idx: int | None = None,
) -> dict[str, Any]:
    """One-shot snapshot: URL, title, selected text, visible text (truncated).

    Useful when Claude wants a compact picture of what the user is looking at.
    """
    url = await safari_current_url(window_idx=window_idx)
    title = await safari_current_title(window_idx=window_idx)
    selected = await safari_selected_text(window_idx=window_idx, tab_idx=tab_idx)
    text = await safari_page_text(window_idx=window_idx, tab_idx=tab_idx)
    return {
        "url": url,
        "title": title,
        "selected": selected,
        "text": text[:4000],
        "text_len": len(text),
    }


__all__ = [
    "safari_activate",
    "safari_is_running",
    "safari_ensure_window",
    "safari_current_url",
    "safari_current_title",
    "safari_tab_list",
    "safari_page_text",
    "safari_page_html",
    "safari_selected_text",
    "safari_eval",
    "safari_click_by_text",
    "safari_fill",
    "safari_new_tab",
    "safari_navigate",
    "safari_wait_for_url",
    "safari_screenshot",
    "safari_dump_state",
]
