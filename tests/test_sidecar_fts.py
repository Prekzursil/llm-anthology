"""`fts_match_expression` — user text must never reach FTS5 as query syntax.

The defect: the search string was bound straight into ``MATCH ?``. Binding was never an
injection risk, but FTS5 parses the BOUND VALUE as an expression, so ordinary typing raised
``sqlite3.OperationalError`` instead of returning results. Measured against the real index
before the fix, all of these crashed:

    foo(   ·   a AND   ·   "unclosed   ·   NEAR/   ·   *   ·   a OR OR b

A user typing a bracket got an exception. These tests pin that they cannot any more, and —
just as importantly — that ordinary search still works, because the cheap way to stop crashes
is to break search.
"""
import sqlite3

import pytest

from llm_anthology import corpus, index, ir, sidecar

# Every input that was measured to raise before the fix.
CRASHERS = ["foo(", "a AND", '"unclosed', "NEAR/", "*", "a OR OR b",
            "(", ")", '"', "**", "a NOT", "OR", "x AND (y", "NEAR(a b"]


def _index_with(rows):
    """An in-memory index holding one one-turn conversation per (id, body).

    The FTS index is CONTENTLESS and indexes title + block text, so the searchable words have
    to live in a real Turn/Block — matching how `tests/test_index.py` builds its fixtures.
    """
    conn = corpus.open_index(":memory:")
    src = index.IndexSource(
        file="f.jsonl", content_hash=index.hash_content("v1"),
        records=[ir.Conversation(
            id=cid, title=body, provider="codex",
            turns=[ir.Turn(role="human", blocks=[ir.Block(type="text", text=body)])])
            for cid, body in rows])
    index.build_index(conn, [src])
    return conn


def _server(conn):
    return sidecar.Sidecar(conn)


# ------------------------------------------------------------------ the expression builder

@pytest.mark.parametrize("raw", CRASHERS)
def test_every_previously_crashing_input_now_produces_a_usable_expression(raw):
    """The expression must be something FTS5 will accept — proven by RUNNING it, not by
    inspecting the string. A builder that produced plausible-looking but invalid syntax would
    pass any assertion about its output and still crash in production."""
    expr = sidecar.fts_match_expression(raw)
    conn = _index_with([("c1", "alpha bravo")])
    try:
        if expr:
            # Must not raise. That is the entire contract.
            conn.execute(
                "SELECT COUNT(*) FROM conversations_fts WHERE conversations_fts MATCH ?",
                (expr,)).fetchone()
    finally:
        conn.close()


def test_punctuation_inside_a_word_becomes_separate_terms():
    """THE SECOND DEFECT, and the one that mattered more.

    Quoting alone left `file.py` as a two-token PHRASE, and a detail=none index cannot run a
    phrase at all. Splitting on the tokenizer's own boundary is what makes quoting safe."""
    assert sidecar.fts_match_expression("file.py") == '"file" "py"'
    assert sidecar.fts_match_expression("foo-bar") == '"foo" "bar"'
    assert sidecar.fts_match_expression("don't") == '"don" "t"'
    assert sidecar.fts_match_expression("C:/Users/x") == '"C" "Users" "x"'


@pytest.mark.parametrize("raw", ["file.py", "foo-bar", "don't", "main.rs", "a.b.c.d",
                                 "https://example.com/p?q=1", "3.14", "e-mail@host.tld"])
def test_ordinary_punctuated_words_do_not_raise_against_a_real_index(raw):
    """The regression that the first fix missed, pinned against a real detail=none index —
    these are not exotic inputs, they are what a corpus of coding sessions is searched for."""
    conn = _index_with([("c1", "alpha bravo")])
    try:
        conn.execute("SELECT COUNT(*) FROM conversations_fts WHERE conversations_fts MATCH ?",
                     (sidecar.fts_match_expression(raw),)).fetchone()
    finally:
        conn.close()


def test_a_quote_is_dropped_as_a_separator():
    """A quote is not alphanumeric, so it is a token separator like any other punctuation —
    there is nothing left to escape. (The earlier version doubled it into the phrase instead,
    which produced exactly the unsupported-phrase crash above.)"""
    assert sidecar.fts_match_expression('say "hi"') == '"say" "hi"'


def test_prefix_search_is_preserved():
    """`foo*` is something people genuinely type, and FTS5 supports `"foo"*`."""
    assert sidecar.fts_match_expression("foo*") == '"foo"*'
    assert sidecar.fts_match_expression("foo* bar") == '"foo"* "bar"'


def test_a_prefix_binds_to_the_last_term_of_a_punctuated_word():
    """`main.r*` should prefix-match `r`, not turn the whole thing into one term."""
    assert sidecar.fts_match_expression("main.r*") == '"main" "r"*'


def test_a_bare_asterisk_yields_nothing_rather_than_a_bare_star():
    """`*` alone has no term to prefix, and a bare `*` is an FTS5 syntax error."""
    assert sidecar.fts_match_expression("*") == ""


def test_tokens_are_ANDed_implicitly():
    assert sidecar.fts_match_expression("alpha bravo") == '"alpha" "bravo"'


def test_input_with_no_usable_token_yields_an_empty_expression():
    for raw in ("", "   ", "\t\n"):
        assert sidecar.fts_match_expression(raw) == ""


# ------------------------------------------------------------------ through the RPC

@pytest.mark.parametrize("raw", CRASHERS)
def test_search_query_rpc_survives_every_crashing_input(raw):
    """End to end: the RPC a user's keystrokes actually reach."""
    conn = _index_with([("c1", "alpha bravo"), ("c2", "charlie delta")])
    try:
        out = _server(conn).dispatch("search.query", {"q": raw})
        assert isinstance(out["hits"], list)
        assert isinstance(out["total"], int)
    finally:
        conn.close()


def test_ordinary_search_still_finds_things():
    """The cheap way to stop crashes is to break search. This is the control that says the
    fix did not do that."""
    conn = _index_with([("c1", "alpha bravo"), ("c2", "charlie delta")])
    try:
        out = _server(conn).dispatch("search.query", {"q": "alpha"})
        assert [h["conversation_id"] for h in out["hits"]] == ["c1"]
        assert out["total"] == 1

        both = _server(conn).dispatch("search.query", {"q": "bravo alpha"})
        assert [h["conversation_id"] for h in both["hits"]] == ["c1"]

        # Implicit AND: a token that matches nothing excludes the row.
        none = _server(conn).dispatch("search.query", {"q": "alpha zulu"})
        assert none["hits"] == [] and none["total"] == 0
    finally:
        conn.close()


def test_a_query_of_only_punctuation_returns_nothing_rather_than_erroring():
    conn = _index_with([("c1", "alpha bravo")])
    try:
        out = _server(conn).dispatch("search.query", {"q": "((("})
        assert out["hits"] == []
        assert out["total"] == 0
    finally:
        conn.close()


def test_prefix_search_works_through_the_rpc():
    conn = _index_with([("c1", "alphabet soup")])
    try:
        out = _server(conn).dispatch("search.query", {"q": "alpha*"})
        assert [h["conversation_id"] for h in out["hits"]] == ["c1"]
    finally:
        conn.close()


def test_the_provider_filter_still_applies_after_sanitising():
    conn = _index_with([("c1", "alpha bravo")])
    try:
        hit = _server(conn).dispatch("search.query", {"q": "alpha", "provider": "codex"})
        assert len(hit["hits"]) == 1
        miss = _server(conn).dispatch("search.query", {"q": "alpha", "provider": "grok"})
        assert miss["hits"] == [] and miss["total"] == 0
    finally:
        conn.close()


def test_the_raw_binding_really_did_crash_before(monkeypatch):
    """BOTH-STATES control. A test suite that only exercises the fixed path proves nothing
    about whether the fix was needed — this pins that the OLD behaviour genuinely raised, so
    a future refactor that reverts to raw binding fails here rather than silently."""
    conn = _index_with([("c1", "alpha bravo")])
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "SELECT COUNT(*) FROM conversations_fts WHERE conversations_fts MATCH ?",
                ("foo(",)).fetchone()
    finally:
        conn.close()
