"""Corpus: the IR extension the cockpit consumes.

`llm_anthology.ir` is conversation-centric — Conversation -> Turn -> Block. That is the whole
world for the HTML/Markdown renderers, but the cockpit also needs the SPAWN GRAPH the
Codex rollout logs carry: which thread spawned which, and the per-thread metadata
(model, tokens, branch, cwd, agent role) that the conversation IR never modelled. This
module ADDS that layer without touching ir.py:

  * ThreadMeta  — one spawn-graph node (a Codex rollout thread).
  * SpawnEdge   — one directed parent -> child spawn.
  * Corpus      — a bag of ir.Conversation PLUS threads{id: ThreadMeta} and edges[],
                  with the four graph helpers (roots / children_of / depth / fan_out)
                  that render the 895-edge spawn tree.

It also owns the ON-DISK contract the parallel build agents write into: INDEX_SCHEMA
(a contentless FTS5 table over conversation records + a threads table + a
thread_spawn_edges table + a resumable ingest_checkpoint table, WAL) and the thin
row<->dataclass mapping (upsert_thread / upsert_edge / add_conversation / search /
set_checkpoint / get_checkpoint / load_corpus). Defining that mapping ONCE, next to
the dataclasses and the DDL, is what keeps the fan-out of ingest agents from drifting
into N incompatible INSERTs.

Phase-0 MEASURED facts this contract is built around (ground truth, not guesses):
  * corpus = 2,249,530 records over 13,711 files — the FTS index is sized for millions,
    so it is contentless (content='') with detail=none: the pair measured at p95 33ms,
    ~6x under budget. The searchable text lives only in the inverted index; the
    displayable columns live in a plain `conversations` table joined by rowid.
  * the LIVE state DB has updated_at_ms but a legacy canonical copy does NOT, so
    ThreadMeta.updated_at_ms is OPTIONAL and its column is NULLABLE — a schema-tolerant
    adapter leaves it None rather than inventing a value.
  * the build is resumable/idempotent: ingest_checkpoint(file, offset, content_hash)
    lets an interrupted run skip unchanged files and re-ingest changed ones, and the
    upserts here are all INSERT-OR-REPLACE / dedup-on-conflict so a replay is a no-op.

PRIVACY: this module reads counts and shapes and moves opaque text into an index; it
never emits conversation content. Tests use synthetic fixtures only.
"""
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

CORPUS_VERSION = 1


# --------------------------------------------------------------- dataclasses

@dataclass
class ThreadMeta:
    """One spawn-graph node. `id` is the only required field; the rest default so a
    partial row from an older/leaner schema still constructs. created_at_ms and
    updated_at_ms are Optional[int]: the legacy canonical DB has no updated_at_ms
    column, so it stays None rather than being back-filled with a fabricated 0."""
    id: str
    title: str = ""
    model_provider: str = ""
    tokens_used: int = 0
    created_at_ms: Optional[int] = None
    updated_at_ms: Optional[int] = None
    git_branch: str = ""
    cwd: str = ""
    agent_role: str = ""
    agent_nickname: str = ""
    preview: str = ""
    rollout_path: str = ""
    #: The ADAPTER that produced this thread ("codex", "grok", "claude-code", ...) — the
    #: same fact `conversations.provider` records, and what the wire calls `provider`.
    #:
    #: DERIVED at load time from that column, not stored on `threads` — the join already
    #: carries the fact, so this needs no migration and works on indexes built before it
    #: existed. It is therefore LAST in the field order and absent from `_THREAD_COLS`,
    #: which keeps `ThreadMeta(*row)` positional construction valid.
    #:
    #: Distinct from `model_provider`, the MODEL VENDOR: measured over 250 real Codex
    #: rollouts, `session_meta.model_provider` is 'openai' 92.8% of the time and absent
    #: for the rest — never "codex". Conflating the two made every Codex node render as
    #: the palette's "unknown" grey.
    #:
    #: NOT named `source`: the LIVE Codex state DB already has a `threads.source` column
    #: meaning how the session was launched ("cli"), which `codex_state._THREAD_FIELD_MAP`
    #: deliberately leaves unmapped. Reusing that name would invite someone to wire "cli"
    #: in here, and a test asserts that column stays unmapped.
    adapter: str = ""


@dataclass
class SpawnEdge:
    """One directed spawn: `parent_thread_id` spawned `child_thread_id`. `status` is
    the spawn/child outcome (e.g. completed / failed) and may be empty."""
    parent_thread_id: str
    child_thread_id: str
    status: str = ""


@dataclass
class Corpus:
    """The whole cockpit view: the existing conversation IR plus the thread graph.

    conversations comes from the existing loaders (unchanged); threads and edges come
    from the Codex rollout adapter. The graph helpers answer only from `edges`, and
    treat any id that appears on an edge as a node even if it is absent from `threads`
    — a dangling parent still roots its visible subtree instead of disappearing.
    """
    conversations: list = field(default_factory=list)   # list[ir.Conversation]
    threads: dict = field(default_factory=dict)          # id -> ThreadMeta
    edges: list = field(default_factory=list)            # list[SpawnEdge]

    # -- builders -------------------------------------------------------------

    def add_thread(self, meta):
        self.threads[meta.id] = meta
        return meta

    def add_edge(self, edge):
        self.edges.append(edge)
        return edge

    # -- graph ----------------------------------------------------------------

    def _nodes(self):
        """Every id in the graph: the threads table UNION every id an edge names."""
        nodes = set(self.threads)
        for e in self.edges:
            nodes.add(e.parent_thread_id)
            nodes.add(e.child_thread_id)
        return nodes

    def _parents_of(self, tid):
        return [e.parent_thread_id for e in self.edges if e.child_thread_id == tid]

    def roots(self):
        """Nodes with no incoming spawn, sorted for deterministic rendering."""
        children = {e.child_thread_id for e in self.edges}
        return sorted(n for n in self._nodes() if n not in children)

    def children_of(self, tid):
        """The distinct children `tid` spawned, sorted."""
        return sorted({e.child_thread_id for e in self.edges
                       if e.parent_thread_id == tid})

    def fan_out(self, tid):
        """Out-degree: how many distinct threads `tid` spawned."""
        return len(self.children_of(tid))

    def depth(self, tid):
        """Distance to the nearest root (a root is 0). Uses the SHORTEST parent path
        so a re-parented/joined node reports its shallowest position, and terminates
        on cyclic/corrupt data instead of recursing forever."""
        return self._depth(tid, frozenset())

    def _depth(self, tid, seen):
        parents = self._parents_of(tid)
        if not parents:
            return 0
        if tid in seen:            # a back-edge: stop, contribute no further depth
            return 0
        seen = seen | {tid}
        return 1 + min(self._depth(p, seen) for p in parents)


# ------------------------------------------------------------- on-disk schema

# Column orders are pinned as constants and reused by every read/write below, so the
# INSERT lists, the SELECT lists and the dataclass field order can never drift apart.
_THREAD_COLS = ("id", "title", "model_provider", "tokens_used", "created_at_ms",
                "updated_at_ms", "git_branch", "cwd", "agent_role", "agent_nickname",
                "preview", "rollout_path")
_EDGE_COLS = ("parent_thread_id", "child_thread_id", "status")
_CONV_COLS = ("conversation_id", "provider", "account", "title", "created_at",
              "updated_at", "turn_count", "char_count", "thread_id", "rollout_path")

# The structural DDL (tables + indexes). WAL is applied separately in init_index:
# `PRAGMA journal_mode=WAL` cannot portably run inside executescript's transaction on
# every supported Python, whereas synchronous can, so it stays here.
INDEX_SCHEMA = """
PRAGMA synchronous=NORMAL;

-- Retrievable per-conversation record. The FTS table is contentless, so the columns
-- a result needs for display live HERE; the FTS rowid equals conversations.rowid.
CREATE TABLE IF NOT EXISTS conversations (
    rowid           INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL UNIQUE,
    provider        TEXT NOT NULL DEFAULT '',
    account         TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT '',
    turn_count      INTEGER NOT NULL DEFAULT 0,
    char_count      INTEGER NOT NULL DEFAULT 0,
    thread_id       TEXT NOT NULL DEFAULT '',
    rollout_path    TEXT NOT NULL DEFAULT ''
);

-- Contentless (content='') FTS5 over the searchable text of each conversation.
-- detail=none drops per-column/position data; the pair was measured at p95 33ms over
-- the 2.2M-record corpus, ~6x under the 200ms budget. Nothing is stored here to
-- retrieve — a MATCH yields rowids that join back to `conversations` for display.
CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
    title,
    body,
    content='',
    detail=none%(delete_opt)s
);

-- One row per Codex rollout thread (the spawn-graph nodes). updated_at_ms is
-- NULLABLE: the legacy canonical DB has no such column, so a schema-tolerant adapter
-- leaves it NULL rather than fabricating a value.
CREATE TABLE IF NOT EXISTS threads (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL DEFAULT '',
    model_provider TEXT NOT NULL DEFAULT '',
    tokens_used    INTEGER NOT NULL DEFAULT 0,
    created_at_ms  INTEGER,
    updated_at_ms  INTEGER,
    git_branch     TEXT NOT NULL DEFAULT '',
    cwd            TEXT NOT NULL DEFAULT '',
    agent_role     TEXT NOT NULL DEFAULT '',
    agent_nickname TEXT NOT NULL DEFAULT '',
    preview        TEXT NOT NULL DEFAULT '',
    rollout_path   TEXT NOT NULL DEFAULT ''
);

-- Directed spawn edges (parent spawned child). 895 edges over 1831 threads in the
-- measured corpus. The (parent, child) PRIMARY KEY dedupes a re-ingested edge so a
-- resumed build is idempotent.
CREATE TABLE IF NOT EXISTS thread_spawn_edges (
    parent_thread_id TEXT NOT NULL,
    child_thread_id  TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (parent_thread_id, child_thread_id)
);

-- Resumable/idempotent ingest bookkeeping: how far into each source file the builder
-- got, plus a content hash so an unchanged file is skipped and a changed one is
-- re-ingested from the recorded offset.
CREATE TABLE IF NOT EXISTS ingest_checkpoint (
    file         TEXT PRIMARY KEY,
    "offset"     INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT ''
);

-- Spawn-tree walk helpers.
CREATE INDEX IF NOT EXISTS idx_edges_parent ON thread_spawn_edges(parent_thread_id);
CREATE INDEX IF NOT EXISTS idx_edges_child  ON thread_spawn_edges(child_thread_id);
CREATE INDEX IF NOT EXISTS idx_threads_updated ON threads(updated_at_ms);
"""


# `contentless_delete=1` lets a contentless FTS5 table retract a rowid's postings, which is
# what makes re-indexing a GROWN session possible without leaving its old text matchable.
# It landed in SQLite 3.43 and is measured working alongside `detail=none` here. On an older
# build the option is simply omitted and `add_conversation` falls back to re-inserting under
# a fresh rowid — correct, but the stale posting is orphaned rather than reclaimed.
_CONTENTLESS_DELETE = sqlite3.sqlite_version_info >= (3, 43)


def init_index(conn):
    """Apply the schema (and WAL) to an existing connection; return it. Idempotent —
    every statement is IF NOT EXISTS, so re-running against a live index is a no-op.

    An index created before `contentless_delete` was added keeps its original FTS table:
    the option cannot be set on an existing virtual table, and `IF NOT EXISTS` deliberately
    leaves it alone rather than silently dropping a user's index to rebuild it."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(INDEX_SCHEMA % {
        "delete_opt": (",\n    contentless_delete=1"\
                       if _CONTENTLESS_DELETE else ""),
    })
    return conn


def open_index(path):
    """Open (creating if absent) the on-disk index at `path`, apply the schema, and
    return a connection whose rows come back as sqlite3.Row so callers read by name."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return init_index(conn)


# ------------------------------------------------------------- write contract

def _placeholders(cols):
    return ",".join("?" for _ in cols)


def _quoted(cols):
    return ",".join('"%s"' % c for c in cols)


def upsert_thread(conn, meta):
    """Write (or replace) one thread row from a ThreadMeta."""
    conn.execute("INSERT OR REPLACE INTO threads(%s) VALUES (%s)"
                 % (_quoted(_THREAD_COLS), _placeholders(_THREAD_COLS)),
                 tuple(getattr(meta, c) for c in _THREAD_COLS))


def upsert_edge(conn, edge):
    """Write (or replace) one spawn edge; the (parent, child) PK makes replays a no-op."""
    conn.execute("INSERT OR REPLACE INTO thread_spawn_edges(%s) VALUES (%s)"
                 % (_quoted(_EDGE_COLS), _placeholders(_EDGE_COLS)),
                 tuple(getattr(edge, c) for c in _EDGE_COLS))


def _fts_can_delete(conn):
    """Was `conversations_fts` created with `contentless_delete=1`?

    A contentless FTS5 table rejects DELETE outright unless that option is set (SQLite
    >= 3.43). It is read off the stored DDL rather than probed by attempting a delete, so a
    failed statement never has to be swallowed mid-transaction. Indexes created BEFORE this
    option was added will answer False forever — the option cannot be set on an existing
    table — which is exactly why `add_conversation` keeps a second path.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='conversations_fts'"
    ).fetchone()
    return bool(row) and "contentless_delete" in (row[0] or "")


def add_conversation(conn, conv, body=None, thread_id="", rollout_path=""):
    """Index one ir.Conversation and return its rowid.

    Idempotent by conversation_id, and RE-INDEXES a conversation whose content changed.

    THE BUG THIS FIXES. This used to early-return the existing rowid and touch nothing, so a
    session that GREW after it was first indexed — a live session, which is the designed flow
    — never became searchable. The new turns reached no index, `turn_count` and `char_count`
    stayed frozen, the ingest reported zero errors, and the checkpoint still advanced, so
    re-running the build did not repair it. With `corpus.create` refusing to clobber and no
    reindex RPC, the only repair a user had was deleting the .sqlite by hand. The same
    early-return stranded `rollout_path` when Codex moved a finished session into
    `archived_sessions/`, so the reader reported "rollout unavailable" for a file that exists.

    RETRACTING THE OLD TERMS is the hard half. The FTS table is contentless, so its postings
    cannot be deleted unless it was created with `contentless_delete=1`. Two paths, and both
    must leave no stale text matchable:

      * a CURRENT index deletes the old posting and updates the row in place, keeping its
        rowid stable;
      * a LEGACY index (created before this option) cannot delete, so the row is re-inserted
        under a fresh rowid and the stale posting is left orphaned. That is invisible to
        search because every query INNER JOINs `conversations` on rowid — including the
        `total` count, which shares the same FROM clause — but the index does grow, and only
        a rebuild reclaims it.

    An UNCHANGED conversation is still a genuine no-op: same rowid returned, no write, no
    index churn on a resumed ingest.

    `body` defaults to the title plus every block's text; pass an explicit body to index a
    sanitized or truncated form instead.
    """
    if body is None:
        body = _conversation_body(conv)
    values = (conv.id, conv.provider, conv.account, conv.title, conv.created_at,
              conv.updated_at, len(conv.turns), len(body), thread_id, rollout_path)
    existing = conn.execute(
        "SELECT rowid, %s FROM conversations WHERE conversation_id=?" % ",".join(_CONV_COLS),
        (conv.id,)).fetchone()

    if existing is not None:
        # Positional, not by name: `row_factory` is not guaranteed to be `sqlite3.Row` here
        # (several callers hand in a bare connection), and `existing["rowid"]` would raise
        # on a plain tuple. The SELECT above pins the order as rowid followed by _CONV_COLS.
        rowid, stored = existing[0], tuple(existing[1:])
        if _fts_can_delete(conn):
            # Re-index unconditionally. No fingerprint is consulted, because the only one
            # available is the stored column tuple and `char_count` is the sole signal in it
            # that tracks the body — so an edit of the SAME LENGTH reads as unchanged. That
            # is not hypothetical: the first version of this compared the tuple, and a test
            # swapping "alpha" for "bravo" (both five characters) silently skipped the
            # re-index. Rewriting a row that did not change is cheap; missing one that did
            # is the bug this whole function exists to fix.
            conn.execute("DELETE FROM conversations_fts WHERE rowid=?", (rowid,))
            conn.execute(
                "UPDATE conversations SET %s WHERE rowid=?"
                % ",".join("%s=?" % c for c in _CONV_COLS), values + (rowid,))
            conn.execute("INSERT INTO conversations_fts(rowid, title, body) VALUES (?,?,?)",
                         (rowid, conv.title, body))
            return rowid

        # LEGACY index: the old posting cannot be retracted at all, so the row has to move to
        # a rowid the stale posting is not attached to. Here the fingerprint IS consulted,
        # because every move orphans another posting and an unconditional re-index would
        # grow the index on every rebuild.
        #
        # RESIDUAL, stated rather than hidden: on a legacy index a body edit of exactly the
        # same length, with the same turn count and title, is indistinguishable from no
        # change and will be skipped. Append-only rollout logs do not produce that shape --
        # a session that grew is longer -- so it is not reachable by the real ingest path.
        # Settling experiment: add a `body_hash` column and compare on it, which needs a
        # schema migration that only helps indexes that must be rebuilt for other reasons.
        if stored == values:
            return rowid
        # `MAX(rowid) + 1` is strictly greater than every LIVE row, so the new rowid was
        # never used by a row that is still present. A plain DELETE-then-INSERT would not
        # do: SQLite reuses the freed rowid when it was the highest, which re-attaches the
        # stale posting to the new row and turns a missing hit into a FALSE one.
        moved = conn.execute("SELECT MAX(rowid) + 1 FROM conversations").fetchone()[0]
        conn.execute("UPDATE conversations SET rowid=? WHERE rowid=?", (moved, rowid))
        conn.execute(
            "UPDATE conversations SET %s WHERE rowid=?"
            % ",".join("%s=?" % c for c in _CONV_COLS), values + (moved,))
        conn.execute("INSERT INTO conversations_fts(rowid, title, body) VALUES (?,?,?)",
                     (moved, conv.title, body))
        return moved

    cur = conn.execute(
        "INSERT INTO conversations(%s) VALUES (%s)"
        % (",".join(_CONV_COLS), _placeholders(_CONV_COLS)), values)
    conn.execute("INSERT INTO conversations_fts(rowid, title, body) VALUES (?,?,?)",
                 (cur.lastrowid, conv.title, body))
    return cur.lastrowid


def set_checkpoint(conn, file, offset, content_hash):
    """Record ingest progress for `file`; re-recording the same file replaces it."""
    conn.execute(
        'INSERT OR REPLACE INTO ingest_checkpoint(file, "offset", content_hash) '
        "VALUES (?,?,?)", (file, offset, content_hash))


# -------------------------------------------------------------- read contract

def search(conn, query, limit=50):
    """FTS MATCH -> up to `limit` conversation rows, best-ranked first. Returns the
    retrievable columns (joined from `conversations`), NOT the indexed body."""
    cols = ",".join("c." + c for c in _CONV_COLS)
    return conn.execute(
        "SELECT %s FROM conversations_fts "
        "JOIN conversations c ON c.rowid = conversations_fts.rowid "
        "WHERE conversations_fts MATCH ? ORDER BY rank LIMIT ?" % cols,
        (query, limit)).fetchall()


def get_checkpoint(conn, file):
    """(offset, content_hash) for `file`, or None if it was never checkpointed."""
    row = conn.execute(
        'SELECT "offset", content_hash FROM ingest_checkpoint WHERE file=?',
        (file,)).fetchone()
    if row is None:
        return None
    return (row[0], row[1])


def load_corpus(conn):
    """Rebuild a Corpus's THREAD GRAPH from the index (threads + edges). Conversations
    are loaded by the existing IR pipeline, so a fresh Corpus's conversations list is
    left empty for the caller to attach.

    `ThreadMeta.adapter` is filled here from `conversations.provider` rather than read
    from a column: the adapter identity already exists in the index via the
    `conversations.thread_id -> threads.id` join, so deriving it needs no migration and
    works on indexes built before the field existed. Ordered by `conversation_id` so a
    thread carrying more than one conversation resolves to the same adapter on every
    load instead of whichever row the query planner happened to return first."""
    threads = {}
    for row in conn.execute("SELECT %s FROM threads" % ",".join(_THREAD_COLS)):
        meta = ThreadMeta(*row)
        threads[meta.id] = meta
    for tid, provider in conn.execute(
            "SELECT thread_id, provider FROM conversations "
            "WHERE thread_id != '' ORDER BY conversation_id"):
        meta = threads.get(tid)
        if meta is not None and not meta.adapter:
            meta.adapter = provider
    edges = [SpawnEdge(*row) for row in
             conn.execute("SELECT %s FROM thread_spawn_edges" % ",".join(_EDGE_COLS))]
    return Corpus(threads=threads, edges=edges)


# -------------------------------------------------------------------- helpers

def _conversation_body(conv):
    """The searchable text of a conversation: its title and every block's text, with
    empty pieces dropped so blank blocks add no noise to the index."""
    parts = [conv.title]
    for turn in conv.turns:
        for block in turn.blocks:
            parts.append(block.text)
    return "\n".join(p for p in parts if p)
