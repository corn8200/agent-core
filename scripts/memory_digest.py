#!/usr/bin/env python3
"""memory_digest.py — 15-min pattern detector over agent memory.

Scans ~/logs/agent-memory.db for anomaly repetition and brief streaks.
Publishes one control-plane anomaly item when a handler-diagnosis keyword
recurs 3+ times in the last 4 hours. Quiet brief streaks update dedup
state without creating UI items or triggering push delivery.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, '/Users/johncornelius/Projects/agent-core')

from core import agent_cp_client as cp  # noqa: E402

DB_PATH = Path.home() / 'logs' / 'agent-memory.db'
LOG_PATH = Path.home() / 'logs' / 'memory-digest.log'
STATE_PATH = Path('/tmp/memory-digest-last.json')
CP_AGENT = 'memory-digest'

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
        "WHERE agent='home_ops' AND category='brief' "
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


def _check_dedup(alert_text: str, state_path: Path) -> bool:
    alert_hash = hashlib.sha256(alert_text.encode()).hexdigest()
    now = datetime.now()

    if not state_path.exists():
        return True

    try:
        state = json.loads(state_path.read_text())
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


def _write_state(alert_text: str, state_path: Path) -> None:
    alert_hash = hashlib.sha256(alert_text.encode()).hexdigest()
    state_path.write_text(json.dumps({
        'last_alert_hash': alert_hash,
        'last_alert_time': datetime.now().isoformat(),
    }))


def _publish_anomaly_item(title: str, message: str, *, dry_run: bool) -> bool:
    payload = {
        'title': title,
        'message': message,
        'kind': 'anomaly',
        'priority': 2,
        'sources': ('ui',),
    }
    if dry_run:
        _log(f"dry-run anomaly item: {json.dumps(payload, sort_keys=True)}")
        return True
    try:
        return cp.event(CP_AGENT, 'anomaly', payload=payload) is not None
    except Exception as exc:
        _log(f"anomaly item publish error: {exc}")
        return False


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db-path', default=str(DB_PATH))
    parser.add_argument('--state-path', default=str(STATE_PATH))
    parser.add_argument('--dry-run', action='store_true')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = Path(args.db_path)
    state_path = Path(args.state_path)

    if not db_path.exists():
        _log("db missing, skipping")
        return 0

    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
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

    if not _check_dedup(alert_body, state_path):
        _log(f"dedup suppressed: {alert_body.replace(chr(10), ' | ')}")
        return 0

    if not handler_pattern:
        _write_state(alert_body, state_path)
        _log(f"quiet pattern recorded without item: {alert_body.replace(chr(10), ' | ')}")
        return 0

    sent = _publish_anomaly_item(alert_title, alert_body, dry_run=args.dry_run)
    if not sent:
        _log(f"anomaly item FAILED: {alert_body.replace(chr(10), ' | ')}")
        return 0

    _write_state(alert_body, state_path)
    _log(f"anomaly item published: {alert_body.replace(chr(10), ' | ')}")

    return 0


if __name__ == '__main__':
    sys.exit(main())
