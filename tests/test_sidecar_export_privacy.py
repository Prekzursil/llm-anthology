"""sidecar.py — the export RPC surface for DECISIONS G-5 and G-6.

SYNTHETIC FIXTURES ONLY. The corpus is a hand-built three-node graph; the "credentials"
are hand-typed nonsense in the shape of real ones; no live store, no real path and no real
conversation is read. `cwd` / `rollout_path` are built from `os.path.expanduser("~")` so
the `~`-relativization assertions hold identically on Windows, Linux and macOS.

WHAT THIS FILE PINS

  * `export.plan` is the PRE-WRITE warning surface — the only moment the user can still
    pick a different mode or opt into a scrub. It carries the credential-shape findings
    and the coverage-limit sentence, and its `est_bytes` describes the bytes THAT MODE
    would write (so a shareable preview is not quoted at full size);
  * `export.run` echoes the mode, carries the same warning block, and still WRITES when
    findings exist — warn, never block, never silently mutate;
  * `mode` / `scrub` are validated at the wire (-32602), and a rejected call writes
    nothing;
  * `scrub` is opt-in and the default run leaves the bytes alone.
"""
import json
import os
import sqlite3

import pytest

from llm_anthology import corpus, export, redact, sidecar

KEY_PROBE = "sk-" + "S" * 40                       # EXAMPLE placeholder, not a real key
HOME = os.path.expanduser("~")
MEDICAL = "tapering sertraline 50mg for Jane Q. Doe, DOB 1970-01-01"


def _server(threads=(), edges=()):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    corpus.init_index(conn)
    srv = sidecar.Sidecar(conn)
    c = corpus.Corpus()
    for t in threads:
        c.add_thread(t)
    for e in edges:
        c.add_edge(e)
    srv.corpus = c
    return srv


def _rich_server():
    return _server(
        threads=[corpus.ThreadMeta(
            id="t1", title="Refactor the parser", git_branch="feature/x",
            cwd=os.path.join(HOME, "src", "repo"), preview=MEDICAL,
            rollout_path=os.path.join(HOME, ".codex", "sessions", "r.jsonl")),
            corpus.ThreadMeta(id="t2", title="key " + KEY_PROBE)],
        edges=[corpus.SpawnEdge("t1", "t2", "completed")])


def _artifact(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------ export.plan: the PRE-write warning

def test_export_plan_carries_the_credential_warning_and_the_coverage_limit():
    plan = _rich_server().dispatch("export.plan", {})
    assert plan["mode"] == export.MODE_FULL
    scan = plan["credential_scan"]
    assert [(f["scope"], f["id"], f["field"], f["shape"]) for f in scan["findings"]] == [
        ("thread", "t2", "title", "openai-api-key")]
    assert scan["scrubbed"] is False                     # a dry run changes nothing
    assert scan["coverage_limit"] == redact.CREDENTIAL_SHAPE_COVERAGE_LIMIT
    # the pre-existing tally is untouched
    assert plan["node_count"] == 2 and plan["edge_count"] == 1
    assert plan["conversation_count"] == 0


def test_export_plan_warning_states_its_blindness_on_a_medical_corpus():
    """The corpus this ships against holds private medical conversations. The dry run
    finds nothing in them — and must still say, in words, that it is blind to exactly
    that, or an empty findings list reads as clearance to share."""
    plan = _server(threads=[corpus.ThreadMeta(id="t1", title=MEDICAL, preview=MEDICAL)]
                   ).dispatch("export.plan", {})
    assert plan["credential_scan"]["findings"] == []
    limit = plan["credential_scan"]["coverage_limit"].lower()
    assert "shape" in limit and "blind" in limit
    assert "personal" in limit and "medical" in limit and "does not mean" in limit


def test_export_plan_estimates_the_bytes_of_the_mode_it_was_asked_about():
    srv = _rich_server()
    full = srv.dispatch("export.plan", {})
    share = srv.dispatch("export.plan", {"mode": export.MODE_SHAREABLE})
    assert full["est_bytes"] == len(
        export.serialize_graph(export.export_graph(srv.corpus)).encode("utf-8"))
    assert share["est_bytes"] == len(export.serialize_graph(
        export.export_graph(srv.corpus, mode=export.MODE_SHAREABLE)).encode("utf-8"))
    # dropping the preview excerpt makes the shareable artifact strictly smaller
    assert share["est_bytes"] < full["est_bytes"]
    assert share["mode"] == export.MODE_SHAREABLE


def test_export_plan_rejects_a_bad_mode():
    """FAIL CLOSED, including on `""` and `null`. `_opt_str`'s "empty means unspecified"
    convention is deliberately NOT used for a privacy mode: a client bug that sent an empty
    string would otherwise be handed archive-of-record bytes while its user believed they
    had picked shareable."""
    srv = _rich_server()
    for bad in ("anonymized", "", 7, None):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch("export.plan", {"mode": bad})
        assert ei.value.code == -32602


# ----------------------------------------------------------------- export.run: G-5 warn

def test_export_run_reports_the_findings_and_still_writes(tmp_path):
    """WARN, NOT BLOCK, and NOT A MUTATION: the probe is reported AND is still in the file
    verbatim. Only the fidelity gates stop a write."""
    dest = str(tmp_path / "warned.json")
    res = _rich_server().dispatch("export.run", {"dest_path": dest})
    assert res["ok"] is True and res["written_path"] == dest
    assert res["mode"] == export.MODE_FULL
    assert res["graph_gate"] is True and res["transcript_gate"] is True
    scan = res["credential_scan"]
    assert [f["field"] for f in scan["findings"]] == ["title"]
    assert scan["scrubbed"] is False
    assert scan["coverage_limit"] == redact.CREDENTIAL_SHAPE_COVERAGE_LIMIT
    with open(dest, encoding="utf-8") as fh:
        assert KEY_PROBE in fh.read()


def test_export_run_default_mode_keeps_the_preview_and_the_absolute_paths(tmp_path):
    dest = str(tmp_path / "full.json")
    res = _rich_server().dispatch("export.run", {"dest_path": dest})
    assert res["mode"] == export.MODE_FULL
    node = export.parse_graph(_artifact(dest)["graph"]).threads["t1"]
    assert node.preview == MEDICAL                       # archive of record
    assert node.cwd == os.path.join(HOME, "src", "repo")
    assert _artifact(dest)["llm_anthology_export_mode"] == export.MODE_FULL


# ------------------------------------------------------------- export.run: G-6 shareable

def test_export_run_shareable_drops_preview_and_relativizes_paths_on_disk(tmp_path):
    dest = str(tmp_path / "share.json")
    res = _rich_server().dispatch(
        "export.run", {"dest_path": dest, "mode": export.MODE_SHAREABLE})
    assert res["ok"] is True and res["mode"] == export.MODE_SHAREABLE
    doc = _artifact(dest)
    assert doc["llm_anthology_export_mode"] == export.MODE_SHAREABLE
    node = export.parse_graph(doc["graph"]).threads["t1"]
    assert node.preview == ""
    assert node.cwd == "~/src/repo"
    assert node.rollout_path == "~/.codex/sessions/r.jsonl"
    assert node.title == "Refactor the parser" and node.git_branch == "feature/x"
    with open(dest, encoding="utf-8") as fh:
        raw = fh.read()
    assert "sertraline" not in raw and "Jane" not in raw


def test_export_run_rejects_a_bad_mode_and_writes_nothing(tmp_path):
    dest = str(tmp_path / "nope.json")
    srv = _rich_server()
    for bad in ("anonymized", ""):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch("export.run", {"dest_path": dest, "mode": bad})
        assert ei.value.code == -32602
    assert not os.path.exists(dest)


# ---------------------------------------------------------------- export.run: G-5 scrub

def test_export_run_scrub_is_opt_in_and_reports_what_it_removed(tmp_path):
    dest = str(tmp_path / "scrubbed.json")
    res = _rich_server().dispatch(
        "export.run", {"dest_path": dest, "scrub": True})
    assert res["ok"] is True
    assert res["credential_scan"]["scrubbed"] is True
    assert len(res["credential_scan"]["findings"]) == 1   # still reported, not hidden
    with open(dest, encoding="utf-8") as fh:
        raw = fh.read()
    assert KEY_PROBE not in raw
    assert export.parse_graph(_artifact(dest)["graph"]).threads["t2"].title == \
        "key [redacted:openai-api-key]"


def test_export_run_rejects_a_non_boolean_scrub(tmp_path):
    dest = str(tmp_path / "x.json")
    srv = _rich_server()
    for bad in ("true", 1, None):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch("export.run", {"dest_path": dest, "scrub": bad})
        assert ei.value.code == -32602
    assert not os.path.exists(dest)


# --------------------------------------------------------------------- wire hygiene

def test_the_wire_projection_sanitizes_the_warning_block():
    """DEFENCE IN DEPTH. The export already strips hidden unicode before it scans, so a
    finding cannot carry an invisible today — this asserts the wire projection does not
    DEPEND on that, by handing it a report that does."""
    srv = _rich_server()
    zw = "\u200b"
    projected = srv._project_export_run({
        "ok": False, "written": False, "path": None, "mode": export.MODE_FULL,
        "added": {"nodes": [], "edges": []}, "removed": {"nodes": [], "edges": []},
        "changed": {}, "missing_tokens": {},
        "credential_scan": {"findings": [{"scope": "thread", "id": "t" + zw,
                                          "field": "title", "shape": "jwt",
                                          "offset": 3, "preview": "eyJ" + zw}],
                            "coverage_limit": "limit" + zw, "scrubbed": False},
    })
    scan = projected["credential_scan"]
    assert zw not in json.dumps(scan)
    assert scan["findings"][0]["id"] == "t" and scan["findings"][0]["preview"] == "eyJ"
    assert scan["findings"][0]["offset"] == 3            # ints survive as ints
    assert scan["coverage_limit"] == "limit"
