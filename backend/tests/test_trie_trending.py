"""
test_trie_trending.py
---------------------
Phase 4: Tests for the trending / recency-aware ranking logic in the Trie.

These tests are SEPARATE FROM test_trie.py by design. test_trie.py covers
the fundamental correctness of the Trie (prefix matching, DFS, limit, edge
cases). test_trie_trending.py covers ONLY the new ranking behaviour.
Mixing them would make test_trie.py's "does basic prefix lookup work?"
tests harder to read, and would mean changing the weight formula silently
breaks tests that were supposed to be about something else.

Run with: pytest backend/tests/test_trie_trending.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.trie import Trie  # noqa: E402


def _trie_with_data() -> Trie:
    """
    A minimal Trie with 4 queries whose ranking differs clearly between
    basic and trending mode. Values chosen to make the arithmetic
    easy to verify by hand in a viva:

      "iphone 15 pro case"    hist=987, recent=0   -> basic=987, trending=987
      "iphone 12 case"        hist=227, recent=0   -> basic=227, trending=227
      "iphone 14 plus case"   hist=204, recent=30  -> basic=204, trending=294  (w=3)
      "iphone charger"        hist=50,  recent=0   -> basic=50,  trending=50

    Under basic: 987 > 227 > 204 > 50
    Under trending (w=3): 987 > 294 > 227 > 50
    "iphone 14 plus case" jumps from rank 3 to rank 2.
    """
    t = Trie()
    t.insert("iphone 15 pro case",  987, 0)
    t.insert("iphone 12 case",      227, 0)
    t.insert("iphone 14 plus case", 204, 30)
    t.insert("iphone charger",       50, 0)
    return t


def test_basic_mode_ranks_by_historical_count_only():
    t = _trie_with_data()
    results = t.suggest("iphone", mode="basic", recency_weight=3)
    queries = [r["query"] for r in results]
    assert queries == [
        "iphone 15 pro case",
        "iphone 12 case",
        "iphone 14 plus case",
        "iphone charger",
    ]


def test_trending_mode_boosts_high_recent_count():
    t = _trie_with_data()
    results = t.suggest("iphone", mode="trending", recency_weight=3)
    queries = [r["query"] for r in results]
    # "iphone 14 plus case" has score 204 + 30*3 = 294 > 227, jumps to rank 2
    assert queries[0] == "iphone 15 pro case"   # 987, unchanged
    assert queries[1] == "iphone 14 plus case"  # 294, promoted
    assert queries[2] == "iphone 12 case"        # 227, demoted
    assert queries[3] == "iphone charger"        # 50


def test_trending_score_matches_formula():
    """Verify the score field = hist + recent * weight, not something else."""
    t = _trie_with_data()
    results = t.suggest("iphone", mode="trending", recency_weight=3)
    by_query = {r["query"]: r for r in results}

    r = by_query["iphone 14 plus case"]
    assert r["count"] == 204
    assert r["recent_count"] == 30
    assert r["score"] == 204 + 30 * 3  # == 294

    r2 = by_query["iphone 12 case"]
    assert r2["score"] == 227 + 0 * 3  # == 227, no boost


def test_basic_mode_score_equals_historical_count():
    """In basic mode, score must equal count -- no recency contribution."""
    t = _trie_with_data()
    results = t.suggest("iphone", mode="basic", recency_weight=3)
    for r in results:
        assert r["score"] == r["count"], (
            f"basic mode score should equal count for {r['query']}: "
            f"got score={r['score']}, count={r['count']}"
        )


def test_zero_recent_count_is_unaffected_by_weight():
    """A query with recent_count=0 scores identically in basic and trending."""
    t = Trie()
    t.insert("python tutorial", 900, 0)
    t.insert("python pip", 100, 0)

    basic = t.suggest("python", mode="basic", recency_weight=3)
    trending = t.suggest("python", mode="trending", recency_weight=3)

    assert [r["query"] for r in basic] == [r["query"] for r in trending]
    assert [r["score"] for r in basic] == [r["score"] for r in trending]


def test_higher_weight_amplifies_recency_boost():
    """Doubling the weight should double the recency contribution."""
    t = Trie()
    t.insert("trending query", 100, 20)

    r_w3 = t.suggest("trend", mode="trending", recency_weight=3)[0]
    r_w6 = t.suggest("trend", mode="trending", recency_weight=6)[0]

    assert r_w3["score"] == 100 + 20 * 3  # 160
    assert r_w6["score"] == 100 + 20 * 6  # 220


def test_trending_mode_still_respects_limit():
    t = _trie_with_data()
    results = t.suggest("iphone", limit=2, mode="trending", recency_weight=3)
    assert len(results) == 2
    # Should be the TOP 2 by trending score
    assert results[0]["query"] == "iphone 15 pro case"   # 987
    assert results[1]["query"] == "iphone 14 plus case"  # 294


def test_insert_overwrites_recent_count():
    """After re-inserting with a new recent_count, trending rank updates."""
    t = Trie()
    t.insert("slow query", 500, 0)
    t.insert("fast query", 100, 0)

    # fast query ranks 2nd initially
    results = t.suggest("", mode="trending", recency_weight=3)
    # empty prefix, but no queries start with "" so we need a real prefix
    results = t.suggest("fa", mode="trending", recency_weight=3)
    assert results[0]["query"] == "fast query"
    assert results[0]["recent_count"] == 0

    # Now "fast query" goes trending
    t.insert("fast query", 101, 50)
    results = t.suggest("fa", mode="trending", recency_weight=3)
    assert results[0]["recent_count"] == 50
    assert results[0]["score"] == 101 + 50 * 3  # 251


def test_response_always_includes_recent_count_and_score_fields():
    """Both modes must return recent_count and score so callers can
    show the formula's inputs and output without having to call the API
    twice or inspect internal state."""
    t = _trie_with_data()
    for mode in ("basic", "trending"):
        results = t.suggest("iphone", mode=mode, recency_weight=3)
        for r in results:
            assert "query" in r
            assert "count" in r
            assert "recent_count" in r
            assert "score" in r
