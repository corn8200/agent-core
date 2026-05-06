#!/usr/bin/env python3
"""voice-memo-intake producer — 60s polling LaunchAgent.

60s POLLING only (NOT WatchPaths). Apple's bird daemon uses APFS clones
for iCloud sync — kqueue mtime is not reliably bumped for cross-device files.

Per tick:
1. List Recordings/ via tmux_relay_shell (TCC-protected)
2. Diff against seen.json
3. For each new .m4a: transcribe via apple-bridge, upload to R2, publish event
4. Mark seen ONLY after cp.event returns OK

PID-lock guard ensures at most one instance per tick.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

import httpx
import agent_cp_client as cp
from core.tools import tmux_relay_shell

AGENT = "voice-memo-intake"
BRIDGE_BASE = "http://100.122.35.56:8765"
RECORDINGS_PATH = "~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
CACHE_DIR = Path.home() / ".cache" / "voice-memo-intake"
SEEN_FILE = CACHE_DIR / "seen.json"
LOCK_FILE = CACHE_DIR / "lock.pid"
R2_PUBLIC_BASE = "https://pub-a5fc31bf3f0b42c69a2565c407a447cd.r2.dev"
R2_BUCKET = "audio-share"
TRANSCRIBE_POLL_INTERVAL = 5  # seconds
TRANSCRIBE_MAX_POLLS = 18  # 90s ceiling


def _load_read_token() -> str:
    token = os.environ.get("BRIDGE_READ_TOKEN", "")
    if token:
        return token
    secrets = Path.home() / ".config" / "secrets.env"
    if secrets.exists():
        for line in secrets.read_text().splitlines():
            if line.startswith("BRIDGE_READ_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def _load_r2_token() -> str:
    for key in ("CLOUDFLARE_R2_TOKEN", "CLOUDFLARE_API_TOKEN"):
        val = os.environ.get(key, "")
        if val:
            return val
    secrets = Path.home() / ".config" / "secrets.env"
    if secrets.exists():
        for line in secrets.read_text().splitlines():
            for key in ("CLOUDFLARE_R2_TOKEN", "CLOUDFLARE_API_TOKEN"):
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def _acquire_lock() -> bool:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)
            print(f"[voice-memo-intake] lock held by pid {pid}, exiting", file=sys.stderr)
            return False
        except (ProcessLookupError, OSError):
            LOCK_FILE.unlink(missing_ok=True)
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def _release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def _load_seen() -> dict[str, str]:
    try:
        return json.loads(SEEN_FILE.read_text())
    except Exception:
        return {}


def _save_seen(seen: dict[str, str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=2))


def _parse_ls_listing(output: str) -> list[dict]:
    files = []
    for line in output.strip().splitlines():
        # ls -lT format: permissions links user group size month day time year name
        # Example: -rw-r--r--  1 john staff  1024000 May  5 14:23:01 2026 Recording.m4a
        line = line.strip()
        if not line or line.startswith("total"):
            continue
        parts = line.split()
        if len(parts) < 9:
            continue
        name = parts[-1]
        if not name.endswith(".m4a"):
            continue
        # Try to parse size
        try:
            size = int(parts[4])
        except (IndexError, ValueError):
            size = 0
        files.append({"name": name, "size": size})
    return files


async def _transcribe(filename: str, token: str) -> str | None:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{BRIDGE_BASE}/voicememos/transcribe/{filename}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code != 200:
            print(f"[voice-memo-intake] transcribe start failed {resp.status_code}", file=sys.stderr)
            return None
        job_id = resp.json().get("job_id", "")
        if not job_id:
            return None

        for _ in range(TRANSCRIBE_MAX_POLLS):
            await asyncio.sleep(TRANSCRIBE_POLL_INTERVAL)
            status_resp = await client.get(
                f"{BRIDGE_BASE}/voicememos/transcribe/status/{job_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if status_resp.status_code != 200:
                continue
            data = status_resp.json()
            if data.get("status") == "done":
                return data.get("transcript", "")
            if data.get("status") == "error":
                print(f"[voice-memo-intake] transcription error: {data.get('error', '')}", file=sys.stderr)
                return None
        print(f"[voice-memo-intake] transcription timed out after {TRANSCRIBE_MAX_POLLS * TRANSCRIBE_POLL_INTERVAL}s", file=sys.stderr)
        return None


def _upload_r2(local_path: str, r2_key: str, r2_token: str) -> str | None:
    env = os.environ.copy()
    if r2_token:
        env["CLOUDFLARE_API_TOKEN"] = r2_token
    try:
        result = subprocess.run(
            [
                "wrangler", "r2", "object", "put",
                f"{R2_BUCKET}/{r2_key}",
                "--file", local_path,
                "--content-type", "audio/mp4",
                "--remote",
            ],
            capture_output=True, text=True, timeout=60,
            env=env,
        )
        if result.returncode == 0:
            return f"{R2_PUBLIC_BASE}/{r2_key}"
        print(f"[voice-memo-intake] R2 upload failed: {result.stderr[:200]}", file=sys.stderr)
    except Exception as exc:
        print(f"[voice-memo-intake] R2 upload error: {exc}", file=sys.stderr)
    return None


async def _process_file(filename: str, read_token: str, r2_token: str) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r2_key = f"voice-memos/{today}/{filename}"

    transcript = await _transcribe(filename, read_token)
    if transcript is None:
        print(f"[voice-memo-intake] skipping {filename} — transcription failed", file=sys.stderr)
        transcript = ""

    # Construct full path via relay for R2 upload
    full_path_expanded = f"{Path.home()}/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/{filename}"
    audio_url = _upload_r2(full_path_expanded, r2_key, r2_token) or ""

    payload = {
        "title": filename,
        "body": transcript[:500],
        "kind": "voice_memo_intake",
        "verbs": ["FILE", "LATER", "KILL", "NOTE"],
        "priority": 1,
        "dedup_key": f"voice-memo-intake:{filename}",
        "sources": ["voice_memo"],
        "metadata": {
            "filename": filename,
            "audio_url": audio_url,
            "full_transcript": transcript,
            "recorded_at": today,
        },
    }

    result = cp.event(AGENT, "voice_memo_intake", payload=payload)
    return result is not None


async def _run() -> None:
    read_token = _load_read_token()
    if not read_token:
        print("[voice-memo-intake] BRIDGE_READ_TOKEN not found", file=sys.stderr)
        sys.exit(1)

    r2_token = _load_r2_token()

    ok, output = await tmux_relay_shell(f"ls -lT {RECORDINGS_PATH}/", timeout=15.0)
    if not ok:
        print(f"[voice-memo-intake] relay failed: {output[:200]}", file=sys.stderr)
        return

    files = _parse_ls_listing(output)
    seen = _load_seen()
    new_seen = dict(seen)

    new_files = [f for f in files if f["name"] not in seen]
    print(f"[voice-memo-intake] {len(files)} files total, {len(new_files)} new", flush=True)

    for f in new_files:
        ok = await _process_file(f["name"], read_token, r2_token)
        if ok:
            new_seen[f["name"]] = datetime.now(timezone.utc).isoformat()
        # Only mark seen AFTER cp.event returns OK — crash mid-pipeline replays cleanly

    _save_seen(new_seen)
    published = len(new_files)
    print(f"[voice-memo-intake] published {published} events", flush=True)


def main() -> None:
    if not _acquire_lock():
        sys.exit(0)
    try:
        asyncio.run(_run())
    except Exception as exc:
        print(f"[voice-memo-intake] fatal: {exc}", file=sys.stderr)
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
