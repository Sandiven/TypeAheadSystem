# Virtual-Node Sensitivity Study — Results

**Script:** `backend/scripts/vnode_sensitivity_study.py`
**Run against:** the real `data/queries.csv` (150,000 rows) shipped with this project.
**Re-run yourself:** `cd backend && python scripts/vnode_sensitivity_study.py`

## Why this study exists

The assignment requires "the cache should be distributed across multiple
logical cache nodes" using consistent hashing. Consistent hashing's actual
load balance depends on the **virtual-node count (V)** — too few virtual
nodes and a handful of physical nodes can end up unevenly loaded purely by
the luck of where MD5 happens to place 3 points on a 4-billion-slot ring.
Rather than pick a published "V=100-200 is typical" number from someone
else's blog post, we measured imbalance **on our own key space**.

## What counts as a "key" here

`GET /suggest` is called once per keystroke (after debouncing — debouncing
changes request *rate*, not which prefix strings get requested). That means
the real cache key population is **every distinct prefix of every query** in
the dataset, not the 150,000 full query strings themselves. We built that
full prefix set from `queries.csv` and got:

```
150,000 full queries  →  1,458,738 distinct prefix keys
```

That ~9.7x expansion is exactly what you'd expect from a typeahead
workload — short prefixes like `"i"` or `"ip"` are shared by thousands of
full queries, but the keyspace as a whole is dominated by the long tail of
near-unique longer prefixes.

## Method

For each candidate V, build a 3-physical-node ring with V virtual replicas
per physical node, route all 1,458,738 prefix keys through it, and measure:

```
imbalance = (max_node_key_count − min_node_key_count) / mean_node_key_count
```

0% = perfectly even load across the 3 nodes. We treat **< 10% imbalance**
as the threshold for "good enough" — at 3 nodes, a 10% swing means the
busiest node handles roughly 1.1x the average load, which is well within
what a cache-aside layer's TTL/eviction behavior can absorb without one
node becoming a meaningful hotspot relative to the others.

## Measured results

| V (vnodes/node) | node-0 | node-1 | node-2 | imbalance | ring build time | routing 1.46M keys |
|---:|---:|---:|---:|---:|---:|---:|
| 50 | 522,777 | 529,010 | 406,951 | **25.10%** | 0.4 ms | 1739 ms |
| 150 | 485,698 | 504,394 | 468,646 | **7.35%** | 0.7 ms | 1845 ms |
| 500 | 472,886 | 492,547 | 493,305 | **4.20%** | 43.5 ms | 1912 ms |

## Decision

**V = 150 virtual nodes per physical node** is the smallest candidate that
clears the 10% imbalance threshold (7.35% measured, vs. 25.10% at V=50).
Going to V=500 buys another ~3 points of balance (4.20% vs 7.35%) at the
cost of ~60x longer ring-construction time (43.5 ms vs 0.7 ms) for a ring
that, in this implementation, is rebuilt on every process start and on
every node-membership change. That trade isn't worth it here — 7.35%
imbalance on a 3-node cache-aside layer is not going to be visible as a
user-facing latency difference, while a slower ring rebuild has a small
but real cost on every restart. **This is the production default wired
into `config.py` (`CACHE_VIRTUAL_NODES=150`).**

Routing time itself (~1.8s to route 1.46M keys, i.e. ~1.2 microseconds per
key) is dominated by Python-level overhead of the benchmark loop itself,
not by the binary search — `bisect_left` over a 450-entry sorted list
(3 nodes × 150 vnodes) is effectively free per call. This is irrelevant to
real request latency anyway, since a real request routes exactly ONE key,
not 1.46 million.

## Bonus measurement: why consistent hashing, with our own numbers

To make the "consistent hashing vs. modulo hashing" argument concrete
instead of asserting it, we measured how many of the 1,458,738 prefix keys
change owning node when a 4th physical node is added to a 3-node ring
(V=150 in both cases):

| Hashing scheme | Keys remapped on 3→4 node growth |
|---|---:|
| Consistent hashing (this implementation) | 366,884 / 1,458,738 = **25.15%** |
| Naive `hash(key) % N` | 1,093,807 / 1,458,738 = **74.98%** |

This matches the theoretical expectation almost exactly: consistent
hashing should remap close to `1/N_new` ≈ 25% of keys when growing from 3
to 4 nodes (only the new node's claimed arc moves), while modulo hashing
remaps close to `(N_new-1)/N_new` ≈ 75% (changing the modulus changes
nearly every key's `% N` result). On a live cache, that's the difference
between a brief, bounded wave of misses confined to ~1/4 of the keyspace
versus an effective full cache wipe across 3 out of every 4 keys the
instant you scale the cache tier.
