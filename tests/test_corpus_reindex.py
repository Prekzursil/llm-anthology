"""A session that GREW since it was indexed must become searchable again.

THE DEFECT. `index.py` correctly re-scans a file whose fingerprint changed, then
`add_conversation` early-returned on the existing `conversation_id` and issued no UPDATE —
there was no `UPDATE conversations` anywhere in the package. So for a live session that is
still being written to, which is the designed flow:

  * the new turns never reach the FTS index — searching for them returns nothing;
  * `turn_count` and `char_count` stay frozen at their first-ingest values;
  * the ingest reports zero errors, and
  * the checkpoint advances, so re-running the build does NOT repair it.

`corpus.create` refuses to clobber an existing index and there is no reindex RPC, so the only
repair available to a user was deleting the .sqlite by hand — for a bug with no symptom
except that search quietly stops finding recent work.

The same root cause stranded `rollout_path`: when Codex moves a session into
`archived_sessions/`, the stored path kept pointing at the old location and the reader
reported "rollout unavailable" for a file that exists.

RETRACTING THE OLD TERMS. The FTS table is contentless, so a plain DELETE is rejected unless
it was created with `contentless_delete=1` (SQLite >= 3.43; measured working here alongside
`detail=none` on 3.50.4). New indexes get it. An index built before this change does not, and
cannot be altered in place — so for those, the row is re-inserted under a fresh rowid and the
stale posting is left orphaned, which is invisible to search because every query INNER JOINs
`conversations` on rowid. Both paths are asserted below; neither may return stale text.
"""
import sqlite3

import pytest

from llm_anthology import corpus, ir


def _conv(cid, title, texts, provider="codex"):
    return ir.Conversation(
        id=cid, title=title, provider=provider,
        turns=[ir.Turn(role="human", blocks=[ir.Block(type="text", text=t)]) for t in texts])


def _hits(conn, term):
    return conn.execute(
        "SELECT COUNT(*) FROM conversations_fts "
        "JOIN conversations c ON c.rowid = conversations_fts.rowid "
        "WHERE conversations_fts MATCH ?", ('"%s"' % term,)).fetchone()[0]


def _legacy_index():
    """An index built the OLD way: contentless FTS with no `contentless_delete`."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    corpus.init_index(conn)
    conn.execute("DROP TABLE conversations_fts")
    conn.execute("CREATE VIRTUAL TABLE conversations_fts USING fts5("
                 "title, body, content='', detail=none)")
    return conn


@pytest.fixture(params=["current", "legacy"])
def conn(request):
    """Every behaviour below must hold on BOTH schemas — the one this version creates and
    the one already sitting on users' disks."""
    c = corpus.open_index(":memory:") if request.param == "current" else _legacy_index()
    yield c
    c.close()


def test_new_text_in_a_grown_session_becomes_searchable(conn):
    corpus.add_conversation(conn, _conv("c1", "t", ["alpha the first message"]))
    assert _hits(conn, "alpha") == 1
    corpus.add_conversation(
        conn, _conv("c1", "t", ["alpha the first message", "bravo a brand new message"]))
    assert _hits(conn, "bravo") == 1, "text added after the first ingest is unsearchable"


def test_text_that_was_REMOVED_stops_matching(conn):
    """The other direction, and the one a naive fix gets wrong: re-indexing must RETRACT the
    old posting, not merely add to it. Leaving it attached to the same rowid produces a false
    positive — a hit on a conversation whose text no longer says that."""
    corpus.add_conversation(conn, _conv("c1", "t", ["alpha", "obsolete"]))
    assert _hits(conn, "obsolete") == 1
    corpus.add_conversation(conn, _conv("c1", "t", ["alpha", "replacement"]))
    assert _hits(conn, "replacement") == 1
    assert _hits(conn, "obsolete") == 0, "stale text still matches after re-indexing"


def test_the_row_is_never_duplicated(conn):
    for i in range(4):
        corpus.add_conversation(conn, _conv("c1", "t", ["alpha %d" % i]))
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
    assert _hits(conn, "alpha") == 1, "one conversation matched more than once"


def test_the_stored_counts_track_the_grown_session(conn):
    corpus.add_conversation(conn, _conv("c1", "t", ["one"]))
    corpus.add_conversation(conn, _conv("c1", "t", ["one", "two", "three"]))
    row = conn.execute("SELECT turn_count, char_count FROM conversations").fetchone()
    assert row["turn_count"] == 3, "turn_count froze at its first-ingest value"
    assert row["char_count"] > 3


def test_a_moved_session_updates_its_rollout_path(conn):
    """Codex moves finished sessions into `archived_sessions/`. The stored path used to keep
    pointing at the old location, so the reader said 'rollout unavailable' for a file that
    exists."""
    corpus.add_conversation(conn, _conv("c1", "t", ["x"]), rollout_path="/live/s.jsonl")
    corpus.add_conversation(conn, _conv("c1", "t", ["x"]),
                            rollout_path="/archived_sessions/s.jsonl")
    assert conn.execute(
        "SELECT rollout_path FROM conversations").fetchone()[0] == "/archived_sessions/s.jsonl"


def test_a_retitled_conversation_is_findable_by_its_NEW_title(conn):
    corpus.add_conversation(conn, _conv("c1", "firstname", ["body"]))
    corpus.add_conversation(conn, _conv("c1", "secondname", ["body"]))
    assert _hits(conn, "secondname") == 1
    assert _hits(conn, "firstname") == 0
    assert conn.execute("SELECT title FROM conversations").fetchone()[0] == "secondname"


def test_an_unchanged_reingest_leaves_the_row_alone(conn):
    """A resumed ingest re-reading an unchanged file must not churn the index."""
    first = corpus.add_conversation(conn, _conv("c1", "t", ["alpha"]))
    again = corpus.add_conversation(conn, _conv("c1", "t", ["alpha"]))
    assert first == again, "an unchanged conversation moved to a new rowid"
    assert _hits(conn, "alpha") == 1


def test_other_conversations_are_untouched_by_a_neighbour_update(conn):
    corpus.add_conversation(conn, _conv("c1", "t", ["alpha"]))
    corpus.add_conversation(conn, _conv("c2", "t", ["charlie"]))
    corpus.add_conversation(conn, _conv("c1", "t", ["alpha", "bravo"]))
    assert _hits(conn, "charlie") == 1, "an unrelated conversation lost its index entry"
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 2


def test_the_explicit_body_argument_still_wins(conn):
    """`body=` lets a caller index a sanitized or truncated form; the update path must honour
    it rather than silently re-deriving from the turns."""
    corpus.add_conversation(conn, _conv("c1", "t", ["secret"]), body="redacted marker")
    corpus.add_conversation(conn, _conv("c1", "t", ["secret", "more"]), body="second marker")
    assert _hits(conn, "second") == 1
    assert _hits(conn, "secret") == 0, "the explicit body was ignored on update"


def test_the_current_schema_can_retract_terms_in_place(conn):
    """The tidiness difference between the two schemas, stated rather than assumed.

    On the current schema the rowid is STABLE across an update, because the old posting can
    be deleted. On a legacy index it cannot, so the row moves to a fresh rowid and the stale
    posting is orphaned — correct, but the index grows.
    """
    first = corpus.add_conversation(conn, _conv("c1", "t", ["alpha"]))
    second = corpus.add_conversation(conn, _conv("c1", "t", ["bravo and then some more"]))
    if corpus._fts_can_delete(conn):
        assert first == second, "an index that can delete should update in place"
    else:
        assert first != second, "an index that cannot delete must move"
    assert _hits(conn, "bravo") == 1 and _hits(conn, "alpha") == 0


def test_the_current_param_really_does_get_the_deleting_schema_on_this_build(conn, request):
    """Why the two tests above branch on the CAPABILITY and not on the param name.

    `init_index` adds `contentless_delete=1` only when
    `_CONTENTLESS_DELETE = sqlite3.sqlite_version_info >= (3, 43)` (`corpus.py:253,263-265`).
    Below 3.43 the "current" fixture therefore produces a table that is structurally
    LEGACY, and a test branching on the param name would demand in-place update from an
    index that cannot delete — red on an old-SQLite runner for a purely environmental
    reason, on a CI matrix that spans 3.9 through 3.13 on three operating systems.

    A `pytest.skip` guard was the obvious fix and is the weaker one: it buys green by
    abandoning the assertion exactly where the schema differs. Branching on
    `_fts_can_delete` keeps BOTH tests running on every build and asserts the behaviour
    that build actually has.

    This test is the control that keeps the param name honest. On a modern SQLite the name
    and the capability must agree; if they ever stop agreeing, the NAME is what is lying,
    and this fails to say so rather than letting the suite drift.
    """
    can_delete = corpus._fts_can_delete(conn)
    if sqlite3.sqlite_version_info >= (3, 43):
        assert can_delete == (request.node.callspec.params["conn"] == "current"), (
            "on SQLite %s the 'current' fixture must get contentless_delete and 'legacy' "
            "must not" % sqlite3.sqlite_version)
    else:
        assert not can_delete, (
            "SQLite %s predates contentless_delete, so NEITHER fixture can retract a "
            "posting and both must take the move path" % sqlite3.sqlite_version)


def test_a_legacy_move_never_lands_on_a_rowid_the_stale_posting_owns(conn):
    """The trap in the legacy path. A plain DELETE-then-INSERT reuses the freed rowid when it
    was the highest, which re-attaches the orphaned posting to the NEW row and converts a
    missing hit into a FALSE one — strictly worse than the bug being fixed. Repeated updates
    on a single-row index are the exact shape that triggers it."""
    for i in range(5):
        corpus.add_conversation(conn, _conv("c1", "t", ["word%d and some padding %s" % (i, "x" * i)]))
    assert _hits(conn, "word4") == 1
    for stale in ("word0", "word1", "word2", "word3"):
        assert _hits(conn, stale) == 0, "%s still matches after four re-indexes" % stale


def test_a_same_length_body_edit_is_re_indexed_on_the_CURRENT_schema(conn):
    """The fingerprint gap, pinned where it is closed.

    `char_count` is the only stored signal that tracks the body, so an edit of the same
    length looks identical in the row. The current schema re-indexes unconditionally and is
    therefore immune. A legacy index consults the fingerprint (it must — every update there
    orphans another posting) and WILL skip this; that residual is documented in
    `add_conversation` rather than papered over here.
    """
    corpus.add_conversation(conn, _conv("c1", "t", ["alpha"]))
    corpus.add_conversation(conn, _conv("c1", "t", ["bravo"]))     # same length, exactly
    if corpus._fts_can_delete(conn):
        assert _hits(conn, "bravo") == 1 and _hits(conn, "alpha") == 0
    else:
        assert _hits(conn, "alpha") == 1, (
            "legacy behaviour changed; if this now re-indexes, delete the documented "
            "residual in add_conversation rather than leaving a stale caveat")


def test_a_fresh_index_is_created_with_contentless_delete():
    """Pin the schema choice itself, so a later edit to the DDL cannot quietly remove it and
    send every user down the orphaning path."""
    if sqlite3.sqlite_version_info < (3, 43):
        pytest.skip("contentless_delete needs SQLite >= 3.43; this build has %s"
                    % sqlite3.sqlite_version)
    c = corpus.open_index(":memory:")
    try:
        sql = c.execute(
            "SELECT sql FROM sqlite_master WHERE name='conversations_fts'").fetchone()[0]
        assert "contentless_delete" in sql, sql
    finally:
        c.close()
