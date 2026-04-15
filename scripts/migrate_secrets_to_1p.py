#!/usr/bin/env python3
"""One-shot migration of ~/.config/secrets.env → 1Password MachineAuto vault.

Idempotent: skips keys that already exist in the vault. Safe to re-run.

Reads the write-capable service account token from /tmp/op-migration-token
(short-lived, 1-day expiration). Falls back to OP_SERVICE_ACCOUNT_TOKEN env
var if /tmp/op-migration-token is missing.

Hard skips: ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN (defense in depth — SDK
billing guard requires these never be populated).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.vault import _load_legacy  # noqa: E402

DANGEROUS = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
VAULT = "MachineAuto"
TOKEN_FILE = Path("/tmp/op-migration-token")


def load_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    env_token = os.environ.get("OP_SERVICE_ACCOUNT_TOKEN", "").strip()
    if env_token:
        return env_token
    sys.exit(f"ERROR: no token at {TOKEN_FILE} and OP_SERVICE_ACCOUNT_TOKEN unset")


def op_env(token: str) -> dict[str, str]:
    return {
        "OP_SERVICE_ACCOUNT_TOKEN": token,
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
    }


def existing_titles(token: str) -> set[str]:
    out = subprocess.run(
        ["op", "item", "list", "--vault", VAULT, "--format=json"],
        env=op_env(token), capture_output=True, text=True, timeout=20,
    )
    if out.returncode != 0:
        sys.exit(f"op item list failed: {out.stderr}")
    items = json.loads(out.stdout)
    return {it["title"] for it in items}


def create_item(token: str, key: str, value: str) -> tuple[bool, str]:
    out = subprocess.run(
        [
            "op", "item", "create",
            "--category=password",
            f"--title={key}",
            f"--vault={VAULT}",
            f"password={value}",
        ],
        env=op_env(token), capture_output=True, text=True, timeout=15,
    )
    if out.returncode == 0:
        return True, ""
    return False, out.stderr.strip()


def main() -> int:
    token = load_token()
    secrets = _load_legacy()
    if not secrets:
        sys.exit("ERROR: no secrets loaded from ~/.config/secrets.env")

    print(f"loaded {len(secrets)} keys from secrets.env")
    existing = existing_titles(token)
    print(f"existing in MachineAuto vault: {len(existing)}")

    to_migrate = {
        k: v for k, v in secrets.items()
        if k not in DANGEROUS and k not in existing and v
    }
    skipped_dangerous = [k for k in secrets if k in DANGEROUS]
    skipped_existing = [k for k in secrets if k in existing]
    skipped_empty = [k for k, v in secrets.items() if not v]

    print(f"migrating: {len(to_migrate)}")
    print(f"skipped (dangerous): {len(skipped_dangerous)} {skipped_dangerous}")
    print(f"skipped (already in vault): {len(skipped_existing)}")
    print(f"skipped (empty value): {len(skipped_empty)} {skipped_empty}")
    print("---")

    successes: list[str] = []
    failures: list[tuple[str, str]] = []

    for i, (key, value) in enumerate(sorted(to_migrate.items()), 1):
        ok, err = create_item(token, key, value)
        status = "OK" if ok else f"FAIL: {err[:80]}"
        print(f"[{i:2d}/{len(to_migrate)}] {key:38s} {status}")
        if ok:
            successes.append(key)
        else:
            failures.append((key, err))

    print("---")
    print(f"created: {len(successes)}")
    print(f"failed:  {len(failures)}")
    if failures:
        print("failures:")
        for k, e in failures:
            print(f"  {k}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
