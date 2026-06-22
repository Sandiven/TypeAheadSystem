-- =============================================================================
-- Migration 002 — Add trending / recency columns to the queries table
-- Phase 4: Trending Searches
--
-- HOW TO RUN:
--   psql -h localhost -U postgres -d typeahead -f db/migrations/002_add_trending_columns.sql
--
-- WHY ALTER TABLE AND NOT DROP + RECREATE:
--   Dropping and recreating the table would destroy all existing data and
--   reset the BIGSERIAL sequence, breaking anything (e.g. external logs,
--   analytics, other systems) that holds a reference to an existing row's
--   `id`. ALTER TABLE is how production schema evolution works: you add
--   columns that have defaults (so every existing row gets a valid value
--   immediately and the table is never in an inconsistent state), and you
--   never destroy data you don't need to destroy. The cost is a brief
--   metadata lock on Postgres (effectively instant for adding a nullable or
--   DEFAULT column at this row count); no full-table rewrite is needed for
--   a DEFAULT-bearing NOT NULL column on modern Postgres (12+).
--
-- WHY THESE THREE COLUMNS AND NOT JUST ONE:
--   The naive approach is to replace `historical_count` with a
--   time-decaying value — multiply by e^(-λt) as time passes, so old
--   popularity fades. That loses all institutional memory of genuine long-
--   term popularity: "python tutorial" might have 900 all-time searches and
--   suddenly rank below a one-day spike of 20 searches for "elden ring dlc"
--   within a single hour. You can't recover the all-time signal once you've
--   baked decay into it. Keeping them as separate columns makes each signal
--   independently interpretable and the combined score transparent:
--
--   historical_count  — The all-time cumulative search count for this query.
--                       Only ever incremented (by 1 per POST /search), never
--                       decremented, never decayed. This is the "legitimacy
--                       anchor" — it answers "how popular has this query
--                       EVER been?" and ensures a query must have genuine
--                       sustained demand to score well overall.
--
--   recent_count      — A rolling count of searches within the current
--                       recency window (configurable; default 1 hour). This
--                       IS reset to 1 when a new window starts (see db.py's
--                       upsert_query for the reset logic). It answers "how
--                       hot is this query RIGHT NOW?" — the trending signal.
--                       Default 0 because no existing query has been searched
--                       in the last hour from the system's perspective; they
--                       all start cold.
--
--   last_searched_at  — The wall-clock timestamp of the most recent POST
--                       /search for this query. Used in upsert_query() to
--                       decide whether recent_count should be incremented
--                       (still within the same window) or reset (window has
--                       elapsed). NULL for existing rows because we genuinely
--                       don't know when they were last searched; upsert_query
--                       will populate this on the next search for each query.
--
-- =============================================================================

BEGIN;

ALTER TABLE queries
    ADD COLUMN IF NOT EXISTS recent_count     BIGINT      NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_searched_at TIMESTAMPTZ;

-- Index on recent_count for the same reason we index historical_count:
-- startup Trie rebuild will now fetch recent_count too, and any admin /
-- debug query that wants "what's trending right now?" should be fast.
-- This index is partial: only rows with recent_count > 0 are actually
-- trending, so a partial index is smaller and faster than a full one.
CREATE INDEX IF NOT EXISTS idx_queries_recent_count
    ON queries (recent_count DESC)
    WHERE recent_count > 0;

COMMIT;
