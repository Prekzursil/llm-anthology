"""DECISION G-17: the two engine-side dead functions are GONE and must stay gone.

WHY A TEST AND NOT JUST A DELETION. A deletion is not self-enforcing. Both functions here
were reachable only from tests, which is exactly the shape that grows back: someone writes a
helper, a test uses it, and the "caller sweep" that would have caught it never runs again.
An absence assertion turns a re-introduction into a red build, and it names the reason so
the next author can argue with the decision instead of guessing at it.

  * ``dedup.collapse_corpus`` — the dedup view could SCAN but the collapse it fed was never
    wired to a corpus load, so no production path ever projected a Corpus through it.
  * ``index.posting_count`` — a one-line ``count(*)`` over ``conversations_fts``.

WHAT REPLACED posting_count, AND WHY THAT IS NOT A LOSS. Nine assertions across
``test_index.py`` and ``test_loaders_corpus.py`` used it, three of them to assert the real
ingest invariant "a replay must not duplicate FTS postings". That invariant is KEPT: each file
now defines a local ``_fts_postings(conn)`` issuing the identical
``SELECT count(*) FROM conversations_fts``. Deleting the production function does NOT delete
the check — it moves a test-only helper out of the shipped package, where its presence implied
a caller that never existed. Duplicated in the two files on purpose rather than hoisted into a
new ``tests/conftest.py``: a repo-wide collection hook is a heavier change than two four-line
helpers, and this suite has no conftest today.

This file adds no ``llm_anthology`` code, so it does not move the 100% coverage gate.
"""
from llm_anthology import dedup, index


def test_dedup_no_longer_exposes_collapse_corpus():
    """`collapse_corpus` projected a Corpus through a dedup view. Zero production callers:
    nothing in `loaders`, `sidecar`, `cli` or `build` ever called it, so the only Corpus the
    app ever rendered was the un-collapsed one. Re-adding it means wiring a caller too."""
    assert not hasattr(dedup, "collapse_corpus"), (
        "dedup.collapse_corpus is back. It is only useful WITH a caller — if you are wiring "
        "the dedup view into a corpus load, say so and delete this assertion deliberately."
    )


def test_index_no_longer_exposes_posting_count():
    """The FTS row count. Kept as an inline `count(*)` in the tests that assert the
    no-duplicate-postings invariant, rather than as a shipped function with no caller."""
    assert not hasattr(index, "posting_count"), (
        "index.posting_count is back. If a production caller now needs the FTS row count, "
        "delete this assertion; if it is only a test needing it, use the conftest helper."
    )


def test_the_dedup_docstring_does_not_advertise_a_deleted_function():
    """A module docstring that still describes `collapse_corpus` is worse than a dead
    function: the function a reader cannot find reads as a broken import, and the three
    passages that mentioned it were load-bearing prose about the CANONICAL-COPY rule, not
    incidental. They were reworded to state the rule without naming the deleted callee."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "llm_anthology" / "dedup.py").read_text(
        encoding="utf-8")
    assert "collapse_corpus" not in source, (
        "dedup.py still names collapse_corpus. Every mention has to go with the function, "
        "or the file documents a callee that no longer exists."
    )
