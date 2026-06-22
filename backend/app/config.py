"""
config.py
---------
WHAT: Centralizes every environment-dependent value (DB connection info,
      trie tuning, cache topology, trending window) behind one object.

WHY IT EXISTS: Without this file, connection strings and tunables end up
      hard-coded inside main.py and db.py. That's fine for a 20-line demo,
      but it's the first thing an interviewer will poke at ("what if you
      had to point this at a staging DB?"). Reading config from environment
      variables (with sane local defaults) is the standard 12-factor-app
      pattern and costs ~15 lines.

CHANGES FROM PHASE 3:
      Added recency_window_minutes and recency_weight for Phase 4 trending.

WHAT BREAKS IF YOU REMOVE THIS FILE: nothing breaks immediately if you
      inline the values into db.py instead -- but you lose the single
      place to change "what database am I talking to", and every other
      module that needs a setting would need to know how to read env vars
      itself. It's a separation-of-concerns file, not a functionality file.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_host: str = os.getenv("DB_HOST", "localhost")
    db_port: int = int(os.getenv("DB_PORT", "5432"))
    db_name: str = os.getenv("DB_NAME", "typeahead")
    db_user: str = os.getenv("DB_USER", "postgres")
    db_password: str = os.getenv("DB_PASSWORD", "postgres")

    # Phase 1 only: cap how many nodes the Trie's DFS will walk before it
    # gives up and just returns the best-so-far results. This exists
    # because of a real measured fact about THIS dataset: some prefixes
    # (e.g. a single letter) match tens of thousands of trie nodes, and
    # walking all of them on every keystroke would blow the latency budget.
    # Phase 3's cache is the real fix for hot prefixes; this is a safety valve.
    max_dfs_nodes: int = int(os.getenv("MAX_DFS_NODES", "20000"))

    suggestion_limit: int = 10

    # --- Phase 3: distributed cache (consistent hashing) ---------------
    cache_node_count: int = int(os.getenv("CACHE_NODE_COUNT", "3"))

    # V=150: smallest virtual-node count that keeps key-distribution
    # imbalance under 10% on our real 1.46M-key prefix space.
    # Measured, not guessed -- see docs/vnode_sensitivity_results.md.
    cache_virtual_nodes: int = int(os.getenv("CACHE_VIRTUAL_NODES", "150"))

    # TTL is the bound on how stale a cached suggestion list can be.
    # Phase 4 makes staleness matter MORE (cached lists can now have the
    # wrong RANKING, not just wrong counts) -- but we keep 45s as a
    # reasonable default and defer active invalidation to Phase 5.
    cache_ttl_seconds: float = float(os.getenv("CACHE_TTL_SECONDS", "45"))

    # --- Phase 4: trending / recency-aware ranking ----------------------

    # How long (in minutes) a search counts as "recent". Queries searched
    # within this window contribute to recent_count; searches older than
    # this trigger a reset of recent_count on the next write (lazy expiry).
    # Default: 60 minutes. In a real system you'd tune this per product:
    # a news site might use 15 minutes; an e-commerce site might use 6 hours.
    # For a viva demo, set RECENCY_WINDOW_MINUTES=1 to see the window expire
    # in real time rather than waiting an hour.
    recency_window_minutes: int = int(os.getenv("RECENCY_WINDOW_MINUTES", "60"))

    # How much weight to give recent_count in the combined score.
    # score = historical_count + (recent_count × recency_weight)
    #
    # HOW THIS VALUE WAS CHOSEN (from our real dataset):
    #   The 10th-ranked query for prefix "iphone" is "iphone 12 case"
    #   with historical_count=227. The 11th-ranked is "iphone 14 plus case"
    #   with historical_count=204. For a trending surge on "iphone 14 plus
    #   case" to push it above rank 10, it needs score > 227, meaning it
    #   needs recent_count × weight > 23. At weight=3, that takes only 8
    #   additional searches in the current window -- clearly too easy.
    #   At weight=5, it takes 5 searches -- still too easy for a query
    #   that already has 204 all-time searches (it would feel random).
    #   The goal is for the recency boost to feel EARNED: a query needs
    #   a genuinely notable recent spike (~20-30 searches in the window)
    #   to outrank a historically more popular query. At weight=3:
    #     iphone 14 plus case: 204 + (30 × 3) = 294 > 227 → passes rank 10
    #   That threshold (30 searches to break top-10 for that gap) feels
    #   right for a "this is actually trending" signal vs noise. We choose
    #   weight=3 as the default and keep it configurable so a viva can ask
    #   "what if you set it to 10?" and we can demonstrate live.
    recency_weight: int = int(os.getenv("RECENCY_WEIGHT", "3"))

    # --- Phase 5: batch writer -------------------------------------------
    # How often the background flush thread writes buffered events to Postgres.
    batch_flush_interval_seconds: float = float(os.getenv("BATCH_FLUSH_INTERVAL", "5.0"))

    # If the buffer accumulates this many DISTINCT queries before the next
    # flush timer fires, flush immediately. Caps memory growth under bursty
    # diverse-query traffic.
    batch_max_buffer_size: int = int(os.getenv("BATCH_MAX_BUFFER_SIZE", "100"))

    @property
    def dsn(self) -> str:
        return (
            f"host={self.db_host} port={self.db_port} "
            f"dbname={self.db_name} user={self.db_user} "
            f"password={self.db_password}"
        )


settings = Settings()
