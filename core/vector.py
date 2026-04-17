"""Mac-side convenience wrapper around the VPS pgvector store.

Thin client: psycopg2 straight to the VPS Postgres over tailnet, OpenAI
embedding call from Mac. Shares a codepath with the canonical VPS module at
`/srv/apps/taskqueue/tasks/vector.py` — keep behaviour in sync if that changes.

Usage:
    from core.vector import search, get_stats, enqueue_index
    hits = search("Eaton interview", source_types=["calendar_event"], limit=5)
    enqueue_index("memory", "user_contact_details", body, {"path": "..."})

Writes go through the RQ `vps` queue so the OpenAI key stays narrow to one
host; `enqueue_index()` is a thin wrapper over that.
"""
from __future__ import annotations

import os
from typing import Optional

import psycopg2
import psycopg2.extras
from openai import OpenAI

from core.vault import get_secret

PG_DSN = os.environ.get(
    "VEC_PG_DSN",
    "postgresql://appuser:Axh3nce42muAkSGl7GGXsMtn2kUPqk6Mgob5ucRIERk@100.118.21.64:5432/appdb",
)
EMBED_MODEL = "text-embedding-3-small"


def _pg():
    return psycopg2.connect(PG_DSN)


def _openai() -> OpenAI:
    key = os.environ.get("OPENAI_API_KEY") or get_secret("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not available (env or MachineAuto vault)")
    return OpenAI(api_key=key)


def _embed(client: OpenAI, text: str) -> list[float]:
    resp = client.embeddings.create(model=EMBED_MODEL, input=[text])
    return resp.data[0].embedding


def search(
    query: str,
    source_types: Optional[list[str]] = None,
    limit: int = 10,
    min_similarity: float = 0.3,
) -> list[dict]:
    """Semantic search across Apple data + emails indexed in pgvector.

    Returns [{source_type, source_id, content, metadata, similarity}, ...]
    sorted by descending cosine similarity.
    """
    client = _openai()
    qvec = _embed(client, query)
    qvec_s = "[" + ",".join(str(x) for x in qvec) + "]"

    with _pg() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        where = ""
        params: list = [qvec_s]
        if source_types:
            where = "WHERE source_type = ANY(%s)"
            params.append(source_types)
        params.append(qvec_s)
        params.append(limit)
        cur.execute(
            f"""
            SELECT source_type, source_id, chunk_idx, content, metadata,
                   1 - (embedding <=> %s::vector) AS similarity
            FROM vec_embeddings
            {where}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()

    return [dict(r) for r in rows if r["similarity"] >= min_similarity]


def enqueue_index(
    source_type: str,
    source_id: str,
    content: str,
    metadata: Optional[dict] = None,
    queue: str = "vps",
) -> str:
    """Enqueue an embed_and_index job on the VPS RQ queue.

    Returns job id. Worker on VPS resolves `tasks.vector.embed_and_index` by
    name and embeds there (keeps OpenAI key on VPS only). Content-hash dedup
    in the worker means re-enqueuing unchanged content is a cheap no-op.
    """
    from rq import Queue
    from redis import Redis

    q = Queue(queue, connection=Redis(host="100.118.21.64", port=6379))
    job = q.enqueue(
        "tasks.vector.embed_and_index",
        source_type,
        source_id,
        content,
        metadata or {},
        job_timeout=180,
    )
    return job.id


def get_stats() -> dict:
    """Per-source counts + total + sync state (delegates to VPS Postgres)."""
    with _pg() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT source_type, COUNT(*) AS chunks,
                   COUNT(DISTINCT source_id) AS items,
                   MAX(updated_at) AS last_update
            FROM vec_embeddings
            GROUP BY source_type
            ORDER BY source_type
            """
        )
        by_type = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) AS total FROM vec_embeddings")
        total = cur.fetchone()["total"]
        cur.execute("SELECT * FROM vec_sync_state ORDER BY source_type")
        sync = [dict(r) for r in cur.fetchall()]
    return {"total": total, "by_type": by_type, "sync_state": sync}
