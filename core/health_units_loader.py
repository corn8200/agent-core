"""CLI loader for the canonical health_units.yaml registry (#664).

Used by agent-core/core/vps_gather.sh and any other shell consumer that
needs the active unit list. Outputs newline-separated unit names so bash
for-loops can iterate trivially.

Usage:
  python3 ~/Projects/agent-core/core/health_units_loader.py [--surface SURFACE]

Default surface is 'vps-gather'. Pass --json for the full structured form
(name + owner + description + surfaces).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REGISTRY_PATH = Path.home() / "claude-config" / "services" / "health_units.yaml"


def _load() -> dict:
    if not _REGISTRY_PATH.exists():
        return {"active": [], "retired": []}
    # PyYAML may not be installed everywhere; fall back to a tiny stdlib parser
    # for the simple shape we control.
    try:
        import yaml  # type: ignore
        return yaml.safe_load(_REGISTRY_PATH.read_text()) or {"active": [], "retired": []}
    except ImportError:
        return _parse_yaml_minimal(_REGISTRY_PATH.read_text())


def _parse_yaml_minimal(text: str) -> dict:
    """Stdlib parser for the narrow YAML shape we use here.

    Handles top-level keys 'active' and 'retired', each a list of dicts
    with name / surfaces / owner / description / retired_after / retired_reason.
    Not a general YAML parser — only what health_units.yaml uses.
    """
    out: dict = {"active": [], "retired": []}
    section: str | None = None
    item: dict | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line in ("active:", "retired:"):
            section = line[:-1]
            item = None
            continue
        if section is None:
            continue
        # New item starts with "  - name: foo"
        if line.lstrip().startswith("- "):
            stripped = line.lstrip()[2:]
            item = {}
            out[section].append(item)
            line = "  " + stripped
        if item is None:
            continue
        s = line.lstrip()
        if ":" not in s:
            continue
        k, _, v = s.partition(":")
        v = v.strip()
        # surfaces: [a, b]  →  ["a","b"]
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            item[k.strip()] = [x.strip() for x in inner.split(",") if x.strip()]
        else:
            item[k.strip()] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--surface", default="vps-gather",
                    help="surface tag to filter by (cp-api | vps-gather)")
    ap.add_argument("--json", action="store_true",
                    help="output structured JSON instead of newline-separated names")
    args = ap.parse_args()

    data = _load()
    active = data.get("active") or []
    matches = [
        u for u in active
        if isinstance(u, dict)
        and u.get("name")
        and args.surface in (u.get("surfaces") or [])
    ]

    if args.json:
        print(json.dumps(matches, indent=2))
    else:
        for u in matches:
            print(u["name"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
