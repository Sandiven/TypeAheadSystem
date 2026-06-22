"""
db.py
-----
WHAT: All SQL in the whole project lives in this one file, behind plain
      Python functions (get_connection_pool, fetch_all_queries,
      upsert_query). Nothing else in the codebase writes SQL directly.

WHY psycopg2 + raw SQL INSTEAD OF AN ORM (SQLAlchemy/Django ORM):
      For a viva, "explain what this line of code does" has to have a
      one-sentence answer. An ORM call like `Query.objects.get_or_create(...)`
      is two or three real SQL statements hidden behind a method name, plus
      a whole identity-map/session-lifecycle model you'd also have to
      defend. A 4-line UPSERT in raw SQL is the entire mechanism, visible.
      ORMs earn their cost on large multi-table apps with deep relationships;
      this project has ONE table. The ORM's main selling points (avoiding
      hand-written joins, migrations across many models, protecting against
      SQL injection) are not where the complexity lives here.
      TRADE-OFF we are accepting: no auto-generated migrations, manual
      connection-pool management, and parameterized-query discipline is on
      us (we still get injection-safety from psycopg2's parameter binding,
      just not for free from an ORM layer).

WHY A CONNECTION POOL (psycopg2.pool) INSTEAD OF ONE GLOBAL CONNECTION:
      FastAPI handles requests concurrently (via async + a thread pool for
      sync code). A single shared psycopg2 connection is not safe to use
      from multiple threads at once. A small pool (e.g. 2-10 connections)
      gives each request its own connection for the duration of the
      request and returns it afterwards -- the standard fix.

CHANGES FROM PHASE 3:
      - fetch_all_queries() now also SELECTs recent_count so the Trie
        starts up with the current trending signal already loaded.
      - upsert_query() is substantially rewritten to handle the
        recent_count / last_searched_at windowed-reset logic. See the
        function's docstring for the full explanation.

WHAT BREAKS IF YOU REMOVE THIS FILE: every other module would need its own
      copy of the connection logic, and a typo in a SQL string would no
      longer be isolated to one reviewable place. The app would still
      "work" if you inlined this into main.py, but you'd have wrecked the
      separation between "HTTP concerns" (main.py) and "persistence
      concerns" (db.py) that makes the system testable in isolation.
"""

from contextlib import contextmanager
from typing import Iterator

import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor

from app.config import settings

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def init_pool(minconn: int = 2, maxconn: int = 10) -> None:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn, maxconn, settings.dsn
        )


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


@contextmanager
def get_cursor(commit: bool = False) -> Iterator[psycopg2.extras.RealDictCursor]:
    """
    Borrow a connection from the pool, hand back a dict-cursor, and always
    return the connection to the pool when done -- even on error. This
    context-manager pattern is what stops connections from leaking under
    exceptions (a very common bug in raw-SQL codebases).
    """
    assert _pool is not None, "init_pool() must be called before get_cursor()"
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def fetch_all_queries() -> list[dict]:
    """
    Used exactly once per process lifetime: at startup, to build the
    in-memory Trie (see main.py's lifespan). Now fetches recent_count
    alongside historical_count so the Trie starts up with the trending
    signal already loaded -- without this, the first TTL window after
    a restart would rank everything by historical_count alone even for
    queries that had a recent spike, until they were searched again
    through POST /search and the Trie re-received their recent_count.
    That would be a confusing, hard-to-reproduce ranking discrepancy.
    """
    with get_cursor() as cur:
        cur.execute(
            "SELECT query_text, historical_count, recent_count FROM queries;"
        )
        return cur.fetchall()


def upsert_query(query_text: str, initial_count: int = 1) -> dict:
    """
    The core write of the whole assignment, extended for Phase 4 trending.

    ── PHASE 3 (unchanged): ATOMIC UPSERT FOR historical_count ────────────
    INSERT ... ON CONFLICT DO UPDATE handles the "new query vs existing
    query" decision atomically inside Postgres -- no SELECT-then-decide
    race condition. See Phase 3's docstring for the full argument.

    ── PHASE 4 (new): WINDOWED recent_count RESET ─────────────────────────
    We also need to increment recent_count, but with a twist: if
    last_searched_at is more than RECENCY_WINDOW_MINUTES ago, recent_count
    should RESET to 1 (this search opens a new window) rather than
    accumulating stale counts from an expired window. A query searched
    1,000 times three days ago and not since should have recent_count=0
    or 1, not 1,001.

    WHY "LAZY EXPIRY" (reset on next write) INSTEAD OF A BACKGROUND JOB:
      Option A (background scheduler / cron): a periodic task scans all
      rows and zeroes out recent_count where last_searched_at > window.
      Cost: a full-table scan every N minutes, an extra process to keep
      running, and a failure mode if the job doesn't run (counts stay
      high forever). Gain: recent_count is always accurate even between
      writes, which matters if you're directly querying the DB for
      "what's trending" -- but we're NOT doing that; only the Trie reads
      this signal, and the Trie only gets updated on writes anyway.
      Option B (lazy expiry, this implementation): reset on the next
      write that notices the window has elapsed. No scheduler, no extra
      process, no cron job. The reset is piggybacked on the write that's
      already happening. Trade-off: between writes, recent_count stays
      "stale-but-harmlessly-so" -- it can only be HIGHER than it should
      be (from counts in an expired window), never lower, and it only
      affects ranking within the Trie which is also only updated on
      writes. For a typeahead system this is fine. For a billing system
      ("user has $X credit remaining") it would absolutely not be fine.

    THE SQL LOGIC (CASE expression, not application code):
      We use a CASE expression inside the UPSERT's DO UPDATE SET clause
      so the window-check-and-reset is a single atomic SQL statement,
      not a SELECT followed by an UPDATE. This keeps the same
      concurrency-safety property as the Phase 3 UPSERT: two concurrent
      POST /search calls for the same query can't race on the window check
      any more than they could race on the count increment, because Postgres
      serializes the conflict resolution at the row level.

      The expression:
        CASE
          WHEN queries.last_searched_at IS NULL
            OR queries.last_searched_at < now() - INTERVAL '%s minutes'
          THEN 1          -- new window: reset to 1 (this search IS the window)
          ELSE queries.recent_count + 1   -- still within window: accumulate
        END

      %s is the recency window in minutes, passed as a parameter (not
      string-interpolated) so there's no SQL injection risk even though
      it's a number -- we keep the parameterized-query discipline uniform.
    """
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO queries (query_text, historical_count, recent_count, last_searched_at)
            VALUES (%s, %s, 1, now())
            ON CONFLICT (query_text)
            DO UPDATE SET
                historical_count = queries.historical_count + 1,
                recent_count = CASE
                    WHEN queries.last_searched_at IS NULL
                      OR queries.last_searched_at < now() - (%s * INTERVAL '1 minute')
                    THEN 1
                    ELSE queries.recent_count + 1
                END,
                last_searched_at = now()
            RETURNING query_text, historical_count, recent_count, last_searched_at;
            """,
            (query_text, initial_count, settings.recency_window_minutes),
        )
        return cur.fetchone()


def fetch_one_query(query_text: str) -> dict | None:
    """
    Phase 5: Fetch a single row by query_text after a batch flush,
    so the Trie can be updated with the post-flush DB counts rather than
    just the incremental batch delta.

    WHY THIS IS SAFE TO CALL OUTSIDE A LOCK:
      This is a read-only SELECT. Concurrent upserts may update the row
      between when we committed the flush and when we call this function,
      but that only means the Trie gets a SLIGHTLY NEWER count, which is
      strictly better than using the older count. The Trie is eventually
      consistent with Postgres; we're not trying to make them bit-identical.
    """
    with get_cursor() as cur:
        cur.execute(
            "SELECT query_text, historical_count, recent_count FROM queries WHERE query_text = %s;",
            (query_text,),
        )
        return cur.fetchone()


def upsert_query_batch(query_text: str, increment: int) -> dict:
    """
    Phase 5: Batch-writer variant of upsert_query().

    WHAT IS DIFFERENT FROM upsert_query():
      Instead of incrementing by 1 (one synchronous search event), this
      increments historical_count by `increment` — the aggregated count
      that the BatchWriter has accumulated for this query over one flush
      window. The recency-window logic is unchanged: we still reset
      recent_count if last_searched_at is outside the window, and
      increment it by `increment` if we're still within the window.

    WHY SEPARATE FUNCTION AND NOT A PARAMETER ON upsert_query():
      Keeping the original upsert_query() intact lets the Phase 2/3/4
      synchronous path remain testable in isolation, and makes the diff
      between "Phase 4 code" and "Phase 5 code" clear. In a real codebase
      you'd likely unify them with a default increment=1 parameter.

    RETURNING CLAUSE:
      Returns the updated row so the batch_writer's on_flush_callback
      can pass the new counts to trie.insert(). This is the same
      "single round-trip, read what you wrote" pattern from upsert_query().
    """
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO queries (query_text, historical_count, recent_count, last_searched_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (query_text)
            DO UPDATE SET
                historical_count = queries.historical_count + %s,
                recent_count = CASE
                    WHEN queries.last_searched_at IS NULL
                      OR queries.last_searched_at < now() - (%s * INTERVAL '1 minute')
                    THEN %s
                    ELSE queries.recent_count + %s
                END,
                last_searched_at = now()
            RETURNING query_text, historical_count, recent_count, last_searched_at;
            """,
            (
                query_text,
                increment,        # initial historical_count on INSERT
                increment,        # initial recent_count on INSERT
                increment,        # historical_count += increment on UPDATE
                settings.recency_window_minutes,
                increment,        # reset recent_count to increment on window expiry
                increment,        # recent_count += increment within window
            ),
        )
        return cur.fetchone()


def count_queries() -> int:
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM queries;")
        return cur.fetchone()["n"]
