"""
hash_ring.py
------------
WHAT: A hand-rolled consistent-hashing ring. Given a set of logical cache
      nodes, places V virtual replicas of each node on a circular hash
      space (0 .. 2**32 - 1), then routes any key to "the first virtual
      node whose hash is >= hash(key), walking clockwise" via binary
      search over a sorted array of virtual-node hashes.

WHY CONSISTENT HASHING AND NOT `hash(key) % N`:
      Modulo hashing looks simpler, but it has a fatal property for any
      system that ever adds or removes a cache node: changing N changes
      `hash(key) % N` for almost every key, not just the keys that
      "belong" to the changed node. Concretely, going from 3 nodes to 4
      reshuffles ~75% of all keys to a different node on the very next
      request -- every one of those keys is now a guaranteed cache MISS
      the instant the topology changes, even though the underlying data
      didn't change at all. At typeahead scale that's a thundering-herd
      of Trie walks the moment you scale the cache tier up or down.

      Consistent hashing fixes this by hashing NODES onto the same ring
      as KEYS. A key is owned by "the nearest node clockwise from it on
      the ring." When a node is added or removed, only the keys that fall
      between the changed node and its previous/next neighbour on the
      ring move -- everything else's owner is untouched. With N nodes,
      removing one node only remaps ~1/N of all keys (not ~(N-1)/N like
      modulo hashing). This is the entire reason distributed caches
      (Memcached client libraries, DynamoDB partitioning, Cassandra,
      Riak) use this technique instead of modulo.

WHY VIRTUAL NODES AT ALL (not just one ring position per physical node):
      With only 1 ring position per physical node, the arc each node owns
      is whatever random gap happens to fall between it and its neighbour
      -- for only 3 physical nodes, that's extremely likely to be wildly
      uneven (one node could easily own 70% of the ring by pure chance of
      where MD5 happened to place 3 points). Spreading each physical node
      across many virtual positions on the ring averages this out: each
      physical node ends up owning many small, scattered arcs instead of
      one big lucky/unlucky one, and the LAW OF LARGE NUMBERS does the
      rest. How many virtual nodes are actually needed to get a
      defensibly even distribution on OUR data is not something to guess
      at -- see scripts/vnode_sensitivity_study.py and
      docs/vnode_sensitivity_results.md for the measured answer.

WHY MD5 (not Python's built-in hash()):
      Python's hash() is salted per-process by default (PYTHONHASHSEED)
      specifically to resist hash-flooding attacks -- which means the
      SAME key hashes to a DIFFERENT value every time you restart the
      process. A cache ring needs a hash that is stable across restarts
      and across every process that might ever need to agree on "who
      owns this key" (e.g. a debug script run separately from the API
      process). MD5 (or SHA-1) gives a deterministic, well-distributed
      128-bit (resp. 160-bit) digest; we truncate to the low 32 bits,
      which is more than enough entropy for a ring sized in the hundreds
      to low-thousands of virtual-node points. MD5's cryptographic
      weaknesses are irrelevant here -- we are not using it for security,
      only for a fast, deterministic, well-mixed integer.

BINARY SEARCH, NOT LINEAR SCAN:
      The ring is stored as a sorted list of (hash, node_id) pairs.
      Routing a key means finding the first ring position >= hash(key),
      which is exactly what `bisect.bisect_left` does in O(log(N*V))
      instead of O(N*V) for a linear scan. At N=3, V=150 that's a
      450-entry list -- a linear scan would still be fast in absolute
      terms, but there's no reason to write an O(n) routing function when
      the standard-library tool for "find insertion point in a sorted
      list" is one import away and turns this into O(log n). This is
      "use the right tool," not premature optimization -- bisect is doing
      exactly what binary search would do by hand, just without us
      re-implementing it.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_left
from dataclasses import dataclass, field


def _hash_to_int(key: str) -> int:
    """
    Deterministic 32-bit hash of a string. MD5 is used purely as a fast,
    stable, well-mixed digest function -- see module docstring for why
    not Python's hash().
    """
    digest = hashlib.md5(key.encode("utf-8")).digest()
    # Take the first 4 bytes as a big-endian unsigned int -> 0 .. 2**32-1.
    return int.from_bytes(digest[:4], byteorder="big", signed=False)


@dataclass
class HashRing:
    """
    physical_nodes: ordered list of logical cache node IDs, e.g.
                    ["node-0", "node-1", "node-2"].
    virtual_nodes:  number of ring positions PER physical node (V). This
                    is the knob the sensitivity study tunes -- see
                    scripts/vnode_sensitivity_study.py.
    """

    physical_nodes: list[str]
    virtual_nodes: int = 150  # default justified in docs/vnode_sensitivity_results.md

    _ring_hashes: list[int] = field(default_factory=list, init=False, repr=False)
    _ring_owners: list[str] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self._build()

    def _build(self) -> None:
        """
        Places `virtual_nodes` replicas of each physical node on the ring.
        Each replica's ring position is hash(f"{node_id}#{replica_index}")
        -- the "#i" suffix is what makes the V replicas of the SAME
        physical node land at V different, effectively-random points
        instead of all hashing to the same spot.
        """
        points: list[tuple[int, str]] = []
        for node_id in self.physical_nodes:
            for replica in range(self.virtual_nodes):
                h = _hash_to_int(f"{node_id}#{replica}")
                points.append((h, node_id))

        # Sort by hash so we can binary-search for "first point >= x".
        # A hash collision between two different (node, replica) pairs is
        # astronomically unlikely at 32 bits with a few thousand points
        # (birthday-bound risk is negligible here), and even if it
        # happened, the sort is still well-defined -- one replica just
        # silently loses an infinitesimal sliver of the ring to the other.
        points.sort(key=lambda pair: pair[0])
        self._ring_hashes = [h for h, _ in points]
        self._ring_owners = [n for _, n in points]

    def get_node(self, key: str) -> str:
        """
        Returns the physical node that owns `key`: walk clockwise from
        hash(key) and return the first virtual node's owner. If hash(key)
        is greater than every ring position, wrap around to index 0 --
        the ring is circular, so "clockwise past the last point" lands
        back at the first point.
        """
        if not self._ring_hashes:
            raise RuntimeError("HashRing has no nodes")

        h = _hash_to_int(key)
        idx = bisect_left(self._ring_hashes, h)
        if idx == len(self._ring_hashes):
            idx = 0  # wrap around
        return self._ring_owners[idx]

    def node_counts(self) -> dict[str, int]:
        """Returns how many ring POSITIONS (not keys) each physical node
        holds. Useful for sanity-checking ring construction, but the
        sensitivity study below measures KEY distribution, which is the
        number that actually matters -- ring-position count and key
        distribution are correlated but not identical, since real query
        traffic is not uniformly distributed over the hash space."""
        counts: dict[str, int] = {n: 0 for n in self.physical_nodes}
        for owner in self._ring_owners:
            counts[owner] += 1
        return counts
