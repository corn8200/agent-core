"""Persistent Playwright browser sessions for agentic workflows.

Agentic code gets its own browser (WebKit by default) with cookies and local
storage persisted per-site under ~/.config/playwright-states/<site>.json.

Usage:
    from core.browser import with_site

    async with with_site("linkedin") as (context, page):
        await page.goto("https://www.linkedin.com/feed/")
        # session state auto-saves on exit

First-time login (run once, headed):
    from core.browser import capture_session
    await capture_session("linkedin", "https://www.linkedin.com/login")

This sits alongside — not inside — Safari. Safari is John's real human browser,
untouched. This module is for automation that needs to log in and stay logged in
without asking the user every run.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

STATES_DIR = Path.home() / ".config" / "playwright-states"
PROFILE_DIR = Path.home() / ".config" / "playwright-profile"


def _state_path(site_name: str) -> Path:
    STATES_DIR.mkdir(parents=True, exist_ok=True)
    return STATES_DIR / f"{site_name}.json"


async def _launch_browser(
    p: Playwright,
    *,
    browser_kind: str = "webkit",
    headless: bool = True,
) -> Browser:
    launcher = getattr(p, browser_kind)
    return await launcher.launch(headless=headless)


async def get_context(
    site_name: str,
    *,
    browser_kind: str = "webkit",
    headless: bool = True,
) -> tuple[Playwright, Browser, BrowserContext]:
    """Launch browser + return context loaded with saved state for `site_name`.

    Caller is responsible for cleanup: await context.close(); await browser.close();
    await p.stop(). Prefer `with_site()` which handles that.
    """
    p = await async_playwright().start()
    browser = await _launch_browser(p, browser_kind=browser_kind, headless=headless)
    state_file = _state_path(site_name)
    context_kwargs: dict = {}
    if state_file.exists():
        context_kwargs["storage_state"] = str(state_file)
    context = await browser.new_context(**context_kwargs)
    return p, browser, context


async def save_state(context: BrowserContext, site_name: str) -> Path:
    """Dump cookies + localStorage for this site. Called automatically by with_site."""
    state_file = _state_path(site_name)
    await context.storage_state(path=str(state_file))
    return state_file


@asynccontextmanager
async def with_site(
    site_name: str,
    *,
    browser_kind: str = "webkit",
    headless: bool = True,
) -> AsyncIterator[tuple[BrowserContext, Page]]:
    """Async context manager. Yields (context, page) with saved state loaded,
    auto-saves state on exit.

        async with with_site("linkedin") as (ctx, page):
            await page.goto("https://www.linkedin.com/feed/")
    """
    p, browser, context = await get_context(
        site_name, browser_kind=browser_kind, headless=headless
    )
    try:
        page = await context.new_page()
        yield context, page
    finally:
        try:
            await save_state(context, site_name)
        except Exception as exc:
            print(f"[browser] save_state({site_name}) failed: {exc}")
        await context.close()
        await browser.close()
        await p.stop()


async def capture_session(
    site_name: str,
    login_url: str,
    *,
    browser_kind: str = "webkit",
    done_url_contains: str | None = None,
) -> Path:
    """Launch a HEADED browser so a human can log in manually, then save state.

    Blocks until the user presses Enter in the terminal (or the URL changes to
    contain `done_url_contains` if provided — not yet implemented). Useful for
    one-time initial session capture.
    """
    p = await async_playwright().start()
    browser = await _launch_browser(p, browser_kind=browser_kind, headless=False)
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(login_url)

    print(f"\n[capture_session] Browser is open at {login_url}")
    print(f"[capture_session] Log in as needed, then press Enter in this terminal "
          f"to save session state for '{site_name}'...")
    try:
        input()
    except EOFError:
        pass

    state_file = await save_state(context, site_name)
    print(f"[capture_session] Saved {state_file}")

    await context.close()
    await browser.close()
    await p.stop()
    return state_file


def list_sites() -> list[str]:
    """Return names of all sites with saved state."""
    if not STATES_DIR.exists():
        return []
    return sorted(p.stem for p in STATES_DIR.glob("*.json"))


def forget_site(site_name: str) -> bool:
    """Delete saved state for a site. Returns True if a file was removed."""
    state_file = _state_path(site_name)
    if state_file.exists():
        state_file.unlink()
        return True
    return False


__all__ = [
    "with_site",
    "get_context",
    "save_state",
    "capture_session",
    "list_sites",
    "forget_site",
    "STATES_DIR",
    "PROFILE_DIR",
]
