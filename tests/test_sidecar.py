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

from aisr import corpus, ir, render_html, sidecar
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


def _operand_index(tmp_path, name, build):
    """Build a standalone on-disk corpus index for a graph.diff operand from SYNTHETIC
    threads/edges (`build(conn)` does the upserts), then CLOSE the builder connection so
    the file is a settled snapshot the diff handler opens on its own — never $CODEX_HOME."""
    path = str(tmp_path / name)
    conn = corpus.open_index(path)
    build(conn)
    conn.commit()
    conn.close()
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


def test_req_int_valid_and_rejects():
    assert sidecar._req_int({"as_of_ms": 100}, "as_of_ms") == 100
    assert sidecar._req_int({"as_of_ms": 0}, "as_of_ms") == 0   # zero is a valid cutoff
    # missing key, non-int string, a bool (int subclass), and a float are all rejected
    for bad in ({}, {"as_of_ms": "5"}, {"as_of_ms": True}, {"as_of_ms": 1.5}):
        with pytest.raises(sidecar.RpcError) as ei:
            sidecar._req_int(bad, "as_of_ms")
        assert ei.value.code == -32602


def test_reject_nonlocal_dest_unc_and_relative_and_ok():
    # an absolute local path passes silently; UNC/protocol-relative and any relative
    # path are rejected before a single filesystem byte is touched.
    sidecar._reject_nonlocal_dest(os.path.join(os.getcwd(), "ok.json"))
    for bad in (r"\\host\share\x.json", "//host/share/x.json",
                "relative/x.json", "x.json"):
        with pytest.raises(sidecar.RpcError) as ei:
            sidecar._reject_nonlocal_dest(bad)
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


# ------------------------------------------------------------------- graph.rollup

def test_graph_rollup_over_synthetic_diamond_and_dangling():
    res = _mem_server().dispatch("graph.rollup", {})
    # keyed in sorted-id order over EVERY node, the dangling ghost/g2 pair included
    assert list(res) == ["g2", "ghost", "t1", "t2", "t3", "t4"]
    # t1's subtree is the whole diamond {t1,t2,t3,t4}: t3 counted ONCE despite two parents
    assert res["t1"] == {"self_tokens": 1000, "subtree_tokens": 1000, "self_count": 1,
                         "subtree_count": 4, "max_depth": 2, "child_count": 2}
    # ghost is an edge-only node (no ThreadMeta, self_tokens 0) that still roots g2
    assert res["ghost"] == {"self_tokens": 0, "subtree_tokens": 0, "self_count": 1,
                            "subtree_count": 2, "max_depth": 1, "child_count": 1}
    assert res["g2"] == {"self_tokens": 0, "subtree_tokens": 0, "self_count": 1,
                         "subtree_count": 1, "max_depth": 0, "child_count": 0}
    assert res["t3"]["subtree_count"] == 1 and res["t3"]["max_depth"] == 0


def test_graph_rollup_aggregates_tokens_up_each_subtree():
    srv = _mem_server()                       # conn present; inject a pure synthetic graph
    srv.corpus = corpus.Corpus(
        threads={"a": ThreadMeta(id="a", tokens_used=100),
                 "b": ThreadMeta(id="b", tokens_used=20),
                 "c": ThreadMeta(id="c", tokens_used=3)},
        edges=[SpawnEdge("a", "b"), SpawnEdge("b", "c")])
    res = srv.dispatch("graph.rollup", {})
    # the rollup sums descendants UP the chain: a totals 100+20+3, not just its own 100
    assert res["a"]["self_tokens"] == 100 and res["a"]["subtree_tokens"] == 123
    assert res["a"]["subtree_count"] == 3 and res["a"]["max_depth"] == 2
    assert res["b"]["subtree_tokens"] == 23 and res["c"]["subtree_tokens"] == 3


def test_graph_rollup_requires_corpus():
    with pytest.raises(sidecar.RpcError) as ei:
        sidecar.Sidecar(None).dispatch("graph.rollup", {})
    assert ei.value.code == -32000


# --------------------------------------------------------------------- graph.diff

def test_graph_diff_no_args_is_the_empty_self_diff():
    # both operands omitted -> the loaded corpus vs itself -> structurally identical
    res = _mem_server().dispatch("graph.diff", {})
    assert res == {"added_nodes": [], "removed_nodes": [], "added_edges": [],
                   "removed_edges": [], "changed_nodes": {}}


def test_graph_diff_old_operand_defaults_to_loaded_corpus(tmp_path):
    srv = _mem_server()
    srv.corpus = corpus.Corpus(threads={"x": ThreadMeta(id="x", tokens_used=5)}, edges=[])

    def _build_new(conn):
        _mk_thread(conn, "x", tokens_used=5)       # a byte-identical, unchanged node
        _mk_thread(conn, "y")                       # a new node
        _mk_edge(conn, "x", "y")                    # a new edge

    new_path = _operand_index(tmp_path, "new.db", _build_new)
    res = srv.dispatch("graph.diff", {"new_index": new_path})   # old omitted -> loaded
    assert res["added_nodes"] == ["y"] and res["removed_nodes"] == []
    assert res["added_edges"] == [{"parent": "x", "child": "y"}]
    assert res["removed_edges"] == [] and res["changed_nodes"] == {}


def test_graph_diff_two_paths_full_delta_privacy_and_field_order(tmp_path):
    def _build_old(conn):
        _mk_thread(conn, "a", title="alpha", tokens_used=10,
                   rollout_path="/old/rollout-a.jsonl")
        _mk_thread(conn, "b", title="beta")
        _mk_edge(conn, "a", "b")

    def _build_new(conn):
        _mk_thread(conn, "a", title="alpha2" + ZW, tokens_used=99,
                   rollout_path="/new/rollout-a.jsonl")
        _mk_thread(conn, "c", title="gamma")
        _mk_edge(conn, "a", "c")

    old_path = _operand_index(tmp_path, "old.db", _build_old)
    new_path = _operand_index(tmp_path, "new.db", _build_new)
    res = _mem_server().dispatch(
        "graph.diff", {"old_index": old_path, "new_index": new_path})

    assert res["added_nodes"] == ["c"] and res["removed_nodes"] == ["b"]
    assert res["added_edges"] == [{"parent": "a", "child": "c"}]
    assert res["removed_edges"] == [{"parent": "a", "child": "b"}]

    changed = res["changed_nodes"]["a"]
    # ThreadMeta declaration order preserved on the wire: title, tokens_used, rollout_path
    assert list(changed) == ["title", "tokens_used", "rollout_path"]
    # a hidden-unicode payload in the NEW title is stripped before it crosses the wire
    assert changed["title"] == ["alpha", "alpha2"] and ZW not in changed["title"][1]
    # ints survive the [old, new] projection unchanged
    assert changed["tokens_used"] == [10, 99]
    # the absolute rollout path is withheld — only its basename crosses, no FS layout
    assert changed["rollout_path"] == ["rollout-a.jsonl", "rollout-a.jsonl"]
    assert "/" not in changed["rollout_path"][0] + changed["rollout_path"][1]


def test_graph_diff_bad_index_params_reject(tmp_path):
    srv = _mem_server()
    missing = str(tmp_path / "gone.db")
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("graph.diff", {"old_index": missing})        # path is not a file
    assert ei.value.code == -32602
    with pytest.raises(sidecar.RpcError) as ei2:
        srv.dispatch("graph.diff", {"new_index": 123})            # path is not a string
    assert ei2.value.code == -32602


def test_graph_diff_requires_corpus():
    with pytest.raises(sidecar.RpcError) as ei:
        sidecar.Sidecar(None).dispatch("graph.diff", {})
    assert ei.value.code == -32000


# ------------------------------------------------- graph.diff (time-travel variant)

def test_graph_diff_time_travel_between_two_as_of_cutoffs():
    # as-of 1000 -> only t1 born (plus the always-present dangling ghost->g2); as-of
    # 1150 -> t1,t2,t4 born with their edges. The delta is pure ADDs, and a node in
    # BOTH snapshots (t1) is byte-identical, so changed_nodes stays empty.
    res = _mem_server().dispatch("graph.diff", {"as_of_a": 1000, "as_of_b": 1150})
    assert res["added_nodes"] == ["t2", "t4"]
    assert res["removed_nodes"] == []
    assert res["added_edges"] == [{"parent": "t1", "child": "t2"},
                                  {"parent": "t1", "child": "t4"}]
    assert res["removed_edges"] == []
    assert res["changed_nodes"] == {}


def test_graph_diff_time_travel_reverse_swaps_added_and_removed():
    res = _mem_server().dispatch("graph.diff", {"as_of_a": 1150, "as_of_b": 1000})
    assert res["added_nodes"] == [] and res["removed_nodes"] == ["t2", "t4"]
    assert res["added_edges"] == []
    assert res["removed_edges"] == [{"parent": "t1", "child": "t2"},
                                    {"parent": "t1", "child": "t4"}]
    assert res["changed_nodes"] == {}


def test_graph_diff_time_travel_omitted_operand_is_now():
    # as_of_b omitted -> the b-side is the full loaded corpus ("now"), so every later
    # node/edge shows as an ADD against the as-of-1000 snapshot.
    res = _mem_server().dispatch("graph.diff", {"as_of_a": 1000})
    assert res["added_nodes"] == ["t2", "t3", "t4"]
    assert res["added_edges"] == [{"parent": "t1", "child": "t2"},
                                  {"parent": "t1", "child": "t4"},
                                  {"parent": "t2", "child": "t3"},
                                  {"parent": "t4", "child": "t3"}]
    assert res["removed_nodes"] == [] and res["changed_nodes"] == {}


def test_graph_diff_index_and_as_of_are_mutually_exclusive():
    srv = _mem_server()
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("graph.diff", {"old_index": "whatever.db", "as_of_a": 5})
    assert ei.value.code == -32602


def test_graph_diff_bad_as_of_type_rejects():
    srv = _mem_server()
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("graph.diff", {"as_of_a": "not-an-int"})
    assert ei.value.code == -32602


# --------------------------------------------------------------- graph.timeline

def test_graph_timeline_events_range_and_undated_count():
    tl = _mem_server().dispatch("graph.timeline", {})
    # scoped to the threads table: t1..t4 are dated, ghost/g2 (edge-only) are not entries
    assert tl == {"events": [1000, 1100, 1150, 1200], "min_ms": 1000,
                  "max_ms": 1200, "undated_count": 0}


def test_graph_timeline_counts_undated_threads():
    srv = _mem_server()                         # conn present; inject an undated node
    srv.corpus = corpus.Corpus(
        threads={"a": ThreadMeta(id="a", created_at_ms=100),
                 "u": ThreadMeta(id="u")},      # created_at_ms defaults None -> undated
        edges=[])
    tl = srv.dispatch("graph.timeline", {})
    assert tl == {"events": [100], "min_ms": 100, "max_ms": 100, "undated_count": 1}


def test_graph_timeline_requires_corpus():
    with pytest.raises(sidecar.RpcError) as ei:
        sidecar.Sidecar(None).dispatch("graph.timeline", {})
    assert ei.value.code == -32000


# --------------------------------------------------------------------- graph.at

def test_graph_at_snapshot_is_coherent_and_sanitized():
    at = _mem_server().dispatch("graph.at", {"as_of_ms": 1150})
    # nodes = every id present as-of 1150, sorted by stable id (t3 not yet born)
    assert [n["id"] for n in at["nodes"]] == ["g2", "ghost", "t1", "t2", "t4"]
    by_id = {n["id"]: n for n in at["nodes"]}

    # child_count/depth are SNAPSHOT-relative: at 1150 t1 has spawned t2+t4 but neither
    # has spawned t3 yet, so t2 is a childless leaf in this moment.
    assert by_id["t1"]["child_count"] == 2 and by_id["t1"]["depth"] == 0
    assert by_id["t2"]["child_count"] == 0 and by_id["t2"]["depth"] == 1
    assert by_id["t4"]["child_count"] == 0 and by_id["t4"]["depth"] == 1
    # the free-text title is sanitized on the way out, exactly as graph.roots does
    assert by_id["t1"]["title"] == "root alpha" and ZW not in by_id["t1"]["title"]
    # a dangling (row-less) endpoint is synthesized as a bare node, always present
    assert by_id["ghost"]["provider"] == "" and by_id["ghost"]["created_at_ms"] is None
    assert by_id["ghost"]["child_count"] == 1 and by_id["g2"]["depth"] == 1

    # edges are the snapshot's, sorted by (parent, child), status preserved/omitted
    assert at["edges"] == [{"parent": "ghost", "child": "g2", "status": "completed"},
                           {"parent": "t1", "child": "t2", "status": "completed"},
                           {"parent": "t1", "child": "t4", "status": "failed"}]


def test_graph_at_before_time_begins_keeps_only_the_undated_dangling_edge():
    at = _mem_server().dispatch("graph.at", {"as_of_ms": 0})
    assert [n["id"] for n in at["nodes"]] == ["g2", "ghost"]   # no dated thread born yet
    assert at["edges"] == [{"parent": "ghost", "child": "g2", "status": "completed"}]


def test_graph_at_far_future_is_the_whole_graph():
    at = _mem_server().dispatch("graph.at", {"as_of_ms": 10_000})
    assert [n["id"] for n in at["nodes"]] == ["g2", "ghost", "t1", "t2", "t3", "t4"]
    by_id = {n["id"]: n for n in at["nodes"]}
    # once t3 is born the diamond is complete, so t2 regains its child (contrast 1150)
    assert by_id["t2"]["child_count"] == 1 and by_id["t1"]["child_count"] == 2


def test_graph_at_bad_as_of_and_missing_reject():
    srv = _mem_server()
    for bad in ({}, {"as_of_ms": "x"}, {"as_of_ms": True}):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch("graph.at", bad)
        assert ei.value.code == -32602


def test_graph_at_requires_corpus():
    with pytest.raises(sidecar.RpcError) as ei:
        sidecar.Sidecar(None).dispatch("graph.at", {"as_of_ms": 1})
    assert ei.value.code == -32000


# ------------------------------------------------------------------- export.plan

def test_export_plan_dry_run_tally():
    plan = _mem_server().dispatch("export.plan", {})
    assert plan["node_count"] == 6          # t1..t4 + the dangling ghost/g2 pair
    assert plan["edge_count"] == 5
    assert plan["conversation_count"] == 3
    assert plan["est_bytes"] == (len("rocket rocket moon alpha")
                                 + len("rocket beta notes here")
                                 + len("a boat that mentions rocket once"))


def test_export_plan_requires_corpus():
    with pytest.raises(sidecar.RpcError) as ei:
        sidecar.Sidecar(None).dispatch("export.plan", {})
    assert ei.value.code == -32000


# -------------------------------------------------------------------- export.run

def test_export_run_writes_graph_artifact_and_passes_gates(tmp_path):
    srv = _mem_server()
    dest = str(tmp_path / "export.json")
    res = srv.dispatch("export.run", {"dest_path": dest})
    assert res["ok"] is True
    assert res["graph_gate"] is True and res["transcript_gate"] is True
    assert res["written_path"] == dest
    assert os.path.isfile(dest)
    # the written artifact round-trips to the same spawn graph (sanitized)
    doc = json.loads(open(dest, encoding="utf-8").read())
    reparsed = sidecar.export.parse_graph(doc["graph"])
    assert sorted(reparsed.threads) == ["t1", "t2", "t3", "t4"]
    assert reparsed.threads["t1"].title == "root alpha"   # ZW stripped in the artifact


def test_export_run_gate_blocks_a_dropped_node(tmp_path, monkeypatch):
    """A serializer that silently drops an ISOLATED node -> the graph gate fires, so
    ok/written are False, no written_path is emitted, and nothing lands on disk."""
    srv = _mem_server()
    srv.corpus = corpus.Corpus(
        threads={"root": ThreadMeta(id="root"), "orphan": ThreadMeta(id="orphan")},
        edges=[])
    dest = str(tmp_path / "blocked.json")
    real = sidecar.export.serialize_graph

    def lossy(cc):
        doc = json.loads(real(cc))
        doc["nodes"] = [n for n in doc["nodes"] if n["id"] != "orphan"]
        return json.dumps(doc)

    monkeypatch.setattr(sidecar.export, "serialize_graph", lossy)
    res = srv.dispatch("export.run", {"dest_path": dest})
    assert res["ok"] is False and res["graph_gate"] is False
    assert res["transcript_gate"] is True
    assert "written_path" not in res
    assert not os.path.isfile(dest)


def test_export_run_rejects_unc_and_relative_dest():
    srv = _mem_server()
    for bad in (r"\\evil\share\out.json", "relative/out.json"):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch("export.run", {"dest_path": bad})
        assert ei.value.code == -32602


def test_export_run_rejects_parent_traversal_dest(tmp_path):
    # absolute + local + non-UNC, so it clears the sidecar's own check and is caught by
    # export_with_gate's confinement guard (the ExportPathError branch -> -32602).
    dest = str(tmp_path) + "/../escape.json"
    with pytest.raises(sidecar.RpcError) as ei:
        _mem_server().dispatch("export.run", {"dest_path": dest})
    assert ei.value.code == -32602


def test_export_run_missing_dest_and_requires_corpus(tmp_path):
    srv = _mem_server()
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("export.run", {})                       # no dest_path
    assert ei.value.code == -32602
    with pytest.raises(sidecar.RpcError) as ei2:
        sidecar.Sidecar(None).dispatch(
            "export.run", {"dest_path": str(tmp_path / "x.json")})
    assert ei2.value.code == -32000


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

    export_dest = str(tmp_path / "e2e-export.json")
    requests = (
        '{"jsonrpc":"2.0","id":1,"method":"health.ping"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"corpus.stats","params":{}}\n'
        '{"jsonrpc":"2.0","id":3,"method":"graph.roots","params":{}}\n'
        '{"jsonrpc":"2.0","id":4,"method":"graph.rollup","params":{}}\n'
        '{"jsonrpc":"2.0","id":5,"method":"graph.diff","params":{}}\n'
        '{"jsonrpc":"2.0","id":6,"method":"graph.timeline","params":{}}\n'
        '{"jsonrpc":"2.0","id":7,"method":"graph.at","params":{"as_of_ms":1150}}\n'
        '{"jsonrpc":"2.0","id":8,"method":"export.plan","params":{}}\n'
        '{"jsonrpc":"2.0","id":10,"method":"graph.diff",'
        '"params":{"as_of_a":1000,"as_of_b":1150}}\n'
        + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "export.run",
                      "params": {"dest_path": export_dest}}) + "\n")
    proc = subprocess.Popen(
        [sys.executable, "-m", "aisr.sidecar", "--index", path],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=_repo_root(), text=True, encoding="utf-8")
    out, err = proc.communicate(requests, timeout=30)   # closing stdin -> clean EOF exit
    assert proc.returncode == 0, "sidecar exited %s; stderr=%r" % (proc.returncode, err)

    replies = {r["id"]: r for r in (json.loads(ln) for ln in out.splitlines() if ln.strip())}
    assert set(replies) == set(range(1, 11)), "expected 10 framed replies, got %r" % (out,)

    health = replies[1]["result"]
    assert health["ok"] is True and health["corpus_ready"] is True
    assert health["engine_version"] and health["ir_version"] == ir.IR_VERSION

    stats = replies[2]["result"]          # _populate: 3 convs / 4 threads / 5 edges
    assert stats["conversations"] == 3 and stats["threads"] == 4 and stats["edges"] == 5
    assert stats["providers"] == {"codex": 2, "claude": 1}

    roots = replies[3]["result"]          # roots by created order: t1 (1000) then ghost (None)
    assert [n["id"] for n in roots] == ["t1", "ghost"]
    assert roots[0]["child_count"] == 2   # t1 -> {t2, t4}

    rollup_res = replies[4]["result"]     # the whole rollup table survives real-stdio JSON
    assert rollup_res["t1"]["subtree_count"] == 4      # the diamond deduped over the wire
    assert rollup_res["ghost"]["child_count"] == 1     # dangling node rolled up too

    diff_res = replies[5]["result"]       # {} -> the loaded corpus vs itself -> empty
    assert diff_res == {"added_nodes": [], "removed_nodes": [], "added_edges": [],
                        "removed_edges": [], "changed_nodes": {}}

    tt_diff = replies[10]["result"]       # time-travel DELTA: t2,t4 born between 1000..1150
    assert tt_diff["added_nodes"] == ["t2", "t4"]
    assert tt_diff["added_edges"] == [{"parent": "t1", "child": "t2"},
                                      {"parent": "t1", "child": "t4"}]
    assert tt_diff["removed_nodes"] == [] and tt_diff["removed_edges"] == []
    assert tt_diff["changed_nodes"] == {}

    timeline_res = replies[6]["result"]   # the four dated thread births, sorted
    assert timeline_res == {"events": [1000, 1100, 1150, 1200], "min_ms": 1000,
                            "max_ms": 1200, "undated_count": 0}

    at_res = replies[7]["result"]         # the spawn graph as-of 1150 (t3 not yet born)
    assert [n["id"] for n in at_res["nodes"]] == ["g2", "ghost", "t1", "t2", "t4"]

    plan_res = replies[8]["result"]       # dry-run tally survives real-stdio JSON
    assert plan_res["node_count"] == 6 and plan_res["edge_count"] == 5
    assert plan_res["conversation_count"] == 3 and plan_res["est_bytes"] > 0

    run_res = replies[9]["result"]        # a real file is written by the sidecar process
    assert run_res["ok"] is True and run_res["written_path"] == export_dest
    assert run_res["graph_gate"] is True and run_res["transcript_gate"] is True
    assert os.path.isfile(export_dest)


def test_e2e_export_run_negative_gate_blocks_corrupted_corpus(tmp_path, monkeypatch):
    """NEGATIVE e2e: the fidelity gate BLOCKS a corrupted export -> NOTHING is written and
    the result carries a diff/report of what was lost. Two independent failure modes:

      (a) export.run RPC handler surfaces a GRAPH-gate failure. A serializer that silently
          drops an isolated node makes the round-trip oracle report a removed node, so the
          `export.run` reply is ``{ok:false, graph_gate:false, transcript_gate:true}`` with
          NO written_path and no file on disk; the underlying gate report names the removed
          node exactly (the concrete diff report).
      (b) the REAL token-multiset TRANSCRIPT gate (NO monkeypatch) blocks a conversation
          whose rendered HTML dropped a prose word, returning the concrete missing-token
          report and writing nothing.

    Why this lives at the dispatch/gate layer and not over the subprocess wire: a corpus
    LOADED from an index can NEVER trip the graph gate -- serialize<->parse is faithful by
    construction (see test_export.py::test_graph_fidelity_gate_passes_for_faithful_roundtrip),
    and export.run bundles no transcripts -- so corpus content alone cannot block the gate
    across the pipe. The gate code exercised here IS the exact code the subprocess runs."""
    # (a) RPC layer: a lossy serializer -> the graph gate fires, export.run writes nothing.
    srv = _mem_server()
    srv.corpus = corpus.Corpus(
        threads={"root": ThreadMeta(id="root"), "orphan": ThreadMeta(id="orphan")},
        edges=[])
    dest = str(tmp_path / "blocked.json")
    real = sidecar.export.serialize_graph

    def lossy(cc):
        doc = json.loads(real(cc))
        doc["nodes"] = [n for n in doc["nodes"] if n["id"] != "orphan"]
        return json.dumps(doc)

    monkeypatch.setattr(sidecar.export, "serialize_graph", lossy)
    res = srv.dispatch("export.run", {"dest_path": dest})
    assert res["ok"] is False and res["graph_gate"] is False
    assert res["transcript_gate"] is True and "written_path" not in res
    assert not os.path.isfile(dest)
    # the underlying gate report names EXACTLY what the round-trip lost (the diff report)
    report = sidecar.export.export_with_gate(
        srv.corpus, dest, root=os.path.dirname(dest))
    assert report["ok"] is False and report["removed"]["nodes"] == ["orphan"]
    assert not os.path.isfile(dest)

    # (b) gate layer, the REAL verify() (monkeypatch undone): a garbled transcript is
    #     blocked with a missing-token report and nothing is written, even though the
    #     graph itself round-trips faithfully.
    monkeypatch.undo()
    conv = ir.Conversation(id="c-neg", title="t", provider="claude",
                           turns=[ir.Turn("assistant",
                                          [ir.Block("text", "alpha beta gamma")])])
    garbled = render_html.render_conversation_html(conv).replace("beta", "")
    dest2 = str(tmp_path / "blocked2.json")
    rep2 = sidecar.export.export_with_gate(
        corpus.Corpus(threads={"only": ThreadMeta(id="only")}), dest2,
        conversations=[(conv, garbled)], root=os.path.dirname(dest2))
    assert rep2["ok"] is False and rep2["missing_tokens"]["c-neg"] == ["beta"]
    assert rep2["removed"] == {"nodes": [], "edges": []}   # the graph was faithful
    assert not os.path.isfile(dest2)
