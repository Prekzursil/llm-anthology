"""Cockpit sidecar: a stdio NDJSON JSON-RPC 2.0 engine over an aisr corpus index.

The Electron/UI cockpit talks to this process over stdin/stdout — ONE compact JSON
object per line, each ``\\n``-terminated and flushed (NOT HTTP, NOT a socket). Every
request is ``{jsonrpc:"2.0",id,method,params}``; every reply is either
``{...,result}`` or ``{...,error:{code,message,data?}}``. The loop is deliberately
dumb and unkillable: one malformed line, one unknown method, one busy database — none
of them may crash the server, they each become a typed error response.

Layering
--------
The RPC surface is built ON the Phase-1 corpus API (``aisr.corpus``): the thread
SPAWN GRAPH (threads + directed edges) is loaded into memory once at construction via
``load_corpus``; the contentless FTS5 index is queried live for search and stats; and
a single conversation transcript is re-parsed on demand from its rollout file via the
Codex rollout adapter. ``dispatch`` is a pure function of ``(method, params)`` so every
handler is testable WITHOUT real stdio; ``serve`` is the thin readline loop on top.

Method status (all implemented; honest caveats inline)
------------------------------------------------------
* ``health.ping``      — FULL. Works even with no index attached (``corpus_ready`` False).
* ``corpus.stats``     — FULL. ``records`` = SUM(turn_count) and ``bytes`` = SUM(char_count)
                         over the index; the raw 2.2M-event count is NOT retained by the
                         index, so these are the honest index-computable aggregates.
* ``graph.roots``      — FULL. ``order`` in {created(default)|recent|title}; limit/offset.
* ``graph.children``   — FULL.
* ``graph.subtree``    — FULL. Optional ``depth`` cap; cycle/diamond-safe.
* ``graph.ancestors``  — FULL. All spawn-ancestors, nearest first.
* ``graph.diff``       — FULL. Structural delta between two corpora. Each side is an
                         on-disk index path (``old_index`` / ``new_index``), a time-travel
                         snapshot of the loaded corpus as-of a birth ms (``as_of_a`` /
                         ``as_of_b``, via ``aisr.timetravel.corpus_as_of``), or — when
                         neither is given — the corpus this sidecar already holds (so
                         ``{}`` is the empty self-diff). A named path must already exist (a
                         diff never creates one) and is read via SELECT only, so the corpus
                         data it holds is never modified; supplying a path AND an as-of for
                         one side is ambiguous (-32602). The DTO carries added/removed
                         node-id + edge sets and a per-field changed-node map; changed
                         ``rollout_path``s are basenamed and every changed free-text value
                         is sanitized. In the time-travel form ``changed_nodes`` is always
                         empty — both snapshots view one immutable corpus.
* ``graph.rollup``     — FULL. ``{thread_id: RollupMetrics}`` over every graph node — the
                         node's own plus its whole-subtree token/count/depth totals,
                         diamond-deduped and cycle-safe (see ``aisr.rollup``). Counts and
                         graph shape only; no conversation text crosses.
* ``graph.timeline``   — FULL. ``{events, min_ms, max_ms, undated_count}`` — the sorted
                         distinct dated node births, their range, and the undated-NODE
                         count over the SAME node set graph.at views (threads UNION edge
                         endpoints, so a dangling endpoint counts as undated — matching the
                         frozen contract; see ``aisr.timetravel.timeline``). Ints/None only.
* ``graph.at``         — FULL. The spawn graph AS-OF ``as_of_ms`` (a time-travel snapshot):
                         nodes born by T (undated/dangling always present) + edges whose
                         CHILD is born by T, projected exactly like graph.subtree.
                         child_count/depth are computed OVER THE SNAPSHOT, so the moment is
                         internally coherent (a node's fan-out matches its visible edges).
* ``export.plan``      — FULL. A dry-run tally ``{node_count, edge_count,
                         conversation_count, est_bytes}`` of what ``export.run`` ACTUALLY
                         writes — the GRAPH only, so ``conversation_count`` is 0 and
                         ``est_bytes`` is the serialized graph's byte size (NOT a transcript
                         Σ char_count that this bite never bundles). No filesystem access.
* ``export.run``       — FULL. Writes the spawn-graph artifact to a drive-absolute-local
                         ``dest_path`` (UNC/network + relative + parent-traversal rejected)
                         and returns ``{ok, graph_gate, transcript_gate, written_path?}``,
                         gate-enforced by ``aisr.export.export_with_gate``. This bite
                         exports the GRAPH only (the sidecar holds no rendered transcripts),
                         so ``transcript_gate`` is vacuously true.
* ``search.query``     — FULL match/provider-filter/paging. ``snippet`` comes from the
                         (sanitized) title — the FTS index is CONTENTLESS so no body
                         span is retrievable — and ``score`` is POSITIONAL because
                         FTS5 ``rank`` collapses to a constant under ``detail=none``
                         (measured ``rank = -0.0`` for every row). Both are honest.
* ``thread.get``       — FULL metadata. ``rollout_path`` is withheld from the wire (a
                         local FS path) and surfaced only as ``has_rollout: bool``.
* ``conversation.get`` — FULL re-parse for a Codex thread that has a readable rollout
                         file; a documented ``{available:false,reason}`` stub when the
                         rollout path is empty, missing, or unreadable (e.g. a
                         non-Codex provider that carries no rollout). Never raises on a
                         bad file — it degrades to the stub.
* ``research.synthesize``     — FULL. TWO-TIER synthesis over the corpus. The default
                         tier (``tier:"cloud"``, or absent) redacts EVERY indexed
                         conversation to a ``redact.MetadataView`` and hands ONLY that
                         allowlist to the corpus-blind research plane
                         (``research.synthesize_over_metadata``): a cloud LLM may be the
                         backend, and it never sees a body/PII/local path — only
                         sanitized metadata + aggregate counts. ``tier:"local"`` runs the
                         LOCAL tier instead: it re-parses rollouts to synthesize over RAW
                         transcript text and feeds a LOCAL backend that never egresses, so
                         raw content is deliberately kept on-box. Any other ``tier`` ->
                         -32602. Returns ``{tier, summary, conversation_count}`` (summary
                         sanitized on the way back out).
* ``research.extract_entities`` — FULL. Metadata-only, exactly like the cloud tier of
                         synthesize: redact -> MetadataView -> ``research.extract_entities``.
                         Returns ``{entities, conversation_count}`` (each entity sanitized).

Privacy (HARD): every free-text field derived from user/model content — a title, a
preview, a search snippet, a transcript block, a tool payload — passes through
``aisr.sanitize.sanitize_for_copy`` before crossing the wire, so a hidden-unicode
prompt-injection channel in the corpus cannot be relayed into the next agent. ``stats``
and ``graph.*`` are aggregate/metadata only; a full transcript crosses only for the one
conversation the user explicitly opened, and even then it is sanitized.

Research plane / ACL split (Phase-4, HARD). ``aisr.research`` is a SEPARATE, corpus-blind
surface: the ONLY conversation data it may feed a cloud LLM is the sanitized metadata
allowlist. This sidecar ENFORCES that split BY CONSTRUCTION — the cloud research handlers
build a ``list[redact.MetadataView]`` via ``_metadata_views`` (``redact.to_metadata_view``
per row) and pass ONLY that to ``research``; a ``Corpus``, a sqlite row, an FTS body, a
``rollout_path`` or any raw text is NEVER handed to the research plane. ``research`` itself
imports ``MetadataView`` for typing only, so it has no runtime path back to the raw corpus.
The one tier that reads raw content — ``tier:"local"`` — bypasses ``research`` entirely and
feeds a LOCAL, no-egress backend. Backends are injected at construction (``research_backend``
/ ``local_backend``); both default to a no-network ``research.MockBackend`` placeholder, so
an unconfigured host synthesizes nothing rather than reaching the network.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import asdict
from datetime import datetime

from aisr import (
    __version__,
    corpus,
    diff,
    export,
    ir,
    redact,
    research,
    rollup,
    sanitize,
    timetravel,
)
from aisr.adapters import codex_rollout

# App-specific JSON-RPC error codes (standard codes -32700/-32600/-32601/-32602/-32603
# are used directly where they apply).
CORPUS_NOT_INDEXED = -32000
THREAD_NOT_FOUND = -32001
DB_BUSY = -32002

_ROOT_ORDERS = ("created", "recent", "title")

# The LOCAL synthesis tier's instruction. This tier reads RAW transcript text and feeds
# a LOCAL backend only, so the prompt never leaves the machine — it is deliberately NOT
# the sanitized-metadata prompt the cloud research plane builds.
_LOCAL_INSTRUCTION = (
    "LOCAL TIER (stays on this machine, never sent to any cloud backend): summarize "
    "the following RAW conversation transcripts."
)


class RpcError(Exception):
    """A JSON-RPC error carrying a numeric code and optional structured ``data``."""

    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# ------------------------------------------------------------------- pure helpers

def _dumps(obj):
    """One compact NDJSON line (no spaces, UTF-8 preserved, no trailing newline)."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _error_response(rid, code, message, data=None):
    """A JSON-RPC error envelope; ``data`` is omitted entirely when None."""
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": error}


def _clean(s):
    """Strip hidden/dangerous unicode from a string bound for the wire."""
    return sanitize.sanitize_for_copy(s)


def _sanitize_tree(obj):
    """Recursively sanitize every string inside a JSON-ish value; other types pass."""
    if isinstance(obj, str):
        return _clean(obj)
    if isinstance(obj, dict):
        return {k: _sanitize_tree(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_tree(v) for v in obj]
    return obj


def _redact_paths(meta):
    """Copy ``meta`` with any absolute ``rollout_path`` reduced to its basename, so the
    local filesystem layout never crosses the wire."""
    result = dict(meta)
    if result.get("rollout_path"):
        result["rollout_path"] = os.path.basename(result["rollout_path"])
    return result


def _to_ms(s):
    """An ISO-8601 timestamp -> epoch milliseconds, or None if absent/unparseable. A
    trailing 'Z' is normalised to +00:00 (datetime.fromisoformat rejects 'Z' on the
    Python versions this project supports)."""
    if not isinstance(s, str) or not s:
        return None
    text = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except ValueError:
        return None


def _spawn_edge(edge):
    """A SpawnEdge -> its wire DTO; empty ``status`` is omitted."""
    dto = {"parent": edge.parent_thread_id, "child": edge.child_thread_id}
    if edge.status:
        dto["status"] = edge.status
    return dto


def _opt_int(params, key, default):
    """An optional non-negative-integer param; absent -> default, malformed -> -32602.
    Booleans are rejected even though ``bool`` is an ``int`` subclass."""
    if key not in params:
        return default
    value = params[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RpcError(-32602, "%s must be a non-negative integer" % key)
    return value


def _req_str(params, key):
    """A required non-empty string param; anything else -> -32602."""
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise RpcError(-32602, "%s must be a non-empty string" % key)
    return value


def _req_int(params, key):
    """A required integer param; missing / non-int / bool -> -32602. Booleans are
    rejected even though ``bool`` is an ``int`` subclass, and a float is rejected too —
    a birth cutoff is an epoch-ms integer."""
    if key not in params:
        raise RpcError(-32602, "%s must be an integer" % key)
    value = params[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise RpcError(-32602, "%s must be an integer" % key)
    return value


def _reject_nonlocal_dest(dest_path):
    """Guard an export destination BEFORE any filesystem access: reject a UNC / network
    path (``\\\\host\\share`` — a crafted target coerces an outbound SMB/NTLM auth, the
    Windows hash-leak class) and any non-absolute path. Drive-absolute local paths only;
    ``aisr.export`` then re-checks UNC + parent-traversal + confinement as defence in
    depth."""
    if dest_path.replace("/", "\\").startswith("\\\\"):
        raise RpcError(-32602, "dest_path must be a local path, not a UNC/network path")
    if not os.path.isabs(dest_path):
        raise RpcError(-32602, "dest_path must be an absolute local path")


def _raw_transcript(conv):
    """Every block's RAW text in an ir.Conversation, joined. This is UNSANITIZED,
    NON-allowlisted body content — it feeds ONLY the LOCAL synthesis tier, which never
    egresses, so raw bodies/PII are intentionally kept here rather than stripped."""
    return "\n".join(block.text for turn in conv.turns for block in turn.blocks)


# -------------------------------------------------------------------- the engine

class Sidecar:
    """Holds the opened index (may be None) + the in-memory thread graph, and answers
    JSON-RPC requests. ``conn`` None means no corpus is attached: ``health.ping`` still
    works (reporting ``corpus_ready`` False) while every data method returns -32000."""

    def __init__(self, conn, research_backend=None, local_backend=None):
        self.conn = conn
        self.corpus = corpus.load_corpus(conn) if conn is not None else corpus.Corpus()
        # The cloud research plane's egress backend and the LOCAL tier's on-box backend.
        # Both default to a no-network placeholder, so an unconfigured host reaches the
        # network for neither; a real cockpit host injects the concrete backends.
        self.research_backend = (
            research.MockBackend() if research_backend is None else research_backend)
        self.local_backend = (
            research.MockBackend() if local_backend is None else local_backend)
        self._handlers = {
            "health.ping": self._health_ping,
            "corpus.stats": self._corpus_stats,
            "graph.roots": self._graph_roots,
            "graph.children": self._graph_children,
            "graph.subtree": self._graph_subtree,
            "graph.ancestors": self._graph_ancestors,
            "graph.diff": self._graph_diff,
            "graph.rollup": self._graph_rollup,
            "graph.timeline": self._graph_timeline,
            "graph.at": self._graph_at,
            "export.plan": self._export_plan,
            "export.run": self._export_run,
            "search.query": self._search_query,
            "thread.get": self._thread_get,
            "conversation.get": self._conversation_get,
            "research.synthesize": self._research_synthesize,
            "research.extract_entities": self._research_extract_entities,
        }

    # -- transport ----------------------------------------------------------------

    def serve(self, stdin, stdout):
        """Read NDJSON requests line by line, writing one flushed response per line
        that warrants one (blank lines and notifications produce no output)."""
        for line in stdin:
            response = self.handle_line(line)
            if response is not None:
                stdout.write(_dumps(response) + "\n")
                stdout.flush()

    def handle_line(self, line):
        """One raw input line -> a response dict, or None to write nothing."""
        line = line.strip()
        if not line:
            return None
        try:
            request = json.loads(line)
        except ValueError:
            return _error_response(None, -32700, "Parse error")
        return self.handle_request(request)

    def handle_request(self, request):
        """A parsed request -> a response dict, or None for a notification (no ``id``)."""
        if not isinstance(request, dict):
            return _error_response(None, -32600, "Invalid Request")
        response = self._compute_response(
            request.get("id"), request.get("method"), request.get("params", {}))
        if "id" not in request:          # a notification is never answered
            return None
        return response

    def _compute_response(self, rid, method, params):
        if not isinstance(method, str):
            return _error_response(rid, -32600, "Invalid Request")
        try:
            return {"jsonrpc": "2.0", "id": rid, "result": self.dispatch(method, params)}
        except RpcError as e:
            return _error_response(rid, e.code, e.message, e.data)
        except Exception as e:           # never let one bad request kill the loop
            return _error_response(rid, -32603, "Internal error", {"detail": str(e)})

    def dispatch(self, method, params):
        """Route ``method`` to its handler. Unknown -> -32601; non-object params ->
        -32602; a SQLite lock/busy -> -32002 (with ``retry_ms``); any other operational
        DB failure -> -32603."""
        if not isinstance(params, dict):
            raise RpcError(-32602, "params must be an object")
        handler = self._handlers.get(method)
        if handler is None:
            raise RpcError(-32601, "method not found: %s" % method)
        try:
            return handler(params)
        except sqlite3.OperationalError as e:
            text = str(e).lower()
            if "lock" in text or "busy" in text:
                raise RpcError(DB_BUSY, "database is busy", {"retry_ms": 100})
            raise RpcError(-32603, "database error", {"detail": str(e)})

    def _require_corpus(self):
        if self.conn is None:
            raise RpcError(CORPUS_NOT_INDEXED, "corpus not indexed")

    # -- handlers -----------------------------------------------------------------

    def _health_ping(self, params):
        return {"ok": True, "engine_version": __version__,
                "ir_version": ir.IR_VERSION, "corpus_ready": self.conn is not None}

    def _corpus_stats(self, params):
        self._require_corpus()
        conn = self.conn
        conversations = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        records = conn.execute(
            "SELECT COALESCE(SUM(turn_count), 0) FROM conversations").fetchone()[0]
        total_bytes = conn.execute(
            "SELECT COALESCE(SUM(char_count), 0) FROM conversations").fetchone()[0]
        threads = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        edges = conn.execute(
            "SELECT COUNT(*) FROM thread_spawn_edges").fetchone()[0]
        providers = {row[0]: row[1] for row in conn.execute(
            "SELECT provider, COUNT(*) FROM conversations GROUP BY provider")}
        return {"conversations": conversations, "records": records, "threads": threads,
                "edges": edges, "bytes": total_bytes, "providers": providers}

    def _graph_roots(self, params):
        self._require_corpus()
        limit = _opt_int(params, "limit", 100)
        offset = _opt_int(params, "offset", 0)
        nodes = [self._thread_node(tid) for tid in self.corpus.roots()]
        nodes = self._order_nodes(nodes, params.get("order"))
        return nodes[offset:offset + limit]

    def _graph_children(self, params):
        self._require_corpus()
        tid = _req_str(params, "thread_id")
        return [self._thread_node(child) for child in self.corpus.children_of(tid)]

    def _graph_subtree(self, params):
        self._require_corpus()
        tid = _req_str(params, "thread_id")
        depth = params.get("depth")
        if depth is not None and (not isinstance(depth, int) or isinstance(depth, bool)
                                  or depth < 0):
            raise RpcError(-32602, "depth must be a non-negative integer")
        node_ids = self._collect_subtree(tid, depth)
        idset = set(node_ids)
        nodes = [self._thread_node(i) for i in node_ids]
        edges = [_spawn_edge(e) for e in self.corpus.edges
                 if e.parent_thread_id in idset and e.child_thread_id in idset]
        return {"nodes": nodes, "edges": edges}

    def _graph_ancestors(self, params):
        self._require_corpus()
        tid = _req_str(params, "thread_id")
        return [self._thread_node(i) for i in self._collect_ancestors(tid)]

    def _graph_diff(self, params):
        """The structural CorpusDiff between two corpora. Each operand is a path to another
        index (``old_index`` / ``new_index``), a time-travel snapshot of the loaded corpus
        as-of a birth ms (``as_of_a`` / ``as_of_b``), or — when neither is given — the
        corpus this sidecar already holds (see ``_diff_operand``). The result is projected
        to the wire with rollout paths basenamed and changed free-text sanitized. In the
        time-travel form ``changed_nodes`` is always empty: both snapshots view one
        immutable corpus, so a node present in both is byte-identical."""
        self._require_corpus()
        old = self._diff_operand(params, "old_index", "as_of_a")
        new = self._diff_operand(params, "new_index", "as_of_b")
        return self._project_diff(diff.diff_corpus(old, new))

    def _graph_rollup(self, params):
        """``{thread_id: RollupMetrics}`` over every node, keyed in sorted-id order. Each
        value is the flat RollupMetrics dataclass (all non-negative ints: self/subtree
        tokens+counts, max_depth, child_count), so no sanitization is needed."""
        self._require_corpus()
        return {tid: asdict(metrics)
                for tid, metrics in rollup.rollup(self.corpus).items()}

    def _graph_timeline(self, params):
        """The node-creation event axis for a time scrubber: ``{events, min_ms, max_ms,
        undated_count}`` from ``aisr.timetravel.timeline`` (sorted distinct dated births,
        their range, and the count of undated NODES — threads UNION edge endpoints, so a
        dangling endpoint counts as undated). Ints/None only — no free text."""
        self._require_corpus()
        return timetravel.timeline(self.corpus)

    def _graph_at(self, params):
        """The spawn graph AS-OF a birth timestamp — a time-travel snapshot.
        ``timetravel.corpus_as_of`` selects the nodes born by ``as_of_ms`` (an undated /
        dangling node is always present) and the edges whose CHILD is born by then, and
        the result is projected exactly like graph.subtree. child_count/depth are computed
        over the SNAPSHOT, so a node's fan-out matches the edges visible as-of T (a
        coherent moment) rather than the final graph."""
        self._require_corpus()
        as_of = _req_int(params, "as_of_ms")
        return self._project_snapshot(timetravel.corpus_as_of(self.corpus, as_of))

    def _export_plan(self, params):
        """A dry-run tally of what ``export.run`` ACTUALLY writes — the spawn GRAPH only
        (this bite bundles NO transcripts, so ``export.run`` writes zero conversations and
        its ``transcript_gate`` is vacuously true). Distinct graph nodes (threads UNION edge
        endpoints), spawn edges, ``conversation_count`` = 0 (none are written), and
        ``est_bytes`` = the serialized graph artifact's UTF-8 byte size — so the dry-run
        preview matches the file run instead of overstating a Σ(char_count) of transcripts
        that never leave the index. No filesystem access; ints only, so no sanitization is
        needed. [Wiring transcript bundling is a later bite; when it lands, this tally grows
        the conversation_count/bytes it will then actually write — settle by mirroring
        export.run's conversation set here.]"""
        self._require_corpus()
        est_bytes = len(export.serialize_graph(self.corpus).encode("utf-8"))
        return {"node_count": len(self.corpus._nodes()),
                "edge_count": len(self.corpus.edges),
                "conversation_count": 0, "est_bytes": est_bytes}

    def _export_run(self, params):
        """Write the spawn-graph export to ``dest_path`` and return the fidelity verdict.
        ``dest_path`` is drive-absolute-local-only (UNC/network and relative paths ->
        -32602, the SMB hash-leak class) and the write is confined to its own parent
        directory; ``aisr.export.export_with_gate`` then enforces the structural
        round-trip gate (and rejects parent-traversal) and writes ONLY if it passes.

        This bite exports the GRAPH artifact — the sidecar holds the spawn graph, not
        rendered transcripts — so ``transcript_gate`` is vacuously true: no conversation
        body is bundled, so none can be lost. [UNVERIFIED: bundling + per-conversation
        token gating is a later bite; settle by wiring conversation.get -> render_html ->
        verify per row and passing the pairs to export_with_gate.]"""
        self._require_corpus()
        dest_path = _req_str(params, "dest_path")
        _reject_nonlocal_dest(dest_path)
        try:
            report = export.export_with_gate(
                self.corpus, dest_path, root=os.path.dirname(dest_path))
        except export.ExportPathError as e:
            raise RpcError(-32602, "unsafe dest_path: %s" % e)
        return self._project_export_run(report)

    def _search_query(self, params):
        self._require_corpus()
        query = _req_str(params, "q")
        limit = _opt_int(params, "limit", 50)
        offset = _opt_int(params, "offset", 0)
        provider = params.get("provider")
        if provider is not None and not isinstance(provider, str):
            raise RpcError(-32602, "provider must be a string")
        start = time.perf_counter()
        total, rows = self._run_search(query, limit, offset, provider)
        hits = []
        for i, row in enumerate(rows):
            hit = {"conversation_id": row["conversation_id"],
                   "snippet": _clean(row["title"]),
                   "score": 1.0 / (offset + i + 1),
                   "provider": row["provider"]}
            if row["thread_id"]:
                hit["thread_id"] = row["thread_id"]
            ts = _to_ms(row["created_at"])
            if ts is not None:
                hit["ts_ms"] = ts
            hits.append(hit)
        took_ms = int((time.perf_counter() - start) * 1000)
        return {"hits": hits, "total": total, "took_ms": took_ms}

    def _thread_get(self, params):
        self._require_corpus()
        tid = _req_str(params, "thread_id")
        meta = self.corpus.threads.get(tid)
        if meta is None:
            raise RpcError(THREAD_NOT_FOUND, "thread not found: %s" % tid)
        return self._thread_meta(meta)

    def _conversation_get(self, params):
        self._require_corpus()
        cid = _req_str(params, "id")
        row = self.conn.execute(
            "SELECT conversation_id, provider, title, thread_id, rollout_path, "
            "created_at, updated_at, account FROM conversations "
            "WHERE conversation_id=?", (cid,)).fetchone()
        if row is None:
            raise RpcError(THREAD_NOT_FOUND, "conversation not found: %s" % cid)
        conv, info = self._reparse_rollout(row["rollout_path"])
        if conv is None:
            return self._conversation_stub(row, info)
        return self._serialize_conversation(conv, info)

    def _reparse_rollout(self, path):
        """Re-parse a rollout file into ``(ir.Conversation, errors)``, or
        ``(None, reason)`` when the path is empty/missing/unreadable. Never raises —
        the failure modes degrade to a reason string. Shared by ``conversation.get``
        (which stubs on None) and the LOCAL research tier (which skips None)."""
        if not path or not os.path.isfile(path):
            return None, "rollout unavailable"
        try:
            doc, errors = codex_rollout.parse_rollout_file(path)
        except OSError as e:
            return None, "rollout unreadable: %s" % e
        return doc.conversation, errors

    # -- research plane (Phase-4 two-tier synthesis) ------------------------------

    def _metadata_views(self):
        """The ACL boundary: project EVERY indexed conversation row into a
        ``redact.MetadataView`` (the strict, sanitized allowlist). This is the ONLY
        value the corpus-blind research plane is ever handed — ``SELECT *`` deliberately
        pulls the whole row (incl. ``rowid`` and the local ``rollout_path``) to PROVE the
        projection drops everything off-allowlist, and the FTS body is not a column here,
        so no raw text can ride along. Sorted by id so the cloud prompt is byte-stable."""
        rows = self.conn.execute(
            "SELECT * FROM conversations ORDER BY conversation_id").fetchall()
        return [redact.to_metadata_view(row) for row in rows]

    def _research_synthesize(self, params):
        """TWO-TIER synthesis. ``tier`` in {"cloud"(default), "local"}.

        cloud -> redact every conversation to a MetadataView and hand ONLY that allowlist
        to the corpus-blind ``research.synthesize_over_metadata`` (a cloud LLM backend
        never sees a body/PII/path). local -> synthesize over RAW transcript text through
        the on-box ``local_backend``, which never egresses. Any other tier -> -32602."""
        self._require_corpus()
        tier = params.get("tier", "cloud")
        if tier == "local":
            return self._research_local()
        if tier != "cloud":
            raise RpcError(-32602, "tier must be 'cloud' or 'local'")
        views = self._metadata_views()
        summary = research.synthesize_over_metadata(views, self.research_backend)
        return {"tier": "cloud", "summary": _clean(summary),
                "conversation_count": len(views)}

    def _research_extract_entities(self, params):
        """Metadata-only entity extraction: redact -> MetadataView -> the corpus-blind
        ``research.extract_entities``. Each returned entity is sanitized before it
        crosses the wire back to the UI."""
        self._require_corpus()
        views = self._metadata_views()
        entities = research.extract_entities(views, self.research_backend)
        return {"entities": [_clean(e) for e in entities],
                "conversation_count": len(views)}

    def _research_local(self):
        """The LOCAL tier: re-parse every conversation that has a readable rollout and
        synthesize over its RAW transcript text via the on-box ``local_backend``. The
        raw prompt is built here and never reaches ``research`` or the network; a
        conversation with no readable rollout simply contributes nothing."""
        transcripts = []
        for row in self.conn.execute(
                "SELECT conversation_id, rollout_path FROM conversations "
                "ORDER BY conversation_id").fetchall():
            conv, _info = self._reparse_rollout(row["rollout_path"])
            if conv is not None:
                transcripts.append(_raw_transcript(conv))
        prompt = "\n\n".join([_LOCAL_INSTRUCTION, *transcripts])
        summary = self.local_backend.synthesize(prompt)
        return {"tier": "local", "summary": _clean(summary),
                "conversation_count": len(transcripts)}

    # -- projections --------------------------------------------------------------

    def _thread_node(self, tid, cx=None):
        """A lean ThreadNode. An id present only on an edge (a dangling parent/child)
        has no threads-table row, so a bare node is synthesized for it. ``cx`` is the
        graph to read from — defaulting to the loaded corpus, but graph.at passes a
        time-travel snapshot so child_count/depth are computed as-of that moment."""
        cx = self.corpus if cx is None else cx
        meta = cx.threads.get(tid)
        if meta is None:
            meta = corpus.ThreadMeta(id=tid)
        node = {"id": meta.id, "title": _clean(meta.title),
                "provider": meta.model_provider, "created_at_ms": meta.created_at_ms,
                "child_count": cx.fan_out(tid),
                "depth": cx.depth(tid)}
        if meta.tokens_used:
            node["tokens"] = meta.tokens_used
        if meta.updated_at_ms is not None:
            node["updated_at_ms"] = meta.updated_at_ms
        if meta.git_branch:
            node["git_branch"] = meta.git_branch
        if meta.cwd:
            node["cwd"] = _clean(meta.cwd)
        if meta.agent_role:
            node["agent_role"] = meta.agent_role
        if meta.agent_nickname:
            node["agent_nickname"] = _clean(meta.agent_nickname)
        if meta.preview:
            node["preview"] = _clean(meta.preview)
        return node

    def _thread_meta(self, m):
        """The full ThreadMeta projection for thread.get. ``rollout_path`` (a local FS
        path) is withheld and surfaced only as ``has_rollout``."""
        return {"id": m.id, "title": _clean(m.title), "provider": m.model_provider,
                "tokens": m.tokens_used, "created_at_ms": m.created_at_ms,
                "updated_at_ms": m.updated_at_ms, "git_branch": m.git_branch,
                "cwd": _clean(m.cwd), "agent_role": m.agent_role,
                "agent_nickname": _clean(m.agent_nickname), "preview": _clean(m.preview),
                "child_count": self.corpus.fan_out(m.id),
                "depth": self.corpus.depth(m.id), "has_rollout": bool(m.rollout_path)}

    def _order_nodes(self, nodes, order):
        if order is None or order == "created":
            return sorted(nodes, key=lambda n: (
                n["created_at_ms"] is None, n["created_at_ms"] or 0, n["id"]))
        if order == "recent":
            return sorted(nodes, reverse=True, key=lambda n: (
                n.get("updated_at_ms") or n["created_at_ms"] or 0))
        if order == "title":
            return sorted(nodes, key=lambda n: n["title"].lower())
        raise RpcError(-32602, "unknown order: %s" % order)

    def _collect_subtree(self, tid, depth):
        order, seen, frontier = [], set(), [(tid, 0)]
        while frontier:
            node, level = frontier.pop(0)
            if node in seen:
                continue
            seen.add(node)
            order.append(node)
            if depth is None or level < depth:
                frontier.extend((c, level + 1) for c in self.corpus.children_of(node))
        return order

    def _collect_ancestors(self, tid):
        order, seen, frontier = [], {tid}, [tid]
        while frontier:
            node = frontier.pop(0)
            for parent in self._parents_of(node):
                if parent not in seen:
                    seen.add(parent)
                    order.append(parent)
                    frontier.append(parent)
        return order

    def _parents_of(self, tid):
        return [e.parent_thread_id for e in self.corpus.edges
                if e.child_thread_id == tid]

    def _diff_operand(self, params, path_key, time_key):
        """Resolve one side of graph.diff — three mutually-exclusive forms:

        * ``time_key`` present (e.g. ``as_of_a``) -> the time-travel snapshot
          ``timetravel.corpus_as_of(self.corpus, T)`` as-of that birth ms.
        * ``path_key`` present (e.g. ``old_index``) -> another on-disk corpus index that
          must already exist (a diff never creates a file). It is opened and read WITHOUT
          applying the schema DDL — ``load_corpus`` issues only SELECTs, so the operand's
          data is never modified — and the connection is closed before returning.
        * NEITHER present -> the corpus this sidecar already holds (``self.corpus``).

        Supplying BOTH a path and an as-of for one side is ambiguous -> -32602."""
        has_path = path_key in params
        has_time = time_key in params
        if has_path and has_time:
            raise RpcError(-32602, "%s and %s are mutually exclusive"
                           % (path_key, time_key))
        if has_time:
            return timetravel.corpus_as_of(self.corpus, _req_int(params, time_key))
        if not has_path:
            return self.corpus
        path = _req_str(params, path_key)
        if not os.path.isfile(path):
            raise RpcError(-32602, "%s not found: %s" % (path_key, path))
        conn = sqlite3.connect(path)
        try:
            conn.row_factory = sqlite3.Row
            return corpus.load_corpus(conn)
        finally:
            conn.close()

    def _project_diff(self, d):
        """A CorpusDiff -> its wire DTO. Node ids pass through as the graph's structural
        keys (as everywhere else on this surface); edges become ``{parent,child}`` objects
        exactly like graph.subtree; changed fields are privacy-projected. Every list is
        already sorted by ``diff_corpus``, so the DTO is byte-stable across runs."""
        return {"added_nodes": d.added_nodes,
                "removed_nodes": d.removed_nodes,
                "added_edges": [{"parent": p, "child": c} for p, c in d.added_edges],
                "removed_edges": [{"parent": p, "child": c} for p, c in d.removed_edges],
                "changed_nodes": self._project_changed(d.changed_nodes)}

    def _project_changed(self, changed):
        """``{id: {field: (old, new)}}`` -> the wire form ``{id: {field: [old, new]}}``, in
        the same sorted-id / declaration order ``diff_corpus`` produced. A changed
        ``rollout_path`` is reduced to its basename on both sides (the absolute FS layout
        never crosses the wire), and every value is run through the shared sanitizer so a
        hidden-unicode payload in a changed title/preview/cwd cannot be relayed onward."""
        out = {}
        for tid, field_diffs in changed.items():
            projected = {}
            for name, (old, new) in field_diffs.items():
                if name == "rollout_path":
                    old, new = os.path.basename(old), os.path.basename(new)
                projected[name] = [_sanitize_tree(old), _sanitize_tree(new)]
            out[tid] = projected
        return out

    def _project_snapshot(self, cx):
        """A time-travel Corpus snapshot -> the ``{nodes, edges}`` GraphSnapshot DTO.
        Nodes are every id in the snapshot (threads UNION edge endpoints) in sorted-id
        order, each a lean ThreadNode projected over the snapshot; edges are the
        snapshot's already-(parent,child)-sorted spawn edges, status preserved. Both are
        byte-stable, and each node's free text is sanitized by ``_thread_node``."""
        return {"nodes": [self._thread_node(tid, cx) for tid in sorted(cx._nodes())],
                "edges": [_spawn_edge(e) for e in cx.edges]}

    def _project_export_run(self, report):
        """export_with_gate's report -> the ExportResult DTO ``{ok, graph_gate,
        transcript_gate, written_path?}``. ``graph_gate`` is the structural round-trip
        verdict (no node/edge add/remove and no changed field); ``transcript_gate`` is
        the token-fidelity verdict (no missing tokens); ``written_path`` is present only
        on a successful write, and is sanitized like any wire string."""
        graph_gate = not (report["added"]["nodes"] or report["added"]["edges"]
                          or report["removed"]["nodes"] or report["removed"]["edges"]
                          or report["changed"])
        result = {"ok": report["ok"], "graph_gate": graph_gate,
                  "transcript_gate": not report["missing_tokens"]}
        if report["ok"]:
            result["written_path"] = _clean(report["path"])
        return result

    def _run_search(self, query, limit, offset, provider):
        """Mirror corpus.search's contentless-FTS JOIN, adding the offset/provider/total
        the cockpit needs (the library primitive offers only limit)."""
        where = "conversations_fts MATCH ?"
        args = [query]
        if provider is not None:
            where += " AND c.provider = ?"
            args.append(provider)
        frm = ("FROM conversations_fts JOIN conversations c "
               "ON c.rowid = conversations_fts.rowid WHERE " + where)
        total = self.conn.execute("SELECT COUNT(*) " + frm, args).fetchone()[0]
        rows = self.conn.execute(
            "SELECT c.conversation_id, c.thread_id, c.provider, c.title, c.created_at "
            + frm + " ORDER BY rank LIMIT ? OFFSET ?",
            args + [limit, offset]).fetchall()
        return total, rows

    def _conversation_stub(self, row, reason):
        """A documented, honest partial for a conversation whose transcript body is not
        retrievable in this bite (no rollout, missing/unreadable file, or non-Codex)."""
        return {"id": row["conversation_id"], "title": _clean(row["title"]),
                "provider": row["provider"], "created_at": row["created_at"],
                "updated_at": row["updated_at"], "account": row["account"],
                "turns": [], "available": False, "reason": reason}

    def _serialize_conversation(self, conv, errors):
        return {"id": conv.id, "title": _clean(conv.title), "provider": conv.provider,
                "created_at": conv.created_at, "updated_at": conv.updated_at,
                "account": conv.account, "ir_version": conv.ir_version,
                "available": True,
                "turns": [self._serialize_turn(t) for t in conv.turns],
                "meta": _sanitize_tree(_redact_paths(conv.meta)),
                "parse_errors": len(errors)}

    def _serialize_turn(self, turn):
        dto = {"role": turn.role, "uuid": turn.uuid, "timestamp": turn.timestamp,
               "blocks": [self._serialize_block(b) for b in turn.blocks]}
        if turn.branch is not None:
            dto["branch"] = turn.branch
        return dto

    def _serialize_block(self, block):
        return {"type": block.type, "text": _clean(block.text),
                "data": _sanitize_tree(block.data),
                "citations": _sanitize_tree(block.citations)}


# -------------------------------------------------------------------- entrypoint

def _parse_args(args):
    """Parse the sidecar CLI: an optional ``--index <path>`` that falls back to
    ``$AISR_INDEX`` then to no corpus. Kept tiny (no branches) so ``main`` stays
    fully covered by its four in-process tests."""
    parser = argparse.ArgumentParser(
        prog="aisr.sidecar",
        description="stdio NDJSON JSON-RPC 2.0 engine over an aisr corpus index")
    parser.add_argument(
        "--index", default=None,
        help="path to the corpus index (SQLite); falls back to $AISR_INDEX, then "
             "serves with no corpus attached (health.ping still answers)")
    return parser.parse_args(args)


def main(argv=None, stdin=None, stdout=None):
    """Open the index (from ``--index`` or $AISR_INDEX; None -> no corpus) and serve on
    the given streams (defaulting to the real stdio). This is the entrypoint the cockpit
    launches as ``python -m aisr.sidecar --index <path>``."""
    args = sys.argv[1:] if argv is None else list(argv)
    path = _parse_args(args).index or os.environ.get("AISR_INDEX")
    conn = corpus.open_index(path) if path else None
    try:
        Sidecar(conn).serve(stdin if stdin is not None else sys.stdin,
                            stdout if stdout is not None else sys.stdout)
    finally:
        if conn is not None:             # release the index on EOF/shutdown
            conn.close()
    return 0


if __name__ == "__main__":       # pragma: no cover
    sys.exit(main())
