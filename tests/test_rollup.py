"""rollup.py — subtree metric aggregation over the Corpus spawn tree.

SYNTHETIC fixtures ONLY. Nothing here is a real thread id, token count, or path — every
tree is a made-up shape that stresses one property of the rollup (a chain, a fan-out, a
diamond, a cycle, a dangling edge), never real corpus content.

What the rollup must guarantee, and the tree that pins each guarantee:
  * `subtree_*` aggregate this node PLUS every distinct descendant (`children_of`) —
    a DIAMOND proves a doubly-reachable node is counted exactly ONCE, not twice.
  * the walk is CYCLE-SAFE — an a<->b cycle must terminate via a visited-set instead of
    recursing forever, and still report the two nodes once each.
  * `max_depth` is the greatest shortest-path distance from the node to any descendant
    (a leaf is 0), mirroring corpus.depth()'s shortest-path convention.
  * every graph node is keyed, INCLUDING an id that only appears on an edge and is
    absent from the threads table (its self_tokens is 0), and the output dict is ordered
    by sorted id so a rollup is diffable/reproducible.
"""
from llm_anthology import corpus, rollup


# --------------------------------------------------------------------- fixtures

def _corpus(threads=(), edges=()):
    """Build a synthetic Corpus. `threads` items are either a bare id (0 tokens) or a
    (id, tokens) pair; `edges` items are (parent, child) pairs."""
    c = corpus.Corpus()
    for t in threads:
        if isinstance(t, tuple):
            tid, tokens = t
            c.add_thread(corpus.ThreadMeta(id=tid, tokens_used=tokens))
        else:
            c.add_thread(corpus.ThreadMeta(id=t))
    for parent, child in edges:
        c.add_edge(corpus.SpawnEdge(parent_thread_id=parent, child_thread_id=child))
    return c


def _m(tid, table):
    """The RollupMetrics for `tid` (readability sugar in the assertions)."""
    return table[tid]


# ---------------------------------------------------------- RollupMetrics surface

def test_rollupmetrics_carries_the_six_contracted_fields():
    rm = rollup.RollupMetrics(self_tokens=5, subtree_tokens=9, self_count=1,
                              subtree_count=3, max_depth=2, child_count=1)
    assert rm.self_tokens == 5 and rm.subtree_tokens == 9
    assert rm.self_count == 1 and rm.subtree_count == 3
    assert rm.max_depth == 2 and rm.child_count == 1


def test_rollupmetrics_is_a_value_object_with_field_equality():
    a = rollup.RollupMetrics(1, 1, 1, 1, 0, 0)
    b = rollup.RollupMetrics(1, 1, 1, 1, 0, 0)
    assert a == b


# ------------------------------------------------------------------ empty / leaf

def test_rollup_of_an_empty_corpus_is_an_empty_dict():
    assert rollup.rollup(corpus.Corpus()) == {}


def test_an_isolated_thread_rolls_up_to_just_itself():
    table = rollup.rollup(_corpus(threads=[("solo", 7)]))
    assert table == {"solo": rollup.RollupMetrics(
        self_tokens=7, subtree_tokens=7, self_count=1,
        subtree_count=1, max_depth=0, child_count=0)}


# ------------------------------------------------------------------------ chain

def test_a_chain_aggregates_tokens_counts_and_depth_up_each_level():
    """root -> a -> b. Each ancestor's subtree sums itself plus everything below it,
    subtree_count grows 1/2/3 and max_depth grows 0/1/2 as you climb."""
    table = rollup.rollup(_corpus(
        threads=[("root", 1), ("a", 10), ("b", 100)],
        edges=[("root", "a"), ("a", "b")]))

    assert _m("b", table) == rollup.RollupMetrics(
        self_tokens=100, subtree_tokens=100, self_count=1,
        subtree_count=1, max_depth=0, child_count=0)
    assert _m("a", table) == rollup.RollupMetrics(
        self_tokens=10, subtree_tokens=110, self_count=1,
        subtree_count=2, max_depth=1, child_count=1)
    assert _m("root", table) == rollup.RollupMetrics(
        self_tokens=1, subtree_tokens=111, self_count=1,
        subtree_count=3, max_depth=2, child_count=1)


# ---------------------------------------------------------------------- fan-out

def test_child_count_is_the_direct_out_degree_not_the_whole_subtree():
    """A parent with two children whose one child has a grandchild: child_count counts
    ONLY the two direct children while subtree_count counts the whole four-node tree."""
    table = rollup.rollup(_corpus(
        threads=[("p", 1), ("x", 2), ("y", 4), ("g", 8)],
        edges=[("p", "x"), ("p", "y"), ("x", "g")]))
    assert _m("p", table).child_count == 2
    assert _m("p", table).subtree_count == 4
    assert _m("p", table).subtree_tokens == 15
    assert _m("p", table).max_depth == 2
    assert _m("x", table).child_count == 1 and _m("y", table).child_count == 0


# ---------------------------------------------------------------------- diamond

def test_a_diamond_counts_the_shared_descendant_exactly_once():
    """a -> b, a -> c, b -> d, c -> d. `d` is reachable by two paths; a naive
    sum-of-children's-subtrees would count d (and its 8 tokens) TWICE. The visited-set
    makes a's subtree {a,b,c,d}: count 4 (not 5) and tokens 1+2+4+8=15 (not 23)."""
    table = rollup.rollup(_corpus(
        threads=[("a", 1), ("b", 2), ("c", 4), ("d", 8)],
        edges=[("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")]))

    assert _m("a", table) == rollup.RollupMetrics(
        self_tokens=1, subtree_tokens=15, self_count=1,
        subtree_count=4, max_depth=2, child_count=2)
    assert _m("b", table) == rollup.RollupMetrics(
        self_tokens=2, subtree_tokens=10, self_count=1,
        subtree_count=2, max_depth=1, child_count=1)
    assert _m("c", table) == rollup.RollupMetrics(
        self_tokens=4, subtree_tokens=12, self_count=1,
        subtree_count=2, max_depth=1, child_count=1)
    assert _m("d", table) == rollup.RollupMetrics(
        self_tokens=8, subtree_tokens=8, self_count=1,
        subtree_count=1, max_depth=0, child_count=0)


# ------------------------------------------------------------------------ cycle

def test_a_two_node_cycle_terminates_and_counts_each_node_once():
    """a <-> b (a spawned b spawned a). The walk must terminate on the back-edge, and
    each node's subtree is {a, b}: count 2, tokens 3+5=8 summed once, max_depth 1."""
    table = rollup.rollup(_corpus(
        threads=[("a", 3), ("b", 5)],
        edges=[("a", "b"), ("b", "a")]))

    assert _m("a", table) == rollup.RollupMetrics(
        self_tokens=3, subtree_tokens=8, self_count=1,
        subtree_count=2, max_depth=1, child_count=1)
    assert _m("b", table) == rollup.RollupMetrics(
        self_tokens=5, subtree_tokens=8, self_count=1,
        subtree_count=2, max_depth=1, child_count=1)


def test_a_self_loop_terminates_and_is_a_single_node_subtree():
    """The degenerate cycle a -> a. `a` is its own child; the visited-set stops the
    walk immediately, so the subtree is just {a} — counted and summed exactly once."""
    table = rollup.rollup(_corpus(threads=[("a", 9)], edges=[("a", "a")]))
    assert _m("a", table) == rollup.RollupMetrics(
        self_tokens=9, subtree_tokens=9, self_count=1,
        subtree_count=1, max_depth=0, child_count=1)


# -------------------------------------------------------------- dangling edge ids

def test_a_child_absent_from_the_threads_table_is_still_a_node_with_zero_tokens():
    """An edge names a child that has no ThreadMeta. It is still a graph node: keyed in
    the rollup, self_tokens 0, its own one-node subtree — and it contributes 0 (not a
    crash) to its parent's subtree_tokens."""
    table = rollup.rollup(_corpus(
        threads=[("p", 7)], edges=[("p", "ghost")]))

    assert "ghost" in table
    assert _m("ghost", table) == rollup.RollupMetrics(
        self_tokens=0, subtree_tokens=0, self_count=1,
        subtree_count=1, max_depth=0, child_count=0)
    assert _m("p", table) == rollup.RollupMetrics(
        self_tokens=7, subtree_tokens=7, self_count=1,
        subtree_count=2, max_depth=1, child_count=1)


def test_a_parent_absent_from_the_threads_table_still_roots_its_subtree():
    """The dangling-parent mirror: an edge's parent has no ThreadMeta, yet it is a node
    that roots the subtree containing its known child (self_tokens 0)."""
    table = rollup.rollup(_corpus(
        threads=[("known", 9)], edges=[("orphan", "known")]))

    assert _m("orphan", table) == rollup.RollupMetrics(
        self_tokens=0, subtree_tokens=9, self_count=1,
        subtree_count=2, max_depth=1, child_count=1)
    assert _m("known", table).self_tokens == 9


# ------------------------------------------------------------------ determinism

def test_the_rollup_dict_is_ordered_by_sorted_thread_id():
    """Output order must be stable/diffable regardless of insertion order: ids added
    out of order come back sorted."""
    table = rollup.rollup(_corpus(
        threads=["m", "a", "z", "d"],
        edges=[("m", "z"), ("a", "d")]))
    assert list(table) == ["a", "d", "m", "z"]


def test_self_count_is_always_one_for_every_node_in_a_mixed_tree():
    table = rollup.rollup(_corpus(
        threads=[("r", 1), ("a", 2), ("b", 3)],
        edges=[("r", "a"), ("r", "b")]))
    assert all(m.self_count == 1 for m in table.values())
