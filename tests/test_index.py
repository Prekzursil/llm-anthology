"""index.py — build & query the contentless FTS5 conversation index.

SYNTHETIC fixtures ONLY. The real corpus is PRIVATE medical/pharma data; nothing here
is a real conversation, thread id, path, token count or body. Every fixture is a
made-up shape that mirrors the Phase-0 MEASURED schema (a contentless FTS5 index over
conversation records, resumable via ingest_checkpoint(file, offset, content_hash)),
never its content.

Three properties are pinned against a REAL sqlite (so behaviour is proven, not
asserted as a string):
  1. RESUMABILITY  — a build interrupted mid-file (crash after a committed chunk)
     RESUMES from the last checkpoint on a fresh connection and lands the SAME index
     as an uninterrupted build — never restarts from zero.
  2. IDEMPOTENCY   — a re-run with a matching content_hash SKIPS the file (fast path),
     and even a forced re-scan (changed hash) adds NO duplicate postings, because
     corpus.add_conversation dedups by conversation_id.
  3. LOCK RETRY    — every write is wrapped in an app-level SQLITE_LOCKED(6)/BUSY(5)
     retry that recovers from a transient lock and re-raises anything else.
"""
import sqlite3

import pytest

from llm_anthology import corpus, index, ir


# --------------------------------------------------------------------- fixtures

# Every sqlite connection a test opens is tracked and closed when it ends, so the
# suite stays free of ResourceWarnings from connections the GC would otherwise reap.
_OPEN = []


def _track(conn):
    _OPEN.append(conn)
    return conn


def _open(path):
    return _track(corpus.open_index(path))


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _fts_postings(conn):
    """Rows in the contentless FTS index. Equals `index.count` in a healthy build — a
    divergence signals a duplicated or orphaned posting, which is what the ingest tests
    below assert cannot happen on a replay.

    This was `index.posting_count` until DECISION G-17. It had zero production callers —
    only these tests and three in `test_loaders_corpus.py` — so it moved out of the shipped
    package rather than being deleted: the INVARIANT it checks is the valuable half, and it
    is asserted here exactly as before. A test-only helper living in `llm_anthology/`
    implied a caller that never existed."""
    return conn.execute("SELECT count(*) FROM conversations_fts").fetchone()[0]


def _conv(cid, title, body):
    """A one-turn synthetic ir.Conversation whose single block carries `body`."""
    return ir.Conversation(
        id=cid, title=title, provider="codex",
        turns=[ir.Turn(role="human", blocks=[ir.Block(type="text", text=body)])])


def _src(file, records, content="v1"):
    return index.IndexSource(file=file, content_hash=index.hash_content(content),
                             records=records)


def _nosleep(_delay):
    """A sleep stand-in so retry tests never actually block."""


def _operr(msg, code=None):
    """A sqlite3.OperationalError, optionally carrying a sqlite_errorcode (as the real
    sqlite3 machinery sets on genuine errors, Python 3.11+)."""
    exc = sqlite3.OperationalError(msg)
    if code is not None:
        exc.sqlite_errorcode = code
    return exc


# --------------------------------------------------------------- hash_content

def test_hash_content_is_stable_across_str_and_bytes():
    """The same bytes hash identically whether handed in as str or bytes, so every
    producer fingerprints a source the same way; different content hashes differently."""
    assert index.hash_content("abc") == index.hash_content(b"abc")
    assert index.hash_content("abc") != index.hash_content("abd")
    assert len(index.hash_content("abc")) == 64  # sha-256 hex


# ---------------------------------------------------------- dataclass surface

def test_index_source_defaults_records_to_an_empty_list():
    s = index.IndexSource(file="f", content_hash="h")
    assert s.file == "f" and s.content_hash == "h" and s.records == []


def test_build_stats_defaults_every_counter_to_zero():
    st = index.BuildStats()
    assert (st.files_total, st.files_skipped, st.records_processed,
            st.chunks_committed) == (0, 0, 0, 0)


# ------------------------------------------------------------------ _is_locked

def test_is_locked_true_on_the_sqlite_locked_errorcode():
    assert index._is_locked(_operr("x", code=index.SQLITE_LOCKED)) is True


def test_is_locked_true_on_the_sqlite_busy_errorcode():
    assert index._is_locked(_operr("x", code=index.SQLITE_BUSY)) is True


def test_is_locked_false_on_an_unrelated_errorcode():
    """A concrete numeric code that is NOT a transient lock (SQLITE_ERROR=1) is not
    retryable, even though the message path might have matched."""
    assert index._is_locked(_operr("database is locked", code=1)) is False


def test_is_locked_falls_back_to_the_message_when_no_code_is_present():
    # no sqlite_errorcode -> message match; both 'locked' and 'busy' are transient
    assert index._is_locked(_operr("database table is locked")) is True
    assert index._is_locked(_operr("database is busy")) is True


def test_is_locked_false_on_an_unrelated_message_without_a_code():
    assert index._is_locked(_operr("no such table: conversations")) is False


# --------------------------------------------------------------------- _retry

def test_retry_returns_the_result_on_first_success():
    slept = []
    assert index._retry(lambda: 42, sleep=slept.append) == 42
    assert slept == []  # no lock -> never slept


def test_retry_recovers_after_transient_locks():
    """Two transient locks then success: the op is retried, sleeping between attempts,
    and the eventual result is returned."""
    calls = {"n": 0}
    slept = []

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _operr("database is locked")
        return "ok"

    assert index._retry(flaky, max_retries=5, retry_delay=0.01,
                        sleep=slept.append) == "ok"
    assert calls["n"] == 3 and slept == [0.01, 0.01]


def test_retry_reraises_after_exhausting_its_attempts():
    """A lock that outlasts max_retries propagates; it slept exactly max_retries times."""
    slept = []

    def always_locked():
        raise _operr("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        index._retry(always_locked, max_retries=2, retry_delay=0, sleep=slept.append)
    assert slept == [0, 0]  # 2 retries, then gave up


def test_retry_reraises_a_non_lock_error_immediately():
    slept = []

    def broken():
        raise _operr("no such table: conversations")

    with pytest.raises(sqlite3.OperationalError):
        index._retry(broken, max_retries=5, sleep=slept.append)
    assert slept == []  # not a lock -> no retry, no sleep


# -------------------------------------------------------------- build_index

def test_build_index_indexes_every_record_and_makes_it_searchable(tmp_path):
    conn = _open(str(tmp_path / "i.sqlite"))
    recs = [_conv("c0", "Widgets", "alpha"), _conv("c1", "Gadgets", "beta")]
    stats = index.build_index(conn, [_src("f.jsonl", recs)])
    assert stats.files_total == 1 and stats.files_skipped == 0
    assert stats.records_processed == 2 and stats.chunks_committed == 1
    assert index.count(conn) == 2 and _fts_postings(conn) == 2
    assert index.search(conn, "alpha")[0]["conversation_id"] == "c0"
    # the title column is indexed too
    assert index.search(conn, "Widgets")[0]["conversation_id"] == "c0"


def test_build_index_links_rollout_path_and_thread_id_from_meta(tmp_path):
    """rollout_path is the source file; thread_id comes from conv.meta when present and
    is empty otherwise — the retrievable columns a search hit joins back to."""
    conn = _open(str(tmp_path / "i.sqlite"))
    linked = _conv("c0", "t", "alpha")
    linked.meta["thread_id"] = "t-42"
    unlinked = _conv("c1", "t", "beta")
    index.build_index(conn, [_src("roll.jsonl", [linked, unlinked])])
    a = index.search(conn, "alpha")[0]
    b = index.search(conn, "beta")[0]
    assert a["thread_id"] == "t-42" and a["rollout_path"] == "roll.jsonl"
    assert b["thread_id"] == "" and b["rollout_path"] == "roll.jsonl"


def test_build_index_chunks_commits_and_reports_progress(tmp_path):
    """chunk_size=2 over 5 records commits three batches (2, 2, 1); progress is called
    with the running offset after each committed batch and the last chunk is clamped."""
    conn = _open(str(tmp_path / "i.sqlite"))
    recs = [_conv("c%d" % i, "t", "tok%d" % i) for i in range(5)]
    offsets = []
    stats = index.build_index(conn, [_src("f.jsonl", recs)], chunk_size=2,
                              progress=lambda f, o: offsets.append((f, o)))
    assert offsets == [("f.jsonl", 2), ("f.jsonl", 4), ("f.jsonl", 5)]
    assert stats.chunks_committed == 3 and stats.records_processed == 5
    assert index.count(conn) == 5
    assert corpus.get_checkpoint(conn, "f.jsonl") == (5, index.hash_content("v1"))


def test_build_index_rejects_a_non_positive_chunk_size(tmp_path):
    conn = _open(str(tmp_path / "i.sqlite"))
    with pytest.raises(ValueError):
        index.build_index(conn, [], chunk_size=0)


def test_build_index_skips_a_source_with_no_records(tmp_path):
    """An empty source is a no-op skip and is not checkpointed (nothing was ingested)."""
    conn = _open(str(tmp_path / "i.sqlite"))
    stats = index.build_index(conn, [_src("empty.jsonl", [])])
    assert stats.files_total == 1 and stats.files_skipped == 1
    assert stats.records_processed == 0 and stats.chunks_committed == 0
    assert index.count(conn) == 0
    assert corpus.get_checkpoint(conn, "empty.jsonl") is None


# ------------------------------------------------------------- RESUMABILITY

def test_build_index_resumes_from_a_checkpoint_after_an_interruption(tmp_path):
    """A build crashes right after its first chunk commits; a resume on a FRESH
    connection to the same file picks up at the checkpoint and lands the identical
    index an uninterrupted build would — proving the checkpoint was persisted to disk
    and the resume neither restarts nor duplicates."""
    recs = [_conv("c%d" % i, "t%d" % i, "tok%d" % i) for i in range(3)]
    h = index.hash_content("v1")

    # reference: an uninterrupted build in its own db
    clean = _open(str(tmp_path / "clean.sqlite"))
    index.build_index(clean, [index.IndexSource("roll.jsonl", h, list(recs))],
                      chunk_size=1)
    assert index.count(clean) == 3

    # interrupted build: raise from progress after the first committed chunk
    path = str(tmp_path / "resume.sqlite")
    conn_a = _open(path)
    seen = []

    def crash(_file, offset):
        seen.append(offset)
        if len(seen) == 1:
            raise RuntimeError("simulated crash after the first chunk")

    with pytest.raises(RuntimeError):
        index.build_index(conn_a, [index.IndexSource("roll.jsonl", h, list(recs))],
                          chunk_size=1, progress=crash)
    assert index.count(conn_a) == 1                          # only chunk 1 persisted
    assert corpus.get_checkpoint(conn_a, "roll.jsonl") == (1, h)
    conn_a.close()                                            # simulate process death

    # resume on a brand-new connection -> an independent signal
    conn_b = _open(path)
    stats = index.build_index(conn_b, [index.IndexSource("roll.jsonl", h, list(recs))],
                              chunk_size=1)
    assert stats.records_processed == 2                      # only records[1:] this run
    assert index.count(conn_b) == 3 and _fts_postings(conn_b) == 3
    assert corpus.get_checkpoint(conn_b, "roll.jsonl") == (3, h)
    for i in range(3):                                        # each token once, no dups
        assert len(index.search(conn_b, "tok%d" % i)) == 1


# --------------------------------------------------------------- IDEMPOTENCY

def test_rerun_with_a_matching_hash_skips_the_file(tmp_path):
    """The content-hash fast path: an identical re-run touches nothing and adds no
    postings — the resumable build is idempotent."""
    conn = _open(str(tmp_path / "i.sqlite"))
    src = _src("f.jsonl", [_conv("c0", "t", "alpha"), _conv("c1", "t", "beta")])
    index.build_index(conn, [src])
    before = _fts_postings(conn)
    stats = index.build_index(conn, [src])
    assert stats.files_skipped == 1 and stats.records_processed == 0
    assert stats.chunks_committed == 0
    assert _fts_postings(conn) == before
    assert len(index.search(conn, "alpha")) == 1


def test_rerun_with_a_changed_hash_reingests_without_duplicating(tmp_path):
    """A changed source (new hash) re-scans from record 0, adds only genuinely new
    conversation_ids, and never duplicates a posting for an already-indexed id."""
    conn = _open(str(tmp_path / "i.sqlite"))
    r0, r1 = _conv("c0", "t", "alpha"), _conv("c1", "t", "beta")
    index.build_index(conn, [index.IndexSource("f.jsonl", index.hash_content("v1"),
                                               [r0, r1])])
    assert index.count(conn) == 2

    r2 = _conv("c2", "t", "gamma")
    h2 = index.hash_content("v2")
    stats = index.build_index(conn, [index.IndexSource("f.jsonl", h2, [r0, r1, r2])])
    assert index.count(conn) == 3 and _fts_postings(conn) == 3
    assert stats.files_skipped == 0 and stats.records_processed == 3
    for tok in ("alpha", "beta", "gamma"):
        assert len(index.search(conn, tok)) == 1
    assert corpus.get_checkpoint(conn, "f.jsonl") == (3, h2)


# --------------------------------------------------------------- LOCK RETRY

def test_build_index_wraps_its_writes_in_the_lock_retry(tmp_path, monkeypatch):
    """A transient SQLITE_LOCKED on the FIRST write is retried and the build completes:
    proof the write path is actually wrapped, not merely that a retry helper exists."""
    real_add = corpus.add_conversation
    calls = {"n": 0}
    slept = []

    def flaky_add(conn, conv, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _operr("database is locked")
        return real_add(conn, conv, **kw)

    monkeypatch.setattr(corpus, "add_conversation", flaky_add)
    conn = _open(str(tmp_path / "i.sqlite"))
    index.build_index(conn, [_src("f.jsonl", [_conv("c0", "t", "alpha")])],
                      chunk_size=1, retry_delay=0, sleep=slept.append)
    assert calls["n"] == 2 and slept == [0]        # failed once, retried, succeeded
    assert index.count(conn) == 1
    assert index.search(conn, "alpha")[0]["conversation_id"] == "c0"


# ------------------------------------------------------- count / search API

def test_count_and_fts_postings_are_zero_on_a_fresh_index(tmp_path):
    conn = _open(str(tmp_path / "i.sqlite"))
    assert index.count(conn) == 0 and _fts_postings(conn) == 0


def test_fts_postings_equal_the_conversation_count_after_a_build(tmp_path):
    conn = _open(str(tmp_path / "i.sqlite"))
    recs = [_conv("c%d" % i, "t", "tok") for i in range(4)]
    index.build_index(conn, [_src("f.jsonl", recs)])
    assert _fts_postings(conn) == index.count(conn) == 4


def test_search_on_a_blank_query_returns_no_rows(tmp_path):
    conn = _open(str(tmp_path / "i.sqlite"))
    index.build_index(conn, [_src("f.jsonl", [_conv("c0", "t", "alpha")])])
    assert index.search(conn, "   ") == []
    assert index.ranked_search(conn, "   ") == []


def test_search_respects_the_limit(tmp_path):
    conn = _open(str(tmp_path / "i.sqlite"))
    recs = [_conv("c%d" % i, "t", "omega") for i in range(3)]
    index.build_index(conn, [_src("f.jsonl", recs)])
    assert len(index.search(conn, "omega", limit=2)) == 2


def test_ranked_search_returns_rows_with_bm25_scores_best_first(tmp_path):
    """ranked_search surfaces a bm25_score per row, ordered best-first (ascending bm25).
    The index is contentless with detail=none, so bm25 carries no term-frequency signal
    and matching rows tend to tie — this asserts the ordering key and the matched set,
    NOT a fine-grained relevance separation the schema cannot provide."""
    conn = _open(str(tmp_path / "i.sqlite"))
    recs = [_conv("c0", "t", "alpha beta"), _conv("c1", "t", "alpha"),
            _conv("c2", "t", "gamma")]
    index.build_index(conn, [_src("f.jsonl", recs)])
    rows = index.ranked_search(conn, "alpha")
    assert {r["conversation_id"] for r in rows} == {"c0", "c1"}
    scores = [r["bm25_score"] for r in rows]
    assert all(isinstance(s, float) for s in scores)
    assert scores == sorted(scores)                      # best-first == ascending bm25
    assert len(index.ranked_search(conn, "alpha", limit=1)) == 1


# ------------------------------------------------- D-3 facets reach index.* (CF-15)
#
# `corpus.search` grew `provider` / `since` / `until` in D-3 and `corpus.search_filter_sql`
# is the ONE policy all filtered call sites share — its own docstring names `search`,
# `search_histogram` and the sidecar's `_run_search` as the three. `index.py` was outside
# D-3's declared scope, so it was left behind: `index.search` delegated WITHOUT the facets
# and `index.ranked_search` hand-rolled SQL that had no filter clause at all. Two different
# mechanisms, one missing pass-through.
#
# HONEST SCOPE, because this is easy to overstate: `ranked_search` has ZERO production
# callers (measured repo-wide; only its own tests and the build artifacts under
# `src-tauri/target/`). So this completes a CONTRACT on a function that is itself an
# unwired seam — it is not a defect a user can currently reach. `index.search` IS reachable
# from `cli` and the export tests, but no caller passes a facet today either.

def _dated(cid, title, body, provider, created):
    conv = _conv(cid, title, body)
    conv.provider = provider
    conv.created_at = created
    return conv


def _faceted_index(tmp_path):
    conn = _open(str(tmp_path / "facets.sqlite"))
    index.build_index(conn, [_src("f.jsonl", [
        _dated("c-codex", "t", "alpha", "codex", "2026-01-15T00:00:00Z"),
        _dated("c-grok", "t", "alpha", "grok", "2026-06-15T00:00:00Z"),
    ])])
    return conn


def test_index_search_passes_the_provider_facet_through(tmp_path):
    conn = _faceted_index(tmp_path)
    assert {r["conversation_id"] for r in index.search(conn, "alpha")} == {"c-codex", "c-grok"}
    assert [r["conversation_id"] for r in
            index.search(conn, "alpha", provider="grok")] == ["c-grok"]


def test_index_search_passes_the_date_bounds_through(tmp_path):
    conn = _faceted_index(tmp_path)
    assert [r["conversation_id"] for r in
            index.search(conn, "alpha", since="2026-03")] == ["c-grok"]
    assert [r["conversation_id"] for r in
            index.search(conn, "alpha", until="2026-03")] == ["c-codex"]


def test_ranked_search_passes_the_facets_through_AND_keeps_its_score(tmp_path):
    """The filter must not cost the thing `ranked_search` exists for: the bm25 column."""
    conn = _faceted_index(tmp_path)
    rows = index.ranked_search(conn, "alpha", provider="grok")
    assert [r["conversation_id"] for r in rows] == ["c-grok"]
    assert isinstance(rows[0]["bm25_score"], float)
    assert [r["conversation_id"] for r in
            index.ranked_search(conn, "alpha", since="2026-03")] == ["c-grok"]


def test_an_unfiltered_call_is_byte_identical_to_before(tmp_path):
    """`search_filter_sql` returns `("", [])` when nothing is filtered, so the unfiltered
    query plan must be untouched — the additive guarantee D-3 states for `corpus.search`."""
    conn = _faceted_index(tmp_path)
    assert ({r["conversation_id"] for r in index.search(conn, "alpha")}
            == {r["conversation_id"] for r in index.search(conn, "alpha", provider=None)})
    assert len(index.ranked_search(conn, "alpha")) == 2


def test_the_facets_use_the_SHARED_policy_not_a_fourth_copy(tmp_path):
    """`corpus.search_filter_sql` is the one policy; a hand-rolled clause here would be a
    fourth copy, and its own docstring says why three would already drift. Asserted by
    BEHAVIOUR — a prefix bound (`2026-06`, not a full timestamp) is the distinctive thing
    that policy does, so matching it is evidence the shared helper is what ran.
    """
    conn = _faceted_index(tmp_path)
    assert [r["conversation_id"] for r in
            index.search(conn, "alpha", since="2026-06", until="2026-06")] == ["c-grok"]
    assert [r["conversation_id"] for r in
            index.ranked_search(conn, "alpha", since="2026-06", until="2026-06")] == ["c-grok"]
