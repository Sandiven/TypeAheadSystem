"""
trie.py
-------
WHAT: A classic character-level Trie (prefix tree) that supports:
        insert(query, historical_count, recent_count)  -> O(L)
        suggest(prefix, limit, mode)                   -> top `limit` matches,
                                                          ranked by score
                                                          (mode=trending) or
                                                          raw count
                                                          (mode=basic)

WHY A TRIE AND NOT A PLAIN DICT/HASHMAP OF "query -> count":
      A hashmap answers "does this exact string exist?" in O(1), but it
      cannot answer "give me every string that STARTS WITH this prefix"
      without scanning every key -- O(N) per keystroke, where N is the
      whole dataset (150,000+ strings here). A Trie turns "find everything
      under this prefix" into "walk to the node for this prefix in O(L)
      time, then walk the subtree under that one node" -- the cost depends
      on how *specific* the prefix already is, not on the size of the
      whole dataset. That's exactly the typeahead access pattern: the
      first character is broad, but by character 3-4 the matching subtree
      is usually small.

CHANGES FROM PHASE 3:
      - TrieNode now stores recent_count alongside count (historical_count).
        __slots__ is updated to include it.
      - insert() takes a recent_count parameter (defaults 0 for backwards
        compatibility with the startup bulk-load path that previously only
        had historical_count -- now fetch_all_queries() provides both).
      - suggest() now accepts a `mode` parameter:
          mode="basic"    -> sort by historical_count (Phase 1/2/3 behaviour)
          mode="trending" -> sort by score = historical_count +
                             (recent_count × recency_weight)
        Both modes use the SAME DFS subtree walk; the only difference is
        the sort key applied to the collected results at the end. This
        is deliberate: we don't want two separate code paths for the walk
        itself, because a bug in one would be invisible from the other.
        The mode parameter is just a switch on the final heapq.nlargest key.

WHY THE SCORING FORMULA IS IN THE TRIE AND NOT IN main.py:
      main.py's job is HTTP concerns (parse the request, pick a mode,
      return the response). Knowing what "score" means -- which columns
      contribute and with what weight -- is a data/ranking concern that
      belongs with the data structure that holds those columns. If scoring
      were computed in main.py, the Trie would hand back flat counts and
      main.py would need to know that recent_count should be multiplied by
      some weight -- leaking knowledge about the Trie's internal structure
      into the HTTP layer. Instead, suggest() accepts a mode string and
      the weight (from config) and handles everything: main.py just calls
      suggest(prefix, limit, mode, recency_weight) and gets back a ranked
      list, same as before.

WHY keep mode="basic" available at all:
      The assignment explicitly requires demonstrating "the difference
      between the two ranking approaches using sample data or logs". The
      cleanest way to do that live in a viva is GET /suggest?q=iphone&mode=basic
      vs GET /suggest?q=iphone&mode=trending -- same prefix, same API surface,
      side-by-side ranking. Without mode=basic, you'd have to restart the
      server with a feature flag to show the before/after, which is clunky
      and slow. Keeping mode as a request parameter costs nothing and makes
      the demo clean. In production you'd remove mode=basic once trending
      is validated -- or keep it behind an internal flag for A/B testing.

SCORING FORMULA (mode="trending"):
      score = historical_count + (recent_count × recency_weight)

      WHY THIS FORMULA AND NOT:
        - Pure decay (historical_count × e^(-λt)): loses all institutional
          memory of genuine long-term popularity. "Python tutorial" with 900
          all-time searches and no recent activity could rank below "elden
          ring dlc" after a 24-hour spike, and never recover without being
          searched again. Also requires computing a per-node time-based
          exponential on every Trie walk, which is exactly where we need to
          be cheapest (see the DFS loop -- it runs per node, not per query).
        - Sliding window only (drop historical_count): a brand-new query
          with 1 search in the last 10 minutes could outrank "iphone 15 pro
          case" with 987 all-time searches. Too easy to spam into top results.
        - This additive hybrid: historical_count provides the legitimacy
          anchor (a query must have sustained real demand to rank near the
          top), while recent_count × weight surfaces genuine trending spikes
          without overwhelming the baseline. The recency_weight parameter
          (default 3, configurable via RECENCY_WEIGHT env var) is the
          explicit, tunable trade-off knob between "stability matters" and
          "recency matters".

      WORKED EXAMPLE FROM OUR REAL DATASET (recency_weight=3):
        For prefix "iphone", under basic ranking:
          rank  1: iphone 15 pro case           historical=987  score=987
          rank 10: iphone 12 case               historical=227  score=227
          rank 11: iphone 14 plus case          historical=204  score=204

        After 30 POST /search calls for "iphone 14 plus case" (simulating
        a trending spike -- see scripts/simulate_trending.py):
          rank 11 → score = 204 + (30 × 3) = 294

        294 > 227, so "iphone 14 plus case" jumps to rank 10, overtaking
        "iphone 12 case". It now ranks above a query with 23 more all-time
        searches purely because of its recent activity. Under mode=basic,
        nothing changes -- "iphone 14 plus case" stays at rank 11 and the
        ranking is identical to Phase 1/2/3.

MEMORY: __slots__ updated to include recent_count. The memory overhead
      is one extra int per end-node (non-end nodes keep recent_count=0
      and are never scored). At ~150,000 end nodes out of ~1.46M total
      nodes, this adds roughly 150,000 × 28 bytes (CPython int overhead)
      ≈ ~4MB of extra RAM -- invisible at this scale.
"""

from __future__ import annotations

import heapq


class TrieNode:
    __slots__ = ("children", "is_end", "count", "recent_count")

    def __init__(self) -> None:
        self.children: dict[str, "TrieNode"] = {}
        self.is_end: bool = False
        self.count: int = 0          # historical_count
        self.recent_count: int = 0   # rolling count within the recency window


class Trie:
    def __init__(self, max_dfs_nodes: int = 20000) -> None:
        self.root = TrieNode()
        self.max_dfs_nodes = max_dfs_nodes
        self.size = 0  # number of distinct queries stored

    def insert(self, query: str, count: int, recent_count: int = 0) -> None:
        """
        Insert or overwrite a query's historical_count and recent_count.

        recent_count defaults to 0 for backwards compatibility, but in
        practice Phase 4 always provides it: at startup, fetch_all_queries()
        returns recent_count from Postgres; after a POST /search, main.py
        calls trie.insert() with the RETURNING values from upsert_query(),
        which now includes recent_count.

        WHY OVERWRITE RATHER THAN INCREMENT IN THE TRIE:
          The Trie is a read cache of the database's truth, not an
          independent counter. The authoritative increment happens in
          Postgres (db.upsert_query) and the Trie is told the new total
          via RETURNING. If we incremented inside the Trie, a process
          restart would lose those in-memory increments and start counting
          from the last DB value again -- which would be correct after a
          bulk reload, but confusing if the Trie and DB drift for any reason.
          "Trust the DB, mirror its value" is cleaner than "maintain a
          parallel counter".
        """
        node = self.root
        for ch in query:
            node = node.children.setdefault(ch, TrieNode())
        if not node.is_end:
            self.size += 1
        node.is_end = True
        node.count = count
        node.recent_count = recent_count

    def _find_prefix_node(self, prefix: str) -> TrieNode | None:
        node = self.root
        for ch in prefix:
            node = node.children.get(ch)
            if node is None:
                return None
        return node

    def suggest(
        self,
        prefix: str,
        limit: int = 10,
        mode: str = "trending",
        recency_weight: int = 3,
    ) -> list[dict]:
        """
        Returns up to `limit` result dicts, all starting with `prefix`,
        ranked by either basic or trending score.

        mode="basic"    -> sort by historical_count only (Phase 1/2/3 behaviour)
        mode="trending" -> sort by historical_count + (recent_count × recency_weight)

        Each returned dict contains:
          query          -- the full query string
          count          -- historical_count (all-time)
          recent_count   -- rolling count in the current recency window
          score          -- the value actually used for ranking
                           (= count in basic mode, = combined score in trending)

        WHY WE RETURN score IN THE RESPONSE:
          The assignment requires demonstrating the difference between the
          two modes. Returning score alongside count and recent_count means
          the caller (main.py, or curl, or the simulate_trending.py script)
          can show exactly WHY a query ranked where it did -- "this query
          has count=204, recent_count=30, weight=3, so score=294" -- without
          needing to reconstruct the formula externally. It's the same
          principle as returning cache_hit and node in Phase 3: put the
          system's reasoning into the response, don't make the caller infer it.

        CACHE INVALIDATION NOTE (Phase 4 limitation):
          If the Phase 3 cache returns a cached suggestion list for a prefix,
          it returns the list as it was ranked at cache-population time.
          A trending surge after that moment will change the correct ranking,
          but the cached list won't reflect it until the TTL expires.
          This is a KNOWN, ACCEPTED limitation for this phase -- the TTL
          (default 45s) bounds how stale a cached ranking can be, and
          active invalidation (push-based, on write) is Phase 5's job.
          For the simulation script, we bypass the cache by calling this
          method directly, so the demo always shows the live Trie state.
        """
        if not prefix:
            return []

        prefix = prefix.lower()
        start_node = self._find_prefix_node(prefix)
        if start_node is None:
            return []

        # Bounded DFS: same as Phase 1/2/3, unchanged.
        results: list[tuple[int, int, int, str]] = []  # (historical, recent, score, query)
        stack: list[tuple[TrieNode, str]] = [(start_node, prefix)]
        visited = 0

        while stack and visited < self.max_dfs_nodes:
            node, path = stack.pop()
            visited += 1
            if node.is_end:
                if mode == "trending":
                    score = node.count + node.recent_count * recency_weight
                else:
                    score = node.count  # mode="basic": pure historical count
                results.append((node.count, node.recent_count, score, path))
            for ch, child in node.children.items():
                stack.append((child, path + ch))

        top = heapq.nlargest(limit, results, key=lambda t: t[2])
        return [
            {
                "query": q,
                "count": hist,
                "recent_count": rc,
                "score": sc,
            }
            for hist, rc, sc, q in top
        ]
    def top_queries(self, limit: int = 10, mode: str = "trending", recency_weight: int = 3) -> list[dict]:
        """
        Walk the ENTIRE trie from root to find top queries globally.
        Only called by GET /trending, not by GET /suggest.
        """
        results = []
        stack = [(self.root, "")]
        visited = 0

        while stack and visited < self.max_dfs_nodes:
            node, path = stack.pop()
            visited += 1
            if node.is_end:
                if mode == "trending":
                    score = node.count + node.recent_count * recency_weight
                else:
                    score = node.count
                results.append((node.count, node.recent_count, score, path))
            for ch, child in node.children.items():
                stack.append((child, path + ch))

        top = heapq.nlargest(limit, results, key=lambda x: x[2])
        return [{"query": q, "count": c, "recent_count": rc, "score": sc}
                for c, rc, sc, q in top]