"""The `conversation.list` RPC — the ordered browse feed for an EXPORT-sourced corpus.

WHY THIS SURFACE EXISTS. Every browse pane in the cockpit is fed by `graph.roots`, which
serves THREADS. A downloaded export declares no thread, so the ingest writes no thread row,
so `graph.roots` returns `[]`. Measured over a CHILD-PROCESS stdio JSON-RPC session against a
two-conversation ChatGPT export before this handler existed:

    corpus.stats      -> {"conversations": 2, "records": 4, "threads": 0, "edges": 0}
    graph.roots       -> []            (order=recent AND the default order)
    search.query q="" -> -32602        ("q must be a non-empty string")
    search.query q=" " -> {"total": 0, "hits": []}
    conversation.list -> -32601        ("method not found: conversation.list")
    conversation.get  -> the full record, available: true

So a user who successfully imported an export saw an empty list, an empty graph, and no way
to reach a conversation without first GUESSING a search term — while the records sat fully
indexed and individually readable. The data was never missing; the LIST VERB was.

WHAT THIS FILE PINS, and why each leg is not redundant:

  * BROWSABILITY. The keystone test drives the real ingest and asserts `graph.roots` is still
    empty while `conversation.list` returns both conversations. Asserting only that the new
    method answers would pass against a handler that lists nothing; asserting only the count
    would pass against one that ignores the export. The pane is proven by the CONTRAST.
  * ORDERING IS TOTAL. Recency descending with `created_at` as the fallback for a row whose
    `updated_at` is empty, `conversation_id` breaking ties. A partial order would make paging
    drop and repeat rows, which is worse than no list at all because nothing signals it.
  * AN UNDATED ROW STILL APPEARS. Codex task exports write `created_at = ""`. Sorting them
    away would silently hide conversations the stats line still counts.
  * THE SEAM. The id a row carries is the key `conversation.get` accepts, driven end to end,
    so the browse journey is proven rather than assumed.
  * REFUSALS. `limit`/`offset`/`provider` are validated at the edge with the same -32602 the
    sibling readers use, and a missing corpus is -32000 rather than a crash.
  * THE WIRE. One leg goes through `handle_line`, so the DTO is proven JSON-serializable as
    NDJSON rather than merely as a Python dict.

PRIVACY: synthetic fixtures only. No test here reads a real export, `~/.codex`, `~/.claude`
or the owner's corpus.
"""
import json
import os
import sqlite3

import pytest

from llm_anthology import corpus, ir, sidecar

# A zero-width space (U+200B) — the hidden-unicode payload `sanitize_for_copy` must strip
# from anything crossing the wire.
ZW = "​"

_OPEN = []


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _conv(cid, title, provider="chatgpt", created="", updated="", turns=1):
    return ir.Conversation(
        id=cid, title=title, provider=provider, account="acct",
        turns=[ir.Turn(role="human", blocks=[ir.Block(type="text", text="body %s" % cid)])
               for _ in range(turns)],
        created_at=created, updated_at=updated)


def _seeded(rows):
    """A sidecar over an in-memory index holding exactly `rows` conversations."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _OPEN.append(conn)
    corpus.init_index(conn)
    for conv in rows:
        corpus.add_conversation(conn, conv)
    return sidecar.Sidecar(conn)


def _ids(out):
    return [row["id"] for row in out]


def _bad(params):
    """Dispatch `conversation.list` expecting -32602; return the message for inspection.

    Seeded with a real row on purpose: a refusal measured against an EMPTY corpus could be
    the query finding nothing rather than the edge refusing the parameter.
    """
    with pytest.raises(sidecar.RpcError) as ei:
        _seeded([_conv("c-1", "one")]).dispatch("conversation.list", params)
    assert ei.value.code == -32602, ei.value.message
    return ei.value.message


# ------------------------------------------------------------------ the keystone: C-B P0

def _chatgpt_export(tmp_path):
    """A two-conversation ChatGPT export in the measured shape: a `mapping` of nodes whose
    active path is resolved from `current_node`. The newer conversation is written FIRST so
    that a handler which merely preserved file order could not pass the ordering assertion."""
    payload = []
    for cid, title, base in (("conv-aaa", "Older chat about pasta", 1_700_000_000.0),
                             ("conv-bbb", "Newer chat about rust", 1_760_000_000.0)):
        payload.append({
            "id": cid, "title": title,
            "create_time": base, "update_time": base + 500.0,
            "current_node": "n2",
            "mapping": {
                "n1": {"id": "n1", "parent": None, "children": ["n2"],
                       "message": {"id": "m1", "author": {"role": "user"},
                                   "create_time": base,
                                   "content": {"content_type": "text",
                                               "parts": ["question in " + cid]}}},
                "n2": {"id": "n2", "parent": "n1", "children": [],
                       "message": {"id": "m2", "author": {"role": "assistant"},
                                   "create_time": base + 500.0,
                                   "content": {"content_type": "text",
                                               "parts": ["answer in " + cid]}}},
            },
        })
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_an_imported_export_is_browsable_even_though_graph_roots_stays_empty(tmp_path):
    """THE defect this handler closes, driven through the real ingest.

    `graph.roots` is asserted EMPTY in the same test rather than in a separate one, because
    the contrast is the finding: the corpus is fully indexed and individually readable, and
    the only browse feed the app had reported nothing. A test that asserted only the new
    method's output would also pass on a build where threads happened to exist.
    """
    index_path = str(tmp_path / "index.db")
    conn = corpus.open_index(index_path)
    _OPEN.append(conn)
    srv = sidecar.Sidecar(conn, build_runner=lambda fn: fn())   # run the ingest inline

    srv.dispatch("corpus.build", {"chatgpt_export": _chatgpt_export(tmp_path)})

    status = srv.dispatch("corpus.build_status", {})
    assert status["state"] == "done" and status["errors"] == [], status
    assert status["indexed_conversations"] == 2, status

    stats = srv.dispatch("corpus.stats", {})
    assert (stats["conversations"], stats["threads"], stats["edges"]) == (2, 0, 0), stats
    assert srv.dispatch("graph.roots", {"order": "recent"}) == [], \
        "an export declares no thread, so the thread feed is empty — that is the premise"

    listed = srv.dispatch("conversation.list", {})
    assert _ids(listed) == ["conv-bbb", "conv-aaa"], listed
    assert [row["title"] for row in listed] == ["Newer chat about rust",
                                                "Older chat about pasta"]
    assert [row["provider"] for row in listed] == ["chatgpt", "chatgpt"]
    assert [row["turn_count"] for row in listed] == [2, 2], listed


def test_the_id_a_row_carries_is_the_key_conversation_get_accepts(tmp_path):
    """The browse seam, end to end: list -> click a row -> read it.

    Pinned because the list is worthless if its identity key is not the one the reader takes.
    The two methods answer in different vocabularies elsewhere on this wire (`search.query`
    spells it `conversation_id`), so which one this DTO uses is a real decision, not a
    formality.
    """
    index_path = str(tmp_path / "index.db")
    conn = corpus.open_index(index_path)
    _OPEN.append(conn)
    srv = sidecar.Sidecar(conn, build_runner=lambda fn: fn())
    srv.dispatch("corpus.build", {"chatgpt_export": _chatgpt_export(tmp_path)})

    first = srv.dispatch("conversation.list", {"limit": 1})[0]
    opened = srv.dispatch("conversation.get", {"id": first["id"]})

    assert opened["available"] is True, opened
    assert opened["id"] == first["id"] == "conv-bbb"
    assert opened["title"] == first["title"]
    assert [b["text"] for t in opened["turns"] for b in t["blocks"]] == [
        "question in conv-bbb", "answer in conv-bbb"]


# ------------------------------------------------------------------------------- ordering

#: `updated_at` empty on purpose for `fallback`, so the COALESCE fallback is exercised rather
#: than assumed; `undated` has neither timestamp; `tie-a`/`tie-b` share one instant.
ORDERED = [
    _conv("oldest", "oldest", created="2023-01-01T00:00:00Z", updated="2023-01-02T00:00:00Z"),
    _conv("fallback", "fallback", created="2025-06-01T00:00:00Z", updated=""),
    _conv("newest", "newest", created="2026-01-01T00:00:00Z", updated="2026-08-01T00:00:00Z"),
    _conv("tie-b", "tie b", created="2024-05-05T00:00:00Z", updated="2024-05-05T00:00:00Z"),
    _conv("tie-a", "tie a", created="2024-05-05T00:00:00Z", updated="2024-05-05T00:00:00Z"),
    _conv("undated", "undated", created="", updated=""),
]


def test_the_list_is_recency_ordered_with_created_at_as_the_fallback():
    """`updated_at` when present, else `created_at`, descending.

    `fallback` must land by its 2025 `created_at` — between `newest` (2026) and the 2024
    ties. A handler that sorted on `updated_at` alone would sink it below both, which is the
    exact mistake an empty-string-is-not-NULL column invites.
    """
    out = _seeded(ORDERED).dispatch("conversation.list", {})
    assert _ids(out) == ["newest", "fallback", "tie-a", "tie-b", "oldest", "undated"], out


def test_ties_break_on_conversation_id_so_the_order_is_total():
    """Two conversations sharing an instant must come back in a FIXED order.

    Without the id tie-break the order between them is whatever SQLite happens to scan, so a
    paged reader can drop one row and repeat another with nothing signalling it.
    """
    out = _seeded(ORDERED).dispatch("conversation.list", {})
    ids = _ids(out)
    assert ids.index("tie-a") < ids.index("tie-b"), "ascending id breaks a recency tie"
    assert _ids(_seeded(list(reversed(ORDERED))).dispatch("conversation.list", {})) == ids, \
        "the order must not depend on insertion order"


def test_an_undated_conversation_is_listed_last_rather_than_hidden():
    """Codex task exports write `created_at = ""`. The stats line counts those rows, so a
    list that dropped them would disagree with the count drawn beside it."""
    out = _seeded(ORDERED).dispatch("conversation.list", {})
    assert _ids(out)[-1] == "undated"
    assert len(out) == len(ORDERED), "every indexed conversation is reachable"


# -------------------------------------------------------------------------------- paging

def test_limit_and_offset_page_the_list_without_overlap_or_gap():
    srv = _seeded(ORDERED)
    whole = _ids(srv.dispatch("conversation.list", {}))
    pages = [_ids(srv.dispatch("conversation.list", {"limit": 2, "offset": off}))
             for off in (0, 2, 4)]
    assert [len(p) for p in pages] == [2, 2, 2], pages
    assert pages[0] + pages[1] + pages[2] == whole


def test_the_default_page_is_the_same_hundred_the_thread_feed_serves():
    """The default `limit` mirrors `graph.roots`, so the pane this feeds needs no new paging
    policy — and it is pinned over a corpus BIGGER than the page, which is the only way the
    assertion can see the number at all.

    The first version of this test compared `{}` against an explicit `{"limit": 100}` over the
    six-row fixture, and a mutation run measured that it SURVIVES changing the default from
    100 to 50: with six rows both calls return all six, so it would have passed against any
    default at or above the corpus size while reading as coverage. 101 rows is what makes the
    default observable.
    """
    srv = _seeded([_conv("c-%03d" % i, "conv %d" % i,
                         created="2026-01-01T00:%02d:%02dZ" % (i // 60, i % 60))
                   for i in range(101)])

    default = srv.dispatch("conversation.list", {})
    assert len(default) == 100, "the default page is 100 rows, not the whole corpus"
    assert default == srv.dispatch("conversation.list", {"limit": 100, "offset": 0})
    assert _ids(default)[0] == "c-100", "still newest first"
    assert _ids(srv.dispatch("conversation.list", {"offset": 100})) == ["c-000"], \
        "and the row the default page cut off is reachable on the next page"


def test_an_offset_past_the_end_is_an_empty_list_not_an_error():
    assert _seeded(ORDERED).dispatch("conversation.list", {"offset": 99}) == []


def test_a_limit_of_zero_returns_nothing_rather_than_everything():
    """`_opt_int` admits 0, and `LIMIT 0` must mean zero rows. A handler that treated a falsy
    limit as "unset" would hand back the whole table to a caller asking for none of it."""
    assert _seeded(ORDERED).dispatch("conversation.list", {"limit": 0}) == []


# ------------------------------------------------------------------------ provider facet

def test_a_provider_filter_narrows_the_list_and_keeps_the_order():
    srv = _seeded(ORDERED + [
        _conv("g-new", "grok new", provider="grok", created="2026-02-02T00:00:00Z"),
        _conv("g-old", "grok old", provider="grok", created="2020-02-02T00:00:00Z")])
    assert _ids(srv.dispatch("conversation.list", {"provider": "grok"})) == ["g-new", "g-old"]
    assert "g-new" in _ids(srv.dispatch("conversation.list", {}))


def test_a_provider_that_matches_nothing_is_an_empty_list_not_an_error():
    """An unknown facet value is a legitimate empty answer — the same thing the search facet
    does. Erroring would make a UI that remembers a filter unusable after a re-import."""
    assert _seeded(ORDERED).dispatch("conversation.list", {"provider": "nope"}) == []


# ------------------------------------------------------------------------------ refusals

def test_a_non_string_provider_is_invalid_params():
    """Bound as-is, a numeric provider would match no row and read as a true negative — the
    silent-empty-result class the search facet already refuses in these words."""
    assert _bad({"provider": 17}) == "provider must be a string"


@pytest.mark.parametrize("params,key", [
    ({"limit": -1}, "limit"),
    ({"limit": "10"}, "limit"),
    ({"limit": True}, "limit"),
    ({"limit": 1.5}, "limit"),
    ({"offset": -1}, "offset"),
    ({"offset": "0"}, "offset"),
    ({"offset": False}, "offset"),
])
def test_a_malformed_limit_or_offset_is_invalid_params(params, key):
    """The same message and code the sibling readers use. Booleans are refused even though
    `bool` subclasses `int`, because `{"limit": true}` is a caller that guessed the API."""
    assert _bad(params) == "%s must be a non-negative integer" % key


def test_it_refuses_when_no_corpus_is_attached():
    """-32000, not a crash: the sidecar serves `health.ping` with no index, and every data
    method has to say WHY it cannot answer."""
    with pytest.raises(sidecar.RpcError) as ei:
        sidecar.Sidecar(None).dispatch("conversation.list", {})
    assert ei.value.code == sidecar.CORPUS_NOT_INDEXED
    assert ei.value.message == "corpus not indexed"


# ------------------------------------------------------------- the wire, and the dispatch

def test_the_method_is_reachable_through_the_dispatch_table():
    """Half the deliverable is the dispatch entry. Before it existed this raised -32601, so
    the assertion is written as the POSITIVE — an absence-only check ("no -32601") would pass
    against a handler wired to the wrong name."""
    assert "conversation.list" in sidecar.Sidecar(None)._handlers
    with pytest.raises(sidecar.RpcError) as ei:
        sidecar.Sidecar(None).dispatch("conversation.does-not-exist", {})
    assert ei.value.code == -32601, "the control: an unknown verb still reports -32601"


def test_the_dto_survives_a_real_NDJSON_ROUND_TRIP():
    """Proven as a LINE, not as a dict. `_dumps` is what the cockpit actually reads, and a
    non-serializable value here would kill the process rather than return an error."""
    srv = _seeded(ORDERED)
    line = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "conversation.list",
                       "params": {"limit": 2}})
    response = json.loads(sidecar._dumps(srv.handle_line(line)))

    assert response["id"] == 7 and "error" not in response, response
    assert _ids(response["result"]) == ["newest", "fallback"]
    assert sorted(response["result"][0]) == [
        "created_at", "id", "provider", "title", "turn_count", "updated_at"]


def test_free_text_and_timestamps_are_sanitized_before_they_reach_the_wire():
    """A title and a timestamp are both copied out of a source file by an adapter, so both
    are other-process-influenceable text bound for a UI. The ordering is computed on the
    STORED value, so stripping the payload for display cannot move a row."""
    srv = _seeded([
        _conv("hidden", "clean" + ZW + "title",
              created="2026-01-01T00:00:00Z" + ZW, updated="2026-02-01T00:00:00Z" + ZW),
        _conv("plain", "plain", created="2020-01-01T00:00:00Z")])
    out = srv.dispatch("conversation.list", {})

    assert _ids(out) == ["hidden", "plain"], "sanitizing must not disturb the order"
    row = out[0]
    assert row["title"] == "cleantitle"
    assert ZW not in row["created_at"] and ZW not in row["updated_at"]
    assert row["created_at"] == "2026-01-01T00:00:00Z"


def test_the_handler_does_not_leave_a_filesystem_path_on_the_wire():
    """`conversations` also holds `rollout_path`, a local path that embeds the owner's
    username. It is NOT selected, and this pins that: a later widening of the SELECT would
    otherwise publish the store layout to the renderer without anything objecting."""
    srv = _seeded([_conv("c-1", "one", created="2026-01-01T00:00:00Z")])
    conn = srv.conn
    conn.execute("UPDATE conversations SET rollout_path=? WHERE conversation_id=?",
                 (os.path.join("C:", "Users", "someone", "secret.jsonl"), "c-1"))
    conn.commit()

    row = srv.dispatch("conversation.list", {})[0]
    assert "secret.jsonl" not in json.dumps(row)
    assert "rollout_path" not in row and "account" not in row
