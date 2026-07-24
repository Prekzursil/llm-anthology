"""sidecar.py — the stdio NDJSON JSON-RPC 2.0 engine the cockpit UI talks to.

SYNTHETIC fixtures ONLY. Every thread id, conversation, rollout line and token
count below is a made-up shape that mirrors the Phase-0 MEASURED schema (a spawn
graph of threads + directed edges over a contentless FTS5 index); none of it is a
real conversation, path, or the real $CODEX_HOME. The privacy boundary the sidecar
enforces — preview/snippet/transcript text passes through aisr.sanitize before it
crosses the wire — is proven here with a zero-width-space payload that must NOT
survive into any emitted DTO.

Coverage note: this file is written RED-first (sidecar.py does not exist yet) and
drives every handler + the envelope/dispatch/loop plumbing to 100% line+branch.
"""
import io
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from aisr import corpus, ir, sidecar
from aisr.corpus import SpawnEdge, ThreadMeta

# A zero-width space (U+200B, category Cf) — a hidden-unicode smuggling payload that
# aisr.sanitize.sanitize_for_copy must strip from anything crossing the wire.
ZW = "​"


# --------------------------------------------------------------------- fixtures

_OPEN = []


def _track(conn):
    _OPEN.append(conn)
    return conn


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _mk_thread(conn, tid, **kw):
    corpus.upsert_thread(conn, ThreadMeta(id=tid, **kw))


def _mk_edge(conn, parent, child, status=""):
    corpus.upsert_edge(conn, SpawnEdge(parent, child, status))


def _mk_conv(conn, cid, provider, title, body, thread_id="", rollout_path="",
             nturns=0, created="2026-01-01T00:00:00Z"):
    conv = ir.Conversation(
        id=cid, title=title, provider=provider,
        turns=[ir.Turn("human", []) for _ in range(nturns)],
        created_at=created, updated_at=created, account="acct")
    corpus.add_conversation(conn, conv, body=body, thread_id=thread_id,
                            rollout_path=rollout_path)


def _populate(conn):
    """The standard synthetic corpus: a rich root, a diamond, and a dangling edge.

    threads table: t1 (all optional fields set, a root) · t2 · t3 · t4.
    edges: t1->t2, t1->t4, t2->t3, t4->t3 (a diamond: t3 has two parents) plus a
    DANGLING edge ghost->g2 whose endpoints are absent from the threads table, so
    `ghost` is a second root that only a synthesized bare ThreadNode can render.
    """
    _mk_thread(conn, "t1", title="root alpha" + ZW, model_provider="openai",
               tokens_used=1000, created_at_ms=1000, updated_at_ms=2000,
               git_branch="main", cwd="/work/t1" + ZW, agent_role="role/architect",
               agent_nickname="Ada" + ZW, preview="first user line" + ZW,
               rollout_path="/real/rollout-t1.jsonl")
    _mk_thread(conn, "t2", title="child beta", model_provider="openai",
               created_at_ms=1100)
    _mk_thread(conn, "t3", title="grandchild gamma", model_provider="openai",
               created_at_ms=1200)
    _mk_thread(conn, "t4", title="child delta", model_provider="openai",
               created_at_ms=1150)
    _mk_edge(conn, "t1", "t2", status="completed")
    _mk_edge(conn, "t1", "t4", status="failed")
    _mk_edge(conn, "t2", "t3")             # empty status -> omitted from the DTO
    _mk_edge(conn, "t4", "t3", status="completed")
    _mk_edge(conn, "ghost", "g2", status="completed")   # dangling parent + child

    _mk_conv(conn, "c-codex-1", "codex", "rocket launch" + ZW + " alpha",
             "rocket rocket moon alpha", thread_id="t1",
             rollout_path="/real/rollout-t1.jsonl", nturns=3,
             created="2026-01-01T00:00:00Z")
    _mk_conv(conn, "c-claude-1", "claude", "beta rocket notes",
             "rocket beta notes here", thread_id="", nturns=1,
             created="2026-01-02T00:00:00Z")
    _mk_conv(conn, "c-codex-2", "codex", "gamma sailing",
             "a boat that mentions rocket once", thread_id="t3", nturns=0,
             created="")                    # empty created_at -> ts_ms omitted
    conn.commit()


def _mem_server():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    corpus.init_index(conn)
    _track(conn)
    _populate(conn)
    return sidecar.Sidecar(conn)


def _disk_index(tmp_path):
    path = str(tmp_path / "index.db")
    conn = _track(corpus.open_index(path))
    _populate(conn)
    conn.commit()
    return path


def _rollout_file(tmp_path):
    """A minimal synthetic Codex rollout: session_meta + one user + one assistant
    turn, each carrying a hidden ZW that must be stripped on the way out."""
    lines = [
        {"type": "session_meta", "timestamp": "2026-01-01T00:00:00Z",
         "payload": {"session_id": "conv-thread-1", "cwd": "/work",
                     "model_provider": "openai", "git": {"branch": "main"}}},
        {"type": "response_item", "timestamp": "2026-01-01T00:00:01Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "hello" + ZW + " there"}]}},
        {"type": "response_item", "timestamp": "2026-01-01T00:00:02Z",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "hi" + ZW + " friend"}]}},
    ]
    path = tmp_path / "rollout-conv.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    return str(path)


# ------------------------------------------------------------- pure helper units

def test_to_ms_none_and_empty_and_bad():
    assert sidecar._to_ms(None) is None          # non-str guard
    assert sidecar._to_ms("") is None            # empty guard
    assert sidecar._to_ms("not-a-date") is None  # ValueError path


def test_to_ms_zulu_and_offset_forms_agree():
    zulu = sidecar._to_ms("2026-01-01T00:00:00Z")
    offset = sidecar._to_ms("2026-01-01T00:00:00+00:00")
    assert zulu == offset
    assert isinstance(zulu, int) and zulu > 0


def test_clean_strips_hidden_unicode():
    assert sidecar._clean("a" + ZW + "b") == "ab"
    assert sidecar._clean("plain") == "plain"


def test_sanitize_tree_recurses_every_shape():
    src = {"s": "x" + ZW, "d": {"k": "y" + ZW}, "l": ["z" + ZW, 7],
           "n": None, "i": 5, "b": True}
    out = sidecar._sanitize_tree(src)
    assert out == {"s": "x", "d": {"k": "y"}, "l": ["z", 7],
                   "n": None, "i": 5, "b": True}


def test_dumps_is_compact_single_line():
    s = sidecar._dumps({"a": 1, "b": [2, 3]})
    assert "\n" not in s and ", " not in s and json.loads(s) == {"a": 1, "b": [2, 3]}


def test_error_response_with_and_without_data():
    with_data = sidecar._error_response(7, -32002, "busy", {"retry_ms": 100})
    assert with_data == {"jsonrpc": "2.0", "id": 7,
                         "error": {"code": -32002, "message": "busy",
                                   "data": {"retry_ms": 100}}}
    bare = sidecar._error_response(None, -32601, "nope")
    assert bare == {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32601, "message": "nope"}}
    assert "data" not in bare["error"]


def test_spawn_edge_status_present_and_omitted():
    assert sidecar._spawn_edge(SpawnEdge("p", "c", "done")) == \
        {"parent": "p", "child": "c", "status": "done"}
    assert sidecar._spawn_edge(SpawnEdge("p", "c", "")) == {"parent": "p", "child": "c"}


def test_redact_paths_basename_and_noop():
    assert sidecar._redact_paths({"rollout_path": "/a/b/c.jsonl", "x": 1}) == \
        {"rollout_path": "c.jsonl", "x": 1}
    assert sidecar._redact_paths({"x": 1}) == {"x": 1}   # no rollout_path -> untouched


def test_opt_int_default_valid_and_rejects():
    assert sidecar._opt_int({}, "limit", 50) == 50
    assert sidecar._opt_int({"limit": 5}, "limit", 50) == 5
    for bad in ({"limit": -1}, {"limit": "5"}, {"limit": True}):
        with pytest.raises(sidecar.RpcError) as ei:
            sidecar._opt_int(bad, "limit", 50)
        assert ei.value.code == -32602


def test_req_str_valid_and_rejects():
    assert sidecar._req_str({"thread_id": "t1"}, "thread_id") == "t1"
    for bad in ({}, {"thread_id": ""}, {"thread_id": 9}):
        with pytest.raises(sidecar.RpcError) as ei:
            sidecar._req_str(bad, "thread_id")
        assert ei.value.code == -32602


# --------------------------------------------------------- construction + health

def test_construct_without_conn_is_empty_corpus():
    srv = sidecar.Sidecar(None)
    assert srv.conn is None
    assert srv.corpus.roots() == []


def test_health_ping_ready_and_not_ready():
    ready = _mem_server().dispatch("health.ping", {})
    assert ready["ok"] is True
    assert ready["engine_version"] == sidecar.__version__
    assert ready["ir_version"] == ir.IR_VERSION
    assert ready["corpus_ready"] is True

    not_ready = sidecar.Sidecar(None).dispatch("health.ping", {})
    assert not_ready["corpus_ready"] is False


# ------------------------------------------------------------------ corpus.stats

def test_corpus_stats_counts_and_providers():
    stats = _mem_server().dispatch("corpus.stats", {})
    assert stats["conversations"] == 3
    assert stats["threads"] == 4          # ghost/g2 are edge-only, not threads rows
    assert stats["edges"] == 5
    assert stats["records"] == 3 + 1 + 0  # SUM(turn_count)
    assert stats["bytes"] == (len("rocket rocket moon alpha")
                              + len("rocket beta notes here")
                              + len("a boat that mentions rocket once"))
    assert stats["providers"] == {"codex": 2, "claude": 1}


def test_corpus_stats_requires_corpus():
    with pytest.raises(sidecar.RpcError) as ei:
        sidecar.Sidecar(None).dispatch("corpus.stats", {})
    assert ei.value.code == -32000


# ------------------------------------------------------------------- graph.roots

def test_graph_roots_default_order_and_synthesized_dangling_node():
    roots = _mem_server().dispatch("graph.roots", {})
    ids = [n["id"] for n in roots]
    assert ids == ["t1", "ghost"]         # default: chronological, None-created last

    rich = roots[0]
    assert rich["id"] == "t1" and rich["provider"] == "openai"
    assert rich["title"] == "root alpha" and ZW not in rich["title"]   # sanitized
    assert rich["preview"] == "first user line"
    assert rich["agent_nickname"] == "Ada" and rich["cwd"] == "/work/t1"
    assert rich["tokens"] == 1000 and rich["updated_at_ms"] == 2000
    assert rich["git_branch"] == "main" and rich["agent_role"] == "role/architect"
    assert rich["child_count"] == 2 and rich["depth"] == 0

    ghost = roots[1]                      # synthesized bare node: every optional absent
    assert ghost["id"] == "ghost" and ghost["provider"] == ""
    assert ghost["title"] == "" and ghost["created_at_ms"] is None
    assert ghost["child_count"] == 1 and ghost["depth"] == 0
    for opt in ("tokens", "updated_at_ms", "git_branch", "cwd", "agent_role",
                "agent_nickname", "preview"):
        assert opt not in ghost


def test_graph_roots_orderings_and_pagination():
    srv = _mem_server()
    assert [n["id"] for n in srv.dispatch("graph.roots", {"order": "recent"})] == \
        ["t1", "ghost"]
    assert [n["id"] for n in srv.dispatch("graph.roots", {"order": "title"})] == \
        ["ghost", "t1"]
    assert [n["id"] for n in srv.dispatch("graph.roots", {"limit": 1})] == ["t1"]
    assert [n["id"] for n in srv.dispatch("graph.roots",
                                          {"limit": 1, "offset": 1})] == ["ghost"]


def test_graph_roots_unknown_order_and_bad_paging_reject():
    srv = _mem_server()
    for params in ({"order": "sideways"}, {"limit": -3}, {"offset": "x"}):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch("graph.roots", params)
        assert ei.value.code == -32602


def test_graph_roots_requires_corpus():
    with pytest.raises(sidecar.RpcError) as ei:
        sidecar.Sidecar(None).dispatch("graph.roots", {})
    assert ei.value.code == -32000


# ---------------------------------------------------------------- graph.children

def test_graph_children_and_leaf_and_bad_param():
    srv = _mem_server()
    assert [n["id"] for n in srv.dispatch("graph.children", {"thread_id": "t1"})] == \
        ["t2", "t4"]
    assert srv.dispatch("graph.children", {"thread_id": "t3"}) == []   # leaf
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("graph.children", {})
    assert ei.value.code == -32602


# ----------------------------------------------------------------- graph.subtree

def test_graph_subtree_full_diamond_dedup_and_edges():
    sub = _mem_server().dispatch("graph.subtree", {"thread_id": "t1"})
    ids = sorted(n["id"] for n in sub["nodes"])
    assert ids == ["t1", "t2", "t3", "t4"]        # t3 appears once despite two parents
    edges = {(e["parent"], e["child"]) for e in sub["edges"]}
    assert edges == {("t1", "t2"), ("t1", "t4"), ("t2", "t3"), ("t4", "t3")}
    # the t2->t3 edge has empty status -> the SpawnEdge DTO omits it
    t2t3 = [e for e in sub["edges"] if e["parent"] == "t2"][0]
    assert "status" not in t2t3


def test_graph_subtree_depth_limits():
    srv = _mem_server()
    just_root = srv.dispatch("graph.subtree", {"thread_id": "t1", "depth": 0})
    assert [n["id"] for n in just_root["nodes"]] == ["t1"]
    one = srv.dispatch("graph.subtree", {"thread_id": "t1", "depth": 1})
    assert sorted(n["id"] for n in one["nodes"]) == ["t1", "t2", "t4"]


def test_graph_subtree_bad_depth_and_missing_thread_id():
    srv = _mem_server()
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("graph.subtree", {"thread_id": "t1", "depth": -1})
    assert ei.value.code == -32602
    with pytest.raises(sidecar.RpcError) as ei2:
        srv.dispatch("graph.subtree", {"thread_id": "t1", "depth": True})
    assert ei2.value.code == -32602
    with pytest.raises(sidecar.RpcError) as ei3:
        srv.dispatch("graph.subtree", {})
    assert ei3.value.code == -32602


# --------------------------------------------------------------- graph.ancestors

def test_graph_ancestors_chain_root_and_bad_param():
    srv = _mem_server()
    anc = {n["id"] for n in srv.dispatch("graph.ancestors", {"thread_id": "t3"})}
    assert anc == {"t2", "t4", "t1"}      # diamond: two parents joining at t1
    assert srv.dispatch("graph.ancestors", {"thread_id": "t1"}) == []   # a root
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("graph.ancestors", {})
    assert ei.value.code == -32602


# ------------------------------------------------------------------ search.query

def test_search_query_hits_total_and_sanitized_snippet():
    res = _mem_server().dispatch("search.query", {"q": "rocket"})
    assert res["total"] == 3
    assert isinstance(res["took_ms"], int) and res["took_ms"] >= 0
    ids = {h["conversation_id"] for h in res["hits"]}
    assert ids == {"c-codex-1", "c-claude-1", "c-codex-2"}
    by_id = {h["conversation_id"]: h for h in res["hits"]}

    one = by_id["c-codex-1"]
    assert ZW not in one["snippet"] and one["snippet"] == "rocket launch alpha"
    assert one["thread_id"] == "t1" and one["provider"] == "codex"
    assert one["ts_ms"] > 0
    assert isinstance(one["score"], float) and one["score"] > 0

    assert "thread_id" not in by_id["c-claude-1"]   # empty thread_id -> omitted
    assert "ts_ms" not in by_id["c-codex-2"]        # empty created_at -> omitted


def test_search_query_provider_filter_and_pagination():
    srv = _mem_server()
    codex = srv.dispatch("search.query", {"q": "rocket", "provider": "codex"})
    assert codex["total"] == 2
    assert {h["conversation_id"] for h in codex["hits"]} == {"c-codex-1", "c-codex-2"}

    page = srv.dispatch("search.query", {"q": "rocket", "limit": 1, "offset": 1})
    assert len(page["hits"]) == 1 and page["total"] == 3


def test_search_query_bad_params_reject():
    srv = _mem_server()
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("search.query", {})                      # missing q
    assert ei.value.code == -32602
    with pytest.raises(sidecar.RpcError) as ei2:
        srv.dispatch("search.query", {"q": "x", "provider": 5})   # non-str provider
    assert ei2.value.code == -32602


def test_search_query_requires_corpus():
    with pytest.raises(sidecar.RpcError) as ei:
        sidecar.Sidecar(None).dispatch("search.query", {"q": "x"})
    assert ei.value.code == -32000


# -------------------------------------------------------------------- thread.get

def test_thread_get_full_meta_sanitized():
    meta = _mem_server().dispatch("thread.get", {"thread_id": "t1"})
    assert meta["id"] == "t1" and meta["provider"] == "openai"
    assert meta["title"] == "root alpha" and ZW not in meta["title"]
    assert meta["cwd"] == "/work/t1" and meta["agent_nickname"] == "Ada"
    assert meta["preview"] == "first user line"
    assert meta["tokens"] == 1000
    assert meta["created_at_ms"] == 1000 and meta["updated_at_ms"] == 2000
    assert meta["child_count"] == 2 and meta["depth"] == 0
    assert meta["has_rollout"] is True          # rollout_path present, path withheld
    assert "rollout_path" not in meta


def test_thread_get_leaf_has_no_rollout():
    meta = _mem_server().dispatch("thread.get", {"thread_id": "t2"})
    assert meta["has_rollout"] is False and meta["updated_at_ms"] is None


def test_thread_get_unknown_and_missing():
    srv = _mem_server()
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("thread.get", {"thread_id": "nope"})
    assert ei.value.code == -32001                # thread-not-found
    with pytest.raises(sidecar.RpcError) as ei2:
        srv.dispatch("thread.get", {})
    assert ei2.value.code == -32602


# -------------------------------------------------------------- conversation.get

def test_conversation_get_reparse_full_transcript(tmp_path):
    path = _rollout_file(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    corpus.init_index(conn)
    _track(conn)
    _mk_conv(conn, "conv-thread-1", "codex", "opening" + ZW, "body text",
             thread_id="conv-thread-1", rollout_path=path, nturns=0)
    conn.commit()
    srv = sidecar.Sidecar(conn)

    conv = srv.dispatch("conversation.get", {"id": "conv-thread-1"})
    assert conv["available"] is True and conv["id"] == "conv-thread-1"
    assert conv["provider"] == "codex" and conv["ir_version"] == ir.IR_VERSION
    assert conv["parse_errors"] == 0
    roles = [t["role"] for t in conv["turns"]]
    assert roles == ["human", "assistant"]
    texts = [b["text"] for t in conv["turns"] for b in t["blocks"]]
    assert texts == ["hello there", "hi friend"]      # ZW stripped from transcript
    for t in texts:
        assert ZW not in t
    # the absolute rollout path is redacted to a basename in the emitted meta
    assert conv["meta"]["rollout_path"] == os.path.basename(path)
    assert "/" not in conv["meta"]["rollout_path"]


def test_conversation_get_stub_when_no_rollout():
    srv = _mem_server()                     # c-claude-1 has rollout_path=""
    conv = srv.dispatch("conversation.get", {"id": "c-claude-1"})
    assert conv["available"] is False
    assert conv["turns"] == [] and conv["provider"] == "claude"
    assert "reason" in conv


def test_conversation_get_stub_when_rollout_missing(tmp_path):
    missing = str(tmp_path / "gone.jsonl")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    corpus.init_index(conn)
    _track(conn)
    _mk_conv(conn, "c-missing", "codex", "t", "b", rollout_path=missing)
    conn.commit()
    conv = sidecar.Sidecar(conn).dispatch("conversation.get", {"id": "c-missing"})
    assert conv["available"] is False


def test_conversation_get_stub_when_rollout_unreadable(tmp_path, monkeypatch):
    path = _rollout_file(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    corpus.init_index(conn)
    _track(conn)
    _mk_conv(conn, "c-broken", "codex", "t", "b", rollout_path=path)
    conn.commit()

    def _boom(_p):
        raise OSError("permission denied")

    monkeypatch.setattr(sidecar.codex_rollout, "parse_rollout_file", _boom)
    conv = sidecar.Sidecar(conn).dispatch("conversation.get", {"id": "c-broken"})
    assert conv["available"] is False and "unreadable" in conv["reason"]


def test_serialize_turn_branch_present_and_absent_and_block_sanitized():
    srv = sidecar.Sidecar(None)          # serialization touches no DB
    with_branch = srv._serialize_turn(ir.Turn(
        "human", [ir.Block("text", "hi" + ZW, data={"k": "v" + ZW}, citations=["c" + ZW])],
        uuid="u", timestamp="ts", branch={"index": 1, "total": 3}))
    assert with_branch["branch"] == {"index": 1, "total": 3}
    block = with_branch["blocks"][0]
    assert block["type"] == "text" and block["text"] == "hi"      # ZW stripped
    assert block["data"] == {"k": "v"} and block["citations"] == ["c"]

    no_branch = srv._serialize_turn(ir.Turn("assistant", []))
    assert "branch" not in no_branch and no_branch["blocks"] == []


def test_conversation_get_unknown_and_missing():
    srv = _mem_server()
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("conversation.get", {"id": "nope"})
    assert ei.value.code == -32001
    with pytest.raises(sidecar.RpcError) as ei2:
        srv.dispatch("conversation.get", {})
    assert ei2.value.code == -32602


# ------------------------------------------------------- dispatch / envelope

def test_dispatch_unknown_method():
    with pytest.raises(sidecar.RpcError) as ei:
        _mem_server().dispatch("no.such.method", {})
    assert ei.value.code == -32601


def test_dispatch_params_must_be_object():
    with pytest.raises(sidecar.RpcError) as ei:
        _mem_server().dispatch("health.ping", ["positional"])
    assert ei.value.code == -32602


def test_dispatch_db_busy_maps_to_32002():
    srv = _mem_server()

    class _BusyConn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

    srv.conn = _BusyConn()
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("corpus.stats", {})
    assert ei.value.code == -32002 and ei.value.data["retry_ms"] > 0


def test_dispatch_other_operational_error_maps_to_internal():
    srv = _mem_server()

    class _BrokenConn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("no such table: conversations")

    srv.conn = _BrokenConn()
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("corpus.stats", {})
    assert ei.value.code == -32603


def test_handle_request_success_and_error_envelope():
    srv = _mem_server()
    ok = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "health.ping",
                             "params": {}})
    assert ok["id"] == 1 and ok["result"]["ok"] is True
    err = srv.handle_request({"jsonrpc": "2.0", "id": 2, "method": "bogus"})
    assert err["error"]["code"] == -32601 and err["id"] == 2


def test_handle_request_non_dict_and_bad_method():
    srv = _mem_server()
    assert srv.handle_request(["not", "a", "dict"])["error"]["code"] == -32600
    bad_method = srv.handle_request({"jsonrpc": "2.0", "id": 3, "method": 42})
    assert bad_method["error"]["code"] == -32600 and bad_method["id"] == 3


def test_handle_request_notification_suppressed():
    srv = _mem_server()
    # No "id" key -> a notification: never answered, even for an unknown method.
    assert srv.handle_request({"jsonrpc": "2.0", "method": "health.ping"}) is None
    assert srv.handle_request({"jsonrpc": "2.0", "method": "bogus"}) is None


def test_handle_request_internal_error(monkeypatch):
    srv = _mem_server()

    def _boom(_params):
        raise ValueError("unexpected")

    srv._handlers["health.ping"] = _boom
    resp = srv.handle_request({"jsonrpc": "2.0", "id": 9, "method": "health.ping"})
    assert resp["error"]["code"] == -32603 and "detail" in resp["error"]["data"]


def test_handle_line_blank_parse_error_and_valid():
    srv = _mem_server()
    assert srv.handle_line("   \n") is None                # blank -> skipped
    assert srv.handle_line("{not json") ["error"]["code"] == -32700
    ok = srv.handle_line('{"jsonrpc":"2.0","id":5,"method":"health.ping"}')
    assert ok["id"] == 5 and ok["result"]["corpus_ready"] is True


# ---------------------------------------------------------------- serve loop

def test_serve_writes_flushes_and_skips_noise():
    srv = _mem_server()
    stdin = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"health.ping"}\n'
        "\n"                                                   # blank -> no output
        '{"jsonrpc":"2.0","method":"health.ping"}\n'          # notification -> no output
        '{"jsonrpc":"2.0","id":2,"method":"corpus.stats","params":{}}\n')
    stdout = io.StringIO()
    srv.serve(stdin, stdout)
    out_lines = [ln for ln in stdout.getvalue().split("\n") if ln]
    assert len(out_lines) == 2                                # only the two requests
    parsed = [json.loads(ln) for ln in out_lines]
    assert parsed[0]["id"] == 1 and parsed[0]["result"]["ok"] is True
    assert parsed[1]["id"] == 2 and parsed[1]["result"]["conversations"] == 3


# ---------------------------------------------------------------------- main

def test_main_with_argv_index_serves(tmp_path):
    path = _disk_index(tmp_path)
    stdin = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"corpus.stats","params":{}}\n')
    stdout = io.StringIO()
    rc = sidecar.main(argv=["--index", path], stdin=stdin, stdout=stdout)
    assert rc == 0
    resp = json.loads(stdout.getvalue().strip())
    assert resp["result"]["conversations"] == 3


def test_main_no_path_reports_corpus_not_ready(monkeypatch):
    monkeypatch.delenv("AISR_INDEX", raising=False)
    stdin = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"health.ping"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"corpus.stats","params":{}}\n')
    stdout = io.StringIO()
    assert sidecar.main(argv=[], stdin=stdin, stdout=stdout) == 0
    lines = [json.loads(ln) for ln in stdout.getvalue().split("\n") if ln]
    assert lines[0]["result"]["corpus_ready"] is False
    assert lines[1]["error"]["code"] == -32000


def test_main_reads_index_from_env(tmp_path, monkeypatch):
    path = _disk_index(tmp_path)
    monkeypatch.setenv("AISR_INDEX", path)
    stdin = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"corpus.stats","params":{}}\n')
    stdout = io.StringIO()
    sidecar.main(argv=[], stdin=stdin, stdout=stdout)
    assert json.loads(stdout.getvalue().strip())["result"]["threads"] == 4


def test_main_defaults_to_sys_streams(tmp_path, monkeypatch):
    path = _disk_index(tmp_path)
    monkeypatch.setattr(sidecar.sys, "argv", ["prog", "--index", path])
    monkeypatch.setattr(sidecar.sys, "stdin", io.StringIO(""))   # empty -> loop no-op
    out = io.StringIO()
    monkeypatch.setattr(sidecar.sys, "stdout", out)
    assert sidecar.main() == 0            # argv/stdin/stdout all default to sys.*
    assert out.getvalue() == ""


# ------------------------------------------------------ e2e over real stdio (subprocess)

def _repo_root():
    """Directory holding the importable ``aisr`` package (aisr/sidecar.py -> up two)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(sidecar.__file__)))


def test_e2e_subprocess_roundtrip_over_real_stdio(tmp_path):
    """Spawn the REAL `python -m aisr.sidecar --index <synthetic>` process and
    round-trip health.ping + corpus.stats + graph.roots over actual OS stdio pipes
    (not in-memory StringIO), asserting the replies match the synthetic corpus. This is
    the Python-side complement to the Rust cargo e2e that proves the SAME wire.

    The index is built from SYNTHETIC fixtures only (`_populate`) — never $CODEX_HOME —
    and its builder connection is CLOSED before the subprocess opens the file."""
    path = str(tmp_path / "e2e.db")
    conn = corpus.open_index(path)
    _populate(conn)
    conn.commit()
    conn.close()                          # release before the sidecar subprocess opens it

    requests = (
        '{"jsonrpc":"2.0","id":1,"method":"health.ping"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"corpus.stats","params":{}}\n'
        '{"jsonrpc":"2.0","id":3,"method":"graph.roots","params":{}}\n')
    proc = subprocess.Popen(
        [sys.executable, "-m", "aisr.sidecar", "--index", path],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=_repo_root(), text=True, encoding="utf-8")
    out, err = proc.communicate(requests, timeout=30)   # closing stdin -> clean EOF exit
    assert proc.returncode == 0, "sidecar exited %s; stderr=%r" % (proc.returncode, err)

    replies = {r["id"]: r for r in (json.loads(ln) for ln in out.splitlines() if ln.strip())}
    assert set(replies) == {1, 2, 3}, "expected 3 framed replies, got %r" % (out,)

    health = replies[1]["result"]
    assert health["ok"] is True and health["corpus_ready"] is True
    assert health["engine_version"] and health["ir_version"] == ir.IR_VERSION

    stats = replies[2]["result"]          # _populate: 3 convs / 4 threads / 5 edges
    assert stats["conversations"] == 3 and stats["threads"] == 4 and stats["edges"] == 5
    assert stats["providers"] == {"codex": 2, "claude": 1}

    roots = replies[3]["result"]          # roots by created order: t1 (1000) then ghost (None)
    assert [n["id"] for n in roots] == ["t1", "ghost"]
    assert roots[0]["child_count"] == 2   # t1 -> {t2, t4}
