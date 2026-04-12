"""SQLite schema and helpers for the unified iMessage bus.

DB location: ~/logs/message_bus.db
Tables: inbound (messages read from chat.db), outbound (messages sent by agents)
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = Path.home() / "logs" / "message_bus.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound (
    id INTEGER PRIMARY KEY,
    chat_db_rowid INTEGER UNIQUE,
    chat_identifier TEXT,
    text TEXT,
    received_at TEXT,
    routed_to TEXT,
    route_method TEXT,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS outbound (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,
    recipient TEXT NOT NULL,
    message TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'normal',
    status TEXT DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    created_at TEXT,
    sent_at TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbound_pending
    ON outbound(status) WHERE status IN ('pending', 'retrying');

CREATE INDEX IF NOT EXISTS idx_inbound_received
    ON inbound(received_at);

CREATE INDEX IF NOT EXISTS idx_outbound_agent
    ON outbound(agent, created_at);
"""


@contextmanager
def _connect():
    """Yield a sqlite3 connection that is guaranteed to close.

    Plain `with sqlite3.connect(...) as conn` only manages the transaction —
    it does NOT close the connection, which leaks fds. This wrapper closes.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def log_inbound(
    chat_db_rowid: int,
    chat_identifier: str,
    text: str,
    received_at: str | None = None,
) -> int | None:
    received_at = received_at or datetime.now().isoformat()
    try:
        with _connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO inbound "
                "(chat_db_rowid, chat_identifier, text, received_at) "
                "VALUES (?, ?, ?, ?)",
                (chat_db_rowid, chat_identifier, text, received_at),
            )
            return cur.lastrowid if cur.rowcount > 0 else None
    except sqlite3.Error:
        return None


def update_inbound_route(chat_db_rowid: int, routed_to: str, route_method: str):
    try:
        with _connect() as conn:
            conn.execute(
                "UPDATE inbound SET routed_to=?, route_method=?, processed_at=? "
                "WHERE chat_db_rowid=?",
                (routed_to, route_method, datetime.now().isoformat(), chat_db_rowid),
            )
    except sqlite3.Error:
        pass


def log_outbound(
    agent: str,
    recipient: str,
    message: str,
    tier: str = "normal",
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO outbound (agent, recipient, message, tier, status, attempts, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', 0, ?)",
            (agent, recipient, message, tier, datetime.now().isoformat()),
        )
        return cur.lastrowid


def mark_sent(outbound_id: int):
    with _connect() as conn:
        conn.execute(
            "UPDATE outbound SET status='sent', sent_at=?, attempts=attempts+1 "
            "WHERE id=?",
            (datetime.now().isoformat(), outbound_id),
        )


def mark_failed(outbound_id: int, error: str):
    with _connect() as conn:
        conn.execute(
            "UPDATE outbound SET status='retrying', error=?, attempts=attempts+1 "
            "WHERE id=? AND attempts < 3",
            (error, outbound_id),
        )
        # If already at max attempts, mark permanently failed
        conn.execute(
            "UPDATE outbound SET status='failed', error=? "
            "WHERE id=? AND attempts >= 3",
            (error, outbound_id),
        )


def get_retry_queue(max_retries: int = 3) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, agent, recipient, message, tier, attempts, error "
            "FROM outbound WHERE status='retrying' AND attempts < ? "
            "ORDER BY created_at ASC LIMIT 20",
            (max_retries,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_outbound(agent: str | None = None, limit: int = 10) -> list[dict]:
    with _connect() as conn:
        if agent:
            rows = conn.execute(
                "SELECT agent, recipient, message, tier, status, sent_at "
                "FROM outbound WHERE agent=? ORDER BY created_at DESC LIMIT ?",
                (agent, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT agent, recipient, message, tier, status, sent_at "
                "FROM outbound ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


# Auto-init on import
init_db()
