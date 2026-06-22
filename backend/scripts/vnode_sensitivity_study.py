"""
vnode_sensitivity_study.py
---------------------------
WHAT: Answers one question with real numbers instead of a guess: "how
      many virtual nodes per physical cache node do we actually need, on
      OUR data, to get an evenly-balanced ring?"

WHY THIS HAS TO RUN ON *OUR* DATA AND NOT JUST CITE A PUBLISHED NUMBER:
      Published rules of thumb ("100-200 virtual nodes is typical") are
      derived from THEIR key distributions, which are not ours. The
      thing that actually determines imbalance is how many DISTINCT KEYS
      get hashed and how they land relative to the ring's virtual-node
      boundaries -- and our key space isn't "150,000 random strings", it's
      "every prefix of every one of 150,000 search queries", because
      GET /suggest is called once per keystroke, and every keystroke
      produces a different prefix string as a cache key. A 1-character
      prefix and a 19-character prefix are both real keys this system
      will see. We build that exact prefix set from the real CSV, hash
      each one, and measure how evenly the resulting load actually
      spreads across nodes at a few candidate virtual-node counts.

METHOD:
  1. Load every query string from data/queries.csv.
  2. Build the set of ALL PREFIXES of all queries (e.g. "iphone 15 pro"
     contributes "i", "ip", "iph", ..., "iphone 15 pro") -- this is the
     full key space /suggest can ever be asked to cache, deduplicated.
  3. For each candidate V in {50, 150, 500}: build a HashRing with 3
     physical nodes and V virtual nodes each, route every prefix key
     through it, and count how many keys land on each physical node.
  4. Report, per V: per-node key counts, and an IMBALANCE metric defined
     as (max_node_count - min_node_count) / mean_node_count -- 0.0 would
     be perfectly even, 1.0 means the most-loaded node has roughly twice
     the least-loaded node's share, etc. This is a standard, easy-to-
     defend-in-a-viva imbalance metric (it's just the normalized range).
  5. Pick the smallest V that gets imbalance under a defensible threshold
     (we use 10%, i.e. 0.10 -- justified in the results doc) and use that
     as the production default in cache_node.py / config.py.

This script is NOT part of the running API -- it's a one-time (or
re-run-when-the-dataset-changes) analysis tool, the same category as
load_dataset.py. Its output is committed as docs/vnode_sensitivity_results.md
so the grading numbers don't depend on re-running it.
"""

from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow `import app`
from app.hash_ring import HashRing  # noqa: E402

CSV_PATH = Path(__file__).resolve().parents[2] / "data" / "queries.csv"
PHYSICAL_NODES = ["node-0", "node-1", "node-2"]
CANDIDATE_VNODE_COUNTS = [50, 150, 500]
IMBALANCE_THRESHOLD = 0.10  # 10% -- see docs/vnode_sensitivity_results.md


def load_queries() -> list[str]:
    if not CSV_PATH.exists():
        raise SystemExit(f"Dataset not found at {CSV_PATH}")
    queries: list[str] = []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            q = row["query"].strip().lower()
            if q:
                queries.append(q)
    return queries


def build_prefix_keyspace(queries: list[str]) -> list[str]:
    """Every distinct prefix of every query, 1 character up to the full
    string -- this IS the real cache-key population /suggest produces,
    since the frontend fires one request per keystroke (after
    debouncing, but debouncing changes request RATE, not which prefix
    strings get requested)."""
    prefixes: set[str] = set()
    for q in queries:
        for i in range(1, len(q) + 1):
            prefixes.add(q[:i])
    return list(prefixes)


def imbalance_ratio(counts: dict[str, int]) -> float:
    values = list(counts.values())
    mean = sum(values) / len(values)
    if mean == 0:
        return 0.0
    return (max(values) - min(values)) / mean


def main() -> None:
    t_load0 = time.perf_counter()
    queries = load_queries()
    keyspace = build_prefix_keyspace(queries)
    t_load = time.perf_counter() - t_load0

    print(f"Loaded {len(queries):,} queries from {CSV_PATH.name}")
    print(f"Built {len(keyspace):,} distinct prefix keys in {t_load:.2f}s")
    print(f"Physical nodes: {PHYSICAL_NODES}")
    print()

    header = f"{'V (vnodes/node)':>18} | {'build_ms':>9} | {'route_ms':>9} | " + " | ".join(
        f"{n:>10}" for n in PHYSICAL_NODES
    ) + f" | {'imbalance':>10} | verdict"
    print(header)
    print("-" * len(header))

    chosen_v = None
    results = []
    for v in CANDIDATE_VNODE_COUNTS:
        t0 = time.perf_counter()
        ring = HashRing(physical_nodes=PHYSICAL_NODES, virtual_nodes=v)
        build_ms = (time.perf_counter() - t0) * 1000

        t1 = time.perf_counter()
        counts = {n: 0 for n in PHYSICAL_NODES}
        for key in keyspace:
            counts[ring.get_node(key)] += 1
        route_ms = (time.perf_counter() - t1) * 1000

        imb = imbalance_ratio(counts)
        verdict = "OK (<10%)" if imb < IMBALANCE_THRESHOLD else "too uneven"
        if imb < IMBALANCE_THRESHOLD and chosen_v is None:
            chosen_v = v

        row = f"{v:>18} | {build_ms:>8.1f}ms | {route_ms:>8.1f}ms | " + " | ".join(
            f"{counts[n]:>10,}" for n in PHYSICAL_NODES
        ) + f" | {imb:>9.2%} | {verdict}"
        print(row)
        results.append((v, counts, imb, build_ms, route_ms))

    print()
    if chosen_v is not None:
        print(f"=> Smallest V under the {IMBALANCE_THRESHOLD:.0%} imbalance threshold: V={chosen_v}")
    else:
        print(f"=> None of the candidates got under {IMBALANCE_THRESHOLD:.0%}; consider a larger V.")

    return results, chosen_v


if __name__ == "__main__":
    main()
