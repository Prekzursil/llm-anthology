"""The metadata RPC surface — the absorbed codex-session-manager annotation layer, over
the wire.

Beyond plain CRUD, three properties are load-bearing and each has a test here:

  * a PARTIAL update must not blank the fields it was not given (the cockpit edits one
    field at a time, so a per-field call that clobbered its siblings would silently
    destroy the owner's notes);
  * free text is sanitized on the way OUT as well as in — an annotation can be pasted
    straight out of a hostile conversation, and this corpus is known to carry
    hidden-unicode payloads;
  * the store's key guard raises `ValueError`, and that must arrive at the client as a
    -32602 param error rather than escaping as an internal fault. `_req_str` accepts
    "   " (a non-empty string) while the guard rejects it, so that gap is real.

Synthetic fixtures only; every connection is in-memory and closed by the fixture.
"""
import json
import sqlite3

import pytest

from llm_anthology import corpus, ir, sidecar

# A zero-width space (U+200B, category Cf) — a hidden-unicode smuggling payload.
ZW = "​"

_OPEN = []


def _track(conn):
    _OPEN.append(conn)
    return conn


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _mk_conv(conn, cid, provider, title, body, thread_id="", nturns=0):
    conv = ir.Conversation(
        id=cid, title=title, provider=provider,
        turns=[ir.Turn("human", []) for _ in range(nturns)],
        created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        account="acct")
    corpus.add_conversation(conn, conv, body=body, thread_id=thread_id)


def _annot_index():
    """A tracked in-memory index holding two conversations to annotate."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    corpus.init_index(conn)
    _track(conn)
    _mk_conv(conn, "c-a", "codex", "Alpha title", "body-a", thread_id="t-a", nturns=2)
    _mk_conv(conn, "c-b", "claude", "Beta title", "body-b", thread_id="t-b", nturns=1)
    conn.commit()
    return conn


def _annot_server():
    return sidecar.Sidecar(_annot_index())


# ------------------------------------------------------------------------ read

def test_metadata_get_unannotated_reads_back_empty_not_an_error():
    srv = _annot_server()
    assert srv.dispatch("metadata.get", {"conversation_id": "c-a"}) == {
        "conversation_id": "c-a", "alias": "", "tags": [], "notes": "", "is_empty": True}


# ----------------------------------------------------------------------- write

def test_metadata_set_then_get_roundtrips():
    srv = _annot_server()
    srv.dispatch("metadata.set", {"conversation_id": "c-a", "alias": "Refactor run",
                                  "tags": ["work", "rust"], "notes": "picked up Tuesday"})
    out = srv.dispatch("metadata.get", {"conversation_id": "c-a"})
    assert out["alias"] == "Refactor run"
    assert out["tags"] == ["rust", "work"]        # the store orders tags deterministically
    assert out["notes"] == "picked up Tuesday"
    assert out["is_empty"] is False


def test_metadata_set_is_partial_an_omitted_field_is_untouched():
    """The crux of the per-field contract: editing the alias must not blank tags/notes."""
    srv = _annot_server()
    srv.dispatch("metadata.set", {"conversation_id": "c-a", "alias": "first",
                                  "tags": ["keep"], "notes": "keep me"})
    out = srv.dispatch("metadata.set", {"conversation_id": "c-a", "alias": "second"})
    assert out["alias"] == "second"
    assert out["tags"] == ["keep"]
    assert out["notes"] == "keep me"


def test_metadata_set_explicit_blank_clears_that_field():
    srv = _annot_server()
    srv.dispatch("metadata.set", {"conversation_id": "c-a", "alias": "x", "tags": ["t"]})
    out = srv.dispatch("metadata.set", {"conversation_id": "c-a", "alias": "", "tags": []})
    assert out["alias"] == ""
    assert out["tags"] == []


def test_metadata_set_rejects_non_list_tags():
    srv = _annot_server()
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("metadata.set", {"conversation_id": "c-a", "tags": "work"})
    assert ei.value.code == -32602


def test_metadata_set_rejects_non_string_alias_or_notes():
    srv = _annot_server()
    for bad in ({"conversation_id": "c-a", "alias": 7},
                {"conversation_id": "c-a", "notes": ["no"]}):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch("metadata.set", bad)
        assert ei.value.code == -32602


def test_metadata_set_whitespace_only_id_surfaces_as_a_param_error():
    srv = _annot_server()
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("metadata.set", {"conversation_id": "   ", "alias": "x"})
    assert ei.value.code == -32602


# ----------------------------------------------------------------------- clear

def test_metadata_clear_drops_the_annotation():
    srv = _annot_server()
    srv.dispatch("metadata.set", {"conversation_id": "c-a", "alias": "gone",
                                  "tags": ["t"]})
    assert srv.dispatch("metadata.clear", {"conversation_id": "c-a"})["is_empty"] is True
    assert srv.dispatch("metadata.get", {"conversation_id": "c-a"})["is_empty"] is True


def test_metadata_clear_whitespace_only_id_surfaces_as_a_param_error():
    srv = _annot_server()
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("metadata.clear", {"conversation_id": "  "})
    assert ei.value.code == -32602


# ---------------------------------------------------------------------- search

def test_metadata_search_with_no_filter_returns_nothing():
    """A blank query must not dump the whole catalogue into the UI."""
    srv = _annot_server()
    srv.dispatch("metadata.set", {"conversation_id": "c-a", "alias": "anything"})
    assert srv.dispatch("metadata.search", {}) == []


def test_metadata_search_by_tag_and_by_text_joins_display_columns():
    srv = _annot_server()
    srv.dispatch("metadata.set", {"conversation_id": "c-a", "alias": "Refactor",
                                  "tags": ["rust"], "notes": "needle here"})
    srv.dispatch("metadata.set", {"conversation_id": "c-b", "tags": ["python"]})

    by_tag = srv.dispatch("metadata.search", {"tag": "rust"})
    assert [r["conversation_id"] for r in by_tag] == ["c-a"]
    row = by_tag[0]
    assert row["provider"] == "codex"
    assert row["title"] == "Alpha title"
    assert row["thread_id"] == "t-a"
    assert row["turn_count"] == 2
    assert row["annotation"]["tags"] == ["rust"]

    assert [r["conversation_id"] for r in
            srv.dispatch("metadata.search", {"text": "needle"})] == ["c-a"]
    # the two filters are ANDed, not ORed
    assert srv.dispatch("metadata.search", {"tag": "python", "text": "needle"}) == []


def test_metadata_search_rejects_non_string_filters():
    srv = _annot_server()
    for bad in ({"text": 5}, {"tag": ["x"]}):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch("metadata.search", bad)
        assert ei.value.code == -32602


def test_metadata_tags_facet_counts_and_is_empty_when_unannotated():
    srv = _annot_server()
    assert srv.dispatch("metadata.tags", {}) == []
    srv.dispatch("metadata.set", {"conversation_id": "c-a", "tags": ["shared", "solo"]})
    srv.dispatch("metadata.set", {"conversation_id": "c-b", "tags": ["shared"]})
    assert srv.dispatch("metadata.tags", {}) == [
        {"tag": "shared", "count": 2}, {"tag": "solo", "count": 1}]


# -------------------------------------------------------------- sanitize / no corpus

def test_metadata_free_text_is_sanitized_on_the_way_out():
    """A hidden zero-width char pasted into an annotation must not reach the UI."""
    srv = _annot_server()
    out = srv.dispatch("metadata.set", {
        "conversation_id": "c-a", "alias": "Al" + ZW + "ias",
        "tags": ["ta" + ZW + "g"], "notes": "no" + ZW + "te"})
    assert out["alias"] == "Alias"
    assert out["tags"] == ["tag"]
    assert out["notes"] == "note"
    assert ZW not in json.dumps(out)


def test_metadata_methods_require_a_corpus():
    srv = sidecar.Sidecar(None)
    for method, params in (("metadata.get", {"conversation_id": "c"}),
                           ("metadata.set", {"conversation_id": "c", "alias": "a"}),
                           ("metadata.clear", {"conversation_id": "c"}),
                           ("metadata.search", {"text": "x"}),
                           ("metadata.tags", {})):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch(method, params)
        assert ei.value.code == sidecar.CORPUS_NOT_INDEXED, method
