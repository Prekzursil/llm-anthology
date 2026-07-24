"""timetravel.py — birth-time "time-travel" over the Corpus spawn graph.

The cockpit renders the Codex spawn graph (see corpus.py) as it stands NOW. Time-travel
answers the scrubber question "what did the graph look like as of time T?" — so an
operator can drag back to any moment and watch the tree grow as threads were spawned.
This module gives the two primitives that answer it, both derived purely from the
per-thread birth time (ThreadMeta.created_at_ms):

    corpus_as_of(corpus, as_of_ms) -> Corpus   # a fresh snapshot as of T
    timeline(corpus)               -> dict      # the scrubber's events + range

BIRTH-TIME FILTER (corpus_as_of). A spawn tree at time T is the subset of nodes and
edges that had been BORN by T:

  * a thread is kept iff created_at_ms <= T. created_at_ms is Optional[int]: a legacy
    canonical DB row has no birth time, so an UNDATED thread (created_at_ms is None) has
    no moment to filter on and is treated as ALWAYS present — it appears at every T
    rather than vanishing from history.
  * an edge is kept iff its CHILD thread was born by T. Faithful to the adapter, an edge
    is born WITH its child (child birth == edge birth), so edge presence is keyed on the
    child ALONE and never on the parent. Two consequences fall straight out and are
    pinned by tests: an edge to an undated/dangling child is always present (its child
    has no birth), even before the parent's own birth; and an edge whose child is still
    in the future is dropped even when its parent is already born.

The boundary is INCLUSIVE (born-at-exactly-T counts as present). A "child" with no
ThreadMeta at all (a dangling edge endpoint) has no birth time either, so it is
undated-and-always-present by the same rule.

The snapshot is a FRESH Corpus with brand-new containers — corpus_as_of mutates
neither the input's threads dict, edges list, nor conversations list — its threads are
keyed in sorted-id order and its edges are sorted, so a snapshot is deterministic and
diffable (it feeds straight into diff_corpus / rollup). It is CYCLE-SAFE by
construction: it filters by attribute comparison and never traverses the graph, so
cyclic or corrupt spawn data cannot make it recurse. The conversation IR carries no
millisecond birth time, so conversations are carried through unchanged (on a new list).

TIMELINE (timeline). The scrubber needs the set of moments it can snap to and the range
it spans: `events` is the sorted, de-duplicated list of every dated thread's
created_at_ms (an edge is born with its child, so child births are already among these
thread births and contribute no new moment); `min_ms`/`max_ms` are the first/last event,
or None when there is no dated thread (an empty or all-undated corpus has no range —
None rather than a fabricated 0, mirroring corpus.py's schema-tolerant Optional[int]
convention); and `undated_count` is how many threads are undated (the ones always
present in every snapshot). It is scoped to corpus.threads — the records that carry a
birth time — so a dangling edge endpoint (no ThreadMeta, no birth) is not a timeline
entry.

PRIVACY: this module reads birth-time ints and graph shape only; it never touches
conversation content. Tests use synthetic fixtures exclusively.
"""
from __future__ import annotations

from aisr.corpus import Corpus


def _birth(corpus: Corpus, tid: str):
    """The millisecond birth time of node `tid`, or None when it is UNDATED — either
    because its ThreadMeta.created_at_ms is None, or because the id has no ThreadMeta at
    all (a dangling edge endpoint). Both undated cases collapse to None so `_is_born`
    treats them identically (always present)."""
    meta = corpus.threads.get(tid)
    if meta is None:
        return None
    return meta.created_at_ms


def _is_born(birth, as_of_ms: int) -> bool:
    """True iff a node with birth time `birth` exists at `as_of_ms`. An undated node
    (birth is None) is ALWAYS present; a dated node is present iff it was born at or
    before T (inclusive)."""
    if birth is None:
        return True
    return birth <= as_of_ms


def corpus_as_of(corpus: Corpus, as_of_ms: int) -> Corpus:
    """A fresh Corpus holding only the spawn-graph structure that had been born by
    `as_of_ms`. Non-mutating, deterministic, and cycle-safe — see the module docstring
    for the full birth-time filter contract."""
    snapshot = Corpus(conversations=list(corpus.conversations))
    for tid in sorted(corpus.threads):
        if _is_born(_birth(corpus, tid), as_of_ms):
            snapshot.add_thread(corpus.threads[tid])
    for edge in sorted(corpus.edges,
                       key=lambda e: (e.parent_thread_id, e.child_thread_id, e.status)):
        if _is_born(_birth(corpus, edge.child_thread_id), as_of_ms):
            snapshot.add_edge(edge)
    return snapshot


def timeline(corpus: Corpus) -> dict:
    """The birth-time timeline of the corpus's threads for a time-travel scrubber:
    `{events, min_ms, max_ms, undated_count}`. See the module docstring for the
    contract; scoped to corpus.threads (the records that carry a birth time)."""
    dated: set = set()
    undated_count = 0
    for meta in corpus.threads.values():
        if meta.created_at_ms is None:
            undated_count += 1
        else:
            dated.add(meta.created_at_ms)
    events = sorted(dated)
    return {
        "events": events,
        "min_ms": events[0] if events else None,
        "max_ms": events[-1] if events else None,
        "undated_count": undated_count,
    }
