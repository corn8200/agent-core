"""Persistence layer for the home-ops twice-daily brief agent.

Pure sqlite3, no ORM. DB lives at ~/logs/home-ops.db (WAL mode).
All connections use contextlib.closing() because `with sqlite3.connect()`
does NOT close the connection and has burned us before (see
feedback_sqlite_and_relay_gotchas.md).
"""

import hashlib
import re
import sqlite3
import string
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
_PUNCT_TABLE = str.maketrans("", "", string.punctuation)


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm_key(description: str) -> str:
    s = description.lower().translate(_PUNCT_TABLE)
    s = _WS.sub(" ", s).strip()
    return s[:60]


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def init_db() -> None:
    """Create all tables if missing. Safe to call repeatedly.

    Also performs idempotent migrations:
      - loose_ends.norm_key column + unique index on (norm_key, source)
      - loose_ends.reinforce_count column
    """
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
                norm_key TEXT,
                reinforce_count INTEGER NOT NULL DEFAULT 1
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

        if not _column_exists(conn, "loose_ends", "norm_key"):
            conn.execute("ALTER TABLE loose_ends ADD COLUMN norm_key TEXT")
        if not _column_exists(conn, "loose_ends", "reinforce_count"):
            conn.execute(
                "ALTER TABLE loose_ends ADD COLUMN reinforce_count INTEGER NOT NULL DEFAULT 1"
            )

        rows = conn.execute(
            "SELECT id, description FROM loose_ends WHERE norm_key IS NULL OR norm_key = ''"
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE loose_ends SET norm_key = ? WHERE id = ?",
                (_norm_key(r["description"]), r["id"]),
            )

        # Collapse any pre-existing duplicates (legacy rows that differ only by
        # phrasing) before creating the unique index. Keep the row with the
        # longest description as canonical; merge last_seen, reinforce_count,
        # and earliest first_seen. Prefer unresolved over resolved.
        dup_groups = conn.execute(
            "SELECT norm_key, source FROM loose_ends "
            "GROUP BY norm_key, source HAVING COUNT(*) > 1"
        ).fetchall()
        for g in dup_groups:
            group_rows = conn.execute(
                "SELECT * FROM loose_ends WHERE norm_key = ? AND source = ? "
                "ORDER BY resolved ASC, LENGTH(description) DESC, id ASC",
                (g["norm_key"], g["source"]),
            ).fetchall()
            keep = group_rows[0]
            merged_last_seen = max(r["last_seen"] for r in group_rows)
            merged_first_seen = min(r["first_seen"] for r in group_rows)
            merged_count = sum(
                (r["reinforce_count"] or 1) for r in group_rows
            )
            conn.execute(
                "UPDATE loose_ends SET last_seen = ?, first_seen = ?, "
                "reinforce_count = ? WHERE id = ?",
                (merged_last_seen, merged_first_seen, merged_count, keep["id"]),
            )
            drop_ids = [r["id"] for r in group_rows[1:]]
            conn.executemany(
                "DELETE FROM loose_ends WHERE id = ?",
                [(i,) for i in drop_ids],
            )

        existing_indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='loose_ends'"
            ).fetchall()
        }
        if "sqlite_autoindex_loose_ends_1" in existing_indexes:
            # Old UNIQUE(description, source) table-level constraint can't be dropped
            # without a table rebuild. Rebuild only if the old constraint is present.
            conn.executescript(
                """
                CREATE TABLE loose_ends_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    description TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_ref TEXT,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    resolved_at TEXT,
                    norm_key TEXT,
                    reinforce_count INTEGER NOT NULL DEFAULT 1
                );
                INSERT INTO loose_ends_new
                    (id, description, source, source_ref, first_seen, last_seen,
                     resolved, resolved_at, norm_key, reinforce_count)
                SELECT id, description, source, source_ref, first_seen, last_seen,
                       resolved, resolved_at, norm_key, reinforce_count
                FROM loose_ends;
                DROP TABLE loose_ends;
                ALTER TABLE loose_ends_new RENAME TO loose_ends;
                """
            )

        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_loose_ends_normkey_source "
            "ON loose_ends(norm_key, source)"
        )


def fingerprint_brief(body: str, context: str = "") -> str:
    """Fuzzy sha256 of brief content: lowercased, digits + date words stripped.

    Caller MUST prefix with mode + date to avoid collisions between morning
    and evening briefs that share anchor events. Pass `context` (e.g.
    "evening|2026-04-13") and it will be prepended to the normalized input
    before hashing. Existing callers that pass only `body` still work.
    """
    norm = body.lower()
    norm = _DATE_WORDS.sub("", norm)
    norm = _DIGITS.sub("", norm)
    norm = _WS.sub(" ", norm).strip()
    if context:
        norm = f"{context}|{norm}"
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
    """Insert or bump last_seen on an open loose end. Returns row id.

    Dedupes on a normalized key derived from description (lowercase,
    punct-stripped, whitespace-collapsed, first 60 chars) + source.
    If the new description is longer/more detailed than the stored one
    for a matching open row, the stored description is replaced.
    """
    now = _now()
    key = _norm_key(description)
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "SELECT id, description FROM loose_ends "
            "WHERE norm_key = ? AND source = ? AND resolved = 0",
            (key, source),
        )
        row = cur.fetchone()
        if row is not None:
            new_desc = (
                description
                if len(description) > len(row["description"])
                else row["description"]
            )
            conn.execute(
                "UPDATE loose_ends SET last_seen = ?, "
                "source_ref = COALESCE(?, source_ref), "
                "description = ?, "
                "reinforce_count = reinforce_count + 1, "
                "resolved = 0, resolved_at = NULL "
                "WHERE id = ?",
                (now, source_ref, new_desc, row["id"]),
            )
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO loose_ends "
            "(description, source, source_ref, first_seen, last_seen, norm_key, reinforce_count) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (description, source, source_ref, now, now, key),
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


def mark_stale_loose_ends(days: int = 60) -> int:
    """Auto-resolve stale ephemeral loose ends. Returns count resolved.

    Only touches source='inferred' rows (never 'user' or 'hard').
    - reinforce_count < 3  : resolve after `days` (default 60)
    - reinforce_count >= 3 : resolve only after 180 days (persistent threads)
    """
    persistent_days = max(days * 3, 180)
    now = _now()
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "UPDATE loose_ends SET resolved = 1, resolved_at = ? "
            "WHERE resolved = 0 AND source = 'inferred' AND ("
            "  (reinforce_count < 3 AND julianday(?) - julianday(last_seen) >= ?) OR "
            "  (reinforce_count >= 3 AND julianday(?) - julianday(last_seen) >= ?)"
            ")",
            (now, now, days, now, persistent_days),
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
    """Upsert a learned fact atomically (race-safe).

    Uses INSERT ... ON CONFLICT on the existing UNIQUE INDEX over LOWER(fact)
    so two concurrent writers can't both SELECT-empty and then collide on
    INSERT. Confidence monotonically increases (max of old, new).
    """
    now = _now()
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO learned_facts "
            "(fact, category, source, confidence, created_at, last_reinforced) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(LOWER(fact)) DO UPDATE SET "
            "  last_reinforced = excluded.last_reinforced, "
            "  confidence = MAX(learned_facts.confidence, excluded.confidence)",
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
