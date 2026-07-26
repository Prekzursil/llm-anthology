"""timetravel.py — birth-time "time-travel" over the Corpus spawn graph.

SYNTHETIC fixtures ONLY. Nothing here is a real thread id, timestamp, or path —
every corpus is a made-up shape that pins one property of the birth-time filter
(a chain born across timestamps, an undated node, a dangling edge child, a cycle),
never the private real corpus content.

What is pinned:
  * corpus_as_of(corpus, T) keeps a thread iff it is born at or before T, with an
    undated thread (created_at_ms=None) ALWAYS present regardless of T;
  * an edge is kept iff its CHILD is born at or before T — edge birth == child birth,
    faithful to the adapter — so edge presence is keyed on the child alone and never
    on the parent (an edge to an undated/dangling child is kept even before the parent
    is born, and an edge is dropped when only its child is still in the future);
  * boundary is INCLUSIVE (born-at exactly T counts as present);
  * the snapshot is a FRESH, NON-mutating Corpus with independent containers, its
    threads keyed in sorted-id order and its edges sorted, and it is cycle-safe
    (it filters by attribute comparison and never traverses the graph);
  * conversations are carried through unchanged on a fresh list;
  * timeline(corpus) reports the sorted DISTINCT dated births (events), the first/last
    of them (min_ms/max_ms, or None when there is no dated thread — never a fabricated
    0), and the count of undated NODES over the SAME node set graph.at views (threads
    UNION edge endpoints), so a dangling edge endpoint counts as undated.
"""
from llm_anthology import corpus, timetravel


# --------------------------------------------------------------------- fixtures

def _tm(tid, created_at_ms=None, **kw):
    return corpus.ThreadMeta(id=tid, created_at_ms=created_at_ms, **kw)


def _edge(parent, child, status="completed"):
    return corpus.SpawnEdge(parent_thread_id=parent, child_thread_id=child,
                            status=status)


def _corpus(threads=(), edges=(), conversations=()):
    """Assemble a synthetic Corpus via the public builders; conversations are opaque
    sentinels since the birth-time filter never inspects conversation content."""
    c = corpus.Corpus(conversations=list(conversations))
    for t in threads:
        c.add_thread(t)
    for e in edges:
        c.add_edge(e)
    return c


def _ids(c):
    """The thread ids present in a corpus, in dict-iteration order (so a test can also
    assert that order is sorted)."""
    return list(c.threads)


def _pairs(c):
    """The (parent, child) pairs of a corpus's edges, in list order."""
    return [(e.parent_thread_id, e.child_thread_id) for e in c.edges]


# ============================================================ corpus_as_of :: dated

def test_a_dated_thread_is_included_at_and_after_its_birth_and_excluded_before():
    """Boundary is inclusive: born-at exactly T counts; one ms earlier does not."""
    c = _corpus(threads=[_tm("t", created_at_ms=100)])
    assert _ids(timetravel.corpus_as_of(c, 99)) == []
    assert _ids(timetravel.corpus_as_of(c, 100)) == ["t"]   # inclusive at the birth ms
    assert _ids(timetravel.corpus_as_of(c, 101)) == ["t"]


def test_chain_born_across_timestamps_reveals_incrementally():
    """root(100) -> a(200) -> b(300). As T climbs, threads and their edges appear in
    birth order; each edge waits for its CHILD to be born."""
    c = _corpus(
        threads=[_tm("root", created_at_ms=100),
                 _tm("a", created_at_ms=200),
                 _tm("b", created_at_ms=300)],
        edges=[_edge("root", "a"), _edge("a", "b")])

    before = timetravel.corpus_as_of(c, 50)
    assert _ids(before) == [] and _pairs(before) == []

    t150 = timetravel.corpus_as_of(c, 150)          # only root; both edges' children future
    assert _ids(t150) == ["root"] and _pairs(t150) == []

    t250 = timetravel.corpus_as_of(c, 250)          # root+a; edge root->a (child a=200) kept
    assert _ids(t250) == ["a", "root"]
    assert _pairs(t250) == [("root", "a")]          # a->b dropped: child b=300 still future

    t300 = timetravel.corpus_as_of(c, 300)          # boundary: b born at exactly 300
    assert _ids(t300) == ["a", "b", "root"]
    assert _pairs(t300) == [("a", "b"), ("root", "a")]


# ========================================================== corpus_as_of :: undated

def test_an_undated_thread_is_always_present():
    """created_at_ms=None means 'no birth time' — the thread is present at every T,
    even one far below any dated birth."""
    c = _corpus(threads=[_tm("u", created_at_ms=None),
                         _tm("late", created_at_ms=10_000)])
    snap = timetravel.corpus_as_of(c, 0)
    assert _ids(snap) == ["u"]                       # 'late' not yet born; 'u' always on


# =================================================== corpus_as_of :: edge child-gating

def test_edge_is_kept_on_child_birth_even_when_the_parent_is_not_yet_born():
    """Edge presence is keyed on the CHILD alone. Parent p(500) is still in the future
    at T=200 but child c(100) is born, so the edge is present and p is a dangling
    (not-yet-born) parent of that edge."""
    c = _corpus(
        threads=[_tm("p", created_at_ms=500), _tm("c", created_at_ms=100)],
        edges=[_edge("p", "c")])
    snap = timetravel.corpus_as_of(c, 200)
    assert _ids(snap) == ["c"]                        # p not born as a thread
    assert _pairs(snap) == [("p", "c")]               # edge kept: child born, parent irrelevant


def test_edge_is_dropped_when_only_its_child_is_still_in_the_future():
    """The mirror: parent p(100) is born but child c(500) is not, so the edge waits."""
    c = _corpus(
        threads=[_tm("p", created_at_ms=100), _tm("c", created_at_ms=500)],
        edges=[_edge("p", "c")])
    snap = timetravel.corpus_as_of(c, 200)
    assert _ids(snap) == ["p"]
    assert _pairs(snap) == []                         # child 500 > 200 -> edge dropped


def test_edge_to_an_undated_child_thread_is_always_present():
    """An edge whose child ThreadMeta is undated is present at every T (child has no
    birth -> always born), even before the parent's own birth."""
    c = _corpus(
        threads=[_tm("p", created_at_ms=100), _tm("u", created_at_ms=None)],
        edges=[_edge("p", "u")])
    snap = timetravel.corpus_as_of(c, 50)             # before p's birth
    assert _ids(snap) == ["u"]                        # p not born; u always on
    assert _pairs(snap) == [("p", "u")]


def test_edge_to_a_dangling_child_without_metadata_is_always_present():
    """The child id has NO ThreadMeta (a dangling edge endpoint). It has no birth time,
    so like any undated node it is always present, and its edge is always kept."""
    c = _corpus(threads=[_tm("p", created_at_ms=100)], edges=[_edge("p", "ghost")])
    snap = timetravel.corpus_as_of(c, 50)
    assert _ids(snap) == []                           # p not born; 'ghost' has no ThreadMeta row
    assert _pairs(snap) == [("p", "ghost")]           # edge kept: dangling child treated as undated


# =========================================== corpus_as_of :: structural / determinism

def test_empty_corpus_travels_to_an_empty_corpus():
    snap = timetravel.corpus_as_of(corpus.Corpus(), 999)
    assert snap.threads == {} and snap.edges == []


def test_threads_and_edges_come_back_sorted():
    """Output order is stable/diffable regardless of insertion order: threads keyed in
    sorted-id order, edges sorted by (parent, child)."""
    c = _corpus(
        threads=[_tm("m", created_at_ms=1), _tm("a", created_at_ms=1),
                 _tm("z", created_at_ms=1)],
        edges=[_edge("m", "z"), _edge("a", "z"), _edge("a", "m")])
    snap = timetravel.corpus_as_of(c, 10)
    assert _ids(snap) == ["a", "m", "z"]
    assert _pairs(snap) == [("a", "m"), ("a", "z"), ("m", "z")]


def test_duplicate_edges_that_pass_the_filter_are_all_kept():
    """corpus_as_of FILTERS, it does not dedup: two identical in-memory edges both
    survive when their child is born."""
    c = _corpus(
        threads=[_tm("a", created_at_ms=1), _tm("b", created_at_ms=1)],
        edges=[_edge("a", "b"), _edge("a", "b")])
    snap = timetravel.corpus_as_of(c, 10)
    assert _pairs(snap) == [("a", "b"), ("a", "b")]


def test_corpus_as_of_is_deterministic_across_repeated_runs():
    c = _corpus(
        threads=[_tm("b", created_at_ms=1), _tm("a", created_at_ms=1)],
        edges=[_edge("a", "b"), _edge("b", "a")])
    first = timetravel.corpus_as_of(c, 10)
    second = timetravel.corpus_as_of(c, 10)
    assert _ids(first) == _ids(second)
    assert _pairs(first) == _pairs(second)


def test_corpus_as_of_does_not_mutate_its_input():
    c = _corpus(
        threads=[_tm("root", created_at_ms=100), _tm("a", created_at_ms=200)],
        edges=[_edge("root", "a")])
    threads_snapshot = dict(c.threads)
    edges_snapshot = list(c.edges)
    timetravel.corpus_as_of(c, 150)
    assert c.threads == threads_snapshot
    assert c.edges == edges_snapshot


def test_returns_a_fresh_corpus_with_independent_containers():
    """A fresh Corpus whose containers are new objects: mutating the snapshot leaves the
    source untouched."""
    c = _corpus(threads=[_tm("a", created_at_ms=1)], edges=[_edge("a", "b")])
    snap = timetravel.corpus_as_of(c, 10)
    assert snap is not c
    assert snap.threads is not c.threads
    assert snap.edges is not c.edges
    snap.add_thread(_tm("injected", created_at_ms=1))
    snap.add_edge(_edge("injected", "x"))
    assert "injected" not in c.threads
    assert ("injected", "x") not in _pairs(c)


def test_conversations_pass_through_unchanged_on_a_fresh_list():
    """The birth-time filter travels only the spawn graph; the conversation IR (which
    carries no ms birth time) is carried through unchanged, but on a NEW list so the
    snapshot's container is independent of the source's."""
    convo = object()                                  # opaque; never inspected
    c = _corpus(threads=[_tm("a", created_at_ms=1)], conversations=[convo])
    snap = timetravel.corpus_as_of(c, 10)
    assert snap.conversations == [convo]
    assert snap.conversations is not c.conversations


def test_corpus_as_of_is_cycle_safe():
    """a <-> b cycle. The filter compares attributes and never walks the graph, so it
    terminates; edge a->b (child b=200) is future-dropped while edge b->a (child a=100)
    is kept even though its parent b is not born."""
    c = _corpus(
        threads=[_tm("a", created_at_ms=100), _tm("b", created_at_ms=200)],
        edges=[_edge("a", "b"), _edge("b", "a")])
    snap = timetravel.corpus_as_of(c, 150)
    assert _ids(snap) == ["a"]
    assert _pairs(snap) == [("b", "a")]


# ================================================================== timeline

def test_timeline_of_an_empty_corpus_has_no_events_or_range():
    tl = timetravel.timeline(corpus.Corpus())
    assert tl == {"events": [], "min_ms": None, "max_ms": None, "undated_count": 0}


def test_timeline_collects_sorted_distinct_dated_births():
    c = _corpus(threads=[_tm("a", created_at_ms=300),
                         _tm("b", created_at_ms=100),
                         _tm("c", created_at_ms=200)])
    tl = timetravel.timeline(c)
    assert tl["events"] == [100, 200, 300]
    assert tl["min_ms"] == 100 and tl["max_ms"] == 300
    assert tl["undated_count"] == 0


def test_timeline_deduplicates_threads_sharing_a_timestamp():
    c = _corpus(threads=[_tm("a", created_at_ms=100),
                         _tm("b", created_at_ms=100),
                         _tm("c", created_at_ms=200)])
    tl = timetravel.timeline(c)
    assert tl["events"] == [100, 200]                 # two births at 100 -> one moment
    assert tl["min_ms"] == 100 and tl["max_ms"] == 200


def test_timeline_counts_undated_threads_separately_from_the_dated_range():
    c = _corpus(threads=[_tm("dated", created_at_ms=100),
                         _tm("u1", created_at_ms=None),
                         _tm("u2", created_at_ms=None)])
    tl = timetravel.timeline(c)
    assert tl["events"] == [100]
    assert tl["min_ms"] == 100 and tl["max_ms"] == 100  # single event -> min == max
    assert tl["undated_count"] == 2


def test_timeline_of_an_all_undated_corpus_has_no_range_but_counts_them():
    c = _corpus(threads=[_tm("u1"), _tm("u2"), _tm("u3")])   # created_at_ms defaults None
    tl = timetravel.timeline(c)
    assert tl["events"] == []
    assert tl["min_ms"] is None and tl["max_ms"] is None     # no dated thread -> no range
    assert tl["undated_count"] == 3


def test_timeline_counts_dangling_edge_endpoints_as_undated_nodes():
    """A dangling edge endpoint (an id on an edge with no ThreadMeta, hence no birth) is an
    always-present node, so timeline counts it as undated over the SAME node set graph.at /
    corpus_as_of view — threads UNION edge endpoints — NOT scoped to the threads table."""
    c = _corpus(threads=[_tm("a", created_at_ms=100)],
                edges=[_edge("a", "ghost"), _edge("ghost", "g2")])  # ghost, g2: no rows
    tl = timetravel.timeline(c)
    assert tl["events"] == [100]                       # only the dated thread contributes
    assert tl["min_ms"] == 100 and tl["max_ms"] == 100
    assert tl["undated_count"] == 2                    # ghost + g2, both row-less endpoints
