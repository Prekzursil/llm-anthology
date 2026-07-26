"""diff.py — deterministic structural diff between two Corpus spawn-trees.

SYNTHETIC fixtures ONLY. Nothing here is a real conversation, thread id, path, or
token count; every corpus is a made-up shape that mirrors the spawn-graph contract
(ThreadMeta nodes + SpawnEdge edges), never the private real corpus content.

What is pinned:
  * the id-set delta semantics — added/removed NODES are the graph-node set delta
    (a node is any ThreadMeta id OR an id an edge names, mirroring Corpus._nodes,
    so a dangling edge endpoint is diffed, not dropped);
  * added/removed EDGES are the (parent, child) pair-set delta, deduped and
    status-free (edge identity ignores status, exactly as the graph helpers do);
  * changed_nodes — per-field {field: (old, new)} for every id whose ThreadMeta is
    present on BOTH sides, scoped to metadata-on-both (a node that only gains/loses
    its ThreadMeta is not a field-level change);
  * determinism — every list is sorted and changed_nodes is keyed in sorted-id
    order, so two runs over the same pair are identical;
  * purity + cycle-safety — the diff mutates neither input and never traverses the
    graph, so cyclic/corrupt spawn data cannot make it recurse.
"""
from aisr import corpus, diff


# --------------------------------------------------------------------- fixtures

def _tm(tid="t1", **kw):
    return corpus.ThreadMeta(id=tid, **kw)


def _edge(parent, child, status="completed"):
    return corpus.SpawnEdge(parent_thread_id=parent, child_thread_id=child,
                            status=status)


def _corpus(threads=(), edges=()):
    """A Corpus assembled from iterables of ThreadMeta and SpawnEdge via the public
    builders — no conversations (the spawn graph is all the diff reads)."""
    c = corpus.Corpus()
    for t in threads:
        c.add_thread(t)
    for e in edges:
        c.add_edge(e)
    return c


# ------------------------------------------------------- CorpusDiff surface

def test_corpusdiff_defaults_are_all_empty():
    d = diff.CorpusDiff()
    assert d.added_nodes == [] and d.removed_nodes == []
    assert d.added_edges == [] and d.removed_edges == []
    assert d.changed_nodes == {}
    assert d.is_empty() is True


def test_corpusdiff_is_empty_is_false_when_any_field_is_populated():
    assert diff.CorpusDiff(added_nodes=["x"]).is_empty() is False


# ------------------------------------------------------------------ identical

def test_identical_corpora_produce_an_empty_diff():
    """Same threads (identical metadata) and same edges on both sides -> no delta.
    Exercises the common-thread loop taking the 'no field differs' path so an
    unchanged corpus never manufactures a false change."""
    left = _corpus(threads=[_tm("a", title="root"), _tm("b", title="leaf")],
                   edges=[_edge("a", "b")])
    right = _corpus(threads=[_tm("a", title="root"), _tm("b", title="leaf")],
                    edges=[_edge("a", "b")])
    d = diff.diff_corpus(left, right)
    assert d.is_empty() is True
    assert d.changed_nodes == {}


def test_two_empty_corpora_produce_an_empty_diff():
    d = diff.diff_corpus(corpus.Corpus(), corpus.Corpus())
    assert d.is_empty() is True


# ------------------------------------------------------ added / removed nodes

def test_a_thread_only_in_new_is_an_added_node():
    d = diff.diff_corpus(corpus.Corpus(), _corpus(threads=[_tm("b")]))
    assert d.added_nodes == ["b"] and d.removed_nodes == []


def test_a_thread_only_in_old_is_a_removed_node():
    d = diff.diff_corpus(_corpus(threads=[_tm("a")]), corpus.Corpus())
    assert d.removed_nodes == ["a"] and d.added_nodes == []


def test_added_nodes_are_sorted():
    new = _corpus(threads=[_tm("c"), _tm("a"), _tm("b")])
    assert diff.diff_corpus(corpus.Corpus(), new).added_nodes == ["a", "b", "c"]


# ----------------------------------------------------- edge-only (dangling) nodes

def test_an_edge_only_endpoint_counts_as_a_node():
    """A node with no ThreadMeta, present only as an edge endpoint, is still a graph
    node (Corpus._nodes semantics), so it appears in the node-set delta."""
    new = _corpus(edges=[_edge("ghost", "known")])
    d = diff.diff_corpus(corpus.Corpus(), new)
    assert d.added_nodes == ["ghost", "known"]
    assert d.added_edges == [("ghost", "known")]


def test_a_removed_edge_only_node_is_a_removed_node():
    old = _corpus(edges=[_edge("x", "y")])
    d = diff.diff_corpus(old, corpus.Corpus())
    assert d.removed_nodes == ["x", "y"]
    assert d.removed_edges == [("x", "y")]


# ------------------------------------------------------ added / removed edges

def test_a_new_edge_between_existing_nodes_is_an_added_edge():
    old = _corpus(threads=[_tm("a"), _tm("b")])
    new = _corpus(threads=[_tm("a"), _tm("b")], edges=[_edge("a", "b")])
    d = diff.diff_corpus(old, new)
    assert d.added_edges == [("a", "b")] and d.removed_edges == []
    # the endpoints already existed as nodes, so the node set is unchanged
    assert d.added_nodes == [] and d.removed_nodes == []


def test_a_dropped_edge_is_a_removed_edge():
    old = _corpus(threads=[_tm("a"), _tm("b")], edges=[_edge("a", "b")])
    new = _corpus(threads=[_tm("a"), _tm("b")])
    d = diff.diff_corpus(old, new)
    assert d.removed_edges == [("a", "b")] and d.added_edges == []


def test_added_edges_are_sorted_by_parent_then_child():
    new = _corpus(edges=[_edge("b", "z"), _edge("a", "y"), _edge("a", "x")])
    d = diff.diff_corpus(corpus.Corpus(), new)
    assert d.added_edges == [("a", "x"), ("a", "y"), ("b", "z")]


def test_edges_are_identified_by_parent_child_ignoring_status():
    """The same (parent, child) with a different status is NOT an edge add/remove —
    edge identity is the pair, not the status; the contract carries no changed_edges."""
    old = _corpus(edges=[_edge("a", "b", status="completed")])
    new = _corpus(edges=[_edge("a", "b", status="failed")])
    d = diff.diff_corpus(old, new)
    assert d.added_edges == [] and d.removed_edges == []
    assert d.is_empty() is True


def test_duplicate_in_memory_edges_dedupe_in_the_diff():
    new = _corpus(edges=[_edge("a", "b"), _edge("a", "b")])
    d = diff.diff_corpus(corpus.Corpus(), new)
    assert d.added_edges == [("a", "b")]


# --------------------------------------------------------------- changed nodes

def test_a_thread_with_a_changed_field_is_a_changed_node():
    old = _corpus(threads=[_tm("a", title="v1")])
    new = _corpus(threads=[_tm("a", title="v2")])
    d = diff.diff_corpus(old, new)
    assert d.changed_nodes == {"a": {"title": ("v1", "v2")}}
    assert d.added_nodes == [] and d.removed_nodes == []


def test_changed_node_reports_only_the_fields_that_differ_in_declaration_order():
    """Only fields whose values differ appear, each as an (old, new) tuple, and the
    map is ordered by ThreadMeta declaration order (tokens_used before git_branch)."""
    old = _corpus(threads=[_tm("a", title="same", tokens_used=10, git_branch="main")])
    new = _corpus(threads=[_tm("a", title="same", tokens_used=20, git_branch="dev")])
    d = diff.diff_corpus(old, new)
    assert d.changed_nodes["a"] == {"tokens_used": (10, 20),
                                    "git_branch": ("main", "dev")}
    assert list(d.changed_nodes["a"]) == ["tokens_used", "git_branch"]
    assert "title" not in d.changed_nodes["a"]


def test_an_unchanged_thread_present_on_both_sides_is_not_a_changed_node():
    same = dict(title="root", model_provider="codex", tokens_used=5)
    old = _corpus(threads=[_tm("a", **same)])
    new = _corpus(threads=[_tm("a", **same)])
    assert diff.diff_corpus(old, new).changed_nodes == {}


def test_a_none_to_value_field_change_is_captured():
    """updated_at_ms is Optional[int]; a None -> value transition is a real change and
    is reported with the None preserved (not coerced to 0)."""
    old = _corpus(threads=[_tm("a", updated_at_ms=None)])
    new = _corpus(threads=[_tm("a", updated_at_ms=2000)])
    d = diff.diff_corpus(old, new)
    assert d.changed_nodes == {"a": {"updated_at_ms": (None, 2000)}}


def test_changed_nodes_are_keyed_in_sorted_id_order():
    old = _corpus(threads=[_tm("b", title="x"), _tm("a", title="x")])
    new = _corpus(threads=[_tm("b", title="y"), _tm("a", title="y")])
    d = diff.diff_corpus(old, new)
    assert list(d.changed_nodes) == ["a", "b"]


def test_metadata_appearing_only_on_one_side_is_not_a_field_level_change():
    """A graph node present on both sides (via an unchanged edge) that gains a
    ThreadMeta only in `new` is NOT a changed_node — changed_nodes is scoped to ids
    with metadata on BOTH sides. The node itself is unchanged (same graph node)."""
    old = _corpus(edges=[_edge("a", "b")])
    new = _corpus(threads=[_tm("b", title="now-has-meta")], edges=[_edge("a", "b")])
    d = diff.diff_corpus(old, new)
    assert d.changed_nodes == {}
    assert d.added_nodes == [] and d.removed_nodes == []
    assert d.is_empty() is True


# --------------------------------------------------------------- cycle safety

def test_diff_is_cycle_safe_on_looped_spawn_data():
    """Cyclic spawn data (a<->b) must diff by set arithmetic without traversing, so
    it terminates and still reports the genuine delta (a new child c)."""
    old = _corpus(edges=[_edge("a", "b"), _edge("b", "a")])
    new = _corpus(edges=[_edge("a", "b"), _edge("b", "a"), _edge("b", "c")])
    d = diff.diff_corpus(old, new)
    assert d.added_nodes == ["c"]
    assert d.added_edges == [("b", "c")]
    assert d.removed_nodes == [] and d.removed_edges == []


# ------------------------------------------------------- purity / determinism

def test_diff_does_not_mutate_its_inputs():
    old = _corpus(threads=[_tm("a", title="v1")], edges=[_edge("a", "b")])
    new = _corpus(threads=[_tm("a", title="v2"), _tm("c")], edges=[_edge("a", "c")])
    old_threads_snapshot = dict(old.threads)
    old_edges_snapshot = list(old.edges)
    new_threads_snapshot = dict(new.threads)
    new_edges_snapshot = list(new.edges)
    diff.diff_corpus(old, new)
    assert old.threads == old_threads_snapshot and old.edges == old_edges_snapshot
    assert new.threads == new_threads_snapshot and new.edges == new_edges_snapshot


def test_diff_is_deterministic_across_repeated_runs():
    old = _corpus(threads=[_tm("a", title="v1"), _tm("keep")],
                  edges=[_edge("a", "b"), _edge("a", "gone")])
    new = _corpus(threads=[_tm("a", title="v2"), _tm("keep"), _tm("added")],
                  edges=[_edge("a", "b"), _edge("a", "fresh")])
    assert diff.diff_corpus(old, new) == diff.diff_corpus(old, new)


# ------------------------------------------------------------------ combined

def test_a_mixed_diff_reports_adds_removes_and_changes_together():
    """One pair exercising every axis at once: a node added, a node removed, an edge
    added, an edge removed, and a field-level change on a surviving node."""
    old = _corpus(
        threads=[_tm("root", title="r"), _tm("stays", title="v1"), _tm("gone")],
        edges=[_edge("root", "stays"), _edge("root", "gone")])
    new = _corpus(
        threads=[_tm("root", title="r"), _tm("stays", title="v2"), _tm("added")],
        edges=[_edge("root", "stays"), _edge("root", "added")])
    d = diff.diff_corpus(old, new)
    assert d.added_nodes == ["added"] and d.removed_nodes == ["gone"]
    assert d.added_edges == [("root", "added")]
    assert d.removed_edges == [("root", "gone")]
    assert d.changed_nodes == {"stays": {"title": ("v1", "v2")}}
    assert d.is_empty() is False
