"""export.py — fidelity-gated export core.

SYNTHETIC fixtures ONLY. Nothing here is a real conversation, thread id, path, or
token count; every corpus is a made-up shape that mirrors the spawn-graph contract
(ThreadMeta nodes + SpawnEdge edges), and every conversation is invented prose —
never the private real corpus content.

What is pinned:
  * serialize_graph / parse_graph are a deterministic, order-stable round-trip
    codec (nodes sorted by id, edges by (parent, child), every field preserved
    including Optional None and the edge status);
  * graph_fidelity_gate is the round-trip ORACLE built on diff_corpus — an empty
    diff means faithful, a non-empty diff names exactly what the round-trip lost;
  * export_with_gate sanitizes every free-text field, runs BOTH the graph gate and
    the per-conversation token-multiset gate, and writes the artifact ONLY when both
    pass; on failure it writes NOTHING and returns a structured
    {added, removed, changed, missing_tokens} report;
  * dest_path is drive-absolute-local-only: UNC/non-local, parent-traversal, and
    out-of-root destinations are rejected (the Windows SMB hash-leak class);
  * the MUTATION tests prove BOTH STATES: the gate is silent on a faithful export
    and BLOCKS (no write) a dropped node, a dropped edge, and a garbled token.

Path semantics are asserted with pure PureWindowsPath rules so the suite is
byte-identical on Linux/macOS/Windows CI; the real file write uses the host-native
tmp_path, so the write path is exercised on every platform.
"""
import json

import pytest

from llm_anthology import corpus, diff, export, ir, render_html, sanitize


# --------------------------------------------------------------------- fixtures

def _tm(tid, **kw):
    return corpus.ThreadMeta(id=tid, **kw)


def _edge(parent, child, status="completed"):
    return corpus.SpawnEdge(parent, child, status)


def _corpus(threads=(), edges=()):
    c = corpus.Corpus()
    for t in threads:
        c.add_thread(t)
    for e in edges:
        c.add_edge(e)
    return c


def _conv(cid, text):
    return ir.Conversation(id=cid, title="t", provider="claude",
                           turns=[ir.Turn("assistant", [ir.Block("text", text=text)])])


def _rendered(conv):
    return render_html.render_conversation_html(conv)


# ------------------------------------------------------- serialize / parse codec

def test_serialize_parse_roundtrip_preserves_every_field():
    """A full corpus (all ThreadMeta fields, an Optional None, an edge status)
    round-trips to a structurally identical corpus."""
    c = _corpus(threads=[
        _tm("b", title="B", model_provider="codex", tokens_used=42,
            created_at_ms=1000, updated_at_ms=2000, git_branch="main",
            cwd="/work", agent_role="impl", agent_nickname="nick",
            preview="prev", rollout_path="/r/b.jsonl"),
        _tm("a", created_at_ms=None, updated_at_ms=None),
    ], edges=[_edge("a", "b", status="failed")])
    reparsed = export.parse_graph(export.serialize_graph(c))
    assert diff.diff_corpus(c, reparsed).is_empty()
    assert reparsed.threads["b"].tokens_used == 42          # int preserved as int
    assert reparsed.threads["a"].created_at_ms is None       # None preserved (not 0)
    assert reparsed.edges[0].status == "failed"              # edge status preserved


def test_serialize_is_deterministic_and_sorted():
    c = _corpus(threads=[_tm("c"), _tm("a"), _tm("b")],
                edges=[_edge("b", "z"), _edge("a", "y")])
    s1 = export.serialize_graph(c)
    s2 = export.serialize_graph(c)
    assert s1 == s2                                          # byte-stable across runs
    doc = json.loads(s1)
    assert [n["id"] for n in doc["nodes"]] == ["a", "b", "c"]
    assert [(e["parent_thread_id"], e["child_thread_id"]) for e in doc["edges"]] == \
        [("a", "y"), ("b", "z")]


def test_empty_corpus_roundtrips():
    c = corpus.Corpus()
    reparsed = export.parse_graph(export.serialize_graph(c))
    assert reparsed.threads == {} and reparsed.edges == []
    assert diff.diff_corpus(c, reparsed).is_empty()


def test_dangling_edge_endpoint_roundtrips():
    """An edge names a child with no ThreadMeta -> still a graph node; the edge
    (and therefore the node) must survive the round-trip."""
    c = _corpus(threads=[_tm("parent")], edges=[_edge("parent", "ghost")])
    reparsed = export.parse_graph(export.serialize_graph(c))
    assert diff.diff_corpus(c, reparsed).is_empty()
    assert "ghost" not in reparsed.threads
    assert (reparsed.edges[0].parent_thread_id,
            reparsed.edges[0].child_thread_id) == ("parent", "ghost")


# ------------------------------------------------------------ graph fidelity gate

def test_graph_fidelity_gate_passes_for_faithful_roundtrip():
    c = _corpus(threads=[_tm("a"), _tm("b")], edges=[_edge("a", "b")])
    assert export.graph_fidelity_gate(c).is_empty() is True


def test_graph_fidelity_gate_detects_a_dropped_node(monkeypatch):
    """BROKEN STATE: a serializer that silently drops an isolated node -> the diff
    oracle reports it as a removed node (the gate fires)."""
    c = _corpus(threads=[_tm("keep"), _tm("orphan")])
    real = export.serialize_graph

    def lossy(cc):
        doc = json.loads(real(cc))
        doc["nodes"] = [n for n in doc["nodes"] if n["id"] != "orphan"]
        return json.dumps(doc)

    monkeypatch.setattr(export, "serialize_graph", lossy)
    d = export.graph_fidelity_gate(c)
    assert d.is_empty() is False
    assert d.removed_nodes == ["orphan"]


# ------------------------------------------------------------- export: happy path

def test_export_writes_when_both_gates_pass(tmp_path):
    c = _corpus(threads=[_tm("root", title="Root", tokens_used=5),
                         _tm("child", title="Child")],
                edges=[_edge("root", "child")])
    conv_a = _conv("conv-a", "hello world")
    conv_b = _conv("conv-b", "second conversation body")
    convs = [(conv_b, _rendered(conv_b)), (conv_a, _rendered(conv_a))]  # unsorted input
    dest = tmp_path / "sub" / "graph.json"                             # nested -> mkdir
    report = export.export_with_gate(c, dest, conversations=convs, root=tmp_path)

    assert report["ok"] is True and report["written"] is True
    assert report["path"] == str(dest)
    assert report["missing_tokens"] == {}
    assert report["added"] == {"nodes": [], "edges": []}
    assert report["removed"] == {"nodes": [], "edges": []}
    assert report["changed"] == {}
    assert dest.exists()

    doc = json.loads(dest.read_bytes().decode("utf-8"))
    assert doc["llm_anthology_export_version"] == export.EXPORT_FORMAT_VERSION
    # conversations are stored sorted by id and carry their prose
    assert [cc["id"] for cc in doc["conversations"]] == ["conv-a", "conv-b"]
    assert "hello" in doc["conversations"][0]["html"]
    assert "world" in doc["conversations"][0]["html"]
    # the graph reconstructs faithfully from the stored artifact
    reparsed = export.parse_graph(doc["graph"])
    assert sorted(reparsed.threads) == ["child", "root"]
    assert reparsed.threads["root"].title == "Root"
    assert [(e.parent_thread_id, e.child_thread_id) for e in reparsed.edges] == \
        [("root", "child")]


def test_export_writes_graph_only_when_no_conversations(tmp_path):
    c = _corpus(threads=[_tm("solo")])
    dest = tmp_path / "graph.json"
    report = export.export_with_gate(c, dest, root=tmp_path)
    assert report["ok"] is True and report["written"] is True
    doc = json.loads(dest.read_bytes().decode("utf-8"))
    assert doc["conversations"] == []


def test_export_defaults_root_to_cwd(tmp_path, monkeypatch):
    """With no explicit root the export is confined to the current working dir."""
    monkeypatch.chdir(tmp_path)
    c = _corpus(threads=[_tm("x")])
    dest = tmp_path / "out.json"
    report = export.export_with_gate(c, dest)          # root defaults to cwd
    assert report["written"] is True
    assert (tmp_path / "out.json").exists()


# ------------------------------------------------------------- export: path safety

def test_export_rejects_unc_destination(tmp_path):
    with pytest.raises(export.ExportPathError):
        export.export_with_gate(_corpus(threads=[_tm("x")]),
                                r"\\evil\share\out.json", root=tmp_path)


def test_export_rejects_unc_root(tmp_path):
    with pytest.raises(export.ExportPathError):
        export.export_with_gate(_corpus(threads=[_tm("x")]),
                                tmp_path / "out.json", root=r"\\evil\share")


def test_export_unc_guard_rejects_a_unc_dest_confined_to_a_unc_root():
    """ISOLATES the UNC guard (mutation-proof). The two tests above trip the UNC guard
    only incidentally — with the guard stubbed to `if False:` they STILL raise, caught by
    the `is_relative_to` confinement check (a UNC dest is not relative to a local root, and
    a local dest is not relative to a UNC root). So confinement, not the guard, is what
    makes them green, and the guard is untested.

    Here the destination `\\\\server\\share\\out.json` IS lexically within the (UNC) root
    `\\\\server\\share`: it carries no `..` and is_relative_to(root) is True, so BOTH
    confinement checks PASS. Only the UNC guard in `_norm_local` can reject it. `match="UNC"`
    pins that it is the guard (its message names UNC), not confinement, that fires — so
    stubbing the guard to `if False:` makes this test go RED. Guards the Windows SMB/NTLM
    hash-leak class (a crafted `\\\\host\\share` coerces an outbound authentication)."""
    with pytest.raises(export.ExportPathError, match="UNC"):
        export.export_with_gate(_corpus(threads=[_tm("x")]),
                                r"\\server\share\out.json", root=r"\\server\share")


def test_export_rejects_parent_traversal(tmp_path):
    dest = str(tmp_path) + "/../escape.json"           # normalizes to a `..` component
    with pytest.raises(export.ExportPathError):
        export.export_with_gate(_corpus(threads=[_tm("x")]), dest, root=tmp_path)


def test_export_rejects_destination_outside_root(tmp_path):
    outside = tmp_path.parent / "outside.json"         # a sibling of the root
    with pytest.raises(export.ExportPathError):
        export.export_with_gate(_corpus(threads=[_tm("x")]), outside, root=tmp_path)


# --------------------------------------------------- export: gate BLOCKS (no write)

def test_export_gate_blocks_a_dropped_node(tmp_path, monkeypatch):
    """BROKEN STATE (graph): a dropped isolated node -> no write + a report that
    names the removed node."""
    c = _corpus(threads=[_tm("root"), _tm("child"), _tm("orphan")],
                edges=[_edge("root", "child")])
    dest = tmp_path / "graph.json"
    real = export.serialize_graph

    def lossy(cc):
        doc = json.loads(real(cc))
        doc["nodes"] = [n for n in doc["nodes"] if n["id"] != "orphan"]
        return json.dumps(doc)

    monkeypatch.setattr(export, "serialize_graph", lossy)
    report = export.export_with_gate(c, dest, root=tmp_path)
    assert report["ok"] is False and report["written"] is False
    assert report["path"] is None
    assert report["removed"]["nodes"] == ["orphan"]
    assert not dest.exists()                           # NOTHING written on failure


def test_export_gate_blocks_a_dropped_edge(tmp_path, monkeypatch):
    """BROKEN STATE (graph): a dropped edge -> no write + a report naming the
    removed edge (the endpoints survive as nodes, so ONLY the edge is flagged)."""
    c = _corpus(threads=[_tm("root"), _tm("child")], edges=[_edge("root", "child")])
    dest = tmp_path / "graph.json"
    real = export.serialize_graph

    def lossy(cc):
        doc = json.loads(real(cc))
        doc["edges"] = []
        return json.dumps(doc)

    monkeypatch.setattr(export, "serialize_graph", lossy)
    report = export.export_with_gate(c, dest, root=tmp_path)
    assert report["ok"] is False and report["written"] is False
    assert [tuple(e) for e in report["removed"]["edges"]] == [("root", "child")]
    assert not dest.exists()


def test_export_gate_blocks_a_garbled_conversation_token(tmp_path):
    """BROKEN STATE (conversation): a rendered HTML with a prose word dropped ->
    the token-multiset gate fires, so no write, even though the graph is faithful."""
    c = _corpus(threads=[_tm("root")])
    conv = _conv("conv-1", "alpha beta gamma")
    garbled = _rendered(conv).replace("beta", "")       # drop a prose word
    dest = tmp_path / "graph.json"
    report = export.export_with_gate(c, dest, conversations=[(conv, garbled)],
                                     root=tmp_path)
    assert report["ok"] is False and report["written"] is False
    assert report["missing_tokens"]["conv-1"] == ["beta"]
    assert report["removed"] == {"nodes": [], "edges": []}   # graph was faithful
    assert not dest.exists()


# ------------------------------------------------------- export: sanitize + purity

def test_export_sanitizes_free_text_fields_in_the_artifact(tmp_path):
    """A zero-width space smuggled into any free-text field is stripped from the
    written artifact (and the gate still passes, since both sides are sanitized)."""
    zwsp = "\u200b"
    c = _corpus(threads=[_tm("a", title="hel" + zwsp + "lo", cwd="c" + zwsp + "wd")],
                edges=[_edge("a", "b", status="ok" + zwsp)])
    dest = tmp_path / "g.json"
    report = export.export_with_gate(c, dest, root=tmp_path)
    assert report["written"] is True
    doc = json.loads(dest.read_bytes().decode("utf-8"))
    assert zwsp not in doc["graph"]
    reparsed = export.parse_graph(doc["graph"])
    assert reparsed.threads["a"].title == "hello"
    assert reparsed.threads["a"].cwd == "cwd"
    assert reparsed.edges[0].status == "ok"
    # the free-text sanitizer is exactly llm_anthology.sanitize.sanitize_for_copy
    assert sanitize.sanitize_for_copy("hel" + zwsp + "lo") == "hello"


def test_export_does_not_mutate_the_input_corpus(tmp_path):
    c = _corpus(threads=[_tm("a", title="orig")], edges=[_edge("a", "b")])
    before_threads = dict(c.threads)
    before_edges = list(c.edges)
    export.export_with_gate(c, tmp_path / "g.json", root=tmp_path)
    assert c.threads == before_threads and c.edges == before_edges
    assert c.threads["a"].title == "orig"                # sanitize built a NEW corpus
