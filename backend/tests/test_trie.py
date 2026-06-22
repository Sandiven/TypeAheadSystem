"""
test_trie.py
------------
WHY THIS FILE EXISTS: db.py's docstring claims "keeping main.py thin keeps
business logic testable in isolation" -- this file is the proof. Every
test here runs with zero FastAPI, zero HTTP, and zero Postgres involved:
just a Trie object. That's the whole point of separating trie.py from
main.py in the first place.

Run with:  pytest backend/tests/test_trie.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.trie import Trie  # noqa: E402


def build_sample_trie() -> Trie:
    t = Trie()
    t.insert("iphone", 500)
    t.insert("iphone 15", 300)
    t.insert("iphone 15 pro", 900)
    t.insert("iphone charger", 200)
    t.insert("ipad", 150)
    t.insert("samsung", 100)
    return t


def test_prefix_match_returns_only_matching_queries():
    t = build_sample_trie()
    results = [r["query"] for r in t.suggest("ip")]
    assert all(q.startswith("ip") for q in results)
    assert "samsung" not in results


def test_results_sorted_by_count_descending():
    t = build_sample_trie()
    results = t.suggest("iphone")
    counts = [r["count"] for r in results]
    assert counts == sorted(counts, reverse=True)
    assert results[0]["query"] == "iphone 15 pro"  # highest count (900)


def test_respects_limit():
    t = build_sample_trie()
    results = t.suggest("ip", limit=2)
    assert len(results) == 2


def test_empty_prefix_returns_empty_list():
    t = build_sample_trie()
    assert t.suggest("") == []


def test_unknown_prefix_returns_empty_list_not_error():
    t = build_sample_trie()
    assert t.suggest("zzz-no-such-prefix") == []


def test_mixed_case_input_is_normalized():
    t = build_sample_trie()
    results_lower = t.suggest("iphone")
    results_mixed = t.suggest("IpHoNe")
    assert results_lower == results_mixed


def test_insert_overwrites_count_without_duplicating_entries():
    t = build_sample_trie()
    assert t.size == 6
    t.insert("iphone", 999)  # re-insert same query with a new count
    assert t.size == 6  # size must NOT grow on re-insert
    results = t.suggest("iphone", limit=10)
    matched = [r for r in results if r["query"] == "iphone"]
    assert matched[0]["count"] == 999


def test_exact_query_can_also_be_a_prefix_of_others():
    # "iphone" itself is both a complete query AND a prefix of "iphone 15".
    # Both must appear when searching "iphone".
    t = build_sample_trie()
    results = {r["query"] for r in t.suggest("iphone", limit=10)}
    assert "iphone" in results
    assert "iphone 15" in results
