"""
test_cache.py
--------------
WHY THIS FILE EXISTS: same argument as test_trie.py -- main.py's job is
HTTP only, so the consistent-hash ring and the cache-aside store have to
be testable with ZERO FastAPI and ZERO Postgres involved. Every test here
runs against plain HashRing / CacheCluster objects.

Run with:  pytest backend/tests/test_cache.py -v
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cache_node import CacheCluster, CacheNode  # noqa: E402
from app.hash_ring import HashRing  # noqa: E402


# ---------------------------------------------------------------------------
# HashRing
# ---------------------------------------------------------------------------

def test_ring_routes_every_key_to_a_known_node():
    ring = HashRing(physical_nodes=["a", "b", "c"], virtual_nodes=50)
    for key in ["iphone", "i", "samsung galaxy", ""]:
        assert ring.get_node(key) in {"a", "b", "c"}


def test_ring_routing_is_deterministic():
    """Same key, same ring config -> same node, every time. A consistent
    hash that isn't actually consistent within a single process would be
    a contradiction in terms."""
    ring = HashRing(physical_nodes=["a", "b", "c"], virtual_nodes=150)
    key = "iphone 15 pro"
    first = ring.get_node(key)
    for _ in range(100):
        assert ring.get_node(key) == first


def test_two_separately_built_rings_with_same_config_agree():
    """Two HashRing instances built from the same node list + same V must
    route identically -- this is what makes it safe for, e.g., a
    standalone debug script to agree with the running API process about
    who owns a key, without sharing any state."""
    ring1 = HashRing(physical_nodes=["a", "b", "c"], virtual_nodes=150)
    ring2 = HashRing(physical_nodes=["a", "b", "c"], virtual_nodes=150)
    for key in ["iphone", "ipad", "samsung", "java tutorial", "x"]:
        assert ring1.get_node(key) == ring2.get_node(key)


def test_adding_a_node_only_remaps_a_minority_of_keys():
    """The entire selling point of consistent hashing over modulo hashing,
    proven directly: growing 3 -> 4 nodes should move roughly 1/4 of keys,
    not anywhere near all of them."""
    keys = [f"prefix-{i}" for i in range(2000)]
    ring3 = HashRing(physical_nodes=["a", "b", "c"], virtual_nodes=150)
    ring4 = HashRing(physical_nodes=["a", "b", "c", "d"], virtual_nodes=150)

    moved = sum(1 for k in keys if ring3.get_node(k) != ring4.get_node(k))
    fraction_moved = moved / len(keys)

    # Generous bounds (not pinned to exactly 25%) since this is a property
    # test over a specific small sample of synthetic keys, not the full
    # sensitivity-study dataset -- see
    # docs/vnode_sensitivity_results.md for the precise, large-sample
    # measurement (25.15% on the real 1.46M-key dataset).
    assert 0.10 < fraction_moved < 0.45


def test_node_counts_sum_to_total_virtual_nodes():
    ring = HashRing(physical_nodes=["a", "b", "c"], virtual_nodes=150)
    counts = ring.node_counts()
    assert sum(counts.values()) == 3 * 150
    assert set(counts.keys()) == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# CacheNode (single node, TTL behaviour)
# ---------------------------------------------------------------------------

def test_cache_node_miss_on_unknown_key():
    node = CacheNode(node_id="n0", default_ttl_seconds=60)
    assert node.get("iphone") is None
    assert node.misses == 1
    assert node.hits == 0


def test_cache_node_hit_after_set():
    node = CacheNode(node_id="n0", default_ttl_seconds=60)
    node.set("iphone", [{"query": "iphone", "count": 100}])
    result = node.get("iphone")
    assert result == [{"query": "iphone", "count": 100}]
    assert node.hits == 1


def test_cache_node_entry_expires_after_ttl():
    node = CacheNode(node_id="n0", default_ttl_seconds=0.05)  # 50ms
    node.set("iphone", [{"query": "iphone", "count": 100}])
    assert node.get("iphone") is not None  # still fresh
    time.sleep(0.07)
    assert node.get("iphone") is None  # expired -> treated as a miss


def test_cache_node_peek_ttl_does_not_count_as_hit_or_miss():
    node = CacheNode(node_id="n0", default_ttl_seconds=60)
    node.set("iphone", [{"query": "iphone", "count": 100}])
    remaining = node.peek_ttl("iphone")
    assert remaining is not None and remaining > 0
    assert node.hits == 0 and node.misses == 0  # debug peek is non-mutating


def test_cache_node_peek_ttl_returns_none_for_expired_entry():
    node = CacheNode(node_id="n0", default_ttl_seconds=0.05)
    node.set("iphone", [{"query": "iphone", "count": 100}])
    time.sleep(0.07)
    assert node.peek_ttl("iphone") is None


# ---------------------------------------------------------------------------
# CacheCluster (routing + cache-aside integration)
# ---------------------------------------------------------------------------

def test_cluster_set_then_get_is_a_hit_on_the_same_node():
    cluster = CacheCluster(node_ids=["n0", "n1", "n2"], virtual_nodes=150, default_ttl_seconds=60)
    set_node = cluster.set("iphone", [{"query": "iphone", "count": 100}])
    value, get_node = cluster.get("iphone")
    assert value == [{"query": "iphone", "count": 100}]
    assert set_node == get_node  # same key must always route to the same node


def test_cluster_debug_reports_miss_for_uncached_prefix():
    cluster = CacheCluster(node_ids=["n0", "n1", "n2"], virtual_nodes=150, default_ttl_seconds=60)
    result = cluster.debug("never-cached-prefix")
    assert result["cache_hit"] is False
    assert result["ttl_remaining_seconds"] is None
    assert result["node"] in {"n0", "n1", "n2"}


def test_cluster_debug_reports_hit_with_ttl_after_set():
    cluster = CacheCluster(node_ids=["n0", "n1", "n2"], virtual_nodes=150, default_ttl_seconds=60)
    cluster.set("iphone", [{"query": "iphone", "count": 100}])
    result = cluster.debug("iphone")
    assert result["cache_hit"] is True
    assert result["ttl_remaining_seconds"] > 0


def test_cluster_stats_aggregate_hits_and_misses():
    cluster = CacheCluster(node_ids=["n0", "n1", "n2"], virtual_nodes=150, default_ttl_seconds=60)
    cluster.get("a")  # miss
    cluster.get("b")  # miss
    cluster.set("a", [{"query": "a", "count": 1}])
    cluster.get("a")  # hit

    stats = cluster.stats()
    assert stats["total_hits"] == 1
    assert stats["total_misses"] == 2
    assert stats["hit_rate"] == round(1 / 3, 4)


def test_cluster_stats_hit_rate_is_none_with_zero_traffic():
    cluster = CacheCluster(node_ids=["n0", "n1", "n2"], virtual_nodes=150, default_ttl_seconds=60)
    assert cluster.stats()["hit_rate"] is None
