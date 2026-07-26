"""Structural diff between two Corpus spawn-trees.

The cockpit needs to answer "what changed between two ingests of the corpus?"
without diffing rendered pixels or re-walking the 895-edge spawn tree by hand.
This module gives the deterministic, id-set-delta primitive that answers it:

    diff_corpus(old, new) -> CorpusDiff

A spawn tree is fully described by its NODES and its directed EDGES, so a
structural diff is three set/field deltas over those:

  * added_nodes / removed_nodes — node-id set delta. A "node" is any id the graph
    carries: a ThreadMeta id OR an id an edge names as a parent/child. That
    mirrors Corpus._nodes(): a dangling endpoint that only appears on an edge is
    still a node, so it is still diffed instead of silently vanishing.
  * added_edges / removed_edges — (parent, child) pair set delta. Edge identity is
    the (parent, child) pair, deduped and status-free, exactly as the corpus graph
    helpers treat edges (children_of dedupes on child, never on status). A
    status-only change on an otherwise-unchanged pair is therefore NOT an edge
    add/remove — the contract carries no changed_edges.
  * changed_nodes — for every id whose ThreadMeta is present in BOTH corpora, the
    per-field {field: (old, new)} map of the fields that differ. Scoped to ids with
    metadata on both sides: a node that only gained or lost its ThreadMeta entirely
    is a node-set event (or nothing), never a field-level change.

Every output is sorted by a stable id, and changed_nodes is keyed in sorted-id
order with each field map in dataclass declaration order, so two runs over the
same pair are byte-identical (diffable / reproducible). The diff is pure — it
reads both corpora and mutates neither — and cycle-safe by construction: it does
no graph traversal, only set and field comparisons, so cyclic or corrupt spawn
data cannot make it recurse.

Tests use SYNTHETIC fixtures only; this module never reads the real corpus.
"""
from dataclasses import dataclass, field, fields

from .corpus import Corpus


@dataclass
class CorpusDiff:
    """The structural delta between two Corpus spawn-trees.

    added_nodes / removed_nodes : sorted list[str] of node ids.
    added_edges / removed_edges : sorted list[tuple[str, str]] of (parent, child).
    changed_nodes : id -> {field_name: (old_value, new_value)} for the ThreadMeta
                    fields that differ, keyed in sorted-id order and each field map
                    in declaration order.

    An all-empty CorpusDiff (the default) means the two corpora are structurally
    identical; is_empty() reports that.
    """
    added_nodes: list = field(default_factory=list)
    removed_nodes: list = field(default_factory=list)
    added_edges: list = field(default_factory=list)
    removed_edges: list = field(default_factory=list)
    changed_nodes: dict = field(default_factory=dict)

    def is_empty(self):
        """True iff there is no structural difference at all."""
        return not (self.added_nodes or self.removed_nodes or self.added_edges
                    or self.removed_edges or self.changed_nodes)


def _node_ids(corpus):
    """Every node id in the spawn graph: the threads table UNION every id an edge
    names as a parent or child. Mirrors Corpus._nodes() so a dangling edge endpoint
    counts as a node and is diffed rather than dropped."""
    ids = set(corpus.threads)
    for edge in corpus.edges:
        ids.add(edge.parent_thread_id)
        ids.add(edge.child_thread_id)
    return ids


def _edge_pairs(corpus):
    """The set of (parent, child) pairs — deduped and status-free — i.e. edge
    identity as the corpus graph helpers see it."""
    return {(edge.parent_thread_id, edge.child_thread_id) for edge in corpus.edges}


def _changed_fields(old_meta, new_meta):
    """The {field: (old, new)} map of ThreadMeta fields that differ, in dataclass
    declaration order (deterministic). Empty when the two are field-equal."""
    diffs = {}
    for f in fields(old_meta):
        old_value = getattr(old_meta, f.name)
        new_value = getattr(new_meta, f.name)
        if old_value != new_value:
            diffs[f.name] = (old_value, new_value)
    return diffs


def diff_corpus(old: Corpus, new: Corpus) -> CorpusDiff:
    """Deterministic structural diff between two Corpus spawn-trees.

    Pure and cycle-safe: reads both corpora, mutates neither, and does no graph
    traversal. Every list is sorted and changed_nodes is built in sorted-id order,
    so the result is byte-stable across runs.
    """
    old_nodes = _node_ids(old)
    new_nodes = _node_ids(new)
    old_edges = _edge_pairs(old)
    new_edges = _edge_pairs(new)

    changed_nodes = {}
    for tid in sorted(set(old.threads) & set(new.threads)):
        field_diffs = _changed_fields(old.threads[tid], new.threads[tid])
        if field_diffs:
            changed_nodes[tid] = field_diffs

    return CorpusDiff(
        added_nodes=sorted(new_nodes - old_nodes),
        removed_nodes=sorted(old_nodes - new_nodes),
        added_edges=sorted(new_edges - old_edges),
        removed_edges=sorted(old_edges - new_edges),
        changed_nodes=changed_nodes,
    )
