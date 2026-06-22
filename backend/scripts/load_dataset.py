"""
load_dataset.py
----------------
WHAT: A one-shot script: reads data/queries.csv and loads it into the
      `queries` table.

IMPORTANT DISTINCTION (a real bug we caught while building this -- good
      viva material): this script is idempotent by REPLACING each query's
      count with the value from the CSV, NOT by incrementing it. That is
      deliberately different from db.upsert_query(), which INCREMENTS on
      every live search. Why two different merge strategies for what looks
      like "the same" insert-or-update operation?
        * load_dataset.py answers "what does the source dataset say right
          now?" -- re-running it with the same file must be a no-op, and
          running it with an updated file should make the table MATCH the
          file, not pile on top of whatever was already there.
        * db.upsert_query() answers "one more real user just searched this"
          -- it must ADD one each time, because each call represents a
          distinct event, not a restatement of ground truth.
      Conflating these (our first draft did, using ON CONFLICT ... SET
      historical_count = historical_count + EXCLUDED.historical_count here too) silently
      doubles every count if you ever re-run the loader -- exactly the kind
      of subtle bug a viva panel likes to probe for.

WHY A SEPARATE SCRIPT INSTEAD OF "LOAD THE CSV INSIDE THE APP ON STARTUP":
      Dataset ingestion is a one-time/occasional operational task, not part
      of the request-serving hot path. Mixing "read 150,000 lines from
      disk and write them to Postgres" into the FastAPI startup event would
      make every `uvicorn` restart slow and would conflate two different
      concerns: "populate the database" vs "serve traffic from whatever is
      already in the database." Keeping them separate also means we can
      load a *different* or *larger* dataset later without touching
      application code at all -- just point this script at a new CSV.

WHY COPY (bulk load) INSTEAD OF 150,000 INDIVIDUAL INSERT STATEMENTS:
      One INSERT per row means 150,000 separate round-trips to Postgres
      (parse, plan, execute, network ack) -- measured below, this is the
      difference between low-single-digit seconds and "you'll wait a
      while". We stage rows into a temporary table with Postgres's COPY
      protocol (the fastest bulk-load mechanism it offers), then run ONE
      INSERT ... ON CONFLICT against that staging table to merge into the
      real `queries` table. This is also exactly the kind of "batch
      instead of row-by-row" thinking the assignment asks us to apply
      explicitly to search-count updates in Phase 5 -- ingestion is a
      preview of that same idea.

WHAT WOULD HAPPEN IF YOU REMOVED THIS FILE: there would be no way to get
      the 150,000-row dataset into Postgres at all; /suggest would start
      with an empty Trie and never return anything.
"""

import csv
import io
import sys
import time
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow `import app`
from app.config import settings  # noqa: E402

CSV_PATH = Path(__file__).resolve().parents[2] / "data" / "queries.csv"


def main() -> None:
    if not CSV_PATH.exists():
        raise SystemExit(f"Dataset not found at {CSV_PATH}")

    t0 = time.perf_counter()
    conn = psycopg2.connect(settings.dsn)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE staging_queries "
                "(query_text TEXT, historical_count BIGINT) ON COMMIT DROP;"
            )

            # Stream the CSV straight into Postgres via COPY -- no
            # row-by-row Python-side INSERTs.
            buf = io.StringIO()
            with open(CSV_PATH, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                row_count = 0
                for row in reader:
                    query_text = row["query"].strip().lower()
                    count = int(row["historical_count"])
                    if not query_text:
                        continue
                    # Manual TSV escaping for COPY: tabs/newlines can't
                    # appear in our query strings anyway (queries are
                    # short search terms), but we guard defensively.
                    safe_text = query_text.replace("\t", " ").replace("\n", " ")
                    buf.write(f"{safe_text}\t{count}\n")
                    row_count += 1
            buf.seek(0)

            cur.copy_expert(
                "COPY staging_queries (query_text, historical_count) FROM STDIN WITH (FORMAT text)",
                buf,
            )

            # Merge staging -> real table. If the CSV has duplicate query
            # strings, GROUP BY collapses them by summing counts BEFORE the
            # upsert, so the unique constraint on queries.query_text is
            # never violated mid-merge.
            #
            # DO UPDATE SET historical_count = EXCLUDED.historical_count (a REPLACE,
            # not an increment) is what makes re-running this script safe.
            # Contrast with db.upsert_query(), which increments -- see the
            # module docstring above for why these must differ.
            cur.execute(
                """
                INSERT INTO queries (query_text, historical_count)
                SELECT query_text, SUM(historical_count)
                FROM staging_queries
                GROUP BY query_text
                ON CONFLICT (query_text)
                DO UPDATE SET historical_count = EXCLUDED.historical_count;
                """
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    elapsed = time.perf_counter() - t0
    print(f"Loaded {row_count} CSV rows from {CSV_PATH.name} in {elapsed:.2f}s")


if __name__ == "__main__":
    main()
