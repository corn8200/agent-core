"""Shared semantic-recall helper for agents, daemons, and briefs.

One function: `get_context(query, kind, ...)`. Wraps `core.vector.search` with
per-kind defaults, graceful degradation (never raises), and a uniform markdown
block ready to prepend to any SDK user prompt.

Usage:
    from core.recall import get_context

    block = get_context(msg_text, kind="imessage")
    user_prompt = f"{block}\\n\\n{task}" if block else task

CLI:
    python -m core.recall --demo <kind> "<query>"           # prints block
    python -m core.recall --demo <kind> "<query>" --raw     # hits as JSON
    python -m core.recall --bench                           # p50/p95 per kind
    python -m core.recall --smoke                           # exit 1 on regression

Design notes:
- Sync. Matches underlying `search()`. No async overhead for single call-sites.
- Never raises. On failure returns "". Callers concatenate unconditionally.
- In-process circuit breaker: if a `kind` exceeds BUDGET_MS, mute for 60s.
- Failures logged to ~/logs/recall.jsonl (one line per miss, bounded).
- Content trimmed to TRIM_CHARS per hit to keep prompt tokens predictable.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# lazy import: core.vector pulls psycopg2 + openai, which we only need on hit path
# ----------------------------------------------------------------------

# Budgets are split by rerank mode because the BGE cross-encoder runs on CPU
# and inference alone costs 3-7s per call. Fast-path (cosine) stays tight.
BUDGET_MS = 1500  # fast path: no rerank
BUDGET_MS_RERANK = 10000  # slow path: cosine + BGE rerank
CIRCUIT_OPEN_SECS = 60
TRIM_CHARS = 500
LOG_PATH = Path.home() / "logs" / "recall.jsonl"


@dataclass(frozen=True)
class KindCfg:
    source_types: tuple[str, ...]
    limit: int
    rerank: bool
    min_similarity: float


#
# min_similarity note: underlying search() applies this threshold BEFORE rerank,
# which means a high floor starves the reranker. For rerank=True kinds we use a
# low floor (0.20) and let the cross-encoder pick winners. For rerank=False
# kinds the floor is the quality gate itself, so it sits higher (0.30-0.40).
KIND_DEFAULTS: dict[str, KindCfg] = {
    "handler": KindCfg(("memory", "note"), 5, True, 0.20),
    "brief": KindCfg(("memory", "note", "email", "calendar_event", "reminder"), 8, True, 0.20),
    "imessage": KindCfg(("imessage_thread", "memory", "contact", "reminder"), 6, False, 0.35),
    "job": KindCfg(("memory", "note"), 10, True, 0.20),
    "agent": KindCfg(("memory", "note"), 4, False, 0.30),
    "swarm": KindCfg(("memory", "note", "email"), 6, True, 0.20),
    "nudge": KindCfg(("calendar_event", "reminder", "memory"), 3, False, 0.35),
    "watch": KindCfg(("memory", "reminder", "calendar_event"), 4, False, 0.30),
    "mailtriage": KindCfg(("email", "memory", "contact"), 5, False, 0.35),
    "sentinel": KindCfg(("memory", "note"), 3, False, 0.40),
}

# Per-agent overrides within kind="agent"
AGENT_OVERRIDES: dict[str, KindCfg] = {
    "Titan": KindCfg(("memory", "note"), 10, True, 0.20),
    "Critic": KindCfg(("memory", "note"), 10, True, 0.20),
}

DEMO_QUERIES: dict[str, str] = {
    "handler": "disk full on VPS",
    "brief": "morning brief sections required",
    "imessage": "drone inspection Sentry",
    "job": "pilot Black Hawk Army aviation",
    "agent": "Sentry AI Thermal business",
    "swarm": "infrastructure health check",
    "nudge": "baseball practice",
    "watch": "what reminders do I have",
    "mailtriage": "invoice bill payment",
    "sentinel": "SDVOSB federal contracting",
}

# in-process circuit breaker: {kind: unix_ts_until_which_muted}
_CIRCUIT: dict[str, float] = {}


def _circuit_open(kind: str) -> bool:
    exp = _CIRCUIT.get(kind, 0.0)
    if exp and time.time() < exp:
        return True
    if exp:
        _CIRCUIT.pop(kind, None)
    return False


def _trip_circuit(kind: str) -> None:
    _CIRCUIT[kind] = time.time() + CIRCUIT_OPEN_SECS


def _log_miss(kind: str, query: str, err: str, elapsed_ms: int) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "ts": time.time(),
                        "kind": kind,
                        "query": query[:120],
                        "err": err[:200],
                        "elapsed_ms": elapsed_ms,
                    }
                )
                + "\n"
            )
    except Exception:
        pass  # never let logging break the hot path


def _resolve_cfg(kind: str, agent_name: Optional[str] = None) -> Optional[KindCfg]:
    if kind == "agent" and agent_name and agent_name in AGENT_OVERRIDES:
        return AGENT_OVERRIDES[agent_name]
    return KIND_DEFAULTS.get(kind)


def _format_block(hits: list[dict], header: str) -> str:
    if not hits:
        return ""
    lines = [header, ""]
    for h in hits:
        st = h.get("source_type", "?")
        sim = h.get("rerank_score") if "rerank_score" in h else h.get("similarity", 0.0)
        sid = h.get("source_id", "")
        content = (h.get("content") or "").strip()
        if len(content) > TRIM_CHARS:
            content = content[:TRIM_CHARS].rstrip() + "…"
        lines.append(f"*source:{st} | id:{sid} | sim:{sim:.2f}*")
        lines.append(content)
        lines.append("---")
    return "\n".join(lines)


def get_context(
    query: str,
    kind: str,
    extra_types: Optional[list[str]] = None,
    limit: Optional[int] = None,
    header: str = "## Relevant prior context",
    agent_name: Optional[str] = None,
) -> str:
    """Semantic recall → markdown block (empty string on any failure).

    Never raises. Callers can unconditionally concat the result.

    Args:
        query: free-form text to embed + search.
        kind: one of KIND_DEFAULTS keys (handler, brief, imessage, etc).
        extra_types: append to the kind's default source_types.
        limit: override the kind's default limit.
        header: markdown header for the block.
        agent_name: when kind="agent", enables per-agent overrides (Titan/Critic).
    """
    if not query or not query.strip():
        return ""

    cfg = _resolve_cfg(kind, agent_name)
    if cfg is None:
        _log_miss(kind, query, f"unknown kind: {kind}", 0)
        return ""

    if _circuit_open(kind):
        return ""

    types = list(cfg.source_types)
    if extra_types:
        types.extend(t for t in extra_types if t not in types)
    use_limit = limit if limit is not None else cfg.limit

    t0 = time.time()
    try:
        # local import keeps the module light for non-hot-path consumers
        from core.vector import search

        hits = search(
            query=query,
            source_types=types,
            limit=use_limit,
            min_similarity=cfg.min_similarity,
            rerank=cfg.rerank,
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        budget = BUDGET_MS_RERANK if cfg.rerank else BUDGET_MS
        if elapsed_ms > budget:
            _trip_circuit(kind)
            _log_miss(kind, query, f"over budget {elapsed_ms}ms > {budget}ms", elapsed_ms)
            # still return the hits — they're already computed
        return _format_block(hits, header)
    except Exception as e:  # noqa: BLE001
        elapsed_ms = int((time.time() - t0) * 1000)
        _trip_circuit(kind)
        _log_miss(kind, query, f"{type(e).__name__}: {e}", elapsed_ms)
        return ""


def raw_search(
    query: str,
    kind: str,
    extra_types: Optional[list[str]] = None,
    limit: Optional[int] = None,
    agent_name: Optional[str] = None,
) -> list[dict]:
    """Same as get_context but returns the raw hits list. [] on failure."""
    if not query or not query.strip():
        return []
    cfg = _resolve_cfg(kind, agent_name)
    if cfg is None:
        return []
    if _circuit_open(kind):
        return []
    types = list(cfg.source_types)
    if extra_types:
        types.extend(t for t in extra_types if t not in types)
    use_limit = limit if limit is not None else cfg.limit
    t0 = time.time()
    try:
        from core.vector import search

        hits = search(
            query=query,
            source_types=types,
            limit=use_limit,
            min_similarity=cfg.min_similarity,
            rerank=cfg.rerank,
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        budget = BUDGET_MS_RERANK if cfg.rerank else BUDGET_MS
        if elapsed_ms > budget:
            _trip_circuit(kind)
            _log_miss(kind, query, f"over budget {elapsed_ms}ms > {budget}ms", elapsed_ms)
        return hits
    except Exception as e:  # noqa: BLE001
        elapsed_ms = int((time.time() - t0) * 1000)
        _trip_circuit(kind)
        _log_miss(kind, query, f"{type(e).__name__}: {e}", elapsed_ms)
        return []


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def _cli_demo(kind: str, query: str, raw: bool) -> int:
    if kind not in KIND_DEFAULTS:
        print(f"unknown kind: {kind}. Known: {', '.join(KIND_DEFAULTS)}")
        return 2
    t0 = time.time()
    if raw:
        hits = raw_search(query, kind=kind)
        print(json.dumps(hits, indent=2, default=str))
        print(f"\n-- {len(hits)} hits in {int((time.time()-t0)*1000)}ms", flush=True)
    else:
        block = get_context(query, kind=kind)
        if not block:
            print(f"(no context for kind={kind} query={query!r})")
            return 0
        print(block)
        print(f"\n-- {int((time.time()-t0)*1000)}ms", flush=True)
    return 0


def _cli_bench() -> int:
    # pre-warm: first OpenAI embed + first reranker load are one-time costs
    # that wouldn't repeat in a long-running daemon. Measure steady-state.
    print("pre-warming OpenAI + reranker (one-time)...", flush=True)
    _CIRCUIT.clear()
    raw_search("warmup", kind="agent")  # no-rerank kind: warms embed + pg
    raw_search("warmup", kind="handler")  # rerank kind: warms BGE model load
    _CIRCUIT.clear()

    rows: list[tuple[str, int, int, int]] = []  # (kind, hits, p50, p95)
    for kind, q in DEMO_QUERIES.items():
        samples_ms: list[int] = []
        hit_count = 0
        for _ in range(5):
            _CIRCUIT.pop(kind, None)  # reset before every sample
            t0 = time.time()
            hits = raw_search(q, kind=kind)
            samples_ms.append(int((time.time() - t0) * 1000))
            hit_count = len(hits)
        samples_ms.sort()
        p50 = samples_ms[2]  # median of 5
        p95 = samples_ms[-2]  # 2nd-worst = p80, reasonable with n=5
        rows.append((kind, hit_count, p50, p95))
    print(f"{'kind':<12} {'hits':>5} {'p50':>7} {'p95':>7}")
    print("-" * 40)
    for kind, hc, p50, p95 in rows:
        flag = "" if p95 <= BUDGET_MS else "  OVER"
        print(f"{kind:<12} {hc:>5} {p50:>6}ms {p95:>6}ms{flag}")
    return 0 if all(r[3] <= BUDGET_MS for r in rows) else 1


def _cli_smoke() -> int:
    bad: list[tuple[str, str]] = []
    for kind, q in DEMO_QUERIES.items():
        _CIRCUIT.pop(kind, None)
        hits = raw_search(q, kind=kind)
        if not hits:
            bad.append((kind, q))
    if bad:
        print("SMOKE FAIL — kinds returning 0 hits:")
        for kind, q in bad:
            print(f"  {kind:<12}  query={q!r}")
        return 1
    print(f"SMOKE OK — all {len(DEMO_QUERIES)} kinds returned hits")
    return 0


def _main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="core.recall")
    sub = p.add_subparsers(dest="cmd")

    p_demo = sub.add_parser("demo", help="print block or JSON for one (kind, query)")
    p_demo.add_argument("kind")
    p_demo.add_argument("query")
    p_demo.add_argument("--raw", action="store_true")

    sub.add_parser("bench", help="latency bench across all kinds")
    sub.add_parser("smoke", help="exit 1 if any kind returns 0 hits for canned query")

    # also support legacy flag form: --demo <kind> <query>
    p.add_argument("--demo", nargs=2, metavar=("KIND", "QUERY"))
    p.add_argument("--raw", action="store_true")
    p.add_argument("--bench", action="store_true")
    p.add_argument("--smoke", action="store_true")

    ns = p.parse_args(argv)

    if ns.cmd == "demo":
        return _cli_demo(ns.kind, ns.query, ns.raw)
    if ns.cmd == "bench" or ns.bench:
        return _cli_bench()
    if ns.cmd == "smoke" or ns.smoke:
        return _cli_smoke()
    if ns.demo:
        return _cli_demo(ns.demo[0], ns.demo[1], ns.raw)

    p.print_help()
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
