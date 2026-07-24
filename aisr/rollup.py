"""rollup.py — aggregate per-thread metrics UP each subtree of the spawn tree.

The cockpit renders the Codex spawn graph (see corpus.py) as a tree of threads. A raw
per-thread token count answers "what did THIS thread cost"; the cockpit also needs the
Phoenix/recall-style ROLLUP — "what did this thread AND everything it spawned cost",
summed up each subtree — so an operator can fold a noisy branch and still see its true
weight. This module computes that rollup once, deterministically, for every node.

`rollup(corpus)` returns `{thread_id: RollupMetrics}` over EVERY graph node — including
an id that appears only on an edge and is absent from the threads table (corpus.py
treats a dangling parent/child as a node, and so do we; its self_tokens is 0). For each
node the subtree is this node plus every distinct descendant reached through
`children_of`, so:

  * self_tokens / self_count  — the node alone (self_count is always 1; it exists so the
    rollup algebra `subtree_count == sum of descendants' self_count` holds).
  * subtree_tokens / subtree_count — the node PLUS all distinct descendants. "Distinct"
    is load-bearing: in a DIAMOND (a->b, a->c, b->d, c->d) `d` is reachable two ways and
    must be counted ONCE, so we sum over the descendant SET, never over children's
    subtree totals (which would double-count the shared node).
  * child_count — the direct out-degree only (== corpus.fan_out).
  * max_depth — the greatest shortest-path distance (in spawn edges) from the node to
    any node in its subtree; a leaf is 0. Computed breadth-first, so it mirrors
    corpus.depth()'s SHORTEST-path convention and terminates on cyclic data.

CYCLE-SAFETY: the descendant walk carries a visited-set, so an a<->b cycle (or a self
loop a->a) terminates instead of recursing forever, and each node is summed exactly
once. Everything is derived from the corpus graph in sorted order, so the returned dict
is ordered by thread id and a rollup is diffable/reproducible.

PRIVACY: this module reads counts and graph shape only; it never touches conversation
content. Tests use synthetic fixtures exclusively.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from aisr.corpus import Corpus


@dataclass(frozen=True)
class RollupMetrics:
    """Aggregated metrics for one node's subtree. Field order matches the cockpit
    contract; every value is a non-negative int. `self_count` is always 1."""
    self_tokens: int
    subtree_tokens: int
    self_count: int
    subtree_count: int
    max_depth: int
    child_count: int


def _node_ids(corpus: Corpus) -> set:
    """Every id in the graph: the threads table UNION every id an edge names — so a
    dangling parent/child (present on an edge but not in `threads`) is still a node."""
    ids = set(corpus.threads)
    for edge in corpus.edges:
        ids.add(edge.parent_thread_id)
        ids.add(edge.child_thread_id)
    return ids


def _tokens_of(corpus: Corpus, tid: str) -> int:
    """The node's own token count, or 0 when the id has no ThreadMeta (a dangling edge
    endpoint) — an absent node contributes 0 to a subtree rather than crashing."""
    meta = corpus.threads.get(tid)
    if meta is None:
        return 0
    return meta.tokens_used


def _walk(corpus: Corpus, root: str):
    """Breadth-first descendant walk from `root`. Returns (nodes, subtree_tokens,
    max_depth): the set of distinct nodes in the subtree (root included), the sum of
    their self_tokens, and the greatest shortest-path depth reached. The visited-set
    makes it cycle-safe and dedupes a diamond's shared node; FIFO order means a node is
    first dequeued at its SHORTEST depth, so max_depth is the shortest-path convention."""
    seen: set = set()
    subtree_tokens = 0
    max_depth = 0
    queue = deque([(root, 0)])
    while queue:
        tid, depth = queue.popleft()
        if tid in seen:            # a back-edge / already-reached node: skip
            continue
        seen.add(tid)
        subtree_tokens += _tokens_of(corpus, tid)
        if depth > max_depth:
            max_depth = depth
        for child in corpus.children_of(tid):
            queue.append((child, depth + 1))
    return seen, subtree_tokens, max_depth


def rollup(corpus: Corpus) -> dict[str, RollupMetrics]:
    """Compute RollupMetrics for every node, keyed and ordered by sorted thread id."""
    table: dict[str, RollupMetrics] = {}
    for tid in sorted(_node_ids(corpus)):
        nodes, subtree_tokens, max_depth = _walk(corpus, tid)
        table[tid] = RollupMetrics(
            self_tokens=_tokens_of(corpus, tid),
            subtree_tokens=subtree_tokens,
            self_count=1,
            subtree_count=len(nodes),
            max_depth=max_depth,
            child_count=len(corpus.children_of(tid)),
        )
    return table
