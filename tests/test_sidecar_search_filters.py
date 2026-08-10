"""DECISION D-3 at the RPC edge: `search.query` gains facets and a hits-over-time roll-up.

WHY THE PARAMETERS ARE ADDITIVE AND NOT A NEW METHOD. The cockpit already calls
``search.query`` (`cockpit/src/ipc/real.ts`), so the pre-D-3 call shape has to keep its
pre-D-3 answer byte for byte — including the ABSENCE of a `histogram` key when nobody asked
for one. `test_the_pre_d3_call_shape_is_unchanged` is the whole reason `histogram` is a
GRANULARITY string rather than a boolean flag defaulting to something: absent means "do not
compute it", so an old caller pays neither a new key nor a second SQL query.

WHAT THIS FILE ADDS OVER `tests/test_corpus_search_filters.py`. That file pins the library
primitive. This one pins the three things only the RPC edge can get wrong:

  1. a malformed bound is an INVALID-PARAMS error (-32602), not a `ValueError` escaping as
     -32603 "internal error" — the difference between the UI being able to say "that is not
     a date" and it having to say "the engine broke";
  2. the filters ride the SANITIZED match expression (`fts_match_expression`), so a facet
     search from a real search box cannot crash on punctuation the way an unfiltered one
     could not either;
  3. `total` and the histogram counts agree with each other over the wire, which is what a
     UI drawing the roll-up beside a paged list depends on.

PRIVACY: synthetic fixtures only.
"""
import sqlite3

import pytest

from llm_anthology import corpus, ir, sidecar

_OPEN = []


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _conv(cid, text, provider, created):
    return ir.Conversation(
        id=cid, title=text, provider=provider, account="acct",
        turns=[ir.Turn(role="human", blocks=[ir.Block(type="text", text=text)])],
        created_at=created, updated_at=created)


#: Two providers, four months, both UTC spellings the adapters really emit, one undated row.
CORPUS = [
    ("jan-codex", "widget alpha", "codex", "2026-01-15T10:00:00Z"),
    ("mar01-codex", "widget bravo", "codex", "2026-03-01T00:00:00+00:00"),
    ("mar31-codex", "widget charlie", "codex", "2026-03-31T23:59:59Z"),
    ("mar15-chatgpt", "widget delta", "chatgpt", "2026-03-15T12:00:00Z"),
    ("apr-codex", "widget echo", "codex", "2026-04-01T00:00:00Z"),
    ("undated-grok", "widget foxtrot", "grok", ""),
]


def _server():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _OPEN.append(conn)
    corpus.init_index(conn)
    for cid, text, provider, created in CORPUS:
        corpus.add_conversation(conn, _conv(cid, text, provider, created))
    return sidecar.Sidecar(conn)


def _ids(out):
    return sorted(hit["conversation_id"] for hit in out["hits"])


def _bad(params):
    """Dispatch `search.query` expecting -32602, and return the message for inspection."""
    with pytest.raises(sidecar.RpcError) as ei:
        _server().dispatch("search.query", params)
    assert ei.value.code == -32602, ei.value.message
    return ei.value.message


# --------------------------------------------------------- the pre-D-3 shape is preserved

def test_the_pre_d3_call_shape_is_unchanged():
    """Exactly the three keys the cockpit already reads, and NO histogram key."""
    out = _server().dispatch("search.query", {"q": "widget"})
    assert set(out) == {"hits", "total", "took_ms"}
    assert out["total"] == 6 and len(out["hits"]) == 6
    assert set(out["hits"][0]) >= {"conversation_id", "snippet", "score", "provider"}


def test_the_existing_provider_filter_still_narrows_hits_and_total():
    out = _server().dispatch("search.query", {"q": "widget", "provider": "codex"})
    assert out["total"] == 4
    assert _ids(out) == ["apr-codex", "jan-codex", "mar01-codex", "mar31-codex"]


def test_a_non_string_provider_is_still_invalid_params():
    assert "provider" in _bad({"q": "widget", "provider": 7})


# ------------------------------------------------------------------- the temporal bounds

def test_since_and_until_narrow_both_the_hits_and_the_total():
    out = _server().dispatch(
        "search.query", {"q": "widget", "since": "2026-03-01", "until": "2026-03-31"})
    assert out["total"] == 3
    assert _ids(out) == ["mar01-codex", "mar15-chatgpt", "mar31-codex"]


def test_a_month_bound_covers_the_whole_month_over_the_wire():
    out = _server().dispatch("search.query", {"q": "widget", "since": "2026-03",
                                              "until": "2026-03"})
    assert _ids(out) == ["mar01-codex", "mar15-chatgpt", "mar31-codex"]


def test_a_malformed_bound_is_invalid_params_not_an_internal_error():
    """A `ValueError` from the library escaping as -32603 would tell the UI the engine broke
    when the truth is that the user typed a bad date. The distinction is the whole point of
    validating at the edge."""
    for bad in ["2026-3", "march", "", "2026-13", "2026/03/15"]:
        assert "since" in _bad({"q": "widget", "since": bad})
        assert "until" in _bad({"q": "widget", "until": bad})


def test_a_non_string_bound_is_invalid_params():
    assert "since" in _bad({"q": "widget", "since": 2026})
    assert "until" in _bad({"q": "widget", "until": None})


def test_a_facet_search_survives_punctuation_the_way_an_unfiltered_one_does():
    """The filters must ride `fts_match_expression`, not the raw string: a filtered search
    from a real search box faces exactly the same FTS5 syntax hazard as an unfiltered one."""
    for raw in ["widget(", "widget AND", '"widget', "NEAR/", "*"]:
        out = _server().dispatch(
            "search.query", {"q": raw, "provider": "codex", "since": "2026-01"})
        assert isinstance(out["total"], int)


def test_a_query_with_no_usable_token_is_zero_hits_not_a_crash_under_filters():
    out = _server().dispatch("search.query", {"q": "((", "provider": "codex",
                                              "since": "2026-01", "histogram": "month"})
    assert out["total"] == 0 and out["hits"] == [] and out["histogram"] == []


# ---------------------------------------------------------- the hits-over-time histogram

def test_the_histogram_is_absent_unless_a_granularity_is_asked_for():
    assert "histogram" not in _server().dispatch("search.query", {"q": "widget"})


def test_the_histogram_is_a_list_of_bucket_count_dtos_ascending():
    out = _server().dispatch("search.query", {"q": "widget", "histogram": "month"})
    assert out["histogram"] == [
        {"bucket": "", "count": 1},
        {"bucket": "2026-01", "count": 1},
        {"bucket": "2026-03", "count": 3},
        {"bucket": "2026-04", "count": 1}]


def test_the_histogram_counts_sum_to_the_total_it_is_drawn_beside():
    """The invariant a paged UI depends on: the roll-up describes the SAME result set the
    list is paging through, so a bar chart cannot disagree with the result count."""
    srv = _server()
    for extra in [{}, {"provider": "codex"}, {"since": "2026-03"},
                  {"provider": "codex", "since": "2026-01", "until": "2026-03"}]:
        params = dict({"q": "widget", "histogram": "month", "limit": 2}, **extra)
        out = srv.dispatch("search.query", params)
        assert sum(b["count"] for b in out["histogram"]) == out["total"], params
        assert len(out["hits"]) <= 2                     # the roll-up is NOT page-scoped


def test_the_histogram_takes_year_and_day_granularity():
    srv = _server()
    assert srv.dispatch("search.query", {"q": "widget", "histogram": "year"})["histogram"] \
        == [{"bucket": "", "count": 1}, {"bucket": "2026", "count": 5}]
    assert srv.dispatch("search.query", {
        "q": "widget", "histogram": "day", "provider": "codex",
        "since": "2026-03", "until": "2026-03"})["histogram"] == [
            {"bucket": "2026-03-01", "count": 1}, {"bucket": "2026-03-31", "count": 1}]


def test_an_unknown_histogram_granularity_is_invalid_params():
    for bad in ["week", "MONTH", "", True, 7, None]:
        assert "histogram" in _bad({"q": "widget", "histogram": bad})


def test_the_march_in_codex_only_question_is_one_rpc_call():
    """The question DECISION D-3 exists for, end to end over the RPC surface."""
    out = _server().dispatch("search.query", {
        "q": "widget", "provider": "codex", "since": "2026-03", "until": "2026-03",
        "histogram": "day"})
    assert out["total"] == 2
    assert _ids(out) == ["mar01-codex", "mar31-codex"]
    assert out["histogram"] == [{"bucket": "2026-03-01", "count": 1},
                                {"bucket": "2026-03-31", "count": 1}]


def test_paging_still_partitions_the_filtered_set():
    srv = _server()
    first = srv.dispatch("search.query", {"q": "widget", "provider": "codex", "limit": 2})
    second = srv.dispatch("search.query", {"q": "widget", "provider": "codex", "limit": 2,
                                          "offset": 2})
    assert first["total"] == second["total"] == 4
    assert len(first["hits"]) == len(second["hits"]) == 2
    assert not set(_ids(first)) & set(_ids(second))
    assert sorted(_ids(first) + _ids(second)) == [
        "apr-codex", "jan-codex", "mar01-codex", "mar31-codex"]


def test_a_filtered_hit_still_carries_its_timestamp_and_thread_linkage_rules():
    """`ts_ms` comes from the same `created_at` the facet filtered on, so a hit inside a
    date range can never carry a timestamp outside it. The undated row still has no
    `ts_ms` at all — which is exactly why a date bound must exclude it."""
    srv = _server()
    hit = srv.dispatch("search.query", {"q": "delta", "since": "2026-03"})["hits"][0]
    assert hit["conversation_id"] == "mar15-chatgpt" and hit["ts_ms"] > 0
    undated = srv.dispatch("search.query", {"q": "foxtrot"})["hits"][0]
    assert "ts_ms" not in undated
    assert srv.dispatch("search.query", {"q": "foxtrot", "since": "2020"})["hits"] == []


def test_a_bucket_key_crosses_the_wire_sanitized():
    """A bucket key is a SLICE OF `created_at`, which an adapter copied out of a source file
    — so it is other-process-influenceable text bound for a UI, exactly the class this
    module's privacy boundary says must pass through `sanitize`. `dispatch` does NOT sanitize
    results globally (each handler does it explicitly, which is how this was missed once), so
    the roll-up has to do it itself.

    The bucket's VALUE is garbage here, and that is fine and out of scope: a conversation
    stamped with a corrupt timestamp buckets somewhere meaningless either way. What must not
    happen is a hidden-unicode payload reaching the wire through a new key.
    """
    zero_width = "​"
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _OPEN.append(conn)
    corpus.init_index(conn)
    corpus.add_conversation(
        conn, _conv("smuggled", "widget golf", "codex", "2026-0%s3-15T00:00:00Z" % zero_width))
    out = sidecar.Sidecar(conn).dispatch("search.query", {"q": "widget",
                                                         "histogram": "month"})
    assert len(out["histogram"]) == 1
    assert zero_width not in out["histogram"][0]["bucket"]


def test_the_corpus_must_be_open_before_a_facet_search():
    with pytest.raises(sidecar.RpcError) as ei:
        sidecar.Sidecar(None).dispatch("search.query", {"q": "widget", "since": "2026"})
    assert ei.value.code != -32602
