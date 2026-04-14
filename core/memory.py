"""Vector memory store for agent-core.

Backs semantic recall for agents via SQLite + sqlite-vec. Embeddings are generated
with OpenAI's text-embedding-3-small (1536 dims).

DB location: ~/logs/agent-memory.db
Tables:
    memories     — raw content + metadata
    memory_vss   — vec0 virtual table with embeddings (rowid matches memories.id)

Usage:
    from core.memory import store, search

    mid = store("VPS healthcheck green at 07:00", agent="wrench", category="infra")
    hits = search("infrastructure status", k=5)
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import sqlite_vec
from openai import OpenAI

DB_PATH = Path.home() / "logs" / "agent-memory.db"
EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMS = 1536

_SCHEMA_MEMORIES = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    category TEXT,
    timestamp TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_memories_agent ON memories(agent);
CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
CREATE INDEX IF NOT EXISTS idx_memories_timestamp ON memories(timestamp);
"""

_SCHEMA_VSS = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS memory_vss USING vec0(
    embedding float[{EMBED_DIMS}]
);
"""


def _load_secrets() -> dict[str, str]:
    """Parse ~/.config/secrets.env into a dict. Ignores comments and blank lines."""
    secrets_file = Path.home() / ".config" / "secrets.env"
    env: dict[str, str] = {}
    if not secrets_file.exists():
        return env
    for line in secrets_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class MemoryStore:
    """SQLite-vec backed memory store with OpenAI embeddings."""

    def __init__(self, db_path: Path | str | None = None, api_key: str | None = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        if api_key is None:
            api_key = os.environ.get("OPENAI_API_KEY") or _load_secrets().get(
                "OPENAI_API_KEY"
            )
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY not found in env or ~/.config/secrets.env"
            )
        self._client = OpenAI(api_key=api_key)
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
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

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(_SCHEMA_MEMORIES)
            conn.execute(_SCHEMA_VSS)

    def _embed(self, text: str) -> list[float]:
        resp = self._client.embeddings.create(model=EMBED_MODEL, input=text)
        return resp.data[0].embedding

    def store(
        self,
        content: str,
        agent: str,
        category: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        if not content or not content.strip():
            raise ValueError("content must be non-empty")
        if not agent:
            raise ValueError("agent must be non-empty")

        embedding = self._embed(content)
        blob = sqlite_vec.serialize_float32(embedding)
        ts = datetime.now().isoformat()
        meta_json = json.dumps(metadata or {})

        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO memories (agent, category, timestamp, content, metadata) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent, category, ts, content, meta_json),
            )
            mem_id = cur.lastrowid
            conn.execute(
                "INSERT INTO memory_vss (rowid, embedding) VALUES (?, ?)",
                (mem_id, blob),
            )
            return mem_id

    def search(
        self,
        query: str,
        k: int = 5,
        agent: str | None = None,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        if not query or not query.strip():
            return []
        if k < 1:
            return []

        embedding = self._embed(query)
        blob = sqlite_vec.serialize_float32(embedding)

        # Over-fetch when filtering so we still return k after post-filter. vec0
        # MATCH does not support joined WHERE filters, so we filter in Python.
        fetch_k = k * 5 if (agent or category) else k

        with self._connect() as conn:
            vss_rows = conn.execute(
                "SELECT rowid, distance FROM memory_vss "
                "WHERE embedding MATCH ? AND k = ? "
                "ORDER BY distance",
                (blob, fetch_k),
            ).fetchall()

            if not vss_rows:
                return []

            ids = [r["rowid"] for r in vss_rows]
            dist_by_id = {r["rowid"]: r["distance"] for r in vss_rows}

            placeholders = ",".join("?" * len(ids))
            mem_rows = conn.execute(
                f"SELECT id, agent, category, timestamp, content, metadata "
                f"FROM memories WHERE id IN ({placeholders})",
                ids,
            ).fetchall()

        by_id = {r["id"]: r for r in mem_rows}
        out: list[dict[str, Any]] = []
        for mid in ids:
            row = by_id.get(mid)
            if row is None:
                continue
            if agent and row["agent"] != agent:
                continue
            if category and row["category"] != category:
                continue
            try:
                meta = json.loads(row["metadata"] or "{}")
            except json.JSONDecodeError:
                meta = {}
            out.append(
                {
                    "id": row["id"],
                    "agent": row["agent"],
                    "category": row["category"],
                    "timestamp": row["timestamp"],
                    "content": row["content"],
                    "metadata": meta,
                    "distance": dist_by_id[mid],
                }
            )
            if len(out) >= k:
                break
        return out

    def prune(self, days: int = 90) -> int:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM memories WHERE timestamp < ?", (cutoff,)
            ).fetchall()
            ids = [r["id"] for r in rows]
            if not ids:
                return 0
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"DELETE FROM memories WHERE id IN ({placeholders})", ids
            )
            conn.execute(
                f"DELETE FROM memory_vss WHERE rowid IN ({placeholders})", ids
            )
            return len(ids)

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()
            return int(row["n"])


_store: MemoryStore | None = None


def get_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store


def store(
    content: str,
    agent: str,
    category: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    return get_store().store(content, agent, category, metadata)


def search(
    query: str,
    k: int = 5,
    agent: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    return get_store().search(query, k, agent, category)


def prune(days: int = 90) -> int:
    return get_store().prune(days)


def count() -> int:
    return get_store().count()
