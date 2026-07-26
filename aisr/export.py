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

  * export_with_gate(corpus, dest_path, conversations=None, *, root=None) -> dict
        Sanitizes every free-text field (aisr.sanitize.sanitize_for_copy), runs the
        graph gate AND a per-conversation token-multiset gate (aisr.verify.verify on
        the sanitized HTML that is actually stored), and writes the artifact ONLY if
        BOTH pass. On failure it writes NOTHING and returns a structured
        {added, removed, changed, missing_tokens} report. `dest_path` is
        drive-absolute-local-only: UNC / non-local paths, parent-traversal, and any
        destination outside `root` are rejected (the Windows SMB/NTLM hash-leak
        class — a crafted `\\\\host\\share` coerces an outbound authentication).

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
from dataclasses import fields as dc_fields
from pathlib import Path, PureWindowsPath

from .corpus import Corpus, ThreadMeta, SpawnEdge
from .diff import diff_corpus
from .sanitize import sanitize_for_copy
from .verify import verify

EXPORT_FORMAT_VERSION = 1

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

def _sanitize_corpus(corpus):
    """A NEW Corpus with every free-text field neutralized via sanitize_for_copy.
    Pure: the input corpus is never mutated (mirrors diff_corpus's discipline)."""
    threads = {}
    for meta in corpus.threads.values():
        values = {f: getattr(meta, f) for f in _NODE_FIELDS}
        for f in _TEXT_NODE_FIELDS:
            values[f] = sanitize_for_copy(values[f])
        threads[values["id"]] = ThreadMeta(**values)
    edges = [SpawnEdge(e.parent_thread_id, e.child_thread_id,
                       sanitize_for_copy(e.status)) for e in corpus.edges]
    return Corpus(threads=threads, edges=edges)


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

def _write_artifact(target, serialized_graph, conv_out):
    """Write the deterministic export bundle as UTF-8 bytes (no newline
    translation, so the file is byte-identical across platforms)."""
    doc = {
        "aisr_export_version": EXPORT_FORMAT_VERSION,
        "graph": serialized_graph,
        "conversations": [{"id": cid, "html": html} for cid, html in conv_out],
    }
    payload = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload.encode("utf-8"))


# --------------------------------------------------------------- the gate

def export_with_gate(corpus, dest_path, conversations=None, *, root=None):
    """Sanitize, gate on structural + textual fidelity, and write the artifact only
    if both gates pass.

    conversations: optional iterable of (ir.Conversation, rendered_html) pairs. Each
    stored HTML is sanitized and checked against its source prose (token multiset);
    any missing token fails the textual gate.
    root: the confinement root (a chosen, drive-absolute directory in production);
    defaults to the current working directory. `dest_path` must resolve within it.

    Returns a report dict: {ok, written, path, added, removed, changed,
    missing_tokens}. On failure ok/written are False, path is None, and the deltas
    name exactly what was lost.
    """
    if root is None:
        root = Path.cwd()
    target = _confined_target(dest_path, root)
    clean = _sanitize_corpus(corpus)

    graph_diff = graph_fidelity_gate(clean)

    missing = {}
    conv_out = []
    for conv, html in (conversations or []):
        clean_html = sanitize_for_copy(html)
        result = verify(conv, clean_html)
        if result["missing_tokens"]:
            missing[conv.id] = result["missing_tokens"]
        conv_out.append((conv.id, clean_html))
    conv_out.sort()

    report = {
        "ok": False,
        "written": False,
        "path": None,
        "added": {"nodes": graph_diff.added_nodes, "edges": graph_diff.added_edges},
        "removed": {"nodes": graph_diff.removed_nodes,
                    "edges": graph_diff.removed_edges},
        "changed": graph_diff.changed_nodes,
        "missing_tokens": missing,
    }
    if graph_diff.is_empty() and not missing:
        _write_artifact(target, serialize_graph(clean), conv_out)
        report["ok"] = True
        report["written"] = True
        report["path"] = str(target)
    return report
