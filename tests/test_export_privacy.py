"""export.py — DECISION G-5 (warn, never silently mutate) and G-6 (two graph modes).

SYNTHETIC FIXTURES ONLY. Every thread, path, title, preview and "credential" below is
hand-typed nonsense in the SHAPE of the real thing. No real conversation, path or token
is read or written by this file, and the medical-sounding probe is invented prose.

WHAT THIS FILE PINS

G-5 — the scan WARNS and does not touch the bytes:
  * the report carries the credential-shape findings WITH their location (scope, id,
    field, offset) and ALWAYS carries the coverage-limit sentence, findings or not;
  * `test_a_warning_does_not_block_the_write_and_the_bytes_are_unaltered` is the
    archive-of-record proof: a corpus full of credential shapes still exports, and the
    probe is still in the file VERBATIM. Nothing is altered unless `scrub=True`;
  * `test_the_medical_probe_produces_no_findings_and_the_report_still_warns` is the one
    that matters most: over medical content the scan is silent — and the report still
    states, in words, that it is blind to exactly that.

G-6 — two modes, and the HARD CONSTRAINT that FULL still round-trips:
  * FULL is the default and reconstructs every ThreadMeta field through `parse_graph`,
    asserted against the written artifact (not just in memory);
  * SHAREABLE drops `preview` entirely, relativizes `cwd`/`rollout_path` to `~`, and
    keeps structure + title + repo/branch — and the diff between the two artifacts names
    exactly those three fields and nothing else;
  * the mode is recorded in the artifact, so a shareable file can never be mistaken for
    an archive of record;
  * an unknown mode is refused before anything is written.
"""
import json
import os

import pytest

from llm_anthology import corpus, diff, export, ir, redact, render_html

# --- synthetic credential-shaped probes (nonsense; none of these authenticate) -------
KEY_PROBE = "sk-" + "S" * 40                       # EXAMPLE placeholder, not a real key
AWS_PROBE = "AKIA" + "SYNTHETIC0000000"            # EXAMPLE placeholder, not a real key
HOME = r"C:\Users\someone"
MEDICAL = ("Ich brauche Hilfe: tapering sertraline 50mg, patient Jane Q. Doe, "
           "DOB 1970-01-01, pharmacy account 998877.")


# --------------------------------------------------------------------- fixtures

def _tm(tid, **kw):
    return corpus.ThreadMeta(id=tid, **kw)


def _corpus(threads=(), edges=()):
    c = corpus.Corpus()
    for t in threads:
        c.add_thread(t)
    for e in edges:
        c.add_edge(e)
    return c


def _rich():
    """A node with EVERY field populated, including the two absolute local paths and the
    200-char-excerpt `preview` that G-6 exists to drop."""
    return _corpus(
        threads=[_tm("t1", title="Refactor the parser", model_provider="openai",
                     tokens_used=17, created_at_ms=1000, updated_at_ms=2000,
                     git_branch="feature/x", cwd=HOME + r"\src\repo",
                     agent_role="impl", agent_nickname="brisk-heron",
                     preview=MEDICAL,
                     rollout_path=HOME + r"\.codex\sessions\rollout-1.jsonl",
                     adapter="codex"),
                 _tm("t2", title="Child", created_at_ms=None)],
        edges=[corpus.SpawnEdge("t1", "t2", "completed")])


def _conv(cid, text):
    return ir.Conversation(id=cid, title="t", provider="claude",
                           turns=[ir.Turn("assistant", [ir.Block("text", text=text)])])


def _artifact(path):
    return json.loads(path.read_bytes().decode("utf-8"))


def _graph_of(path):
    return export.parse_graph(_artifact(path)["graph"])


# ==================================================================== G-6: FULL mode

def test_full_is_the_default_mode_and_is_recorded_in_the_artifact():
    assert export.EXPORT_MODES == (export.MODE_FULL, export.MODE_SHAREABLE)
    assert export.MODE_FULL == "full" and export.MODE_SHAREABLE == "shareable"


def test_full_mode_round_trips_every_node_field_through_the_written_artifact(tmp_path):
    """THE HARD CONSTRAINT. `parse_graph` rebuilds a ThreadMeta from the full node field
    set, so FULL mode must keep round-tripping — asserted against the bytes on disk, not
    an in-memory value."""
    src = _rich()
    dest = tmp_path / "full.json"
    report = export.export_with_gate(src, dest, root=tmp_path)   # no mode -> FULL
    assert report["ok"] is True and report["mode"] == export.MODE_FULL

    reparsed = _graph_of(dest)
    assert diff.diff_corpus(src, reparsed).is_empty()            # structurally identical
    node = reparsed.threads["t1"]
    original = src.threads["t1"]
    for field in export._NODE_FIELDS:                            # EVERY field, verbatim
        assert getattr(node, field) == getattr(original, field), field
    assert node.preview == MEDICAL                               # kept: archive of record
    assert node.cwd == HOME + r"\src\repo"                       # absolute path kept
    assert reparsed.threads["t2"].created_at_ms is None          # None stays None
    assert reparsed.edges[0].status == "completed"
    assert _artifact(dest)["llm_anthology_export_mode"] == export.MODE_FULL


def test_full_mode_keeps_the_export_format_version(tmp_path):
    dest = tmp_path / "full.json"
    export.export_with_gate(_rich(), dest, root=tmp_path)
    assert _artifact(dest)["llm_anthology_export_version"] == export.EXPORT_FORMAT_VERSION


# =============================================================== G-6: SHAREABLE mode

def test_shareable_mode_drops_preview_entirely_and_relativizes_both_paths(tmp_path):
    dest = tmp_path / "share.json"
    report = export.export_with_gate(_rich(), dest, root=tmp_path,
                                     mode=export.MODE_SHAREABLE, home=HOME)
    assert report["ok"] is True and report["mode"] == export.MODE_SHAREABLE
    node = _graph_of(dest).threads["t1"]
    assert node.preview == ""                                    # DROPPED, not truncated
    assert node.cwd == "~/src/repo"
    assert node.rollout_path == "~/.codex/sessions/rollout-1.jsonl"
    # nothing from the dropped excerpt, and no OS username, survives anywhere in the file
    raw = dest.read_bytes().decode("utf-8")
    assert "sertraline" not in raw and "Jane" not in raw
    assert "someone" not in raw
    assert _artifact(dest)["llm_anthology_export_mode"] == export.MODE_SHAREABLE


def test_shareable_mode_keeps_structure_titles_and_repo_branch(tmp_path):
    """The owner explicitly accepted keeping titles and repo/branch. Pinned, so weakening
    OR tightening that trade is a visible change rather than a silent one."""
    dest = tmp_path / "share.json"
    export.export_with_gate(_rich(), dest, root=tmp_path,
                            mode=export.MODE_SHAREABLE, home=HOME)
    reparsed = _graph_of(dest)
    node = reparsed.threads["t1"]
    assert node.title == "Refactor the parser"
    assert node.git_branch == "feature/x"
    assert (node.tokens_used, node.created_at_ms, node.updated_at_ms) == (17, 1000, 2000)
    assert (node.model_provider, node.adapter) == ("openai", "codex")
    assert node.agent_role == "impl"
    # The NICKNAME is now dropped — see the CF-18 batch decision recorded in
    # `redact.shareable_thread`. Asserted here as "" rather than deleted from the test,
    # because this test exists to make a change to the trade VISIBLE, and silently removing
    # the field it used to guard would be the exact opposite.
    assert node.agent_nickname == ""
    assert sorted(reparsed.threads) == ["t1", "t2"]               # structure intact
    assert [(e.parent_thread_id, e.child_thread_id) for e in reparsed.edges] == \
        [("t1", "t2")]


def test_shareable_artifact_round_trips_to_exactly_what_was_written(tmp_path):
    """SHAREABLE is deliberately NOT a round-trip of the source (that is the point), but
    it must be a faithful round-trip of the file: re-serializing the re-parse is
    byte-identical, so the artifact is self-consistent and diffable."""
    dest = tmp_path / "share.json"
    report = export.export_with_gate(_rich(), dest, root=tmp_path,
                                     mode=export.MODE_SHAREABLE, home=HOME)
    assert report["added"] == {"nodes": [], "edges": []}          # the graph gate passed
    assert report["removed"] == {"nodes": [], "edges": []} and report["changed"] == {}
    serialized = _artifact(dest)["graph"]
    assert export.serialize_graph(export.parse_graph(serialized)) == serialized


def test_shareable_differs_from_full_in_exactly_the_projected_fields(tmp_path):
    full = tmp_path / "full.json"
    share = tmp_path / "share.json"
    export.export_with_gate(_rich(), full, root=tmp_path)
    export.export_with_gate(_rich(), share, root=tmp_path,
                            mode=export.MODE_SHAREABLE, home=HOME)
    d = diff.diff_corpus(_graph_of(full), _graph_of(share))
    assert d.added_nodes == [] and d.removed_nodes == []
    assert d.added_edges == [] and d.removed_edges == []
    assert set(d.changed_nodes) == {"t1"}
    # FOUR now, not three: `agent_nickname` joined the projection. This by-construction
    # set is the point of the test — a projection that starts touching a fifth field has to
    # come through here, so the count is deliberately spelled out rather than relaxed.
    # `title` and `git_branch` are absent because THIS fixture carries no home path in
    # them; they are scrubbed, and `test_a_TITLE_that_is_a_path...` covers that.
    assert set(d.changed_nodes["t1"]) == {"preview", "cwd", "rollout_path", "agent_nickname"}


def test_shareable_mode_defaults_home_to_the_process_home(tmp_path):
    inside = os.path.join(os.path.expanduser("~"), "src")
    c = _corpus(threads=[_tm("t1", cwd=inside)])
    dest = tmp_path / "share.json"
    export.export_with_gate(c, dest, root=tmp_path, mode=export.MODE_SHAREABLE)
    assert _graph_of(dest).threads["t1"].cwd == "~/src"


def test_an_unknown_mode_is_refused_before_anything_is_written(tmp_path):
    dest = tmp_path / "nope.json"
    with pytest.raises(export.ExportModeError):
        export.export_with_gate(_rich(), dest, root=tmp_path, mode="anonymized")
    assert not dest.exists()


def test_the_input_corpus_is_never_mutated_by_either_mode(tmp_path):
    src = _rich()
    before = (src.threads["t1"].preview, src.threads["t1"].cwd,
              src.threads["t1"].rollout_path)
    export.export_with_gate(src, tmp_path / "a.json", root=tmp_path)
    export.export_with_gate(src, tmp_path / "b.json", root=tmp_path,
                            mode=export.MODE_SHAREABLE, home=HOME)
    assert (src.threads["t1"].preview, src.threads["t1"].cwd,
            src.threads["t1"].rollout_path) == before


# ============================================== G-5: the scan WARNS, it does not mutate

def test_the_report_always_carries_the_coverage_limit_even_with_zero_findings(tmp_path):
    dest = tmp_path / "clean.json"
    report = export.export_with_gate(_corpus(threads=[_tm("t1", title="ordinary")]),
                                     dest, root=tmp_path)
    scan = report["credential_scan"]
    assert scan["findings"] == []
    assert scan["scrubbed"] is False
    assert scan["coverage_limit"] == redact.CREDENTIAL_SHAPE_COVERAGE_LIMIT


def test_the_medical_probe_produces_no_findings_and_the_report_still_warns(tmp_path):
    """THE MOST IMPORTANT TEST IN THIS FILE. On a corpus whose real risk is medical, the
    credential scan is silent — so the report MUST still say, in words, that it detects
    shapes only and is blind to personal and medical content. Otherwise an empty findings
    list reads as 'safe to share' and lowers the reader's guard on the risk that actually
    applies to them."""
    dest = tmp_path / "medical.json"
    report = export.export_with_gate(
        _corpus(threads=[_tm("t1", title=MEDICAL, preview=MEDICAL)]), dest, root=tmp_path)
    scan = report["credential_scan"]
    assert scan["findings"] == []                       # blind, as designed
    limit = scan["coverage_limit"].lower()
    assert "shape" in limit
    assert "personal" in limit and "medical" in limit and "blind" in limit
    assert "does not mean" in limit
    assert report["written"] is True                    # and it still exported


def test_a_credential_shape_in_any_node_or_edge_field_is_reported_with_its_location(
        tmp_path):
    c = _corpus(threads=[_tm("t1", title="key " + KEY_PROBE, preview=AWS_PROBE),
                         _tm("t2", cwd=r"C:\tmp\notes " + AWS_PROBE)],
                edges=[corpus.SpawnEdge("t1", "t2", "failed " + KEY_PROBE)])
    report = export.export_with_gate(c, tmp_path / "g.json", root=tmp_path)
    findings = report["credential_scan"]["findings"]
    assert [(f["scope"], f["id"], f["field"], f["shape"]) for f in findings] == [
        ("edge", "t1->t2", "status", "openai-api-key"),
        ("thread", "t1", "preview", "aws-access-key-id"),
        ("thread", "t1", "title", "openai-api-key"),
        ("thread", "t2", "cwd", "aws-access-key-id"),
    ]
    title_hit = findings[2]
    assert title_hit["offset"] == len("key ")            # the location, not just the file
    assert KEY_PROBE not in title_hit["preview"]         # masked, never the value
    assert title_hit["preview"].startswith(KEY_PROBE[:4])


def test_findings_are_deterministic_across_runs(tmp_path):
    c = _corpus(threads=[_tm("t1", title=KEY_PROBE), _tm("t0", preview=AWS_PROBE)])
    first = export.export_with_gate(c, tmp_path / "1.json", root=tmp_path)
    second = export.export_with_gate(c, tmp_path / "2.json", root=tmp_path)
    assert first["credential_scan"]["findings"] == second["credential_scan"]["findings"]
    assert [f["id"] for f in first["credential_scan"]["findings"]] == ["t0", "t1"]


def test_a_warning_does_not_block_the_write_and_the_bytes_are_unaltered(tmp_path):
    """ARCHIVE-OF-RECORD PROOF, and the core of G-5: a corpus stuffed with credential
    shapes still exports, and the probe is still in the file VERBATIM. The scan reports;
    it does not touch content. Only the fidelity gates block a write."""
    dest = tmp_path / "warned.json"
    c = _corpus(threads=[_tm("t1", title="key " + KEY_PROBE)])
    report = export.export_with_gate(c, dest, root=tmp_path)
    assert report["ok"] is True and report["written"] is True
    assert len(report["credential_scan"]["findings"]) == 1
    assert report["credential_scan"]["scrubbed"] is False
    assert KEY_PROBE in dest.read_bytes().decode("utf-8")   # NOT silently mutated
    assert _graph_of(dest).threads["t1"].title == "key " + KEY_PROBE


def test_a_bundled_conversation_is_scanned_and_located_by_conversation_id(tmp_path):
    conv = _conv("conv-1", "here is the key " + KEY_PROBE + " keep it safe")
    html = render_html.render_conversation_html(conv)
    report = export.export_with_gate(_corpus(threads=[_tm("t1")]), tmp_path / "g.json",
                                     conversations=[(conv, html)], root=tmp_path)
    findings = report["credential_scan"]["findings"]
    assert [(f["scope"], f["id"], f["field"], f["shape"]) for f in findings] == [
        ("conversation", "conv-1", "html", "openai-api-key")]
    assert findings[0]["offset"] > 0                     # a real offset into the stored HTML
    assert report["written"] is True                     # warn, not block


# ------------------------------------------------------------------ scrub is OPT-IN

def test_scrub_opt_in_removes_the_reported_shapes_and_says_so(tmp_path):
    dest = tmp_path / "scrubbed.json"
    c = _corpus(threads=[_tm("t1", title="key " + KEY_PROBE, preview=AWS_PROBE)],
                edges=[corpus.SpawnEdge("t1", "t2", "failed " + AWS_PROBE)])
    report = export.export_with_gate(c, dest, root=tmp_path, scrub=True)
    assert report["ok"] is True and report["written"] is True
    scan = report["credential_scan"]
    assert scan["scrubbed"] is True
    assert len(scan["findings"]) == 3                    # what WAS there is still reported
    raw = dest.read_bytes().decode("utf-8")
    assert KEY_PROBE not in raw and AWS_PROBE not in raw
    node = _graph_of(dest).threads["t1"]
    assert node.title == "key [redacted:openai-api-key]"
    assert node.preview == "[redacted:aws-access-key-id]"
    assert _graph_of(dest).edges[0].status == "failed [redacted:aws-access-key-id]"


def test_scrub_opt_in_scrubs_a_bundled_conversation_on_both_sides_of_the_token_gate(
        tmp_path):
    """The token-multiset gate compares source prose against the stored HTML. With scrub
    on, BOTH sides are scrubbed identically, so a deliberate removal does not masquerade
    as lost content — and the write still happens."""
    conv = _conv("conv-1", "alpha " + KEY_PROBE + " omega")
    html = render_html.render_conversation_html(conv)
    dest = tmp_path / "s.json"
    report = export.export_with_gate(_corpus(threads=[_tm("t1")]), dest,
                                     conversations=[(conv, html)], root=tmp_path,
                                     scrub=True)
    assert report["missing_tokens"] == {}
    assert report["written"] is True
    stored = _artifact(dest)["conversations"][0]["html"]
    assert KEY_PROBE not in stored
    assert "alpha" in stored and "omega" in stored       # real prose untouched


def test_scrub_does_not_blind_the_token_gate_to_real_loss(tmp_path):
    """BOTH STATES for the gate itself: with scrub ON, an UNRELATED dropped prose word is
    still caught, so scrubbing cannot be used to smuggle content loss past the gate."""
    conv = _conv("conv-1", "alpha beta " + KEY_PROBE)
    garbled = render_html.render_conversation_html(conv).replace("beta", "")
    dest = tmp_path / "s.json"
    report = export.export_with_gate(_corpus(threads=[_tm("t1")]), dest,
                                     conversations=[(conv, garbled)], root=tmp_path,
                                     scrub=True)
    assert report["missing_tokens"]["conv-1"] == ["beta"]
    assert report["ok"] is False and not dest.exists()


def test_scrub_does_not_mutate_the_input_corpus_or_conversation(tmp_path):
    c = _corpus(threads=[_tm("t1", title="key " + KEY_PROBE)],
                edges=[corpus.SpawnEdge("t1", "t2", "s " + AWS_PROBE)])
    conv = _conv("conv-1", "body " + KEY_PROBE)
    html = render_html.render_conversation_html(conv)
    export.export_with_gate(c, tmp_path / "s.json", conversations=[(conv, html)],
                            root=tmp_path, scrub=True)
    assert c.threads["t1"].title == "key " + KEY_PROBE
    assert c.edges[0].status == "s " + AWS_PROBE
    assert conv.turns[0].blocks[0].text == "body " + KEY_PROBE


# ----------------------------------------------- the scan describes the ARTIFACT written

def test_the_scan_describes_the_artifact_not_the_source_corpus(tmp_path):
    """A credential living ONLY in `preview` is reported in FULL mode (it is in the file)
    and NOT in SHAREABLE mode (preview never reaches the file). The findings answer "what
    is in the thing you are about to hand over", which is the only useful question."""
    c = _corpus(threads=[_tm("t1", preview="leak " + KEY_PROBE)])
    full = export.export_with_gate(c, tmp_path / "f.json", root=tmp_path)
    share = export.export_with_gate(c, tmp_path / "s.json", root=tmp_path,
                                    mode=export.MODE_SHAREABLE, home=HOME)
    assert [f["field"] for f in full["credential_scan"]["findings"]] == ["preview"]
    assert share["credential_scan"]["findings"] == []
    assert KEY_PROBE not in (tmp_path / "s.json").read_bytes().decode("utf-8")


def test_a_key_smuggled_apart_by_hidden_unicode_is_still_found(tmp_path):
    """ORDER IS LOAD-BEARING: sanitize FIRST, then scan. A zero-width char inside a key
    breaks the shape match on the raw text, so a scanner that ran before the
    hidden-unicode strip would report the corpus clean while writing the working key into
    the artifact."""
    smuggled = "sk-" + "\u200b" + "S" * 40      # a zero-width space splits the shape
    assert redact.scan_credential_shapes(smuggled) == []       # invisible to a raw scan
    report = export.export_with_gate(_corpus(threads=[_tm("t1", title=smuggled)]),
                                     tmp_path / "g.json", root=tmp_path)
    findings = report["credential_scan"]["findings"]
    assert [f["shape"] for f in findings] == ["openai-api-key"]


# ------------------------------------------- the dry-run preview shares one definition

def test_scan_for_export_matches_the_real_run_and_never_writes(tmp_path):
    """`scan_for_export` is the pre-write warning (the only moment the user can still
    change their mind). It must agree with the report the write produces, so both are
    computed from the same projection by the same code."""
    c = _corpus(threads=[_tm("t1", title="key " + KEY_PROBE, preview=AWS_PROBE)])
    dry = export.scan_for_export(c)
    dest = tmp_path / "g.json"
    wet = export.export_with_gate(c, dest, root=tmp_path)["credential_scan"]
    assert dry == wet
    assert dry["scrubbed"] is False
    assert dry["coverage_limit"] == redact.CREDENTIAL_SHAPE_COVERAGE_LIMIT

    share = export.scan_for_export(c, mode=export.MODE_SHAREABLE, home=HOME)
    assert [f["field"] for f in share["findings"]] == ["title"]   # preview never written


def test_export_graph_is_the_single_definition_of_what_a_mode_writes(tmp_path):
    dest = tmp_path / "share.json"
    src = _rich()
    export.export_with_gate(src, dest, root=tmp_path, mode=export.MODE_SHAREABLE,
                            home=HOME)
    projected = export.export_graph(src, mode=export.MODE_SHAREABLE, home=HOME)
    assert export.serialize_graph(projected) == _artifact(dest)["graph"]
    assert export.export_graph(src).threads["t1"].preview == MEDICAL   # FULL keeps it


def test_export_graph_refuses_an_unknown_mode():
    with pytest.raises(export.ExportModeError):
        export.export_graph(_rich(), mode="anonymized")


def test_shareable_and_scrub_compose(tmp_path):
    dest = tmp_path / "both.json"
    c = _corpus(threads=[_tm("t1", title="key " + KEY_PROBE, preview=MEDICAL,
                             cwd=HOME + r"\src")])
    report = export.export_with_gate(c, dest, root=tmp_path,
                                     mode=export.MODE_SHAREABLE, home=HOME, scrub=True)
    assert report["written"] is True and report["mode"] == export.MODE_SHAREABLE
    assert report["credential_scan"]["scrubbed"] is True
    node = _graph_of(dest).threads["t1"]
    assert node.preview == "" and node.cwd == "~/src"
    assert node.title == "key [redacted:openai-api-key]"
