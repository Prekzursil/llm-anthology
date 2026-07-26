"""Cockpit sidecar: a stdio NDJSON JSON-RPC 2.0 engine over an llm_anthology corpus index.

The Electron/UI cockpit talks to this process over stdin/stdout — ONE compact JSON
object per line, each ``\\n``-terminated and flushed (NOT HTTP, NOT a socket). Every
request is ``{jsonrpc:"2.0",id,method,params}``; every reply is either
``{...,result}`` or ``{...,error:{code,message,data?}}``. The loop is deliberately
dumb and unkillable: one malformed line, one unknown method, one busy database — none
of them may crash the server, they each become a typed error response.

Layering
--------
The RPC surface is built ON the Phase-1 corpus API (``llm_anthology.corpus``): the thread
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
                         ``as_of_b``, via ``llm_anthology.timetravel.corpus_as_of``), or — when
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
                         diamond-deduped and cycle-safe (see ``llm_anthology.rollup``). Counts and
                         graph shape only; no conversation text crosses.
* ``graph.timeline``   — FULL. ``{events, min_ms, max_ms, undated_count}`` — the sorted
                         distinct dated node births, their range, and the undated-NODE
                         count over the SAME node set graph.at views (threads UNION edge
                         endpoints, so a dangling endpoint counts as undated — matching the
                         frozen contract; see ``llm_anthology.timetravel.timeline``). Ints/None only.
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
                         gate-enforced by ``llm_anthology.export.export_with_gate``. This bite
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

* ``metadata.get`` / ``metadata.set`` / ``metadata.clear`` / ``metadata.search`` /
  ``metadata.tags`` — FULL. The app-owned annotation layer absorbed from
                         codex-session-manager: alias / tags / notes per conversation.
                         ``set`` is a PARTIAL update — an omitted field is left unchanged,
                         an explicit ``""`` (or ``[]``) clears it — because the cockpit
                         edits one field at a time and a per-field call must not blank its
                         siblings. ``search`` matches ANNOTATIONS ONLY (never message
                         bodies), ANDing ``text`` and ``tag``, and returns [] for a blank
                         query rather than the whole catalogue. These annotations are
                         LOCAL-ONLY: they are deliberately absent from
                         ``redact.MetadataView``, so they can never ride the cloud research
                         plane. ``metadata`` never opens a session file, so no annotation
                         write can mutate the owner's originals.

* ``dedup.scan`` / ``dedup.sessions`` — FULL. The Codex physical-copy -> logical-session
                         collapse absorbed from codex-session-manager. ``scan`` walks the
                         known stores under an EXPLICIT ``codex_home`` (required, and
                         refused if UNC or relative — a UNC root would emit outbound
                         SMB/NTLM), consolidates, persists, and returns counts including
                         ``flagged_truncated``. ``sessions`` returns the view, re-deriving
                         the canonical choice rather than trusting the stored flag. A VIEW,
                         never a delete: every copy stays listed in ``duplicate_paths``, and
                         ``has_larger_copy`` reports when the canonical copy is a truncated
                         prefix of a sibling it demoted.

* ``maintenance.plan`` / ``maintenance.execute`` / ``maintenance.restore`` /
  ``maintenance.runs`` — FULL, and the ONLY DESTRUCTIVE surface in the engine. ``plan`` is
                         PURE (it creates nothing, not even the checkpoint directory) and
                         returns a preview plus a SINGLE-USE HANDLE. ``execute`` runs the
                         handle: ``apply`` defaults to False, so the destructive act is
                         always an explicit second step, and it refuses without the exact
                         typed confirmation. ``restore`` rolls a checkpoint back (also
                         dry-run by default). ``runs`` is the audit ledger.
                         THE CLIENT NEVER SENDS A PREVIEW BACK — see the section comment on
                         the handlers. ``maintenance`` validates paths against the roots
                         carried inside the preview, which is sound in-process and unsound
                         the moment a preview can be rebuilt from client JSON, so the server
                         keeps its own preview object and the forged-preview class cannot be
                         expressed at all. Every caller-supplied root is additionally refused
                         at this edge if UNC (an outbound SMB/NTLM vector) or relative.

Privacy (HARD): every free-text field derived from user/model content — a title, a
preview, a search snippet, a transcript block, a tool payload — passes through
``llm_anthology.sanitize.sanitize_for_copy`` before crossing the wire, so a hidden-unicode
prompt-injection channel in the corpus cannot be relayed into the next agent. ``stats``
and ``graph.*`` are aggregate/metadata only; a full transcript crosses only for the one
conversation the user explicitly opened, and even then it is sanitized.

Research plane / ACL split (Phase-4, HARD). ``llm_anthology.research`` is a SEPARATE, corpus-blind
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

from llm_anthology import (
    __version__,
    corpus,
    dedup,
    diff,
    export,
    ir,
    maintenance,
    metadata as metadata_store,
    redact,
    research,
    rollup,
    sanitize,
    timetravel,
)
from llm_anthology.adapters import codex_rollout

# App-specific JSON-RPC error codes (standard codes -32700/-32600/-32601/-32602/-32603
# are used directly where they apply).
CORPUS_NOT_INDEXED = -32000
THREAD_NOT_FOUND = -32001
DB_BUSY = -32002
# A maintenance request the safety model REFUSED. Distinct from -32602: the params were
# well-formed, the operation was declined (unconfirmed, out of the store root, a plan that
# collides with itself, a stale plan). A client must be able to tell those apart.
MAINTENANCE_REFUSED = -32003

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


def _reject_nonlocal_path(path, label):
    """Guard ANY caller-supplied filesystem path BEFORE any filesystem access: reject a
    UNC / network path (``\\\\host\\share`` — a crafted target coerces an outbound SMB/NTLM
    auth, the Windows hash-leak class) and any non-absolute path. Drive-absolute local
    paths only.

    Labelled so every path-bearing method reports in its own terms; the concrete modules
    (``export``, ``maintenance``) then re-check UNC + parent-traversal + confinement as
    defence in depth, because a guard that lives only at the RPC edge is one refactor away
    from being bypassed."""
    if path.replace("/", "\\").startswith("\\\\"):
        raise RpcError(-32602, "%s must be a local path, not a UNC/network path" % label)
    if not os.path.isabs(path):
        raise RpcError(-32602, "%s must be an absolute local path" % label)


def _reject_nonlocal_dest(dest_path):
    """The export-destination spelling of :func:`_reject_nonlocal_path`."""
    _reject_nonlocal_path(dest_path, "dest_path")


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
        # The metadata layer owns its OWN table, created idempotently here rather than in
        # corpus.py's schema, so the annotation store is additive over any existing index.
        if conn is not None:
            metadata_store.ensure_schema(conn)
            dedup.ensure_schema(conn)
            maintenance.ensure_schema(conn)
        # Previews the SERVER produced, held by handle. See _maintenance_plan for why the
        # client is never allowed to hand a preview back.
        self._plans = {}
        self._next_plan_id = 1
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
            "metadata.get": self._metadata_get,
            "metadata.set": self._metadata_set,
            "metadata.clear": self._metadata_clear,
            "metadata.search": self._metadata_search,
            "metadata.tags": self._metadata_tags,
            "dedup.scan": self._dedup_scan,
            "dedup.sessions": self._dedup_sessions,
            "maintenance.plan": self._maintenance_plan,
            "maintenance.execute": self._maintenance_execute,
            "maintenance.restore": self._maintenance_restore,
            "maintenance.runs": self._maintenance_runs,
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
        undated_count}`` from ``llm_anthology.timetravel.timeline`` (sorted distinct dated births,
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
        directory; ``llm_anthology.export.export_with_gate`` then enforces the structural
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

    # -- metadata (the absorbed csm annotation layer) ------------------------------
    #
    # LOCAL-ONLY BY DESIGN. Alias/tags/notes are free text the owner authored, so they are
    # deliberately absent from `redact.MetadataView` and can never ride the cloud research
    # plane (see redact.py's docstring). They cross only this stdio wire, to the UI.
    # `metadata` never opens a session file, so none of these methods can mutate the
    # owner's originals.

    def _annotation(self, meta):
        """One `metadata.Metadata` as a wire dict. Free text is sanitized on the way OUT as
        well as in, exactly like every other text-bearing method here — an annotation is
        still attacker-influenced if it was pasted from a conversation."""
        return {
            "conversation_id": meta.conversation_id,
            "alias": _clean(meta.alias),
            "tags": [_clean(t) for t in meta.tags],
            "notes": _clean(meta.notes),
            "is_empty": meta.is_empty,
        }

    def _metadata_get(self, params):
        """Annotations for one conversation. Un-annotated reads back as an EMPTY annotation
        (``is_empty`` true), never an error, so the UI can render unconditionally."""
        self._require_corpus()
        cid = _req_str(params, "conversation_id")
        return self._annotation(metadata_store.get_metadata(self.conn, cid))

    def _metadata_set(self, params):
        """Partial update: an OMITTED field is left unchanged, an explicit "" (or []) clears
        it. That distinction is the whole point — the cockpit edits one field at a time and
        a per-field call must not silently blank the other two."""
        self._require_corpus()
        cid = _req_str(params, "conversation_id")
        tags = params.get("tags")
        if tags is not None and not isinstance(tags, list):
            raise RpcError(-32602, "tags must be a list of strings")
        alias, notes = params.get("alias"), params.get("notes")
        for name, value in (("alias", alias), ("notes", notes)):
            if value is not None and not isinstance(value, str):
                raise RpcError(-32602, "%s must be a string" % name)
        # metadata._check_id raises ValueError for a key that cannot be stored; surface it
        # as an RPC param error rather than letting it escape as an internal fault.
        try:
            meta = metadata_store.set_metadata(
                self.conn, cid, alias=alias, tags=tags, notes=notes)
        except ValueError as exc:
            raise RpcError(-32602, str(exc)) from exc
        return self._annotation(meta)

    def _metadata_clear(self, params):
        """Drop the whole annotation. An absent row is a silent no-op, mirroring csm."""
        self._require_corpus()
        cid = _req_str(params, "conversation_id")
        try:
            return self._annotation(metadata_store.clear_metadata(self.conn, cid))
        except ValueError as exc:
            raise RpcError(-32602, str(exc)) from exc

    def _metadata_search(self, params):
        """Search ANNOTATIONS (never message bodies) by free text and/or tag, ANDed, joined
        to the display columns the listing needs. With neither filter the result is empty —
        a blank query must not dump the whole catalogue into the UI."""
        self._require_corpus()
        text, tag = params.get("text", ""), params.get("tag", "")
        for name, value in (("text", text), ("tag", tag)):
            if not isinstance(value, str):
                raise RpcError(-32602, "%s must be a string" % name)
        rows = metadata_store.search_conversations(self.conn, text=text, tag=tag)
        # Column order is fixed by metadata.search_conversations' SELECT; sqlite3.Row
        # supports positional access, so this works with or without a row_factory.
        return [
            {
                "conversation_id": r[0],
                "provider": r[1],
                "account": r[2],
                "title": _clean(r[3]),
                "created_at": r[4],
                "updated_at": r[5],
                "turn_count": r[6],
                "thread_id": r[7],
                "annotation": self._annotation(
                    metadata_store.get_metadata(self.conn, r[0])),
            }
            for r in rows
        ]

    def _metadata_tags(self, params):
        """The tag facet: tag -> conversation count, case-collapsed and deterministically
        ordered by the store."""
        self._require_corpus()
        return [{"tag": _clean(tag), "count": count}
                for tag, count in metadata_store.tag_counts(self.conn).items()]

    # -- dedup (Codex physical copies -> one logical session) ----------------------
    #
    # A VIEW, never a delete: `dedup` contains no write/delete/move call, so nothing here
    # can remove one of the owner's files. The paths it returns are LOCAL filesystem paths
    # (a rollout_path embeds the owner's username), so they travel only over this stdio
    # wire to the UI and are absent from `redact.MetadataView`.

    def _dedup_session(self, session):
        """One `dedup.LogicalSession` as a wire dict.

        ``has_larger_copy`` is deliberately on the wire: the canonical rule prefers the LIVE
        store over a mirror, which is correct, but that means a crash-truncated live rollout
        can outrank a complete backup of the same session. Nothing is lost on disk, yet the
        view would show the shorter conversation — so the condition is REPORTED and the UI
        can offer the fuller copy."""
        canonical = session.canonical
        return {
            "session_id": session.session_id,
            "canonical_path": canonical.file_path,
            "store_kind": canonical.store_kind,
            "size_bytes": canonical.size_bytes,
            "last_write_ms": canonical.last_write_ms,
            "copy_count": session.copy_count,
            "duplicate_paths": list(session.duplicate_paths),
            "is_identified": session.is_identified,
            "has_larger_copy": session.has_larger_copy,
        }

    def _dedup_scan(self, params):
        """Scan the known Codex stores under an EXPLICIT ``codex_home``, consolidate, persist.

        ``codex_home`` is REQUIRED and never defaulted. That is a deliberate safety choice,
        not ceremony: ``loaders.load_corpus`` with no ``codex_home`` falls back to the LIVE
        Codex store, and an automated probe really did read the owner's real sessions that
        way. A scan of private data must be something the caller asked for by name."""
        self._require_corpus()
        home = _req_str(params, "codex_home")
        _reject_nonlocal_path(home, "codex_home")
        copies, errors = dedup.scan_stores(dedup.known_store_roots(home))
        sessions = dedup.consolidate(copies)
        dedup.save_sessions(self.conn, sessions)
        self.conn.commit()
        return {
            "session_count": len(sessions),
            "copy_count": sum(s.copy_count for s in sessions),
            "duplicate_count": sum(s.copy_count - 1 for s in sessions),
            "flagged_truncated": sum(1 for s in sessions if s.has_larger_copy),
            "unidentified": sum(1 for s in sessions if not s.is_identified),
            "errors": [str(e) for e in errors],
        }

    def _dedup_sessions(self, params):
        """The persisted dedup view. ``load_sessions`` re-derives the canonical choice rather
        than trusting the stored flag, so the rule has exactly one implementation."""
        self._require_corpus()
        return [self._dedup_session(s) for s in dedup.load_sessions(self.conn)]

    # -- maintenance (the ONLY destructive surface) ---------------------------------
    #
    # WHY THE CLIENT NEVER SENDS A PREVIEW BACK. `maintenance` validates paths against the
    # roots carried INSIDE the preview it is given. That is sound while a preview can only
    # be produced in-process, and it is exactly what breaks if an RPC layer rebuilds one
    # from client JSON: a forged preview could name its own store/checkpoint/destination
    # root and the executor would honour them. So `plan` keeps the SERVER's own preview
    # object under a single-use handle and `execute` runs that object. The forged-preview
    # class is removed structurally rather than defended against.
    #
    # The engine still re-checks everything on its own (a poisoned root is refused, a plan
    # source outside `preview.allowed` is refused, every path is re-confined), because a
    # guard that lives only at the RPC edge is one refactor from being bypassed.

    @staticmethod
    def _copy_dto(copy):
        return {"session_id": copy.session_id, "file_path": copy.file_path,
                "store_kind": copy.store_kind.value, "last_write_ms": copy.last_write_ms,
                "size_bytes": copy.size_bytes, "is_hot": copy.is_hot}

    def _preview_dto(self, plan_id, preview):
        return {
            "plan_id": plan_id,
            "action": preview.action.value,
            "store_root": preview.store_root,
            "destination_root": preview.destination_root,
            "checkpoint_root": preview.checkpoint_root,
            "allowed": [self._copy_dto(t) for t in preview.allowed],
            "blocked": [{"target": self._copy_dto(b.target), "reason": b.reason,
                         "detail": _clean(b.detail)} for b in preview.blocked],
            "warnings": [{"severity": int(w.severity), "severity_name": w.severity.name,
                          "message": _clean(w.message)} for w in preview.warnings],
            "plan": [{"session_id": m.session_id, "source": m.source,
                      "destination": m.destination} for m in preview.plan],
            "requires_checkpoint": preview.requires_checkpoint,
            "requires_typed_confirmation": preview.requires_typed_confirmation,
            "required_typed_confirmation": preview.required_typed_confirmation,
        }

    @staticmethod
    def _result_dto(result):
        return {
            "executed": result.executed,
            "manifest_path": result.manifest_path,
            "moves": [{"session_id": m.session_id, "source": m.source,
                       "destination": m.destination} for m in result.moves],
            "unaccounted": list(result.unaccounted),
        }

    @staticmethod
    def _maintenance_call(fn, *args, **kwargs):
        """Run an engine call, mapping its refusals onto RPC codes.

        MaintenancePathError subclasses BOTH MaintenanceRefused and ValueError, so it is
        caught FIRST and reported as a param error; a plain refusal is a well-formed request
        the safety model declined, which is a different thing and gets its own code."""
        try:
            return fn(*args, **kwargs)
        except maintenance.MaintenancePathError as exc:
            raise RpcError(-32602, str(exc)) from exc
        except maintenance.MaintenanceRefused as exc:
            raise RpcError(MAINTENANCE_REFUSED, str(exc)) from exc

    def _maintenance_plan(self, params):
        """PURE. Build a preview and hold it under a single-use handle; no filesystem
        mutation happens here. Every caller-supplied root is refused if UNC or relative
        before it reaches the engine."""
        self._require_corpus()
        store_root = _req_str(params, "store_root")
        checkpoint_root = _req_str(params, "checkpoint_root")
        destination_root = params.get("destination_root", "")
        if not isinstance(destination_root, str):
            raise RpcError(-32602, "destination_root must be a string")
        for label, value in (("store_root", store_root),
                             ("checkpoint_root", checkpoint_root)):
            _reject_nonlocal_path(value, label)
        if destination_root:
            _reject_nonlocal_path(destination_root, "destination_root")

        raw_action = _req_str(params, "action")
        try:
            action = maintenance.MaintenanceAction(raw_action)
        except ValueError as exc:
            raise RpcError(-32602, "action must be one of %s" % ", ".join(
                a.value for a in maintenance.MaintenanceAction)) from exc

        raw_targets = params.get("targets")
        if not isinstance(raw_targets, list) or not raw_targets:
            raise RpcError(-32602, "targets must be a non-empty list")
        targets = []
        for item in raw_targets:
            if not isinstance(item, dict):
                raise RpcError(-32602, "each target must be an object")
            file_path = item.get("file_path")
            if not isinstance(file_path, str) or not file_path:
                raise RpcError(-32602, "each target needs a non-empty file_path")
            targets.append(maintenance.SessionCopy(
                session_id=str(item.get("session_id", "")),
                file_path=file_path,
                store_kind=maintenance.SessionStoreKind.UNKNOWN,
                size_bytes=item.get("size_bytes", 0) or 0))

        request = maintenance.MaintenanceRequest(
            action=action, targets=tuple(targets), store_root=store_root,
            checkpoint_root=checkpoint_root, destination_root=destination_root)
        preview = self._maintenance_call(maintenance.plan_maintenance, request)

        plan_id = "plan-%d" % self._next_plan_id
        self._next_plan_id += 1
        self._plans[plan_id] = preview
        return self._preview_dto(plan_id, preview)

    def _maintenance_execute(self, params):
        """Run a handle the server issued. ``apply`` defaults to False, so the destructive
        act is always an explicit second step; the handle is consumed either way so a plan
        can never be replayed."""
        self._require_corpus()
        plan_id = _req_str(params, "plan_id")
        confirmation = params.get("confirmation", "")
        if not isinstance(confirmation, str):
            raise RpcError(-32602, "confirmation must be a string")
        apply_it = params.get("apply", False)
        if not isinstance(apply_it, bool):
            raise RpcError(-32602, "apply must be a boolean")
        if plan_id not in self._plans:
            raise RpcError(MAINTENANCE_REFUSED,
                           "unknown or already-used plan_id %r; re-plan" % plan_id)
        preview = self._plans[plan_id]
        result = self._maintenance_call(
            maintenance.execute_maintenance, preview, confirmation, apply=apply_it)
        # Consumed only once the engine ACCEPTED it: a refused confirmation must be
        # correctable without forcing a re-plan, while a completed run cannot be replayed.
        del self._plans[plan_id]
        if result.executed and result.manifest_path:
            maintenance.record_run(self.conn, result.manifest_path)   # commits internally
        return self._result_dto(result)

    def _maintenance_restore(self, params):
        """Roll a checkpoint back. ``apply`` defaults to False here too, so a caller can see
        what a restore would do before doing it."""
        self._require_corpus()
        manifest_path = _req_str(params, "manifest_path")
        _reject_nonlocal_path(manifest_path, "manifest_path")
        apply_it = params.get("apply", False)
        if not isinstance(apply_it, bool):
            raise RpcError(-32602, "apply must be a boolean")
        skip = params.get("skip_unaccounted", False)
        if not isinstance(skip, bool):
            raise RpcError(-32602, "skip_unaccounted must be a boolean")
        result = self._maintenance_call(
            maintenance.restore_checkpoint, manifest_path, apply=apply_it,
            skip_unaccounted=skip)
        return self._result_dto(result)

    def _maintenance_runs(self, params):
        """The recorded destructive runs, newest first — the audit trail a UI shows."""
        self._require_corpus()
        limit = _opt_int(params, "limit", 50)
        return [dict(row) for row in maintenance.list_runs(self.conn, limit=limit)]

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
    ``$LLM_ANTHOLOGY_INDEX`` then to no corpus. Kept tiny (no branches) so ``main`` stays
    fully covered by its four in-process tests."""
    parser = argparse.ArgumentParser(
        prog="llm_anthology.sidecar",
        description="stdio NDJSON JSON-RPC 2.0 engine over an llm_anthology corpus index")
    parser.add_argument(
        "--index", default=None,
        help="path to the corpus index (SQLite); falls back to $LLM_ANTHOLOGY_INDEX, then "
             "serves with no corpus attached (health.ping still answers)")
    return parser.parse_args(args)


def main(argv=None, stdin=None, stdout=None):
    """Open the index (from ``--index`` or $LLM_ANTHOLOGY_INDEX; None -> no corpus) and serve on
    the given streams (defaulting to the real stdio). This is the entrypoint the cockpit
    launches as ``python -m llm_anthology.sidecar --index <path>``."""
    args = sys.argv[1:] if argv is None else list(argv)
    path = _parse_args(args).index or os.environ.get("LLM_ANTHOLOGY_INDEX")
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
