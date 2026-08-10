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

It also owns the ON-DISK contract the parallel build agents write into: INDEX_SCHEMA (a
contentless FTS5 table over conversation records + threads + thread_spawn_edges + a
conversation_rollouts leg table + a conversation_bodies archive + a resumable
ingest_checkpoint table, WAL) and the thin row<->dataclass mapping (upsert_thread /
add_conversation / search / set_checkpoint / rollout_legs / load_conversation_turns / ...).
Defining that mapping ONCE, next to the dataclasses and the DDL, is what keeps the fan-out
of ingest agents from drifting into N incompatible INSERTs.

Phase-0 MEASURED facts this contract is built around (ground truth, not guesses):
  * corpus = 2,249,530 records over 13,711 files — the FTS index is sized for millions
    and is contentless (content=''), so the searchable text lives only in the inverted
    index while the displayable columns live in a plain `conversations` table joined by
    rowid. `detail` is FULL (G-4); the bodies themselves live in `conversation_bodies`.
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

-- Contentless (content='') FTS5 over the searchable text of each conversation. `detail`
-- is FULL: under the old `detail=none` a phrase query, NEAR, a column filter and any
-- bm25 carrying a term-frequency signal were all impossible — `_fts_opts` at EOF holds
-- the measurement and the index-size price. A MATCH yields rowids joined to that table.
CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
    title,
    body,
    content='',
    detail=full%(fts_opts)s
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

-- Every rollout file ONE conversation was stitched from, in merge order. A resumed Codex
-- session writes a new rollout per resume, and `loaders._merge_resumed_leg` folds those
-- legs into one conversation — measured on the live store, 236 conversations spanning 1189
-- files, the widest 66 legs. `conversations.rollout_path` holds only the LAST leg (the file
-- `codex resume` continues), so without this table the reader could open just that one
-- while the FTS body already spanned every leg: a search could match text and open a
-- transcript that does not contain it.
--
-- A TABLE rather than a column on `conversations`, and the choice is forced. Every
-- statement here is IF NOT EXISTS, so an index built before a change keeps its ORIGINAL
-- shape: a new COLUMN would silently not exist on an existing index and every INSERT
-- naming it would fail, which would mean either an ALTER-TABLE migration or asking users
-- to delete their corpus. A new TABLE simply appears, empty, the first time the new engine
-- opens an old index — the reader then falls back to `conversations.rollout_path` (exactly
-- the pre-change behaviour) and a plain rebuild repopulates it, because `_persist_graph`
-- runs ahead of the checkpoint-skippable conversation ingest.
CREATE TABLE IF NOT EXISTS conversation_rollouts (
    conversation_id TEXT NOT NULL,
    leg_index       INTEGER NOT NULL,
    rollout_path    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (conversation_id, leg_index)
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
    leaves it alone rather than silently dropping a user's index to rebuild it.

    That same rule is why a NEW FACT is added as a new TABLE and never as a column on an
    existing one. `conversation_rollouts` appears — empty — the first time this runs against
    an index that predates it, and every reader treats "no rows" as "not recorded yet" and
    falls back; a new COLUMN would instead be silently absent and every INSERT naming it
    would raise, which is a migration, not a no-op."""
    wal = "PRAGMA journal_mode=WAL"  # EXECUTED at 294, after the version gate at 293
    check_schema_version(conn)  # REFUSES here, before ANY statement — see D-1 at EOF
    conn.execute(wal)
    conn.executescript((INDEX_SCHEMA + _BODIES_SCHEMA) % {"fts_opts": _fts_opts()})
    stamp_schema_version(conn)
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

    IT ALSO STORES THE TRANSCRIPT (G-4). `set_conversation_body` writes `conv.turns` into
    `conversation_bodies` as a seekable-zstd archive, which is what makes the index an
    ARCHIVE rather than a set of pointers into files the user may move or delete. It runs
    FIRST, and unconditionally, for two reasons: it must also fill in a body for a row that
    already exists without one (the state of every conversation the moment a pre-G-4 index
    is rebuilt), and it must not be skipped by either early return below — the legacy
    `stored == values` no-op reached one of them.

    The FTS `body` and the stored archive are DIFFERENT facts and are deliberately not
    derived from each other: `body` is searchable text a caller may sanitize or truncate,
    the archive is the structured turns as parsed. Keeping them separate is what lets the
    index hold a redacted search surface over a faithful transcript.
    """
    if body is None:
        body = _conversation_body(conv)
    set_conversation_body(conn, conv.id, conv.turns, conv.meta)
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


def set_conversation_rollouts(conn, conversation_id, paths):
    """Record, IN ORDER, every rollout file `conversation_id` was stitched from.

    REPLACES the whole list rather than appending, because the ingest is authoritative: a
    leg the user deleted must disappear from the record too, or the reader is sent at a
    file the ingest itself no longer believes in. An unchanged list is a genuine no-op —
    the stored order is compared first — so a resumed or repeated build writes nothing and
    the table does not churn. (The comparison is not an optimisation: `_merge_resumed_leg`
    appends one IndexSource per LEG carrying the same merged conversation, so a 66-leg
    thread would otherwise rewrite its 66 rows 66 times.)

    Not `INSERT OR REPLACE` per row: that grows the list monotonically, because a row whose
    `leg_index` no longer exists has nothing to be replaced BY and would survive forever.
    """
    paths = list(paths)
    if rollout_legs(conn, conversation_id) == paths:
        return
    conn.execute("DELETE FROM conversation_rollouts WHERE conversation_id=?",
                 (conversation_id,))
    conn.executemany(
        "INSERT INTO conversation_rollouts(conversation_id, leg_index, rollout_path) "
        "VALUES (?,?,?)",
        [(conversation_id, i, path) for i, path in enumerate(paths)])


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


def rollout_legs(conn, conversation_id):
    """Every rollout file recorded for `conversation_id`, in the order they were merged.

    [] for a conversation with no recorded legs — which is BOTH "this id is not in the
    index" and "this index predates `conversation_rollouts`". The caller must treat the
    empty answer as "not recorded", never as "this conversation has no rollout": the
    fallback is `conversations.rollout_path`, which every index has always carried.
    """
    return [row[0] for row in conn.execute(
        "SELECT rollout_path FROM conversation_rollouts WHERE conversation_id=? "
        "ORDER BY leg_index", (conversation_id,))]


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


def _fts_opts():
    """The trailing FTS5 options `INDEX_SCHEMA` interpolates: `contentless_delete=1`, or ""
    on a SQLite too old to support it. A function only so `init_index` fits on one line per
    step: the version gate has to run before the WAL pragma (see below), and every line
    above this point in the file is cited BY LINE from elsewhere in the tree.

    WHY `detail` IS NO LONGER `none`, and what it cost (G-4). The old pair was chosen on a
    real measurement — p95 33 ms over 2.2M records, ~6x under a 200 ms budget — but that
    measured SPEED, which was never the problem. Measured here on SQLite 3.50.4, over 200
    synthetic docs where the query term is rare enough for bm25's IDF to stay positive:

      | shape          | phrase | NEAR | `col:` | distinct bm25 scores |
      |----------------|--------|------|--------|----------------------|
      | detail=none    | raises | raises | raises | 1 of 3 (all -0.0)  |
      | detail=column  | raises | raises | ok     | 1 of 3 (all -0.0)  |
      | detail=full    | ok     | ok   | ok     | 3 of 3, tf-ordered   |

    So `detail=column` is not a middle ground: it buys the column filter and leaves ranking
    just as degenerate. Under `full`, a doc holding the term 8 times outranks one holding it
    once, and a long doc holding it once ranks last — term frequency AND length
    normalisation are both live, which is what "relevance" has to mean.

    THE PRICE, measured the same way: 4,000 docs of identical text indexed both ways came to
    163,840 bytes at `detail=none` and 413,696 at `detail=full` — **2.52x** the index, which
    is the cost of storing positions. That is the trade this option records.

    `contentless_delete=1` lets a contentless table retract a rowid's postings, which is what
    makes re-indexing a GROWN session possible. It landed in SQLite 3.43 and is measured
    working alongside `detail=full` (and `detail=none`, and `detail=column`) on 3.50.4. On an
    older build it is simply omitted and `add_conversation` falls back to re-inserting under
    a fresh rowid — correct, but the stale posting is orphaned rather than reclaimed."""
    return ",\n    contentless_delete=1" if _CONTENTLESS_DELETE else ""


# ------------------------------------------------- on-disk schema version (D-1)
#
# WHAT THIS IS FOR. Until now an index recorded nothing about its own shape, so a build
# that changes the schema in a way `IF NOT EXISTS` cannot patch up had no way to tell an
# index it understands from one it does not — and an OLDER build pointed at a NEWER
# index would happily write into it. The marker makes both detectable.
#
# WHY A TABLE, NOT `PRAGMA user_version`. `init_index` above states the schema's own
# migration rule: a NEW FACT is added as a new TABLE and never as a new column, because
# every statement is `IF NOT EXISTS` and so a new table simply appears (empty) on an old
# index while a new column would be silently absent and every INSERT naming it would
# raise. `schema_meta(key, value)` obeys that rule, is readable in any sqlite browser,
# and extends to the next piece of metadata without another migration. `user_version` is
# a bare int in the file header with room for exactly one fact.
#
# WHY THIS SECTION SITS AT THE END OF THE MODULE, below the code that calls it. Module
# globals resolve at CALL time, so the position is harmless — and it is deliberate:
# `corpus.py` line numbers are cited BY LINE from other modules (`sidecar.py:737,742`,
# `discover.py:285`, `claude_code.py:78`, `cockpit/src/ipc/types.ts`) and pinned by
# `tests/test_citation_anchors.py`, so inserting lines higher up silently rots citations
# in files this change does not own. Append-only is the honest way to grow this file.
#
# Distinct from `CORPUS_VERSION` at the top of the module, which is deliberately left
# alone: it has no on-disk meaning and, measured across the repo, no production caller at
# all (only a test asserting it is an int). Repurposing it would have made ONE name mean
# two things; adding a second, precisely-scoped constant means neither is ambiguous.

#: The version of the ON-DISK index schema this build writes and knows how to read.
#: Bump it in the SAME commit as the change it describes, and say in that commit whether
#: the delta is additive (then also add the previous version to `_ADDITIVE_FROM`).
#:
#: 2 (G-4) — the delta is NOT additive. `conversation_bodies` on its own would have been
#: (a new table simply appears), but the same change recreates `conversations_fts` at
#: `detail=full`, and an FTS5 table's option set is fixed at CREATE time: `IF NOT EXISTS`
#: deliberately leaves an existing virtual table alone rather than dropping a user's index
#: to rebuild it. So a version-1 index cannot be carried forward in place, and version 1
#: stays OUT of `_ADDITIVE_FROM`. The cost is stated where a user meets it: every index
#: built before this has to be rebuilt once, and `IndexRebuildRequired` says so by name
#: instead of opening it and half-reading it.
SCHEMA_VERSION = 2

#: What an index carrying NO marker row IS. Every index in existence before this change
#: has no marker, and all of them are the schema this build calls 1 — so an absent marker
#: is read as 1 and stamped, never treated as "unknown, refuse". It is a SEPARATE
#: constant from `SCHEMA_VERSION` on purpose: when `SCHEMA_VERSION` becomes 2, an
#: unmarked index is still a 1, and assuming otherwise would skip a real migration.
UNSTAMPED_VERSION = 1

#: The `schema_meta` key the version lives under. Other keys are free for later facts.
SCHEMA_VERSION_KEY = "schema_version"

#: Index versions this build can migrate FORWARD in place, without a rebuild. A version
#: belongs here only when every change between it and `SCHEMA_VERSION` was ADDITIVE — a
#: new TABLE, never a changed or added column — because that is the entire delta
#: `init_index` can apply on its own, and applying it is then the whole migration.
#:
#: STILL EMPTY at `SCHEMA_VERSION` 2, and now for a substantive reason rather than for want
#: of an older version. It was empty at 1 because 1 is the first version there is; the note
#: here predicted that the unit rebuilding the FTS table would bump to 2 and leave this
#: empty, because recreating a virtual table is not additive. That is exactly what G-4 did,
#: so a version-1 index correctly reports that a rebuild is required instead of being opened
#: and half-read.
#:
#: The next ADDITIVE change (a new table and nothing else) is the one that should finally put
#: `2` in here, in the same commit that bumps `SCHEMA_VERSION` to 3.
_ADDITIVE_FROM = frozenset()

#: The marker table's own DDL, kept OUT of `INDEX_SCHEMA`: this is the one table that has
#: to exist before the schema `INDEX_SCHEMA` describes is applied, because the version
#: gate reads it first and decides whether applying that schema is safe at all.
_SCHEMA_META_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


class IndexVersionError(RuntimeError):
    """This index's schema version is not one this build can safely open.

    The base class of both refusals so a caller that only needs "unusable, tell the user
    why" can catch one thing and relay `str(exc)`, which is written to be read by a
    person. Raised directly when the marker is present but unreadable.
    """


class IndexTooNewError(IndexVersionError):
    """The index was written by a NEWER build. Refusing is the point: `init_index` is
    otherwise happy to re-create anything missing, which on a newer index means writing a
    shape that build deliberately changed."""


class IndexRebuildRequired(IndexVersionError):
    """The index is OLDER and the delta is not additive, so it cannot be migrated in
    place. Reported, never acted on: silently rebuilding would delete an archive the user
    may want to export or open with the older build first."""


def _version_verdict(found, expected, additive_from):
    """Pure policy: what to DO about an index at version `found`.

    One of exactly four words — "ok" (same version), "newer" (refuse), "migrate"
    (additive, apply in place), "rebuild" (older and breaking). Pure and fully
    argument-driven so the whole policy is testable without a database, and so the
    module-level constants stay patchable by a test proving the older-index half that no
    real index can exercise yet.
    """
    if found == expected:
        return "ok"
    if found > expected:
        return "newer"
    if found in additive_from:
        return "migrate"
    return "rebuild"


def read_schema_version(conn):
    """The version stamped on `conn`, or None if it carries no marker.

    None is a legitimate, expected answer: it is what every index built before the marker
    existed reports, and it covers both "no `schema_meta` table" and "table but no row".
    The table's presence is read off `sqlite_master` rather than probed by running the
    SELECT and catching the error — the same reason `_fts_can_delete` does, so a failed
    statement never has to be swallowed mid-transaction.

    Raises IndexVersionError if a marker IS present but is not an integer. A hand-edited
    or truncated marker is exactly the case where guessing is worst: "unreadable" must
    not silently become "current".
    """
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if table is None:
        return None
    row = conn.execute("SELECT value FROM schema_meta WHERE key=?",
                       (SCHEMA_VERSION_KEY,)).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        raise IndexVersionError(
            "this index records an unreadable schema version (%.40r); it may be corrupt "
            "or hand-edited. This build writes version %d."
            % (row[0], SCHEMA_VERSION))


def _holds_an_index(conn):
    """Does `conn` already hold a corpus index, as opposed to being an empty database?

    `conversations` is the probe because it is the core table every index has always had —
    an index cannot exist without it, and nothing else in this schema creates it. A count of
    `sqlite_master` objects would be the more obvious "is this file empty" test and is the
    wrong question: a connection can legitimately carry the `conversation_metadata` table
    (`metadata.ensure_schema` runs on a bare connection) before an index is ever built, and
    that must still read as "no index here".
    """
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='conversations'"
    ).fetchone() is not None


def check_schema_version(conn):
    """Decide whether this build may open `conn`, and return the version it is at.

    Called by `init_index` before ANY statement runs — not merely before the DDL, but
    ahead of the `PRAGMA journal_mode=WAL` too, which is why that pragma is declared a
    line early and executed a line late. Measured: with the pragma running first, a
    refused open flipped a `journal_mode=delete` index to `wal` and changed the file
    hash. `journal_mode` is a persistent header setting, so that was still a write into
    an archive this build does not understand. A refusal now leaves the file
    byte-identical, which `test_a_refused_open_does_not_write_a_single_byte` pins.

    Both refusals name both versions, because "wrong version" is useless to someone
    deciding whether to upgrade the app or rebuild the index.

    Returns the version found — `UNSTAMPED_VERSION` for an unmarked index — for "ok" and
    for "migrate" alike: an additive migration needs no work here, since `init_index`'s
    own `IF NOT EXISTS` DDL creates exactly what an additive delta added.

    AN ABSENT MARKER MEANS TWO DIFFERENT THINGS and only one of them is a version. This was
    a latent defect from the moment the gate was written, invisible for exactly as long as
    `SCHEMA_VERSION` was also 1:

      * a database that ALREADY HOLDS an index but no marker is a genuine pre-marker index,
        and `UNSTAMPED_VERSION` is what it is;
      * an EMPTY database is not an old index at all — it is the file `init_index` is about
        to create one in, and `open_index` creates it on every first run.

    Reading the second as `UNSTAMPED_VERSION` was harmless while that equalled
    `SCHEMA_VERSION` (the verdict came out "ok" either way). At `SCHEMA_VERSION` 2 it made
    every `sqlite3.connect` of a new file report as a version-1 index, so `init_index`
    refused to create one and told the user to rebuild an index that did not exist.
    Measured on the bump: 432 tests failed, every one of them at `open_index`.

    The presence of `conversations` is therefore the second signal, and it is read off
    `sqlite_master` for the same reason `read_schema_version` and `_fts_can_delete` do —
    so no failed statement has to be swallowed mid-transaction.
    """
    found = read_schema_version(conn)
    if found is None:
        if not _holds_an_index(conn):
            return SCHEMA_VERSION            # a new database, not an old index
        found = UNSTAMPED_VERSION
    verdict = _version_verdict(found, SCHEMA_VERSION, _ADDITIVE_FROM)
    if verdict == "newer":
        raise IndexTooNewError(
            "this index is schema version %d, but this build of LLM Anthology only "
            "understands version %d. Update the app, or point it at a different index — "
            "opening it with an older build could corrupt it." % (found, SCHEMA_VERSION))
    if verdict == "rebuild":
        raise IndexRebuildRequired(
            "this index is schema version %d and this build needs version %d; the change "
            "between them cannot be applied in place, so the index has to be rebuilt. "
            "Nothing has been modified — the existing index is still readable by the "
            "build that wrote it." % (found, SCHEMA_VERSION))
    return found


def stamp_schema_version(conn):
    """Record `SCHEMA_VERSION` on `conn`, creating the marker table if it is absent.

    Writes only when the marker does not already read `SCHEMA_VERSION`, so opening an
    up-to-date index — which every read path does — dirties nothing.

    COMMITS. Every other writer here leaves the transaction to its caller, but this one
    runs inside `init_index`, which read paths call and which no caller expects to leave a
    write transaction open: uncommitted, the marker would be rolled back on close AND the
    connection would hold a lock the sidecar's second connection then blocks on.
    """
    conn.executescript(_SCHEMA_META_DDL)
    if read_schema_version(conn) == SCHEMA_VERSION:
        return
    conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                 (SCHEMA_VERSION_KEY, str(SCHEMA_VERSION)))
    conn.commit()


# ------------------------------------------------- who already holds an id (G-1)
#
# Appended for the same reason the D-1 block above is appended: this file's line numbers
# are cited BY LINE from `sidecar.py`, `discover.py`, `claude_code.py`, `mock.ts` and
# `types.ts`, and `tests/test_citation_anchors.py` turns a shifted anchor into a red
# build. Growing the module at the end is what keeps a new fact from silently rotting
# citations in files the change does not own.

def indexed_provider(conn, conversation_id):
    """The `provider` already recorded for `conversation_id`, or None when the index holds
    no such conversation.

    WHY A READER EXISTS FOR EXACTLY THIS ONE COLUMN. `add_conversation` is idempotent BY
    ID and — since it began re-indexing rather than early-returning — OVERWRITES. That is
    right for a session that GREW, and wrong for two DIFFERENT sources claiming one id:
    the second write silently replaces the first, no error, no count change, nothing to
    notice. `loaders._admit` refuses that collision for THREAD ids and reports both
    sources; the export ingest needs the same refusal for CONVERSATION ids, and the
    incumbent's provider is the one fact it needs to make it.

    Here rather than as a raw SELECT in `loaders`, because this module owns the
    row<->dataclass mapping and the column list ONCE — a second private query somewhere
    else is exactly how column knowledge drifts (the reason `_CONV_COLS` exists).

    Indexed POSITIONALLY: several callers hand in a bare connection with no row_factory,
    so `row["provider"]` would raise on a plain tuple.
    """
    row = conn.execute("SELECT provider FROM conversations WHERE conversation_id=?",
                       (conversation_id,)).fetchone()
    return None if row is None else row[0]


# ------------------------------------- the corpus is an ARCHIVE, not an index (G-4)
#
# Appended, and the IMPORTS are appended with it, for the reason the two blocks above are:
# `corpus.py` line numbers are cited BY LINE from `sidecar.py`, `discover.py`,
# `claude_code.py`, `mock.ts` and `types.ts`, and `tests/test_citation_anchors.py` turns a
# shifted anchor into a red build. Three of those anchors sit at lines 179, 197 and 303, so
# adding an `import` at the top of the module — the ordinary place for one — would rot
# citations in four files this change does not own and may not edit. Module-level imports
# are legal anywhere and resolve before any function here runs, so the honest cost of
# append-only is this comment rather than a silent breakage elsewhere.
#
# WHAT WAS WRONG. `conversations` held METADATA ONLY and the FTS is contentless, so no
# conversation TEXT was stored anywhere in the index. `sidecar._conversation_get` re-parsed
# the transcript out of `rollout_path` on every single read and degraded to
# `available:false` / "rollout unavailable" when the file was gone. Move, compact or delete
# the sources and every conversation in a 122 MB corpus became a stub. G-1 makes the
# archive the guaranteed product, so that was a contradiction at the foundation — and it
# had never surfaced only because 400/400 sampled paths on the owner's live store still
# existed.
#
# WHY `llm_anthology.archive` RATHER THAN A SECOND CODEC. It is a seekable-zstd
# implementation — per-record frames plus a seek table in a trailing skippable frame — and
# it had ZERO importers, having been written for a rollout re-encoding that was never
# wired. It is exactly the random-access-into-compressed-content primitive this needs, so
# it is imported rather than a `zstandard.compress` call being sprinkled here.
#
# WHY ONE FRAME PER TURN. A frame is independently decompressible, so the framing IS the
# access granularity. One frame per CONVERSATION would mean the largest real conversation
# (18.0 M chars, measured) has to be inflated whole to read anything; one frame per LINE
# would drown 122 MB of text in per-frame headers. Per-turn sits where the reader's own
# unit of work sits. NOTE, stated plainly: no caller reads a single frame in isolation yet
# — `load_conversation_turns` wants them all — so per-turn framing is a property of the
# stored format rather than an exercised optimisation today. It costs nothing to have and
# cannot be retrofitted without rewriting every archive, which is why it is decided now.

import json                                                              # noqa: E402
from . import archive, ir                                                # noqa: E402

#: `conversation_bodies` DDL, kept OUT of `INDEX_SCHEMA` for the same append-only reason
#: this whole section is down here: `INDEX_SCHEMA` ends above line 292, and growing it
#: would shift `PRAGMA journal_mode=WAL` (cited as `corpus.py:292`) and `sqlite3.connect`
#: (`corpus.py:303`). `init_index` concatenates the two before formatting, so the on-disk
#: result is identical to having written it inline.
#:
#: A new TABLE, never a column on `conversations` — the rule `init_index` states. It
#: therefore appears EMPTY the first time this build opens an index that predates it, and
#: `load_conversation_turns` answering None is what the reader treats as "fall back".
#:
#: `text_bytes` is the decompressed size of the archive. It duplicates something the seek
#: table already knows, deliberately: it is the one size fact a person auditing the file in
#: any sqlite browser can read WITHOUT a zstd decoder, which matters for a format whose
#: whole claim is self-containment. `test_text_bytes_equals_the_archives_own_decompressed_size`
#: stops the two drifting apart.
#:
#: `meta` is `ir.Conversation.meta` as JSON, and it is a COLUMN rather than an extra frame so
#: the archive keeps its one-frame-per-turn invariant (a header frame would make `frame(i)`
#: mean turn i-1, which is the kind of off-by-one that only shows up in production). It is
#: stored because it is part of the conversation: the adapters put `thread_id`, the parsed
#: `rollout_path` and their hidden-character audit in there, so a reader served from the
#: archive without it would silently answer with an emptier conversation than a re-parse did
#: — the degraded-transcript failure this whole unit exists to avoid.
#:
#: Contains no literal `%`: `init_index` runs `%`-formatting over the concatenation.
_BODIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_bodies (
    conversation_id TEXT PRIMARY KEY,
    text_bytes      INTEGER NOT NULL DEFAULT 0,
    meta            TEXT NOT NULL DEFAULT '{}',
    archive         BLOB NOT NULL
);
"""

#: Compact JSON: this is machine-read only, and at 122 MB of corpus the separators are not
#: free. `ensure_ascii=False` keeps real UTF-8 in the frame instead of tripling the cost of
#: every non-ASCII character — agentic transcripts are full of box drawing and CJK.
_JSON = {"separators": (",", ":"), "ensure_ascii": False}


def _block_record(block):
    """One ir.Block as a plain dict.

    `data` and `citations` pass through as-is rather than being copied or coerced. They are
    already required to be JSON-serializable by a contract that predates this code:
    `sidecar._serialize_block` puts both on the JSON-RPC wire, so an adapter that put a
    non-serializable value in either would already have crashed the reader.
    """
    return {"type": block.type, "text": block.text,
            "data": block.data, "citations": block.citations}


def _turn_record(turn):
    """One ir.Turn as the JSON payload of one archive frame.

    Field names match the dataclass rather than being shortened. The frame is compressed,
    so repeated keys cost almost nothing, and a reader debugging a corrupt archive gets
    something legible out of it.

    `branch` is written only when it is set, because it is `Optional[dict]` and `None` is
    its meaningful default — the same rule `sidecar._serialize_turn` follows.
    """
    record = {"role": turn.role, "uuid": turn.uuid, "timestamp": turn.timestamp,
              "blocks": [_block_record(b) for b in turn.blocks]}
    if turn.branch is not None:
        record["branch"] = turn.branch
    return record


def _turn_from_record(record):
    """One decoded frame back into an ir.Turn. `.get` per field so a frame written by an
    older/leaner encoder still constructs, which is the same schema-tolerance `ThreadMeta`
    is built with."""
    return ir.Turn(
        role=record.get("role", ""),
        uuid=record.get("uuid", ""),
        timestamp=record.get("timestamp", ""),
        branch=record.get("branch"),
        blocks=[ir.Block(type=b.get("type", "unknown"), text=b.get("text", ""),
                         data=b.get("data") or {}, citations=b.get("citations") or [])
                for b in record.get("blocks", ())])


def encode_turns(turns):
    """`turns` -> a seekable-zstd archive, one frame per turn. Pure, so the encoding is
    testable and measurable without a database."""
    return archive.encode_records(
        json.dumps(_turn_record(t), **_JSON) for t in turns)


def set_conversation_body(conn, conversation_id, turns, meta=None):
    """Store `turns` (+ `meta`) as the archived body of `conversation_id`, replacing any
    previous one.

    REPLACES rather than appends, because the ingest is authoritative — the same rule
    `set_conversation_rollouts` follows. A session that GREW must read back grown, and a
    session re-parsed from a repaired source must read back repaired.

    Unconditional rather than compare-first. The only cheap fingerprint available is the
    stored `text_bytes`, and an edit that keeps the length identical is exactly the case
    `add_conversation` already learned not to trust: it compared a column tuple and silently
    skipped re-indexing when "alpha" became "bravo". Re-writing a body that did not change
    costs one BLOB write on a path that is already parsing a transcript.
    """
    blob = encode_turns(turns)
    conn.execute(
        "INSERT OR REPLACE INTO conversation_bodies"
        "(conversation_id, text_bytes, meta, archive) VALUES (?,?,?,?)",
        (conversation_id, archive.SeekableReader(blob).decompressed_size,
         json.dumps(meta or {}, **_JSON), sqlite3.Binary(blob)))


def load_conversation_body(conn, conversation_id):
    """The archived `(turns, meta)` of `conversation_id`, or None when no body is stored.

    None vs `([], {})` IS THE WHOLE CONTRACT and the caller must not conflate them:

      * `([], {})` — a body IS stored and the conversation genuinely has no turns. A Codex
        rollout carrying only a `session_meta` line parses to exactly that.
      * None — nothing is stored. Either the id is not in the index, or the index predates
        `conversation_bodies` (every index built before G-4). ONLY this answer may fall
        back to re-parsing the source file.

    Collapsing the two would make a legitimately-empty conversation re-open its rollout on
    every read, which is the behaviour this table exists to end.

    ONE ROW READ, returning both halves, rather than a turns reader plus a meta reader. The
    two facts live in the same row and every caller wants both, so splitting them would buy a
    second query and an opportunity for the pair to disagree.
    """
    row = conn.execute(
        "SELECT meta, archive FROM conversation_bodies WHERE conversation_id=?",
        (conversation_id,)).fetchone()
    if row is None:
        return None
    turns = [_turn_from_record(json.loads(frame))
             for frame in archive.SeekableReader(bytes(row[1]))]
    return turns, json.loads(row[0])
