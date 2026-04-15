#!/usr/bin/env python3
"""memory_digest.py — 15-min pattern detector over agent memory.

Scans ~/logs/agent-memory.db for anomaly repetition and brief streaks.
Fires Pushover P0 when a handler-diagnosis keyword recurs 3+ times in
the last 4 hours, or when 3+ consecutive morning briefs share an opening
topic. Dedups against /tmp/memory-digest-last.json so it never spams.

HARD RULE: Pushover priority is ALWAYS 0. Never escalate.
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, '/Users/johncornelius/Projects/agent-core')

from core.vault import hydrate_env, get_secret  # noqa: E402
hydrate_env()

DB_PATH = Path.home() / 'logs' / 'agent-memory.db'
LOG_PATH = Path.home() / 'logs' / 'memory-digest.log'
STATE_PATH = Path('/tmp/memory-digest-last.json')

RECENT_WINDOW_MIN = 30
HANDLER_WINDOW_HOURS = 4
BRIEF_STREAK_MIN = 3
ANOMALY_REPEAT_MIN = 3
DEDUP_COOLDOWN_HOURS = 2

STOPWORDS = {
    'the', 'and', 'for', 'are', 'but', 'not', 'you', 'all', 'can', 'had',
    'her', 'was', 'one', 'our', 'out', 'day', 'get', 'has', 'him', 'his',
    'how', 'man', 'new', 'now', 'old', 'see', 'two', 'way', 'who', 'boy',
    'did', 'its', 'let', 'put', 'say', 'she', 'too', 'use', 'with', 'from',
    'this', 'that', 'have', 'been', 'were', 'they', 'them', 'then', 'than',
    'will', 'what', 'when', 'your', 'about', 'there', 'which', 'their',
    'would', 'could', 'should', 'these', 'those', 'some', 'into', 'over',
    'also', 'just', 'only', 'such', 'very', 'more', 'most', 'much', 'even',
    'still', 'being', 'where', 'while', 'after', 'before', 'other', 'both',
    'each', 'every', 'here', 'said', 'says', 'like', 'make', 'made', 'time',
    'any', 'may', 'might', 'since', 'upon', 'back', 'down', 'off',
    'anomaly', 'diagnosis', 'detected', 'issue', 'problem', 'alert',
    'error', 'warning', 'fail', 'failed', 'failure', 'noted', 'found',
    'check', 'checked', 'status', 'report', 'likely', 'currently',
}


def _log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat(timespec='seconds')
    with LOG_PATH.open('a') as f:
        f.write(f"[{ts}] {msg}\n")


def _load_secrets() -> dict:
    return {
        "PUSHOVER_APP_TOKEN": get_secret("PUSHOVER_APP_TOKEN") or "",
        "PUSHOVER_USER_KEY": get_secret("PUSHOVER_USER_KEY") or "",
    }


def _fetch(conn: sqlite3.Connection, sql: str, params: tuple) -> list:
    conn.row_factory = sqlite3.Row
    return conn.execute(sql, params).fetchall()


def _extract_keywords(content: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", content.lower())
    return [w for w in words if w not in STOPWORDS and len(w) >= 4]


def _detect_handler_pattern(conn: sqlite3.Connection) -> tuple[str, int] | None:
    cutoff = (datetime.now() - timedelta(hours=HANDLER_WINDOW_HOURS)).isoformat()
    rows = _fetch(
        conn,
        "SELECT content FROM memories "
        "WHERE agent='handler' AND category='diagnosis' AND timestamp >= ? "
        "ORDER BY timestamp DESC",
        (cutoff,),
    )
    if len(rows) < ANOMALY_REPEAT_MIN:
        return None

    counter: Counter[str] = Counter()
    for row in rows:
        counter.update(set(_extract_keywords(row['content'])))

    for kw, cnt in counter.most_common(5):
        if cnt >= ANOMALY_REPEAT_MIN:
            return (kw, cnt)
    return None


def _detect_brief_streak(conn: sqlite3.Connection) -> tuple[str, int] | None:
    rows = _fetch(
        conn,
        "SELECT content FROM memories "
        "WHERE agent='morning_brief' AND category='brief' "
        "ORDER BY timestamp DESC LIMIT 5",
        (),
    )
    if len(rows) < BRIEF_STREAK_MIN:
        return None

    heads = [re.sub(r"\s+", " ", r['content'][:100].strip().lower()) for r in rows]
    head_counts: Counter[str] = Counter()
    for head in heads:
        tokens = _extract_keywords(head)
        head_counts.update(set(tokens))

    for kw, cnt in head_counts.most_common(3):
        if cnt >= BRIEF_STREAK_MIN:
            return (kw, cnt)
    return None


def _check_dedup(alert_text: str) -> bool:
    alert_hash = hashlib.sha256(alert_text.encode()).hexdigest()
    now = datetime.now()

    if not STATE_PATH.exists():
        return True

    try:
        state = json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return True

    last_hash = state.get('last_alert_hash')
    last_time_str = state.get('last_alert_time')

    if last_hash != alert_hash:
        return True

    if last_time_str:
        try:
            last_time = datetime.fromisoformat(last_time_str)
            if (now - last_time) > timedelta(hours=DEDUP_COOLDOWN_HOURS):
                return True
        except ValueError:
            return True

    return False


def _write_state(alert_text: str) -> None:
    alert_hash = hashlib.sha256(alert_text.encode()).hexdigest()
    STATE_PATH.write_text(json.dumps({
        'last_alert_hash': alert_hash,
        'last_alert_time': datetime.now().isoformat(),
    }))


def _send_pushover(title: str, message: str, secrets: dict) -> bool:
    token = secrets.get('PUSHOVER_APP_TOKEN') or secrets.get('PUSHOVER_TOKEN')
    user = secrets.get('PUSHOVER_USER_KEY') or secrets.get('PUSHOVER_USER')
    if not token or not user:
        _log("pushover skipped: PUSHOVER token/user missing from secrets.env")
        return False

    data = urllib.parse.urlencode({
        'token': token,
        'user': user,
        'title': title,
        'message': message,
        'priority': 0,
    }).encode()
    req = urllib.request.Request('https://api.pushover.net/1/messages.json', data=data)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if 200 <= resp.status < 300:
                return True
            _log(f"pushover HTTP {resp.status}")
            return False
    except Exception as exc:
        _log(f"pushover error: {exc}")
        return False


def main() -> int:
    if not DB_PATH.exists():
        _log("db missing, skipping")
        return 0

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.execute("PRAGMA busy_timeout=3000")
    except sqlite3.Error as exc:
        _log(f"db connect failed: {exc}")
        return 1

    try:
        recent_cutoff = (datetime.now() - timedelta(minutes=RECENT_WINDOW_MIN)).isoformat()
        recent = _fetch(
            conn,
            "SELECT agent, category FROM memories WHERE timestamp >= ?",
            (recent_cutoff,),
        )
        recent_count = len(recent)

        handler_pattern = _detect_handler_pattern(conn)
        brief_pattern = _detect_brief_streak(conn)
    finally:
        conn.close()

    findings: list[str] = []
    if handler_pattern:
        kw, cnt = handler_pattern
        findings.append(
            f"Handler anomaly '{kw}' repeated {cnt}x in last {HANDLER_WINDOW_HOURS}h"
        )
    if brief_pattern:
        kw, cnt = brief_pattern
        findings.append(
            f"Morning brief topic '{kw}' streak ({cnt} consecutive briefs)"
        )

    if not findings:
        _log(f"no patterns (recent={recent_count})")
        return 0

    alert_title = "Memory digest: pattern detected"
    alert_body = "\n".join(findings)

    if not _check_dedup(alert_body):
        _log(f"dedup suppressed: {alert_body.replace(chr(10), ' | ')}")
        return 0

    secrets = _load_secrets()
    sent = _send_pushover(alert_title, alert_body, secrets)
    if sent:
        _write_state(alert_body)
        _log(f"pushover sent: {alert_body.replace(chr(10), ' | ')}")
    else:
        _log(f"pushover FAILED: {alert_body.replace(chr(10), ' | ')}")

    return 0


if __name__ == '__main__':
    sys.exit(main())
