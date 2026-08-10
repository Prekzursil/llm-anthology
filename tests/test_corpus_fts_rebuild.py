"""DECISION G-4, half two: the FTS index is rebuilt so relevance means something.

WHAT WAS WRONG. The index was `fts5(title, body, content='', detail=none)`. `detail=none`
drops per-column and per-position data, and the consequences are not subtle: an exact PHRASE
query, `NEAR`, and a column filter all RAISE, and bm25 has no term frequencies to score with,
so every matching row ties. `sidecar._run_search` had already been forced to abandon
`ORDER BY rank` for exactly that reason and sort by recency instead, calling it "the honest
substitute for relevance given rank carries no signal".

`detail=none` was chosen on a real measurement — p95 33 ms over 2.2M records, ~6x under a
200 ms budget — but that measured SPEED, which was never the problem.

WHAT THIS FILE PINS, and how it avoids proving nothing. Every capability test comes in a
PAIR: the same query is run against the shipped schema (must work) and against a
`detail=none` twin table built in the same test (must raise). That twin is the both-states
control. Without it, a suite could go green against a schema that had silently reverted —
these tests would simply stop exercising the feature and nobody would know, which is the
exact failure mode `test_index_schema_declares_...` fell into when the string `detail=none`
survived inside an SQL comment and kept a containment assertion passing.

Measured on this box, SQLite 3.50.4, over 200 synthetic docs where the query term is rare
enough for bm25's IDF to stay positive:

    detail=none    phrase/NEAR/col: raise . bm25 = -0.0 for every row (1 distinct of 3)
    detail=column  phrase/NEAR raise, col: works . bm25 = -0.0 for every row
    detail=full    all four work . 3 distinct scores, ordered by term frequency

`detail=column` is therefore NOT a middle ground; it buys the column filter and leaves
ranking just as degenerate.

PRIVACY: synthetic fixtures only.
"""
import sqlite3

import pytest

from llm_anthology import corpus, index, ir

_OPEN = []


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _open():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _OPEN.append(conn)
    return corpus.init_index(conn)


def _conv(cid, title, text):
    return ir.Conversation(
        id=cid, title=title, provider="codex", account="acct",
        turns=[ir.Turn(role="human", blocks=[ir.Block(type="text", text=text)])],
        created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z")


def _seed(conn, rows):
    """`rows` is [(id, title, body)] — the body is passed explicitly so each test controls
    the indexed text exactly."""
    for cid, title, body in rows:
        corpus.add_conversation(conn, _conv(cid, title, body), body=body)
    conn.commit()
    return conn


def _match(conn, expr):
    return sorted(r[0] for r in conn.execute(
        "SELECT c.conversation_id FROM conversations_fts "
        "JOIN conversations c ON c.rowid = conversations_fts.rowid "
        "WHERE conversations_fts MATCH ?", (expr,)))


def _detail_none_twin(conn, rows):
    """The BOTH-STATES CONTROL: the same rows in a `detail=none` contentless table, which is
    what this index was until G-4. A capability test that passes on the shipped schema is
    only evidence if it FAILS here."""
    conn.execute("CREATE VIRTUAL TABLE twin USING fts5("
                 "title, body, content='', detail=none)")
    for i, (_cid, title, body) in enumerate(rows, start=1):
        conn.execute("INSERT INTO twin(rowid, title, body) VALUES (?,?,?)", (i, title, body))
    return conn


def _twin_raises(conn, expr):
    with pytest.raises(sqlite3.OperationalError) as exc:
        conn.execute("SELECT rowid FROM twin WHERE twin MATCH ?", (expr,)).fetchall()
    return str(exc.value)


# --------------------------------------------------------------- the shipped shape

def test_the_fts_table_is_created_at_detail_full():
    """Read off the STORED DDL, not off the schema template. The template is a string with
    an SQL comment in it that mentions the old value, so a containment check against the
    template is not a measurement of what was created."""
    conn = _open()
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='conversations_fts'").fetchone()[0]
    assert "detail=full" in ddl
    assert "detail=none" not in ddl
    assert "content=''" in ddl, "still contentless — the bodies live in conversation_bodies"


def test_contentless_delete_is_still_set_and_the_minimum_is_not_widened():
    """`contentless_delete=1` needs SQLite >= 3.43 and is what lets a GROWN session be
    re-indexed. The rebuild must not have quietly dropped it, and must not have raised the
    floor either: on an older build the option is omitted and the legacy re-insert path in
    `add_conversation` still has to work."""
    conn = _open()
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='conversations_fts'").fetchone()[0]
    if sqlite3.sqlite_version_info >= (3, 43):
        assert "contentless_delete=1" in ddl
        assert corpus._fts_can_delete(conn) is True
    else:                                                      # pragma: no cover
        assert "contentless_delete" not in ddl
        assert corpus._fts_can_delete(conn) is False


# ------------------------------------------------------- phrase queries

_PHRASE_ROWS = [("c-adjacent", "t", "the quick brown fox"),
                ("c-scrambled", "t", "brown is quick and so is the fox")]


def test_an_exact_PHRASE_matches_only_the_adjacent_pair():
    conn = _seed(_open(), _PHRASE_ROWS)
    assert _match(conn, '"quick brown"') == ["c-adjacent"]
    # both words are present in both rows, so an AND cannot tell them apart — which is
    # precisely the distinction a phrase query exists to make.
    assert _match(conn, "quick AND brown") == ["c-adjacent", "c-scrambled"]


def test_a_phrase_query_RAISES_on_the_old_detail_none_shape():
    """The control. If this ever stops raising, the test above has stopped proving anything
    and this file's premise is void."""
    conn = _detail_none_twin(_open(), _PHRASE_ROWS)
    assert "phrase queries are not supported" in _twin_raises(conn, '"quick brown"')


# ------------------------------------------------------------------ NEAR

_NEAR_ROWS = [("c-near", "t", "deploy the release today"),
              ("c-far", "t", "deploy " + " padding" * 20 + " release")]


def test_NEAR_matches_only_within_the_stated_distance():
    conn = _seed(_open(), _NEAR_ROWS)
    assert _match(conn, "NEAR(deploy release, 3)") == ["c-near"]
    assert _match(conn, "NEAR(deploy release, 40)") == ["c-far", "c-near"]


def test_NEAR_RAISES_on_the_old_detail_none_shape():
    conn = _detail_none_twin(_open(), _NEAR_ROWS)
    assert "NEAR queries are not supported" in _twin_raises(conn, "NEAR(deploy release, 3)")


# --------------------------------------------------------- column filters

_COL_ROWS = [("c-in-title", "sentinel in the title", "unrelated body text"),
             ("c-in-body", "unrelated title text", "sentinel in the body")]


def test_a_match_can_be_confined_to_ONE_column():
    conn = _seed(_open(), _COL_ROWS)
    assert _match(conn, "title: sentinel") == ["c-in-title"]
    assert _match(conn, "body: sentinel") == ["c-in-body"]
    assert _match(conn, "sentinel") == ["c-in-body", "c-in-title"]


def test_a_column_filter_RAISES_on_the_old_detail_none_shape():
    conn = _detail_none_twin(_open(), _COL_ROWS)
    assert "column queries are not supported" in _twin_raises(conn, "title: sentinel")


# ------------------------------------------------------------------ bm25

#: Filler shared by every doc so the query term stays rare and bm25's IDF stays positive.
#: With the term in EVERY doc, fts5's IDF goes non-positive and every score collapses to
#: ~0 regardless of `detail` — which is how the first draft of this measurement produced a
#: false "detail=full is degenerate too". The corpus shape is part of the experiment.
_FILLER = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor"

_BM25_ROWS = (
    [("c-fill%d" % i, "t", _FILLER) for i in range(12)]
    + [("c-once", "t", _FILLER + " zebra"),
       ("c-eight", "t", _FILLER + " zebra" * 8),
       ("c-once-but-long", "t", _FILLER + " zebra " + _FILLER * 8)]
)


def _bm25(conn):
    return conn.execute(
        "SELECT c.conversation_id AS cid, bm25(conversations_fts) AS score "
        "FROM conversations_fts JOIN conversations c ON c.rowid = conversations_fts.rowid "
        "WHERE conversations_fts MATCH 'zebra' ORDER BY bm25(conversations_fts)").fetchall()


def test_bm25_ranks_by_TERM_FREQUENCY_and_document_length():
    """The claim "real relevance ranking" reduced to two falsifiable orderings: eight hits
    outrank one, and one hit in a long document ranks below one hit in a short one."""
    rows = _bm25(_seed(_open(), _BM25_ROWS))
    assert [r["cid"] for r in rows] == ["c-eight", "c-once", "c-once-but-long"]
    scores = [r["score"] for r in rows]
    assert len(set(scores)) == 3, "three distinct scores, not a tie: %r" % (scores,)
    assert scores == sorted(scores), "fts5 bm25 is negative-is-better"


def test_bm25_is_DEGENERATE_on_the_old_detail_none_shape():
    """The control, and the whole reason `_run_search` had to sort by recency: under
    `detail=none` every matching row scores identically, so `ORDER BY rank` imposed no order
    at all and LIMIT/OFFSET paging over it partitioned the result set only by scan order."""
    conn = _detail_none_twin(_open(), _BM25_ROWS)
    scores = [r[0] for r in conn.execute(
        "SELECT bm25(twin) FROM twin WHERE twin MATCH 'zebra'")]
    assert len(scores) == 3, "the same three docs match"
    assert len(set(scores)) == 1, (
        "detail=none is supposed to tie every row; it returned %r, so this control is no "
        "longer measuring what it claims" % (scores,))


# ---------------------------------------------- the builder's own ranked query

def test_index_ranked_search_now_separates_relevance():
    """`index.ranked_search` exposes the bm25 score per row and its docstring used to
    disclose that the score could not separate anything. It can now."""
    conn = _seed(_open(), _BM25_ROWS)
    rows = index.ranked_search(conn, "zebra")
    assert [r["conversation_id"] for r in rows] == [
        "c-eight", "c-once", "c-once-but-long"]
    assert len({r["bm25_score"] for r in rows}) == 3


# ------------------------------------------- re-indexing still retracts old terms

def test_a_reindexed_conversation_stops_matching_its_OLD_text():
    """The property `contentless_delete=1` exists for, re-asserted against the new shape:
    a grown/edited conversation must not stay matchable on text it no longer contains."""
    conn = _open()
    corpus.add_conversation(conn, _conv("c1", "t", "alpha"), body="alpha")
    assert _match(conn, "alpha") == ["c1"]
    corpus.add_conversation(conn, _conv("c1", "t", "bravo"), body="bravo")
    assert _match(conn, "bravo") == ["c1"]
    if sqlite3.sqlite_version_info >= (3, 43):
        assert _match(conn, "alpha") == [], "the retracted posting is still matchable"
