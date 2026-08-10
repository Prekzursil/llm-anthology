"""index.py — build & query the contentless FTS5 conversation index.

`llm_anthology.corpus` owns the on-disk CONTRACT: the INDEX_SCHEMA (a contentless FTS5 table +
threads/edges/checkpoint tables) and the thin row<->dataclass primitives
(add_conversation / set_checkpoint / get_checkpoint / search / open_index). This module
is the BUILDER that drives those primitives over a whole corpus of source files, adding
the two properties a 2.2M-record / 13,711-file ingest needs and a single INSERT cannot
express on its own:

  * CHUNKED + RESUMABLE. Records are ingested in `chunk_size` batches; after each batch
    the ingest_checkpoint(file, offset, content_hash) is advanced and the transaction
    committed. A build interrupted mid-file resumes at the last committed batch on the
    next run (even from a fresh connection) instead of restarting from zero. The offset
    counts RECORDS consumed from the source, so a resume restarts at records[offset:].

  * IDEMPOTENT. A re-run whose content_hash matches the checkpoint SKIPS the file
    outright (the fast path). A changed file (new hash) is re-scanned from record 0, but
    because corpus.add_conversation is idempotent by conversation_id, already-indexed
    records are no-ops and only genuinely new ids are added — a replay adds no duplicate
    posting.

  * LOCK-TOLERANT. Under WAL a concurrent writer raises a transient SQLITE_BUSY(5), and
    shared-cache table contention raises SQLITE_LOCKED(6); every write here runs through
    `_retry`, which backs off and retries those two codes and re-raises anything else.

Query side: `count` is the fast total, `search` is the ranked bm25 search (delegating to
corpus.search, whose `ORDER BY rank` IS bm25), and `ranked_search` exposes the bm25 score
per row. The index is contentless (content='') but is `detail=full` since G-4, so bm25 DOES
carry a term-frequency and document-length signal: measured, a doc holding the term eight
times outranks one holding it once, and one hit in a long doc ranks below one in a short one.

PRIVACY: this module moves opaque conversation text into an inverted index and reads
counts/shapes; it never emits conversation content. Tests use synthetic fixtures only.
"""
import hashlib
import sqlite3
import time
from dataclasses import dataclass, field

from . import corpus

DEFAULT_CHUNK_SIZE = 500
DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_DELAY = 0.05
DEFAULT_LIMIT = 50

# Transient SQLite contention codes worth retrying. The task names SQLITE_LOCKED(6);
# BUSY(5) is its WAL sibling (a busy writer). Anything else is a real error, not a lock.
SQLITE_LOCKED = 6
SQLITE_BUSY = 5
_RETRYABLE = (SQLITE_LOCKED, SQLITE_BUSY)


# ------------------------------------------------------------------ dataclasses

@dataclass
class IndexSource:
    """One source file's worth of records to ingest.

    `file` names the source (and is the ingest_checkpoint key), `content_hash` is its
    current content fingerprint (compute it with `hash_content`), and `records` is the
    ordered list of ir.Conversation objects parsed from it. Checkpoint offsets count
    records consumed from `records`, so a resumed build restarts at records[offset:].
    """
    file: str
    content_hash: str
    records: list = field(default_factory=list)


@dataclass
class BuildStats:
    """The observable outcome of a build_index run."""
    files_total: int = 0        # sources seen this run
    files_skipped: int = 0      # a matching checkpoint already covered every record
    records_processed: int = 0  # records handed to add_conversation THIS run
    chunks_committed: int = 0   # batches committed THIS run


# ---------------------------------------------------------------------- hashing

def hash_content(data):
    """SHA-256 hex digest of `data` (a str is UTF-8 encoded first). The canonical way to
    compute an IndexSource.content_hash so every producer fingerprints identically."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ lock retry

def _is_locked(exc):
    """True when `exc` is a transient SQLITE_LOCKED/BUSY a retry may clear.

    Prefers the numeric sqlite_errorcode the sqlite3 machinery attaches to genuine
    errors (Python 3.11+); falls back to matching the message text when no code is
    present (e.g. an exception constructed outside sqlite3)."""
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code in _RETRYABLE
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _retry(op, *, max_retries=DEFAULT_MAX_RETRIES, retry_delay=DEFAULT_RETRY_DELAY,
           sleep=time.sleep):
    """Run `op()` and return its result, retrying on a transient lock up to
    `max_retries` times (sleeping `retry_delay` between attempts). A non-lock
    OperationalError, or a lock that outlasts the retries, propagates unchanged."""
    attempt = 0
    while True:
        try:
            return op()
        except sqlite3.OperationalError as exc:
            if attempt >= max_retries or not _is_locked(exc):
                raise
            attempt += 1
            sleep(retry_delay)


# ------------------------------------------------------------------ the build

def _thread_id(conv):
    """The thread id linked to a conversation (from its meta), or '' when unlinked."""
    return conv.meta.get("thread_id", "")


def build_index(conn, sources, *, chunk_size=DEFAULT_CHUNK_SIZE,
                max_retries=DEFAULT_MAX_RETRIES, retry_delay=DEFAULT_RETRY_DELAY,
                sleep=time.sleep, progress=None):
    """Ingest `sources` into the FTS index at `conn`, chunked and resumable.

    For each IndexSource: if a checkpoint exists whose content_hash matches, resume at
    records[offset:] (a fully-covered file is skipped untouched); otherwise (no
    checkpoint, or the hash changed) re-scan from record 0 — add_conversation being
    idempotent by conversation_id means already-indexed records are no-ops. Records are
    ingested in `chunk_size` batches and, after each, the checkpoint is advanced and the
    transaction committed, so an interruption resumes at the last committed batch. Every
    write goes through the SQLITE_LOCKED/BUSY retry. `progress(file, offset)` is called
    after each committed batch. Returns a BuildStats.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")

    stats = BuildStats()

    def do(op):
        return _retry(op, max_retries=max_retries, retry_delay=retry_delay, sleep=sleep)

    for src in sources:
        stats.files_total += 1
        ckpt = corpus.get_checkpoint(conn, src.file)
        start = 0
        if ckpt is not None and ckpt[1] == src.content_hash:
            start = ckpt[0]
        n = len(src.records)
        if start >= n:
            stats.files_skipped += 1
            continue
        i = start
        while i < n:
            end = min(i + chunk_size, n)
            for rec in src.records[i:end]:
                tid = _thread_id(rec)
                do(lambda rec=rec, tid=tid: corpus.add_conversation(
                    conn, rec, thread_id=tid, rollout_path=src.file))
                stats.records_processed += 1
            i = end
            do(lambda i=i: corpus.set_checkpoint(conn, src.file, i, src.content_hash))
            do(conn.commit)
            stats.chunks_committed += 1
            if progress is not None:
                progress(src.file, i)
    return stats


# ------------------------------------------------------------------ query side

def count(conn):
    """Fast total of indexed conversation records."""
    return conn.execute("SELECT count(*) FROM conversations").fetchone()[0]


def search(conn, query, limit=DEFAULT_LIMIT):
    """Ranked bm25 search over the index, delegating to corpus.search (whose
    `ORDER BY rank` is bm25). A blank query yields no rows rather than an FTS error."""
    q = query.strip()
    if not q:
        return []
    return corpus.search(conn, q, limit=limit)


def ranked_search(conn, query, limit=DEFAULT_LIMIT):
    """Like `search`, but each returned row carries a `bm25_score`, best-first (ascending
    bm25 — in FTS5, more-negative is better).

    THIS DOCSTRING USED TO DISCLOSE A RESIDUAL THAT IS NOW GONE. It said bm25 "has no
    term-frequency signal and matching rows tend to tie", which was true and measured under
    `detail=none`: every matching row scored exactly -0.0. G-4 rebuilt the table at
    `detail=full`, so the score now separates rows by term frequency AND by document length.
    The disclosure is kept as a note rather than deleted because the ORDERING of an
    `index.ranked_search` result silently changed meaning at that commit, and a reader
    comparing old output to new deserves to know why."""
    q = query.strip()
    if not q:
        return []
    return conn.execute(
        "SELECT c.*, bm25(conversations_fts) AS bm25_score "
        "FROM conversations_fts "
        "JOIN conversations c ON c.rowid = conversations_fts.rowid "
        "WHERE conversations_fts MATCH ? ORDER BY bm25(conversations_fts) LIMIT ?",
        (q, limit)).fetchall()
