"""
main.py
-------
WHAT: The FastAPI app for Phase 5 — the final phase. All HTTP routing lives
      here. Delegates to:
        - db.py        : SQL / persistence
        - trie.py      : in-memory index
        - cache_node.py: consistent-hash cache cluster
        - batch_writer.py: buffered write path (NEW Phase 5)
        - metrics.py   : latency / counter tracking (NEW Phase 5)

CHANGES FROM PHASE 4:
  1. POST /search no longer writes to Postgres directly. It calls
     batch_writer.record() and returns immediately. The DB write happens in
     the background on the next flush.
  2. GET /metrics: new endpoint returning latency percentiles, cache stats,
     and batch-writer counters (the live performance report).
  3. GET /trending: new endpoint returning the top 10 globally trending
     queries; used by the frontend's Trending panel.
  4. Active cache invalidation: after each batch flush, affected cache
     prefixes are removed immediately (not waiting for TTL expiry).
  5. App title / version bumped to Phase 5.

WHAT IS NOT CHANGED:
  - GET /suggest: identical behaviour. Cache-aside, mode=basic|trending.
  - GET /cache/debug: unchanged.
  - The Trie: still updated after each write (now after each batch flush).
  - Single-process caveat: the Trie and cache cluster are still in-process.
    Multi-worker deployment would still need real Redis + a shared Trie
    (e.g. backed by Postgres on demand). This is a documented limitation
    from Phase 2 onwards.
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import db
from app.batch_writer import BatchWriter
from app.cache_node import CacheCluster
from app.config import settings
from app.metrics import metrics
from app.trie import Trie

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("typeahead")

trie = Trie(max_dfs_nodes=settings.max_dfs_nodes)

cache = CacheCluster(
    node_ids=[f"node-{i}" for i in range(settings.cache_node_count)],
    virtual_nodes=settings.cache_virtual_nodes,
    default_ttl_seconds=settings.cache_ttl_seconds,
)


def _on_batch_flush(batch: dict) -> None:
    """
    Callback invoked by BatchWriter after each successful DB flush.

    WHAT WE DO HERE (three things, in order):
      1. Update the Trie with the new counts for every flushed query.
         This replaces the per-request trie.insert() from Phase 4 — the
         Trie now updates in batches at flush time, not on every /search.
      2. Actively invalidate cache entries for every prefix that could be
         affected by the flushed queries. Phase 3/4 relied on TTL expiry
         (up to 45 seconds of stale data). Phase 5 evicts the affected
         entries immediately after each flush, so the next /suggest for
         those prefixes gets a fresh Trie walk.
      3. Log a summary for the performance report.

    WHY THIS IS A CALLBACK (not inline in batch_writer.py):
      batch_writer.py should know nothing about the Trie or the cache.
      Keeping it as a callback passed in at construction time means:
        - batch_writer.py is independently testable (no Trie/cache imports).
        - main.py remains the only file that knows how all modules connect.
        - Swapping the cache implementation (e.g. to real Redis) only
          touches main.py, not batch_writer.py.

    ACTIVE INVALIDATION LOGIC:
      For each flushed query (e.g. "iphone charger"), we generate every
      prefix:  "i", "ip", ..., "iphone charger"
      and tell the cache to evict those keys (both ::basic and ::trending).
      This is O(total_characters_across_flushed_queries) per flush, which
      in practice is a few hundred string operations — negligible.
    """
    # Step 1: collect all affected prefixes
    affected_prefixes = set()
    for query_text in batch:
        for i in range(1, len(query_text) + 1):
            affected_prefixes.add(query_text[:i])

    # Step 2: update Trie with fresh DB values
    # We re-fetch the updated rows from Postgres so the Trie gets the correct
    # total historical_count (not just the incremental batch count, which could
    # undercount if two flush windows overlapped — rare but possible under load).
    # ALTERNATIVE: pass the RETURNING row from upsert_query_batch back through
    # the callback. That avoids a re-fetch but requires threading row results
    # through batch_writer._write_batch → on_flush_callback, complicating
    # batch_writer's interface. For this demo scale, a re-fetch of the
    # flushed queries is acceptable; at very high QPS you'd want the
    # RETURNING approach.
    for query_text in batch:
        try:
            row = db.fetch_one_query(query_text)
            if row:
                trie.insert(row["query_text"], row["historical_count"], row["recent_count"])
        except Exception as exc:
            logger.warning("Trie update failed for %r: %s", query_text, exc)

    # Step 3: actively invalidate cache for affected prefixes
    deleted = cache.active_invalidate(list(affected_prefixes))
    logger.info(
        "Post-flush invalidation: %d prefixes checked, %d cache entries evicted",
        len(affected_prefixes),
        deleted,
    )


# Phase 5: BatchWriter constructed once at module level.
# flush_interval_seconds and max_buffer_size are configurable via env vars
# (added to config.py), with sensible defaults for the demo.
batch_writer = BatchWriter(
    flush_interval_seconds=settings.batch_flush_interval_seconds,
    max_buffer_size=settings.batch_max_buffer_size,
    on_flush_callback=_on_batch_flush,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    db.init_pool()
    t0 = time.perf_counter()
    rows = db.fetch_all_queries()
    for row in rows:
        trie.insert(row["query_text"], row["historical_count"], row["recent_count"])
    elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.info(
        "Trie ready: %d queries loaded from Postgres in %.1f ms",
        trie.size,
        elapsed_ms,
    )
    yield
    # --- shutdown: flush whatever's buffered before the process exits ---
    batch_writer.stop()
    db.close_pool()


app = FastAPI(
    title="Search Typeahead System - Phase 5",
    description="Final phase: batch writes, active cache invalidation, metrics endpoint.",
    version="0.5.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str


class SearchResponse(BaseModel):
    message: str
    query: str
    buffered: bool   # Phase 5: always True — indicates the write was buffered


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "queries_indexed": trie.size}


@app.get("/suggest")
def suggest(
    q: str = Query(default="", description="Prefix typed by the user"),
    mode: str = Query(default="trending", description="'basic' or 'trending'"),
) -> dict:
    """
    GET /suggest?q=<prefix>&mode=<basic|trending>

    Unchanged from Phase 4 in behaviour. Phase 5 adds metrics recording
    (suggest latency + cache hit) so GET /metrics can report percentiles.
    """
    t0 = time.perf_counter()

    if mode not in ("basic", "trending"):
        raise HTTPException(status_code=400, detail="mode must be 'basic' or 'trending'")

    if not q:
        latency_ms = round((time.perf_counter() - t0) * 1000, 3)
        metrics.record_suggest(latency_ms, cache_hit=False)
        return {
            "query": q, "mode": mode, "suggestions": [],
            "count": 0, "latency_ms": latency_ms, "cache_hit": False, "node": None,
        }

    prefix = q.lower()
    cache_key = f"{prefix}::{mode}"
    cached, node_id = cache.get(cache_key)

    if cached is not None:
        latency_ms = round((time.perf_counter() - t0) * 1000, 3)
        metrics.record_suggest(latency_ms, cache_hit=True)
        return {
            "query": q, "mode": mode, "suggestions": cached,
            "count": len(cached), "latency_ms": latency_ms,
            "cache_hit": True, "node": node_id,
        }

    results = trie.suggest(
        prefix,
        limit=settings.suggestion_limit,
        mode=mode,
        recency_weight=settings.recency_weight,
    )
    cache.set(cache_key, results)

    latency_ms = round((time.perf_counter() - t0) * 1000, 3)
    metrics.record_suggest(latency_ms, cache_hit=False)
    return {
        "query": q, "mode": mode, "suggestions": results,
        "count": len(results), "latency_ms": latency_ms,
        "cache_hit": False, "node": node_id,
    }


@app.get("/trending")
def trending(limit: int = Query(default=10, ge=1, le=50)) -> dict:
    """
    GET /trending?limit=10

    Returns the top globally trending queries — used by the frontend's
    Trending panel. Implemented as a suggest with q="" and mode=trending,
    which returns the top-scored queries from the Trie root, no prefix filter.

    WHY A SEPARATE ENDPOINT INSTEAD OF GET /suggest?q=&mode=trending:
      The empty-prefix case in /suggest returns [] (we short-circuit to avoid
      a full Trie walk on every empty keystroke). /trending intentionally
      DOES the full walk, and it makes sense to name it clearly rather than
      special-casing the empty-prefix path in /suggest. This also keeps the
      frontend's mental model clean: there are two distinct features
      (prefix autocomplete vs. global trending), and they have distinct URLs.

    CACHING: results are cached under key "::trending_global" with the same
    TTL as other cache entries. The frontend polls this every 10 seconds,
    so even a 45-second TTL means the trending panel updates within one
    polling period after a trend shift.
    """
    cache_key = "::trending_global"
    cached, node_id = cache.get(cache_key)
    if cached is not None:
        return {"trending": cached, "count": len(cached), "cache_hit": True}

    # results = trie.suggest(
    #     prefix="",
    #     limit=limit,
    #     mode="trending",
    #     recency_weight=settings.recency_weight,
    # )
    results = trie.top_queries(limit=limit, mode="trending",
                               recency_weight=settings.recency_weight)
    cache.set(cache_key, results)
    return {"trending": results, "count": len(results), "cache_hit": False}


@app.get("/cache/debug")
def cache_debug(
    prefix: str = Query(..., description="Prefix to inspect"),
) -> dict:
    if not prefix:
        raise HTTPException(status_code=400, detail="prefix must be a non-empty string")
    return cache.debug(prefix.lower())


@app.get("/cache/stats")
def cache_stats() -> dict:
    return cache.stats()


@app.get("/metrics")
def get_metrics() -> dict:
    """
    GET /metrics

    Returns the live performance report. Polls every 5 seconds in the
    frontend's metrics sidebar. This is what you show during the demo to
    prove the system is performing as designed.

    See metrics.py for what each field means and why it exists.
    """
    return metrics.snapshot(batch_writer)


@app.post("/search", response_model=SearchResponse)
def search(body: SearchRequest) -> SearchResponse:
    """
    POST /search

    PHASE 5 CHANGE: The DB write is now BUFFERED.
      Old path (Phase 4): body → normalize → db.upsert_query() → trie.insert() → respond
      New path (Phase 5): body → normalize → batch_writer.record() → respond

    The response returns in microseconds instead of milliseconds.
    The actual DB write happens in the background on the next flush.

    TRADE-OFF: The returned SearchResponse no longer includes the updated DB
      count, because we don't know it yet (the write is deferred). We return
      `buffered: true` instead, which the frontend displays to confirm the
      event was accepted into the buffer.

    WHY THIS TRADE-OFF IS ACCEPTABLE FOR TYPEAHEAD:
      The user doesn't need to see their exact new count. They need the
      autocomplete to eventually reflect their search. "Eventually" = within
      one flush interval (default 5 seconds). For a typeahead system, that's
      indistinguishable from real-time. For an e-commerce checkout confirmation,
      it would not be acceptable (the user needs to see their order confirmed).

    NORMALIZATION: same as Phase 4 — trim + lowercase at the HTTP layer.
    REJECTION: empty/whitespace strings still rejected with 400.
    """
    query = body.query.strip().lower()

    if not query:
        raise HTTPException(
            status_code=400,
            detail="query must be a non-empty, non-whitespace string",
        )

    # Phase 5: buffer the event — returns immediately, no DB round-trip.
    batch_writer.record(query)
    metrics.record_search()

    logger.debug("POST /search buffered: query=%r", query)

    return SearchResponse(
        message="Searched",
        query=query,
        buffered=True,
    )
