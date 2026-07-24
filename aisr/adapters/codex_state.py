"""$CODEX_HOME/state_5.sqlite (the LIVE Codex state DB) -> Corpus thread graph.

The Codex rollout *logs* carry conversation content; the Codex *state* DB carries the
SPAWN GRAPH the cockpit renders — which thread spawned which (thread_spawn_edges) plus
per-thread metadata (model, tokens, branch, cwd, agent role, timestamps) in `threads`.
This adapter reads that DB into `corpus.ThreadMeta` nodes + `corpus.SpawnEdge` edges.

Four properties are load-bearing, and each is a MEASURED fact about the real store, not
a guess (Phase-0 ground truth: threads=1831, thread_spawn_edges=895):

  * READ-ONLY + IMMUTABLE. The DB is opened `file:...?mode=ro&immutable=1`, so a LIVE,
    WAL-mode store being written by a running Codex is read without taking a lock or
    triggering journal recovery, and this process can never mutate it.

  * SCHEMA-TOLERANT. The live `threads` table has ~33 columns and BOTH created_at_ms
    and updated_at_ms; the legacy canonical copy lacks updated_at_ms entirely. The
    adapter discovers the columns at runtime (PRAGMA table_info) and selects only the
    mapped-AND-present ones, so a missing column leaves its ThreadMeta field at the
    dataclass DEFAULT — updated_at_ms stays None rather than a fabricated 0 — and
    unmapped live columns (source, sandbox_policy, model, ...) are simply ignored.
    A NULL in a present column collapses to that same default, matching the on-disk
    index's NOT NULL threads table (a NULL git_branch becomes '', not None).

  * ORDER DERIVED FROM AVAILABLE TIMESTAMPS. Threads are inserted most-recent-first
    using the best timestamp column that EXISTS (updated_at_ms > created_at_ms >
    updated_at > created_at), falling back to id order when none is present — so a
    plain iteration of Corpus.threads is recency-ordered even on the leaner legacy DB.

  * RETRY-THEN-SKIP. A transient busy/locked OperationalError is retried a few times;
    if it persists, or the DB is missing/unopenable, the read is SKIPPED and an EMPTY
    Corpus is returned so a build never crashes on a busy or absent state store.

Counts are ENUMERATED AT RUNTIME (see `counts`); nothing here hardcodes 895/1831.

PRIVACY: this adapter reads counts and shapes and copies opaque metadata into the IR;
it never inspects or emits conversation content. Tests use synthetic fixtures only.
"""
import dataclasses
import os
import sqlite3
import time

from aisr import corpus

_DB_NAME = "state_5.sqlite"
_DEFAULT_HOME = "~/.codex"

# How hard to try a busy DB before skipping it.
_RETRIES = 3
_RETRY_DELAY = 0.1  # seconds between busy retries

# threads.<column>  ->  ThreadMeta field. Only these columns are ever read; iteration
# order is the ThreadMeta field order, so the SELECT list and the constructor kwargs
# can never drift. A column absent from a leaner schema leaves its field at the default.
_THREAD_FIELD_MAP = {
    "id": "id",
    "title": "title",
    "model_provider": "model_provider",
    "tokens_used": "tokens_used",
    "created_at_ms": "created_at_ms",
    "updated_at_ms": "updated_at_ms",
    "git_branch": "git_branch",
    "cwd": "cwd",
    "agent_role": "agent_role",
    "agent_nickname": "agent_nickname",
    "preview": "preview",
    "rollout_path": "rollout_path",
}

# Timestamp columns to ORDER threads by, most-preferred first. The legacy canonical DB
# lacks updated_at_ms, so the order is derived from whichever of these is present.
_ORDER_PREFERENCE = ("updated_at_ms", "created_at_ms", "updated_at", "created_at")

# thread_spawn_edges columns == SpawnEdge fields (direct 1:1).
_EDGE_FIELDS = ("parent_thread_id", "child_thread_id", "status")


def _defaults(cls):
    """Field-name -> default value for a dataclass, using '' for a field that has no
    default (only ThreadMeta.id / SpawnEdge parent+child). A NULL or absent column
    collapses to this, so absent-or-NULL yields one consistent, contract-valid value."""
    return {f.name: (f.default if f.default is not dataclasses.MISSING else "")
            for f in dataclasses.fields(cls)}


_THREAD_DEFAULTS = _defaults(corpus.ThreadMeta)
_EDGE_DEFAULTS = _defaults(corpus.SpawnEdge)


# --------------------------------------------------------------- public surface

def load_corpus(codex_home=None):
    """Read $CODEX_HOME/state_5.sqlite into a fresh Corpus thread graph.

    Opens the DB READ-ONLY + immutable, reads the threads and thread_spawn_edges tables
    schema-tolerantly, and returns a Corpus. A transient busy/locked DB is retried then
    SKIPPED (empty Corpus), as is a missing/unopenable DB — the build never crashes.
    """
    path = _db_path(codex_home)
    return _read_with_retry(lambda: _connect(path),
                            _RETRIES, _RETRY_DELAY, time.sleep)


def read_corpus(conn, into=None):
    """Populate a Corpus thread graph from an already-open connection.

    `into` defaults to a new Corpus; pass an existing one (e.g. already carrying the
    conversation IR) to MERGE the threads/edges into it. Returns the populated Corpus.
    """
    c = into if into is not None else corpus.Corpus()
    for meta in _read_threads(conn):
        c.add_thread(meta)
    for edge in _read_edges(conn):
        c.add_edge(edge)
    return c


def counts(conn):
    """Runtime row counts for the graph tables — never a hardcoded constant. A table
    absent from a leaner schema counts as 0 rather than raising."""
    return {"threads": _count(conn, "threads"),
            "edges": _count(conn, "thread_spawn_edges")}


# ------------------------------------------------------------- db-path + connect

def _db_path(codex_home=None):
    """The state_5.sqlite path: an explicit codex_home, else $CODEX_HOME, else ~/.codex."""
    home = codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser(_DEFAULT_HOME)
    return os.path.join(home, _DB_NAME)


def _ro_uri(path):
    """A read-only + immutable sqlite URI for `path`. `?`/`#` inside the path are
    percent-escaped so they cannot leak into the URI's own query string."""
    esc = path.replace("?", "%3f").replace("#", "%23")
    return "file:%s?mode=ro&immutable=1" % esc


def _connect(path):
    """Open `path` READ-ONLY + immutable. Row access in the readers is positional, so
    the default row_factory is fine. A missing file raises OperationalError here, which
    the retry loop treats as a (non-busy) skip."""
    return sqlite3.connect(_ro_uri(path), uri=True)


def _is_busy(exc):
    """True only for a transient 'database is locked' / 'busy' OperationalError — the
    single class the retry loop retries. A missing/malformed DB is not busy, so it is
    skipped immediately without burning the retry budget."""
    msg = str(exc).lower()
    return "lock" in msg or "busy" in msg


def _read_with_retry(open_conn, retries, retry_delay, sleep):
    """Open via `open_conn()` and read a Corpus, retrying a BUSY db up to `retries`
    times (sleeping `retry_delay` between tries) then SKIPPING to an empty Corpus. Any
    non-busy open/read error also skips, so a missing or malformed DB never crashes."""
    attempt = 0
    while True:
        conn = None
        try:
            conn = open_conn()
            return read_corpus(conn)
        except sqlite3.OperationalError as exc:
            if _is_busy(exc) and attempt < retries:
                attempt += 1
                sleep(retry_delay)
            else:
                return corpus.Corpus()
        finally:
            if conn is not None:
                conn.close()


# ------------------------------------------------------------- schema-tolerant read

def _table_columns(conn, table):
    """The column names present in `table`, or [] if the table is absent. `table` is
    only ever a hardcoded literal ('threads' / 'thread_spawn_edges'), never user input,
    so its interpolation carries no injection surface."""
    return [row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)]


def _row_kwargs(fields, row, defaults):
    """Zip selected values onto their target dataclass field names, collapsing a NULL
    to that field's default so absent-or-NULL yields one consistent value."""
    return {field: (defaults[field] if value is None else value)
            for field, value in zip(fields, row)}


def _read_threads(conn):
    cols = _table_columns(conn, "threads")
    present = [c for c in _THREAD_FIELD_MAP if c in cols]
    if "id" not in present:                 # nothing to key a node by -> no threads
        return []
    fields = [_THREAD_FIELD_MAP[c] for c in present]
    order_col = next((c for c in _ORDER_PREFERENCE if c in cols), None)
    sql = "SELECT %s FROM threads" % ",".join(present)
    if order_col:
        sql += " ORDER BY %s DESC, id" % order_col   # most-recent-first, id tie-break
    else:
        sql += " ORDER BY id"                        # no timestamp -> deterministic id
    return [corpus.ThreadMeta(**_row_kwargs(fields, row, _THREAD_DEFAULTS))
            for row in conn.execute(sql)]


def _read_edges(conn):
    cols = _table_columns(conn, "thread_spawn_edges")
    present = [c for c in _EDGE_FIELDS if c in cols]
    if "parent_thread_id" not in present or "child_thread_id" not in present:
        return []                            # can't form a directed edge -> no edges
    sql = "SELECT %s FROM thread_spawn_edges" % ",".join(present)
    return [corpus.SpawnEdge(**_row_kwargs(present, row, _EDGE_DEFAULTS))
            for row in conn.execute(sql)]


def _count(conn, table):
    if not _table_columns(conn, table):      # absent table -> 0, never a raise
        return 0
    return conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
