"""Persistence layer for the home-ops twice-daily brief agent.

Pure sqlite3, no ORM. DB lives at ~/logs/home-ops.db (WAL mode).
All connections use contextlib.closing() because `with sqlite3.connect()`
does NOT close the connection and has burned us before (see
feedback_sqlite_and_relay_gotchas.md).
"""

import hashlib
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

DB_PATH = Path.home() / "logs" / "home-ops.db"

_DATE_WORDS = re.compile(
    r"\b(today|tomorrow|yesterday|tonight|morning|afternoon|evening|night|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
    r"january|february|march|april|june|july|august|september|october|november|december|"
    r"am|pm)\b",
    re.IGNORECASE,
)
_DIGITS = re.compile(r"\d+")
_WS = re.compile(r"\s+")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init_db() -> None:
    """Create all tables if missing. Safe to call repeatedly."""
    with closing(_connect()) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS nudge_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT UNIQUE NOT NULL,
                sent_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                subject TEXT,
                body TEXT
            );

            CREATE TABLE IF NOT EXISTS loose_ends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT NOT NULL,
                source TEXT NOT NULL,
                source_ref TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                resolved_at TEXT,
                UNIQUE(description, source)
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brief_fingerprint TEXT,
                received_at TEXT NOT NULL,
                channel TEXT NOT NULL,
                user_msg TEXT NOT NULL,
                sentiment TEXT
            );

            CREATE TABLE IF NOT EXISTS learned_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fact TEXT NOT NULL,
                category TEXT NOT NULL,
                source TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.8,
                created_at TEXT NOT NULL,
                last_reinforced TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_learned_facts_fact
                ON learned_facts(LOWER(fact));
            """
        )


def fingerprint_brief(body: str) -> str:
    """Fuzzy sha256 of brief content: lowercased, digits + date words stripped."""
    norm = body.lower()
    norm = _DATE_WORDS.sub("", norm)
    norm = _DIGITS.sub("", norm)
    norm = _WS.sub(" ", norm).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def already_sent(fingerprint: str) -> bool:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT 1 FROM nudge_history WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return row is not None


def log_brief(fingerprint: str, mode: str, subject: str, body: str) -> None:
    """Record a sent brief. Silently ignores duplicate fingerprints."""
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO nudge_history (fingerprint, sent_at, mode, subject, body) "
            "VALUES (?, ?, ?, ?, ?)",
            (fingerprint, _now(), mode, subject, body),
        )


def upsert_loose_end(
    description: str, source: str, source_ref: str | None = None
) -> int:
    """Insert or bump last_seen on an open loose end. Returns row id."""
    now = _now()
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "SELECT id FROM loose_ends WHERE description = ? AND source = ?",
            (description, source),
        )
        row = cur.fetchone()
        if row is not None:
            conn.execute(
                "UPDATE loose_ends SET last_seen = ?, source_ref = COALESCE(?, source_ref), "
                "resolved = 0, resolved_at = NULL WHERE id = ?",
                (now, source_ref, row["id"]),
            )
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO loose_ends (description, source, source_ref, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            (description, source, source_ref, now, now),
        )
        return int(cur.lastrowid)


def get_open_loose_ends(limit: int = 50) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM loose_ends WHERE resolved = 0 "
            "ORDER BY last_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def resolve_loose_end(id: int) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE loose_ends SET resolved = 1, resolved_at = ? WHERE id = ?",
            (_now(), id),
        )


def mark_stale_loose_ends(days: int = 30) -> int:
    """Auto-resolve loose ends not seen in N days. Returns count resolved."""
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "UPDATE loose_ends SET resolved = 1, resolved_at = ? "
            "WHERE resolved = 0 AND julianday(?) - julianday(last_seen) >= ?",
            (_now(), _now(), days),
        )
        return cur.rowcount


def record_feedback(fingerprint: str, channel: str, user_msg: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO feedback (brief_fingerprint, received_at, channel, user_msg) "
            "VALUES (?, ?, ?, ?)",
            (fingerprint, _now(), channel, user_msg),
        )


def get_recent_feedback(limit: int = 20) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM feedback ORDER BY received_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def add_learned_fact(
    fact: str, category: str, source: str, confidence: float = 0.8
) -> None:
    """Upsert a learned fact. On conflict (case-insensitive), bump last_reinforced."""
    now = _now()
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "SELECT id, confidence FROM learned_facts WHERE LOWER(fact) = LOWER(?)",
            (fact,),
        )
        row = cur.fetchone()
        if row is not None:
            new_conf = min(1.0, max(float(row["confidence"]), confidence))
            conn.execute(
                "UPDATE learned_facts SET last_reinforced = ?, confidence = ? WHERE id = ?",
                (now, new_conf, row["id"]),
            )
            return
        conn.execute(
            "INSERT INTO learned_facts "
            "(fact, category, source, confidence, created_at, last_reinforced) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (fact, category, source, confidence, now, now),
        )


def get_learned_facts(category: str | None = None, limit: int = 100) -> list[dict]:
    with closing(_connect()) as conn:
        if category is None:
            rows = conn.execute(
                "SELECT * FROM learned_facts ORDER BY last_reinforced DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM learned_facts WHERE category = ? "
                "ORDER BY last_reinforced DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def forget_fact(fact: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM learned_facts WHERE LOWER(fact) = LOWER(?)", (fact,))


if __name__ == "__main__":
    init_db()
    print(f"home-ops state initialized -> {DB_PATH}")
