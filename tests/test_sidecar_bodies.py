"""DECISION G-4, the reader half: `conversation.get` serves the STORED body.

THE DEFECT. `_conversation_get` called `_reparse_conversation` on every read, which opens
`rollout_path` and parses the source file again. So the answer to "what is in this
conversation" depended on a file the index does not own: move the store, compact it, or
delete the export you imported from, and the reader returned
`{available: false, reason: "rollout unavailable"}` for a conversation it had fully indexed.

THE ORDER, and why it is that way round. Stored body first, re-parse only when NO body is
stored. Not "freshest wins": a body is rewritten by `add_conversation` on every re-index, so
the stored copy IS the ingest's answer, and preferring the file would put the reader back on
a dependency the archive exists to remove.

THREE OUTCOMES, all still reachable, and the middle one is the migration path:

  1. a body is stored           -> served from the archive, no filesystem access at all
  2. NO body is stored          -> re-parse `rollout_path` (every index built before G-4)
  3. neither is available       -> the `available:false` stub, unchanged

PRIVACY: synthetic fixtures only.
"""
import json
import os
import sqlite3

import pytest

from llm_anthology import corpus, ir, sidecar

_OPEN = []


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _rollout(tmp_path, name="rollout-2026-08-10T10-00-00-abc.jsonl",
             text="the text that only the FILE knows"):
    """A minimal but REAL Codex rollout, parsed by the real adapter."""
    path = tmp_path / name
    path.write_text(
        '{"timestamp":"2026-08-10T10:00:00.000Z","type":"session_meta",'
        '"payload":{"id":"sess-1","cwd":"/w","originator":"codex"}}\n'
        '{"timestamp":"2026-08-10T10:00:01.000Z","type":"response_item",'
        '"payload":{"type":"message","role":"user",'
        '"content":[{"type":"input_text","text":"%s"}]}}\n' % text,
        encoding="utf-8")
    return str(path)


def _index(turns, rollout_path, cid="c1", meta=None):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _OPEN.append(conn)
    corpus.init_index(conn)
    conv = ir.Conversation(id=cid, title="a synthetic title", provider="codex",
                           account="acct", turns=turns,
                           created_at="2026-01-01T00:00:00Z",
                           updated_at="2026-01-02T00:00:00Z",
                           meta=dict(meta or {}))
    corpus.add_conversation(conn, conv, rollout_path=rollout_path)
    conn.commit()
    return sidecar.Sidecar(conn), conn


def _turn(text, role="human"):
    return ir.Turn(role=role, blocks=[ir.Block(type="text", text=text)])


def _texts(out):
    return [b["text"] for t in out["turns"] for b in t["blocks"]]


def _drop_body(conn, cid="c1"):
    """Make the index look pre-G-4 for one conversation: the row is indexed, the body is
    not stored. This is the ONLY state that may fall back to the source file."""
    conn.execute("DELETE FROM conversation_bodies WHERE conversation_id=?", (cid,))
    conn.commit()


# ------------------------------------------------- 1. served from the archive

def test_conversation_get_serves_the_stored_body_after_the_SOURCE_IS_DELETED(tmp_path):
    """THE G-1 PROPERTY, end to end and at the wire. Index a conversation, delete the
    rollout it came from, and read it back — the transcript is still there."""
    path = _rollout(tmp_path)
    srv, _conn = _index([_turn("stored at ingest")], path)
    os.remove(path)
    assert not os.path.exists(path)

    out = srv.dispatch("conversation.get", {"id": "c1"})

    assert out["available"] is True, out.get("reason")
    assert _texts(out) == ["stored at ingest"]
    assert out["parse_errors"] == 0
    assert out["ir_version"] == ir.IR_VERSION


def test_the_archive_is_PREFERRED_over_a_source_file_that_still_exists(tmp_path):
    """Not "freshest wins". The stored body and the file are given DIFFERENT text so the
    answer names which one was read; a re-parse would return the file's text."""
    path = _rollout(tmp_path, text="the text that only the FILE knows")
    srv, _conn = _index([_turn("the text the ARCHIVE holds")], path)
    assert os.path.exists(path)

    assert _texts(srv.dispatch("conversation.get", {"id": "c1"})) == [
        "the text the ARCHIVE holds"]


def test_serving_from_the_archive_touches_NO_source_file(tmp_path, monkeypatch):
    """The stronger form of the test above: the reparse path is replaced by a landmine, so
    a read that reaches the filesystem at all fails loudly instead of quietly agreeing."""
    srv, _conn = _index([_turn("archived")], _rollout(tmp_path))

    def _boom(*_a, **_k):
        raise AssertionError("conversation.get re-parsed a source file it did not need")

    monkeypatch.setattr(srv, "_reparse_conversation", _boom)
    monkeypatch.setattr(srv, "_reparse_rollout", _boom)

    assert _texts(srv.dispatch("conversation.get", {"id": "c1"})) == ["archived"]


def test_every_IR_FIELD_survives_the_round_trip_to_the_wire(tmp_path):
    """The archive must not quietly serve a flattened transcript: roles, uuids, timestamps,
    the branch marker and the typed blocks all have to arrive."""
    turns = [
        ir.Turn(role="human", uuid="u-1", timestamp="2026-01-01T00:00:01Z",
                blocks=[ir.Block(type="text", text="a question")]),
        ir.Turn(role="assistant", uuid="u-2", timestamp="2026-01-01T00:00:02Z",
                branch={"index": 1, "total": 2},
                blocks=[ir.Block(type="thinking", text="deliberating"),
                        ir.Block(type="tool_use", text="Read",
                                 data={"name": "Read"},
                                 citations=[{"url": "https://example.invalid/d"}])]),
    ]
    srv, _conn = _index(turns, _rollout(tmp_path))

    out = srv.dispatch("conversation.get", {"id": "c1"})

    assert [t["role"] for t in out["turns"]] == ["human", "assistant"]
    assert [t["uuid"] for t in out["turns"]] == ["u-1", "u-2"]
    assert [t["timestamp"] for t in out["turns"]] == [
        "2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z"]
    assert "branch" not in out["turns"][0], "an unset branch is omitted, as before"
    assert out["turns"][1]["branch"] == {"index": 1, "total": 2}
    second = out["turns"][1]["blocks"]
    assert [b["type"] for b in second] == ["thinking", "tool_use"]
    assert second[1]["data"] == {"name": "Read"}
    assert second[1]["citations"] == [{"url": "https://example.invalid/d"}]


def test_the_display_columns_come_from_the_conversations_ROW(tmp_path):
    """title / provider / created_at / updated_at / account are indexed columns and are not
    duplicated into the archive — the archive would then be a second, drift-capable copy of
    facts the row already owns."""
    srv, _conn = _index([_turn("x")], _rollout(tmp_path))
    out = srv.dispatch("conversation.get", {"id": "c1"})
    assert out["id"] == "c1" and out["title"] == "a synthetic title"
    assert out["provider"] == "codex" and out["account"] == "acct"
    assert out["created_at"] == "2026-01-01T00:00:00Z"
    assert out["updated_at"] == "2026-01-02T00:00:00Z"


def test_the_stored_meta_reaches_the_wire_and_is_still_path_redacted(tmp_path):
    """`meta` is stored so the archive answers with the same conversation a re-parse did,
    and the local filesystem layout is still reduced to a basename on the way out."""
    path = _rollout(tmp_path)
    srv, _conn = _index([_turn("x")], path,
                        meta={"thread_id": "t7", "rollout_path": path})
    out = srv.dispatch("conversation.get", {"id": "c1"})
    assert out["meta"]["thread_id"] == "t7"
    assert out["meta"]["rollout_path"] == os.path.basename(path)
    assert "/" not in out["meta"]["rollout_path"] and "\\" not in out["meta"]["rollout_path"]


def test_the_stored_LEG_PATH_LIST_never_reaches_the_wire(tmp_path):
    """A PRIVACY DEFECT THIS UNIT WOULD OTHERWISE HAVE INTRODUCED.

    `loaders._bind` puts `meta["rollout_paths"]` — a LIST of absolute local paths, one per
    resumed leg — on every ingested conversation (`loaders.py:546`, `:705`). Before G-4 that
    list could not reach the UI: the wire only ever saw a FRESHLY RE-PARSED conversation, and
    an adapter does not produce the key. Storing `meta` faithfully and serving it verbatim
    would have published the caller's whole session-store layout, and `_redact_paths` would
    not have caught it — it basenames the SINGULAR `rollout_path` and knows nothing about the
    plural.

    `_reparse_conversation` already states the policy this restores: the leg PATHS stay off
    the wire, and `meta` carries the COUNT plus the single basenamed resume target.
    """
    srv, _conn = _index([_turn("x")], "", meta={
        "thread_id": "t7",
        "rollout_paths": ["/synthetic/store/2026/07/24/leg-one.jsonl",
                          "/synthetic/store/2026/07/24/leg-two.jsonl"]})

    out = srv.dispatch("conversation.get", {"id": "c1"})

    assert "rollout_paths" not in out["meta"], "the leg path LIST must not be relayed"
    assert out["meta"]["rollout_legs"] == 2, "the count replaces it, as on the reparse path"
    assert out["meta"]["rollout_path"] == "leg-two.jsonl", "the resume target, basenamed"
    blob = json.dumps(out["meta"])
    assert "/synthetic/store" not in blob and "\\synthetic\\store" not in blob
    assert "2026/07/24" not in blob


def test_a_SINGLE_leg_body_reports_no_leg_count_exactly_as_the_reparse_path_does(tmp_path):
    """The control for the rule above and the compatibility promise beside it: `rollout_legs`
    is set only when there really was a fold, because absent means "one file" and that is
    what every conversation looked like before legs existed. The lone path is still reduced
    to a basename."""
    srv, _conn = _index([_turn("x")], "", meta={
        "rollout_paths": ["/synthetic/store/only-leg.jsonl"]})
    out = srv.dispatch("conversation.get", {"id": "c1"})
    assert "rollout_legs" not in out["meta"]
    assert "rollout_paths" not in out["meta"]
    assert out["meta"]["rollout_path"] == "only-leg.jsonl"


def test_a_conversation_with_GENUINELY_ZERO_turns_does_not_reopen_its_rollout(tmp_path):
    """The `[]`-vs-None contract, at the wire. An indexed conversation whose body is stored
    and empty is AVAILABLE with no turns — it must not be mistaken for "nothing stored" and
    sent back to the file, which is the loop this table exists to break."""
    srv, _conn = _index([], _rollout(tmp_path))
    out = srv.dispatch("conversation.get", {"id": "c1"})
    assert out["available"] is True and out["turns"] == []


# ------------------------------------------- 2. the pre-G-4 fall-back still works

def test_an_index_with_NO_stored_body_still_re_parses_its_rollout(tmp_path):
    """THE MIGRATION PATH. Every index built before G-4 has no `conversation_bodies` rows,
    so the reader has to fall back — otherwise upgrading the app would empty every
    conversation in an index that still works perfectly well."""
    path = _rollout(tmp_path, text="only the file knows this")
    srv, conn = _index([_turn("stored")], path)
    _drop_body(conn)

    out = srv.dispatch("conversation.get", {"id": "c1"})

    assert out["available"] is True, out.get("reason")
    assert _texts(out) == ["only the file knows this"]


def test_the_fall_back_is_reached_ONLY_when_no_body_row_exists(tmp_path):
    """Both directions in one test, because the whole risk here is a reader that picks the
    wrong branch: the same connection answers from the archive with the row present and from
    the file with it absent."""
    path = _rollout(tmp_path, text="from the file")
    srv, conn = _index([_turn("from the archive")], path)
    assert _texts(srv.dispatch("conversation.get", {"id": "c1"})) == ["from the archive"]
    _drop_body(conn)
    assert _texts(srv.dispatch("conversation.get", {"id": "c1"})) == ["from the file"]


# --------------------------------------------- 3. the honest stub is still there

def test_no_body_and_no_rollout_is_still_an_available_false_stub(tmp_path):
    """The genuinely-unrecoverable case keeps its documented shape. This is the ONLY
    remaining route to a stub, and it must not be deleted: a pre-G-4 index whose sources
    are gone really has nothing to show."""
    path = _rollout(tmp_path)
    srv, conn = _index([_turn("stored")], path)
    _drop_body(conn)
    os.remove(path)

    out = srv.dispatch("conversation.get", {"id": "c1"})

    assert out["available"] is False
    assert out["turns"] == [] and "unavailable" in out["reason"]
    assert out["provider"] == "codex" and out["title"] == "a synthetic title"


def test_an_unknown_id_is_still_an_error_not_a_stub():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _OPEN.append(conn)
    corpus.init_index(conn)
    with pytest.raises(sidecar.RpcError) as exc:
        sidecar.Sidecar(conn).dispatch("conversation.get", {"id": "nope"})
    assert exc.value.code == sidecar.THREAD_NOT_FOUND


def test_a_body_whose_meta_is_hostile_text_is_still_sanitized(tmp_path):
    """Stored meta is attacker-influenceable — it can carry whatever an imported export put
    in it — so it goes through the same hidden-unicode strip every other wire string does."""
    srv, _conn = _index([_turn("x")], _rollout(tmp_path),
                        meta={"note": "plain​text"})
    out = srv.dispatch("conversation.get", {"id": "c1"})
    assert out["meta"]["note"] == "plaintext"
    assert json.dumps(out["meta"]).count("\\u200b") == 0
