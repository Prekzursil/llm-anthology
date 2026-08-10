"""DECISION D-3: faceted + temporal search filters, in the library primitive.

WHAT WAS MISSING. `corpus.search` was `MATCH ? ORDER BY rank LIMIT ?` with no WHERE clause
and a `(query, limit)` signature, even though `provider`, `created_at`, `turn_count` and
`thread_id` were already on the joined `conversations` row. So "what was I working on in
March, in Codex only?" — the question a search box over a multi-provider archive exists to
answer — was unanswerable, and answering it needs a query change, not a migration.

THREE DESIGN CHOICES THESE TESTS PIN, because each has a wrong-looking-right alternative.

1. DATE BOUNDS ARE COMPARED AS VARIABLE-WIDTH PREFIXES, not padded to a full day.
   `conversations.created_at` is an ISO-8601 *string* and the adapters do not agree on its
   suffix: `codex_rollout` writes `...Z`, `chatgpt` writes `datetime.isoformat()` which is
   `...+00:00`, `grok` can carry nanoseconds, and `codex.py` writes `""`. A naive
   `created_at <= '2026-03-31'` therefore drops every March-31 conversation, because
   `'2026-03-31T23:59:59Z' > '2026-03-31'` lexically. Padding the bound to
   `'2026-03-31T99:99:99'` would work by accident and break on the first row that carries an
   offset instead of a Z.
   So each bound is compared against `substr(created_at, 1, len(bound))`: `until='2026-03'`
   compares seven characters and covers the whole month, `until='2026'` compares four and
   covers the whole year. That is inclusive at whatever granularity the caller expressed and
   needs NO calendar arithmetic — no 28-vs-31 day table, no leap-year branch.

2. AN UNDATED ROW IS EXCLUDED BY ANY DATE BOUND, in both directions.
   `substr('', 1, 7)` is `''`, and `'' <= '2026-03'` is TRUE, so a `until`-only filter would
   silently ADMIT every conversation the ingest could not date while a `since`-only filter
   rejected them. Same filter, opposite answers, no error. Both directions require a
   non-empty `created_at` instead, so "in this date range" never means "or undated".

3. THE HISTOGRAM'S COUNTS SUM TO THE MATCH COUNT — the undated rows get the `""` bucket
   rather than being dropped. A roll-up whose total disagrees with the result count is a
   roll-up that silently loses rows, and a UI drawing it would show a March that is missing
   conversations it could still open from the list beside it.

PRIVACY: synthetic fixtures only. No real conversation, title, provider account or path.
"""
import sqlite3

import pytest

from llm_anthology import corpus, ir

_OPEN = []


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _mem_index():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _OPEN.append(conn)
    return corpus.init_index(conn)


def _conv(cid, text, provider="codex", created="2026-01-01T00:00:00Z"):
    """One one-turn conversation. `text` is both the title and the block body, so it is
    reachable by MATCH through the contentless FTS index."""
    return ir.Conversation(
        id=cid, title=text, provider=provider, account="acct",
        turns=[ir.Turn(role="human", blocks=[ir.Block(type="text", text=text)])],
        created_at=created, updated_at=created)


#: The fixture corpus every filter test below reads. Deliberately spans two providers, four
#: months, BOTH timestamp suffixes the adapters really produce, and one undated row.
CORPUS = [
    ("jan-codex", "widget alpha", "codex", "2026-01-15T10:00:00Z"),
    ("mar01-codex", "widget bravo", "codex", "2026-03-01T00:00:00+00:00"),
    ("mar31-codex", "widget charlie", "codex", "2026-03-31T23:59:59Z"),
    ("mar15-chatgpt", "widget delta", "chatgpt", "2026-03-15T12:00:00Z"),
    ("apr-codex", "widget echo", "codex", "2026-04-01T00:00:00Z"),
    ("undated-grok", "widget foxtrot", "grok", ""),
]


def _loaded():
    conn = _mem_index()
    for cid, text, provider, created in CORPUS:
        corpus.add_conversation(conn, _conv(cid, text, provider, created))
    return conn


def _ids(rows):
    return sorted(row["conversation_id"] for row in rows)


# ------------------------------------------------------------------ the provider facet

def test_an_unfiltered_search_still_returns_every_provider():
    """The old call shape must keep its old answer — the filters are additive."""
    conn = _loaded()
    assert _ids(corpus.search(conn, "widget")) == [
        "apr-codex", "jan-codex", "mar01-codex", "mar15-chatgpt", "mar31-codex",
        "undated-grok"]


def test_the_provider_filter_keeps_only_that_provider():
    conn = _loaded()
    assert _ids(corpus.search(conn, "widget", provider="chatgpt")) == ["mar15-chatgpt"]
    assert _ids(corpus.search(conn, "widget", provider="grok")) == ["undated-grok"]


def test_a_provider_nothing_carries_returns_no_rows_rather_than_everything():
    """The failure mode of a filter built by string concatenation is that an unmatched value
    quietly drops the clause. An unknown provider must return zero rows, not the whole set."""
    conn = _loaded()
    assert corpus.search(conn, "widget", provider="gemini") == []


def test_the_provider_filter_is_exact_not_a_prefix():
    conn = _loaded()
    assert corpus.search(conn, "widget", provider="code") == []
    assert corpus.search(conn, "widget", provider="codexx") == []


# --------------------------------------------------------------- the temporal bounds

def test_a_day_bound_is_inclusive_on_both_ends():
    conn = _loaded()
    assert _ids(corpus.search(conn, "widget", since="2026-03-01", until="2026-03-31")) == [
        "mar01-codex", "mar15-chatgpt", "mar31-codex"]


def test_the_until_bound_survives_a_time_of_day_and_both_utc_spellings():
    """`until='2026-03-31'` against `'2026-03-31T23:59:59Z'`. A whole-string comparison
    excludes it; the prefix comparison keeps it. The `+00:00` row is the same trap at the
    other end — it is the spelling `datetime.isoformat()` produces, and it sorts BELOW `Z`."""
    conn = _loaded()
    assert _ids(corpus.search(conn, "widget", until="2026-03-31")) == [
        "jan-codex", "mar01-codex", "mar15-chatgpt", "mar31-codex"]
    assert _ids(corpus.search(conn, "widget", since="2026-03-01", until="2026-03-01")) == [
        "mar01-codex"]


def test_a_month_bound_covers_the_whole_month_with_no_calendar_arithmetic():
    conn = _loaded()
    assert _ids(corpus.search(conn, "widget", since="2026-03", until="2026-03")) == [
        "mar01-codex", "mar15-chatgpt", "mar31-codex"]


def test_a_year_bound_covers_the_whole_year():
    conn = _loaded()
    assert _ids(corpus.search(conn, "widget", since="2026", until="2026")) == [
        "apr-codex", "jan-codex", "mar01-codex", "mar15-chatgpt", "mar31-codex"]
    assert corpus.search(conn, "widget", since="2027") == []


def test_a_since_bound_alone_is_open_ended_upward():
    conn = _loaded()
    assert _ids(corpus.search(conn, "widget", since="2026-03")) == [
        "apr-codex", "mar01-codex", "mar15-chatgpt", "mar31-codex"]


def test_either_date_bound_excludes_a_conversation_the_ingest_could_not_date():
    """Both directions, because `substr('',1,7) <= '2026-03'` is TRUE: without an explicit
    non-empty requirement an `until` filter admits exactly the rows a `since` filter drops."""
    conn = _loaded()
    assert "undated-grok" not in _ids(corpus.search(conn, "widget", until="2026-12"))
    assert "undated-grok" not in _ids(corpus.search(conn, "widget", since="2020"))
    assert "undated-grok" in _ids(corpus.search(conn, "widget"))


def test_a_malformed_bound_raises_instead_of_silently_matching_nothing():
    """`'2026-3'` is six characters, so a prefix comparison would test `'2026-0' >= '2026-3'`
    and return nothing at all. Answering a typo with an empty result set is the worst
    available behaviour: it looks like "you wrote nothing in March"."""
    conn = _loaded()
    for bad in ["2026-3", "26-03", "2026-13", "2026-00", "2026-02-32", "2026-03-00",
                "march", "", "2026-03-15T10:00:00Z", "2026/03/15", "20x6", "2026-0a",
                "2026-03-15-01", "٢٠٢٦"]:
        with pytest.raises(ValueError):
            corpus.search(conn, "widget", since=bad)
        with pytest.raises(ValueError):
            corpus.search(conn, "widget", until=bad)


def test_a_non_ascii_digit_year_is_refused_even_though_str_isdigit_accepts_it():
    """The reason `_is_num` does not use `str.isdigit`. `'٢٠٢٦'` is four digit characters by
    that test and `int()` parses it as 2026, but it can never be a prefix of an ISO-8601
    column — so tolerating it would answer a valid-looking bound with an empty result set."""
    arabic_indic = "٢٠٢٦"
    assert arabic_indic.isdigit() and int(arabic_indic) == 2026
    conn = _loaded()
    with pytest.raises(ValueError):
        corpus.search(conn, "widget", since=arabic_indic)


def test_a_well_formed_but_nonexistent_day_behaves_as_an_inclusive_month_bound():
    """`2026-02-30` does not exist. Accepted anyway, and harmless BY CONSTRUCTION rather
    than by luck: as an inclusive prefix bound it admits all of February and nothing later,
    which is what a calendar table would have computed."""
    conn = _loaded()
    assert corpus.search(conn, "widget", until="2026-02-30") == corpus.search(
        conn, "widget", until="2026-02")


def test_a_non_string_bound_raises():
    conn = _loaded()
    with pytest.raises(ValueError):
        corpus.search(conn, "widget", since=2026)


# ------------------------------------------------------------------- the facets compose

def test_march_in_codex_only_is_answerable_in_one_call():
    """The question DECISION D-3 exists for."""
    conn = _loaded()
    assert _ids(corpus.search(conn, "widget", provider="codex",
                              since="2026-03", until="2026-03")) == [
        "mar01-codex", "mar31-codex"]


def test_the_limit_still_applies_under_a_filter():
    conn = _loaded()
    assert len(corpus.search(conn, "widget", provider="codex", limit=2)) == 2


def test_a_filter_does_not_reorder_the_rows_it_keeps():
    """Relevance ordering is the previous unit's contract; a WHERE clause must not disturb
    it. Asserted as an order-preserving SUBSEQUENCE rather than a pinned permutation, so the
    test does not quietly re-specify bm25."""
    conn = _loaded()
    unfiltered = [row["conversation_id"] for row in corpus.search(conn, "widget")]
    filtered = [row["conversation_id"] for row in corpus.search(conn, "widget",
                                                                provider="codex")]
    assert filtered == [cid for cid in unfiltered if cid in set(filtered)]
    assert len(filtered) == 4


def test_search_still_returns_the_retrievable_columns_under_a_filter():
    conn = _loaded()
    row = corpus.search(conn, "delta", provider="chatgpt")[0]
    assert row["conversation_id"] == "mar15-chatgpt"
    assert row["provider"] == "chatgpt"
    assert row["created_at"] == "2026-03-15T12:00:00Z"
    assert row["turn_count"] == 1


# ------------------------------------------------------------ the WHERE-fragment seam

def test_search_filter_sql_is_empty_when_nothing_is_filtered():
    """The unfiltered path must add no SQL at all, so the pre-D-3 query plan is untouched."""
    assert corpus.search_filter_sql() == ("", [])


def test_search_filter_sql_ands_each_bound_and_binds_every_value():
    """No filter value may be interpolated into the SQL — each one arrives as a parameter."""
    sql, args = corpus.search_filter_sql(provider="codex", since="2026-03", until="2026-03")
    assert sql.startswith(" AND ")
    assert args == ["codex", "2026-03", "2026-03"]
    assert "codex" not in sql and "2026" not in sql


# ------------------------------------------------------------- the hits-over-time roll-up

def test_the_histogram_buckets_by_month_in_ascending_order():
    conn = _loaded()
    assert corpus.search_histogram(conn, "widget") == [
        ("", 1), ("2026-01", 1), ("2026-03", 3), ("2026-04", 1)]


def test_the_histogram_counts_sum_to_the_number_of_matching_rows():
    """The invariant that makes the roll-up safe to draw beside the result list."""
    conn = _loaded()
    for kw in [{}, {"provider": "codex"}, {"since": "2026-03"},
               {"provider": "codex", "since": "2026-01", "until": "2026-03"}]:
        rows = corpus.search(conn, "widget", limit=1000, **kw)
        buckets = corpus.search_histogram(conn, "widget", **kw)
        assert sum(count for _b, count in buckets) == len(rows), kw


def test_the_histogram_takes_year_and_day_granularity_too():
    conn = _loaded()
    assert corpus.search_histogram(conn, "widget", bucket="year") == [
        ("", 1), ("2026", 5)]
    assert corpus.search_histogram(conn, "widget", bucket="day", provider="codex",
                                   since="2026-03", until="2026-03") == [
        ("2026-03-01", 1), ("2026-03-31", 1)]


def test_the_histogram_honours_every_filter_the_search_does():
    conn = _loaded()
    assert corpus.search_histogram(conn, "widget", provider="chatgpt") == [("2026-03", 1)]
    assert corpus.search_histogram(conn, "widget", since="2026-04") == [("2026-04", 1)]


def test_the_histogram_of_a_query_that_matches_nothing_is_empty():
    conn = _loaded()
    assert corpus.search_histogram(conn, "absenttoken") == []


def test_the_histogram_rejects_an_unknown_bucket_rather_than_guessing():
    conn = _loaded()
    for bad in ["week", "MONTH", "", None, 7]:
        with pytest.raises(ValueError):
            corpus.search_histogram(conn, "widget", bucket=bad)


def test_the_histogram_validates_its_date_bounds_like_search_does():
    conn = _loaded()
    with pytest.raises(ValueError):
        corpus.search_histogram(conn, "widget", since="2026-3")
