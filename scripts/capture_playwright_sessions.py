#!/usr/bin/env python3
"""Interactive helper — one command to capture all 6 Playwright site sessions.

Run this once:
    ~/Projects/agent-core/.venv/bin/python ~/Projects/agent-core/scripts/capture_playwright_sessions.py

For each site in SITES, opens a HEADED WebKit browser at the login URL,
waits for you to log in + press Enter, then dumps cookies + localStorage
to ~/.config/playwright-states/<site>.json. Skip individual sites by
passing their names as args: `... capture_playwright_sessions.py linkedin github`

Already-captured sessions get a "keep/recapture" prompt rather than being
overwritten automatically.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.browser import capture_session, list_sites, forget_site, STATES_DIR


SITES: dict[str, str] = {
    "linkedin": "https://www.linkedin.com/login",
    "gmail":    "https://mail.google.com/",
    "icims":    "https://www.icims.com/candidate-login",
    "github":   "https://github.com/login",
    "workday":  "https://ferguson.wd5.myworkdayjobs.com/FergusonCareers/login",
    "monarch":  "https://app.monarchmoney.com/login",
}


def banner(text: str) -> None:
    line = "=" * 68
    print(f"\n{line}\n  {text}\n{line}")


async def capture_one(name: str, url: str, already_have: set[str]) -> bool:
    if name in already_have:
        resp = input(
            f"\n[{name}] session already exists at {STATES_DIR / f'{name}.json'}.\n"
            f"[{name}] keep existing (Enter) or recapture (r)? "
        ).strip().lower()
        if resp != "r":
            print(f"[{name}] keeping existing session.")
            return True
        forget_site(name)

    banner(f"{name.upper()} — {url}")
    print(f"[{name}] Launching headed WebKit browser...")
    print(f"[{name}] Log in as usual, complete any 2FA, get to your home/feed page.")
    print(f"[{name}] Then return to THIS terminal and press Enter.")
    try:
        path = await capture_session(name, url, browser_kind="webkit")
        print(f"[{name}] ✓ saved {path}")
        return True
    except Exception as exc:
        print(f"[{name}] ✗ failed: {exc}")
        return False


async def main() -> int:
    targets = sys.argv[1:] or list(SITES.keys())
    missing = [t for t in targets if t not in SITES]
    if missing:
        print(f"Unknown site(s): {', '.join(missing)}")
        print(f"Known: {', '.join(SITES)}")
        return 2

    already = set(list_sites())
    banner("Playwright session capture")
    print(f"Captured so far: {sorted(already) or '(none)'}")
    print(f"Targets this run: {targets}")
    print("Browser opens HEADED. You log in → press Enter → state saves. Then next site.")

    results: dict[str, bool] = {}
    for name in targets:
        results[name] = await capture_one(name, SITES[name], already)

    banner("Summary")
    for name, ok in results.items():
        print(f"  {'✓' if ok else '✗'} {name}")
    failed = [n for n, ok in results.items() if not ok]
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\naborted")
        sys.exit(130)
