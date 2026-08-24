"""Compatibility wrapper for the retired remote pgvector store.

The old remote Postgres and RQ worker were removed with the VPS. Read and write
helpers now fail closed unless a replacement DSN is explicitly supplied.

Usage:
    from core.vector import search, get_stats, enqueue_index
    hits = search("Eaton interview", source_types=["calendar_event"], limit=5)
    enqueue_index("memory", "user_contact_details", body, {"path": "..."})

"""
from __future__ import annotations

import os
from typing import Optional

import psycopg2
import psycopg2.extras
from openai import OpenAI

from core.retired_services import RetiredServiceError, raise_retired
from core.vault import get_secret

PG_DSN = os.environ.get("VEC_PG_DSN", "").strip()
EMBED_MODEL = "text-embedding-3-small"


def _pg():
    if not PG_DSN:
        raise_retired("vector-postgres")
    return psycopg2.connect(PG_DSN)


def _openai() -> OpenAI:
    key = os.environ.get("OPENAI_API_KEY") or get_secret("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not available (env or MachineAuto vault)")
    return OpenAI(api_key=key)


def _embed(client: OpenAI, text: str) -> list[float]:
    resp = client.embeddings.create(model=EMBED_MODEL, input=[text])
    return resp.data[0].embedding


_RERANKER = None


def _get_reranker():
    global _RERANKER
    if _RERANKER is None:
        from sentence_transformers import CrossEncoder

        model = os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
        _RERANKER = CrossEncoder(model, max_length=512, trust_remote_code=False)
    return _RERANKER


def search(
    query: str,
    source_types: Optional[list[str]] = None,
    limit: int = 10,
    min_similarity: float = 0.3,
    rerank: bool = False,
    rerank_pool: int = 40,
) -> list[dict]:
    """Semantic search across Apple data + emails indexed in pgvector.

    Returns [{source_type, source_id, content, metadata, similarity}, ...]
    sorted by descending cosine similarity (or rerank_score if rerank=True).

    When rerank=True: pgvector returns `rerank_pool` candidates (default 40),
    BGE cross-encoder scores each (query, content) pair, top `limit` returned
    sorted by rerank_score. First call loads ~568MB model into RAM.
    """
    client = _openai()
    qvec = _embed(client, query)
    qvec_s = "[" + ",".join(str(x) for x in qvec) + "]"

    fetch_limit = max(rerank_pool, limit) if rerank else limit

    with _pg() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        where = ""
        params: list = [qvec_s]
        if source_types:
            where = "WHERE source_type = ANY(%s)"
            params.append(source_types)
        params.append(qvec_s)
        params.append(fetch_limit)
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

    hits = [dict(r) for r in rows if r["similarity"] >= min_similarity]

    if rerank and hits:
        model = _get_reranker()
        pairs = [(query, (h.get("content") or "")[:2000]) for h in hits]
        scores = model.predict(pairs, batch_size=32, show_progress_bar=False)
        for h, s in zip(hits, scores):
            h["rerank_score"] = float(s)
        hits.sort(key=lambda h: -h["rerank_score"])
        hits = hits[:limit]

    return hits


def enqueue_index(
    source_type: str,
    source_id: str,
    content: str,
    metadata: Optional[dict] = None,
    queue: str = "",
) -> str:
    """Enqueue an embed_and_index job on a configured replacement RQ queue.

    Returns job id. Without ``VECTOR_RQ_REDIS_URL`` or an explicit queue name,
    this fails closed before opening Redis.
    """
    from rq import Queue
    from redis import Redis

    redis_url = os.environ.get("VECTOR_RQ_REDIS_URL", "").strip()
    queue_name = queue or os.environ.get("VECTOR_RQ_QUEUE", "").strip()
    if not redis_url or not queue_name:
        raise RetiredServiceError("vector-rq")
    q = Queue(queue_name, connection=Redis.from_url(redis_url))
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
    """Per-source counts + total + sync state from a configured replacement DB."""
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
