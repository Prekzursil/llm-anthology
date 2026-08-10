"""Fidelity-gated export core.

The cockpit can render and diff the corpus in memory; this module is the last mile
that turns a Corpus into a durable, shareable ARTIFACT on disk — but only after two
independent fidelity gates agree that nothing was lost. Losing content silently is
the failure mode a "faithful renderer" exists to prevent, so the export refuses to
write unless it can prove faithfulness first.

Four public pieces:

  * serialize_graph(corpus) -> str
        A deterministic, order-stable JSON codec for the spawn graph: nodes sorted
        by id, edges by (parent, child), every ThreadMeta / SpawnEdge field carried
        verbatim (ints stay ints, Optional None stays None, the edge status stays).
        Two runs over the same corpus are byte-identical, so an artifact is diffable
        and reproducible.

  * parse_graph(str) -> Corpus
        The exact inverse: rebuilds the thread graph. A node that exists only as an
        edge endpoint (a dangling parent/child with no ThreadMeta) is reconstructed
        implicitly by its edge, mirroring Corpus._nodes().

  * graph_fidelity_gate(corpus) -> CorpusDiff
        The round-trip ORACLE. It serializes, re-parses, and returns
        diff_corpus(corpus, reparsed) — the diff primitive IS the oracle. An empty
        diff (`.is_empty()`) means the serialization round-trips faithfully; a
        non-empty diff names exactly what the round-trip added / removed / changed.

  * export_with_gate(corpus, dest_path, conversations=None, *, root=None,
                     mode="full", scrub=False, home=None) -> dict
        Sanitizes every free-text field (llm_anthology.sanitize.sanitize_for_copy), projects
        the graph for `mode`, SCANS for credential shapes, runs the graph gate AND a
        per-conversation token-multiset gate (llm_anthology.verify.verify on the sanitized
        HTML that is actually stored), and writes the artifact ONLY if BOTH pass. On
        failure it writes NOTHING and returns a structured
        {added, removed, changed, missing_tokens, credential_scan} report. `dest_path` is
        drive-absolute-local-only: UNC / non-local paths, parent-traversal, and any
        destination outside `root` are rejected (the Windows SMB/NTLM hash-leak
        class — a crafted `\\\\host\\share` coerces an outbound authentication).

THE TWO PRIVACY DECISIONS THIS MODULE IMPLEMENTS (owner-locked safety calls):

  G-5 — WARN BY DEFAULT, SCRUB OPT-IN, NEVER SILENTLY MUTATE. Every export scans the
  text it is about to write for credential SHAPES and returns each hit WITH its location
  (scope / id / field / offset, plus a masked preview — the matched run itself never
  travels). It changes NOTHING unless the caller passes `scrub=True`, because the
  archive-of-record principle applies to the export path too.
      The load-bearing part is the coverage statement the report always carries, findings
  or not: the scan detects credential shapes ONLY and is BLIND to personal and medical
  content. A "no findings" verdict over a corpus of private medical conversations would be
  factually correct and dangerously misleading, because it lowers the reader's guard on
  the risk that actually applies to them. It is a credential-shape matcher and is
  deliberately not described as anything wider, here or in the UI.

  G-6 — TWO GRAPH MODES, chosen by the user, never inferred. FULL is every field
  unchanged and round-trippable. SHAREABLE drops `preview` ENTIRELY (all three adapters
  populate it as `first_user[:200]`, i.e. a verbatim excerpt of every conversation's
  opening user message), relativizes `cwd` and `rollout_path` to `~` (they are absolute
  paths carrying the OS username), and keeps structure + titles + repo/branch — the
  owner's explicitly accepted residual. The mode is recorded in the artifact so a
  shareable file cannot later be mistaken for an archive of record. Because the user picks
  the mode, G-5's never-silently-mutate rule still holds.
      The projection runs BEFORE the fidelity gate, so the gate proves the artifact
  reconstructs exactly what was written; FULL's round-trip to the ORIGINAL corpus is
  asserted separately (tests/test_export_privacy.py), since `parse_graph` rebuilds a
  ThreadMeta from the whole _NODE_FIELDS set and that must keep working.
      Scope note, not a caveat: `mode` projects the graph METADATA. A bundled transcript
  is the body itself and is not abridged by mode — it is subject only to hidden-unicode
  sanitization and, if asked for, the credential scrub.

Known limits of the diff oracle (honest, and inherited from diff_corpus's contract):
  * A node whose ThreadMeta is dropped while an edge to it SURVIVES is not flagged —
    it remains a graph node via the edge, and changed_nodes only covers metadata
    present on BOTH sides. Only an isolated node's removal, or an edge removal, is
    caught structurally.
  * An edge STATUS-only corruption is invisible (edge identity is the (parent, child)
    pair; diff carries no changed_edges).
  A byte-level `serialize(reparsed) == serialized` idempotence assertion would close
  both, and is the settling experiment if those cases ever need policing; this module
  stays faithful to the WU contract that the diff primitive is the oracle.

PRIVACY: synthetic fixtures only in tests; this module never reads the real corpus.
Determinism: outputs are sorted by stable id and written as UTF-8 bytes (no newline
translation), so an artifact is byte-identical on Linux / macOS / Windows.
"""
import json
import os
from dataclasses import fields as dc_fields, replace as dc_replace
from pathlib import Path, PureWindowsPath

from . import redact
from .corpus import Corpus, ThreadMeta, SpawnEdge
from .diff import diff_corpus
from .sanitize import sanitize_for_copy
from .verify import verify

EXPORT_FORMAT_VERSION = 1

#: G-6 export modes. FULL is the archive of record — every field, unchanged,
#: round-trippable. SHAREABLE is the hand-it-to-someone-else projection.
MODE_FULL = "full"
MODE_SHAREABLE = "shareable"
EXPORT_MODES = (MODE_FULL, MODE_SHAREABLE)

# Field orders are derived from the dataclasses so serialization can never drift out
# of sync with the model as ThreadMeta / SpawnEdge evolve.
_NODE_FIELDS = tuple(f.name for f in dc_fields(ThreadMeta))
_EDGE_FIELDS = tuple(f.name for f in dc_fields(SpawnEdge))

# The genuinely free-text (human/agent-authored) node fields. `id` is a structural
# key (sanitizing it would silently re-identify the node and break the graph), and
# the numeric fields are not text — so neither is sanitized. The edge `status` is the
# only free-text edge field; the parent/child ids are structural keys.
_TEXT_NODE_FIELDS = ("title", "model_provider", "git_branch", "cwd",
                     "agent_role", "agent_nickname", "preview", "rollout_path")


class ExportPathError(ValueError):
    """A requested export destination is unsafe: UNC / non-local, a parent-traversal,
    or outside the chosen export root (the Windows SMB hash-leak class)."""


class ExportModeError(ValueError):
    """An unrecognized export mode. Fail closed rather than silently falling back to
    FULL: a caller that asked for something it believed was a privacy mode must not be
    handed the archive-of-record bytes instead."""


# --------------------------------------------------------------- graph codec

def serialize_graph(corpus):
    """Corpus spawn-graph -> deterministic JSON string.

    Nodes are emitted sorted by id and edges sorted by (parent, child); every field
    is carried verbatim. sort_keys makes the per-record key order stable too, so the
    whole string is byte-identical across runs and platforms.
    """
    nodes = [{f: getattr(meta, f) for f in _NODE_FIELDS}
             for meta in corpus.threads.values()]
    nodes.sort(key=lambda n: n["id"])
    edges = [{f: getattr(edge, f) for f in _EDGE_FIELDS} for edge in corpus.edges]
    edges.sort(key=lambda e: (e["parent_thread_id"], e["child_thread_id"]))
    return json.dumps({"nodes": nodes, "edges": edges},
                      ensure_ascii=False, sort_keys=True, indent=2)


def parse_graph(serialized):
    """The inverse of serialize_graph: JSON string -> Corpus spawn-graph."""
    doc = json.loads(serialized)
    threads = {}
    for node in doc["nodes"]:
        meta = ThreadMeta(**{f: node[f] for f in _NODE_FIELDS})
        threads[meta.id] = meta
    edges = [SpawnEdge(**{f: edge[f] for f in _EDGE_FIELDS}) for edge in doc["edges"]]
    return Corpus(threads=threads, edges=edges)


def graph_fidelity_gate(corpus):
    """Round-trip oracle: serialize the corpus, re-parse it, and return the
    structural diff between the original and the re-parse. `.is_empty()` iff the
    serialization round-trips faithfully."""
    return diff_corpus(corpus, parse_graph(serialize_graph(corpus)))


# --------------------------------------------------------------- sanitization

def _map_text_fields(corpus, fn):
    """A NEW Corpus with `fn` applied to every free-text node field and to the edge
    status. Pure: the input corpus is never mutated (mirrors diff_corpus's discipline).

    One traversal shared by the two text transforms an export applies — hidden-unicode
    neutralization (always) and credential scrubbing (opt-in) — so they cannot drift apart
    on which fields count as free text.
    """
    threads = {}
    for meta in corpus.threads.values():
        values = {f: getattr(meta, f) for f in _NODE_FIELDS}
        for f in _TEXT_NODE_FIELDS:
            values[f] = fn(values[f])
        threads[values["id"]] = ThreadMeta(**values)
    edges = [SpawnEdge(e.parent_thread_id, e.child_thread_id, fn(e.status))
             for e in corpus.edges]
    return Corpus(threads=threads, edges=edges)


def _sanitize_corpus(corpus):
    """A NEW Corpus with every free-text field neutralized via sanitize_for_copy."""
    return _map_text_fields(corpus, sanitize_for_copy)


def _scrub_corpus(corpus):
    """A NEW Corpus with every credential-shaped run in a free-text field replaced by
    `[redacted:<shape>]`. OPT-IN — reached only when the caller passed `scrub=True`."""
    return _map_text_fields(corpus, redact.scrub_credential_shapes)


def _scrub_conversation(conv):
    """A NEW ir.Conversation with every block's text scrubbed of credential shapes.

    Needed so the token-multiset gate keeps working under `scrub=True`: the gate compares
    SOURCE prose against the STORED HTML, so if only the HTML were scrubbed every removed
    credential would be reported as lost content. Scrubbing both sides identically keeps
    the gate sensitive to real loss (proved by a test that still catches a dropped word).
    """
    turns = [dc_replace(turn, blocks=[
        dc_replace(b, text=redact.scrub_credential_shapes(b.text)) for b in turn.blocks])
        for turn in conv.turns]
    return dc_replace(conv, turns=turns)


# ------------------------------------------------------- G-6 shareable projection

def _project_shareable(corpus, home=None):
    """A NEW Corpus projected for SHAREABLE mode: every node through
    `redact.shareable_thread` (preview dropped, cwd/rollout_path relativized to `~`,
    structure + title + repo/branch kept), edges untouched (a (parent, child, status)
    triple carries no path and no excerpt).

    Applied BEFORE the fidelity gate, so the gate proves the artifact reconstructs
    exactly what was written rather than failing on the deliberate projection.
    """
    return Corpus(threads={tid: redact.shareable_thread(meta, home=home)
                           for tid, meta in corpus.threads.items()},
                  edges=list(corpus.edges))


# ------------------------------------------- G-5 credential-shape scan (WARN, no mutate)

def _sorted_findings(findings):
    """A stable order for the report: scope, then id, then field, then offset. Two runs
    over the same corpus produce the same list, so a report is diffable."""
    return sorted(findings,
                  key=lambda f: (f["scope"], f["id"], f["field"], f["offset"]))


def _located(hits, scope, ident, field):
    """Attach a LOCATION to each raw scanner hit. A finding with no location is just an
    alarm; `scope`/`id`/`field`/`offset` is what lets the reader go look at the thing."""
    return [dict(hit, scope=scope, id=ident, field=field) for hit in hits]


def _graph_findings(corpus):
    """Credential-shape findings over the graph AS IT WILL BE WRITTEN (post-sanitize,
    post-mode-projection), so the report answers "what is in the file you are about to
    hand over" rather than "what was in memory"."""
    out = []
    for meta in corpus.threads.values():
        for f in _TEXT_NODE_FIELDS:
            out += _located(redact.scan_credential_shapes(getattr(meta, f)),
                            "thread", meta.id, f)
    for e in corpus.edges:
        out += _located(redact.scan_credential_shapes(e.status), "edge",
                        "%s->%s" % (e.parent_thread_id, e.child_thread_id), "status")
    return out


# --------------------------------------------------------------- path safety

def _norm_local(raw, label):
    """os.PathLike | str -> a PureWindowsPath, rejecting UNC / protocol-relative
    inputs up front. PureWindowsPath models Windows path semantics on every host, so
    the same rule runs identically on Linux / macOS / Windows."""
    text = os.fspath(raw)
    if text.replace("/", "\\").startswith("\\\\"):        # \\host\share, //host/share
        raise ExportPathError("refusing UNC / non-local %s: %r" % (label, text))
    return PureWindowsPath(text)


def _confined_target(dest_path, root):
    """Validate `dest_path` and return the host-native Path to write to.

    Rejects UNC/non-local paths, parent-traversal, and any destination not lexically
    within `root`. `..` is rejected explicitly because PureWindowsPath does not
    collapse it, so `root/../evil` would otherwise pass is_relative_to.
    """
    dest = _norm_local(dest_path, "destination")
    root_w = _norm_local(root, "export root")
    if ".." in dest.parts:
        raise ExportPathError(
            "refusing parent-traversal in destination: %r" % os.fspath(dest_path))
    if not dest.is_relative_to(root_w):
        raise ExportPathError(
            "destination is not within the export root: %r not under %r"
            % (os.fspath(dest_path), os.fspath(root)))
    return Path(os.fspath(dest_path))


# --------------------------------------------------------------- artifact write

def _write_artifact(target, serialized_graph, conv_out, mode):
    """Write the deterministic export bundle as UTF-8 bytes (no newline
    translation, so the file is byte-identical across platforms).

    `mode` is recorded IN the artifact so a SHAREABLE file can never be mistaken for an
    archive of record months later: the file states which projection produced it.
    """
    doc = {
        "llm_anthology_export_version": EXPORT_FORMAT_VERSION,
        "llm_anthology_export_mode": mode,
        "graph": serialized_graph,
        "conversations": [{"id": cid, "html": html} for cid, html in conv_out],
    }
    payload = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload.encode("utf-8"))


# --------------------------------------------------------------- the gate

def export_with_gate(corpus, dest_path, conversations=None, *, root=None,
                     mode=MODE_FULL, scrub=False, home=None):
    """Sanitize, project for `mode`, SCAN for credential shapes, gate on structural +
    textual fidelity, and write the artifact only if both gates pass.

    conversations: optional iterable of (ir.Conversation, rendered_html) pairs. Each
    stored HTML is sanitized and checked against its source prose (token multiset);
    any missing token fails the textual gate.
    root: the confinement root (a chosen, drive-absolute directory in production);
    defaults to the current working directory. `dest_path` must resolve within it.

    mode (G-6) — the caller's choice, never inferred:
      * MODE_FULL (default): every field, unchanged, round-trippable. The archive of
        record. It carries `preview` (an adapter-populated 200-char excerpt of each
        conversation's opening user message) and the absolute `cwd` / `rollout_path`.
      * MODE_SHAREABLE: `preview` dropped entirely, both paths relativized to `~`,
        structure + title + repo/branch kept. Measurably less leaky, NOT safe — it still
        carries titles, which for the Codex adapter are derived from raw content.
      An unrecognized mode raises ExportModeError before anything is written.

    scrub (G-5) — OPT-IN, default False. When False, the credential-shape scan REPORTS
    and changes nothing: the archive-of-record principle applies to the export path too,
    so content is never silently altered. When True the reported shapes are replaced with
    `[redacted:<shape>]` on both the graph text and the bundled HTML (and on the source
    prose the token gate compares against, so a deliberate removal is not mistaken for
    lost content).

    home: the home directory `MODE_SHAREABLE` relativizes against; defaults to the
    process's own. An injectable seam so the projection is testable identically on
    Windows, Linux and macOS.

    Returns a report dict: {ok, written, path, mode, added, removed, changed,
    missing_tokens, credential_scan}. On failure ok/written are False, path is None, and
    the deltas name exactly what was lost.

    `credential_scan` is {findings, coverage_limit, scrubbed}:
      * findings — every credential-shaped hit in the artifact ABOUT TO BE (or just)
        written, each with scope / id / field / shape / offset and a MASKED preview.
        With `scrub=True` these describe what was found and removed;
      * coverage_limit — ALWAYS present, findings or not. It is the load-bearing part:
        the scan matches credential SHAPES and is blind to personal and medical content,
        so an empty `findings` list is NOT a safety verdict. A "clean" report over a
        corpus of private medical conversations would be factually correct and
        dangerously misleading, and this sentence is what stops it being read that way.
        See llm_anthology.redact.CREDENTIAL_SHAPE_COVERAGE_LIMIT.
    A finding never blocks the write; only the two fidelity gates do.
    """
    if mode not in EXPORT_MODES:
        raise ExportModeError("unknown export mode %r (expected one of %s)"
                              % (mode, ", ".join(EXPORT_MODES)))
    if root is None:
        root = Path.cwd()
    target = _confined_target(dest_path, root)
    clean = _sanitize_corpus(corpus)
    if mode == MODE_SHAREABLE:
        clean = _project_shareable(clean, home=home)

    findings = _graph_findings(clean)

    missing = {}
    conv_out = []
    for conv, html in (conversations or []):
        clean_html = sanitize_for_copy(html)
        findings += _located(redact.scan_credential_shapes(clean_html),
                             "conversation", conv.id, "html")
        if scrub:
            clean_html = redact.scrub_credential_shapes(clean_html)
            conv = _scrub_conversation(conv)
        result = verify(conv, clean_html)
        if result["missing_tokens"]:
            missing[conv.id] = result["missing_tokens"]
        conv_out.append((conv.id, clean_html))
    conv_out.sort()

    if scrub:
        clean = _scrub_corpus(clean)

    # Gated LAST, over the corpus that is actually written — so the round-trip verdict
    # covers the post-projection, post-scrub bytes rather than an earlier draft of them.
    graph_diff = graph_fidelity_gate(clean)

    report = {
        "ok": False,
        "written": False,
        "path": None,
        "mode": mode,
        "added": {"nodes": graph_diff.added_nodes, "edges": graph_diff.added_edges},
        "removed": {"nodes": graph_diff.removed_nodes,
                    "edges": graph_diff.removed_edges},
        "changed": graph_diff.changed_nodes,
        "missing_tokens": missing,
        "credential_scan": {
            "findings": _sorted_findings(findings),
            "coverage_limit": redact.CREDENTIAL_SHAPE_COVERAGE_LIMIT,
            "scrubbed": bool(scrub),
        },
    }
    if graph_diff.is_empty() and not missing:
        _write_artifact(target, serialize_graph(clean), conv_out, mode)
        report["ok"] = True
        report["written"] = True
        report["path"] = str(target)
    return report
