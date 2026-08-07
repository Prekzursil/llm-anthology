"""codex_state adapter — $CODEX_HOME/state_5.sqlite (the LIVE Codex state DB) -> Corpus.

SYNTHETIC fixtures ONLY. The real state DB is PRIVATE medical/pharma data; nothing
here is a real thread id, path, cwd, title, or token count. Every fixture is a made-up
sqlite shape that MIRRORS the Phase-0 measured schema (threads + thread_spawn_edges,
895 edges / 1831 threads on disk) — never its content.

Schema-tolerance is proved by building the `threads` table TWO ways:
  * the LIVE shape, WITH updated_at_ms (plus unmapped live columns the adapter ignores);
  * the LEGACY-canonical shape, WITHOUT updated_at_ms (the field must stay None, never a
    fabricated 0), with the row order DERIVED from whichever timestamp remains.
"""
import sqlite3

import pytest

from llm_anthology import corpus
from llm_anthology.adapters import codex_state


# --------------------------------------------------------------------- fixtures

# Every sqlite connection a test opens is tracked and closed on teardown so the suite
# stays free of ResourceWarnings (mirrors tests/test_corpus.py).
_OPEN = []


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _conn():
    c = sqlite3.connect(":memory:")
    _OPEN.append(c)
    return c


# The LIVE column set: the twelve mapped columns PLUS three unmapped live columns
# (source/sandbox_policy/model) the adapter must ignore. SYNTHETIC.
_LIVE_COLS = (
    ("id", "TEXT PRIMARY KEY"), ("title", "TEXT"), ("model_provider", "TEXT"),
    ("tokens_used", "INTEGER"), ("created_at_ms", "INTEGER"),
    ("updated_at_ms", "INTEGER"), ("git_branch", "TEXT"), ("cwd", "TEXT"),
    ("agent_role", "TEXT"), ("agent_nickname", "TEXT"), ("preview", "TEXT"),
    ("rollout_path", "TEXT"),
    ("source", "TEXT"), ("sandbox_policy", "TEXT"), ("model", "TEXT"),
)
# The legacy-canonical set: NO updated_at_ms (created_at_ms survives as the order key).
_LEGACY_COLS = tuple(c for c in _LIVE_COLS if c[0] != "updated_at_ms")


def _create_threads(conn, columns):
    conn.execute("CREATE TABLE threads (%s)"
                 % ", ".join("%s %s" % c for c in columns))


def _create_edges(conn, columns=("parent_thread_id TEXT", "child_thread_id TEXT",
                                  "status TEXT")):
    conn.execute("CREATE TABLE thread_spawn_edges (%s)" % ", ".join(columns))


def _insert(conn, table, **values):
    cols = list(values)
    conn.execute("INSERT INTO %s (%s) VALUES (%s)"
                 % (table, ",".join(cols), ",".join("?" for _ in cols)),
                 [values[c] for c in cols])


def _live_conn():
    """A synthetic LIVE-shaped DB: one root thread, one child, one spawn edge."""
    conn = _conn()
    _create_threads(conn, _LIVE_COLS)
    _create_edges(conn)
    _insert(conn, "threads", id="root", title="Root", model_provider="codex",
            tokens_used=1200, created_at_ms=1000, updated_at_ms=2000,
            git_branch="feat/x", cwd="/repo", agent_role="architect",
            agent_nickname="Ada", preview="hello", rollout_path="/r/root.jsonl",
            source="cli", sandbox_policy="workspace-write", model="gpt-x")
    _insert(conn, "threads", id="child", title="Child", model_provider="codex",
            tokens_used=50, created_at_ms=1500, updated_at_ms=1600,
            rollout_path="/r/child.jsonl")
    _insert(conn, "thread_spawn_edges", parent_thread_id="root",
            child_thread_id="child", status="completed")
    return conn


# ------------------------------------------------------------ read_corpus core

def test_read_corpus_populates_the_thread_graph_from_a_live_shaped_db():
    c = codex_state.read_corpus(_live_conn())
    assert isinstance(c, corpus.Corpus)
    assert set(c.threads) == {"root", "child"}
    root = c.threads["root"]
    assert (root.title, root.model_provider, root.tokens_used) == \
        ("Root", "codex", 1200)
    assert (root.created_at_ms, root.updated_at_ms) == (1000, 2000)
    assert (root.git_branch, root.cwd, root.agent_role, root.agent_nickname) == \
        ("feat/x", "/repo", "architect", "Ada")
    assert (root.preview, root.rollout_path) == ("hello", "/r/root.jsonl")
    # the spawn edge round-trips as a SpawnEdge
    assert len(c.edges) == 1
    e = c.edges[0]
    assert (e.parent_thread_id, e.child_thread_id, e.status) == \
        ("root", "child", "completed")
    # and the graph is navigable
    assert c.roots() == ["root"] and c.children_of("root") == ["child"]
    assert c.depth("child") == 1 and c.fan_out("root") == 1


def test_unmapped_live_columns_are_ignored():
    """source/sandbox_policy/model exist in the live schema but map to no ThreadMeta
    field; the adapter selects only mapped-AND-present columns, so they are dropped
    without error rather than crashing on an unknown field."""
    root = codex_state.read_corpus(_live_conn()).threads["root"]
    assert not hasattr(root, "source") and not hasattr(root, "model")
    # exactly the contracted fields, nothing smuggled in
    assert set(vars(root)) == {
        "id", "title", "model_provider", "tokens_used", "created_at_ms",
        "updated_at_ms", "git_branch", "cwd", "agent_role", "agent_nickname",
        "preview", "rollout_path", "adapter"}
    # `adapter` is a real ThreadMeta field but is NOT sourced from this DB: it is derived
    # from `conversations.provider` in corpus.load_corpus, and this adapter reads the live
    # state DB, which has no conversations table. So it must stay at its default here.
    # Note the live schema's own `source` column ("cli" — how the session was launched)
    # is a DIFFERENT fact and stays unmapped; that is why the field is not called `source`.
    assert root.adapter == ""


def test_updated_at_ms_stays_none_when_the_column_is_absent():
    """The legacy-canonical schema-tolerance guarantee: with NO updated_at_ms column
    the field is left None (not a fabricated 0), while created_at_ms still populates."""
    conn = _conn()
    _create_threads(conn, _LEGACY_COLS)
    _create_edges(conn)
    _insert(conn, "threads", id="t1", title="Legacy", model_provider="codex",
            tokens_used=7, created_at_ms=1234, rollout_path="/r/1.jsonl")
    m = codex_state.read_corpus(conn).threads["t1"]
    assert m.updated_at_ms is None
    assert m.created_at_ms == 1234 and m.tokens_used == 7 and m.title == "Legacy"


def test_read_corpus_merges_into_an_existing_corpus_preserving_conversations():
    existing = corpus.Corpus(conversations=["conv-sentinel"])
    existing.add_thread(corpus.ThreadMeta(id="pre-existing"))
    out = codex_state.read_corpus(_live_conn(), into=existing)
    assert out is existing
    assert out.conversations == ["conv-sentinel"]              # untouched
    assert {"pre-existing", "root", "child"} <= set(out.threads)


def test_read_corpus_returns_a_fresh_corpus_when_into_is_none():
    c = codex_state.read_corpus(_live_conn())
    assert c.conversations == [] and set(c.threads) == {"root", "child"}


# ------------------------------------------------- order derived from timestamps

def test_threads_are_ordered_most_recent_first_by_updated_at_ms():
    conn = _conn()
    _create_threads(conn, _LIVE_COLS)
    _create_edges(conn)
    _insert(conn, "threads", id="t1", updated_at_ms=100, created_at_ms=1)
    _insert(conn, "threads", id="t2", updated_at_ms=300, created_at_ms=1)
    _insert(conn, "threads", id="t3", updated_at_ms=200, created_at_ms=1)
    assert list(codex_state.read_corpus(conn).threads) == ["t2", "t3", "t1"]


def test_thread_order_falls_back_to_created_at_ms_without_updated_at_ms():
    """updated_at_ms absent -> the order is DERIVED from the next available timestamp
    (created_at_ms), still most-recent-first."""
    conn = _conn()
    _create_threads(conn, _LEGACY_COLS)
    _create_edges(conn)
    _insert(conn, "threads", id="t1", created_at_ms=10)
    _insert(conn, "threads", id="t2", created_at_ms=30)
    _insert(conn, "threads", id="t3", created_at_ms=20)
    assert list(codex_state.read_corpus(conn).threads) == ["t2", "t3", "t1"]


def test_thread_order_falls_back_to_id_when_no_timestamp_column_exists():
    """No timestamp column at all -> deterministic id order rather than an error."""
    conn = _conn()
    _create_threads(conn, (("id", "TEXT PRIMARY KEY"), ("title", "TEXT")))
    _create_edges(conn)
    for tid in ("t3", "t1", "t2"):
        _insert(conn, "threads", id=tid, title="x")
    assert list(codex_state.read_corpus(conn).threads) == ["t1", "t2", "t3"]


# --------------------------------------------------------- NULL-column coercion

def test_null_string_columns_become_the_empty_string():
    """A nullable TEXT column (git_branch is NULL-able in the live schema) yields ''
    not None, matching the on-disk threads table's NOT NULL contract."""
    conn = _conn()
    _create_threads(conn, _LIVE_COLS)
    _create_edges(conn)
    _insert(conn, "threads", id="t1", title="T", updated_at_ms=5)  # git_branch NULL
    m = codex_state.read_corpus(conn).threads["t1"]
    assert m.git_branch == "" and m.cwd == "" and m.agent_role == ""
    assert m.agent_nickname == "" and m.model_provider == "" and m.preview == ""


def test_null_tokens_used_becomes_zero():
    conn = _conn()
    _create_threads(conn, _LIVE_COLS)
    _create_edges(conn)
    _insert(conn, "threads", id="t1", title="T", updated_at_ms=5)  # tokens_used NULL
    assert codex_state.read_corpus(conn).threads["t1"].tokens_used == 0


# ------------------------------------------------------ absent / partial tables

def test_no_threads_table_yields_no_threads():
    conn = _conn()
    _create_edges(conn)
    _insert(conn, "thread_spawn_edges", parent_thread_id="p", child_thread_id="c",
            status="ok")
    c = codex_state.read_corpus(conn)
    assert c.threads == {} and len(c.edges) == 1  # edges still read


def test_no_edges_table_yields_no_edges():
    conn = _conn()
    _create_threads(conn, _LIVE_COLS)
    _insert(conn, "threads", id="t1", updated_at_ms=5)
    c = codex_state.read_corpus(conn)
    assert set(c.threads) == {"t1"} and c.edges == []  # threads still read


def test_edges_tolerate_a_missing_status_column():
    """An edges table without `status` still yields SpawnEdges (status defaults '')."""
    conn = _conn()
    _create_threads(conn, _LIVE_COLS)
    _insert(conn, "threads", id="p", updated_at_ms=5)
    _insert(conn, "threads", id="c", updated_at_ms=6)
    _create_edges(conn, columns=("parent_thread_id TEXT", "child_thread_id TEXT"))
    _insert(conn, "thread_spawn_edges", parent_thread_id="p", child_thread_id="c")
    e = codex_state.read_corpus(conn).edges[0]
    assert (e.parent_thread_id, e.child_thread_id, e.status) == ("p", "c", "")


def test_null_edge_status_becomes_the_empty_string():
    conn = _conn()
    _create_edges(conn)
    _insert(conn, "thread_spawn_edges", parent_thread_id="p", child_thread_id="c")
    assert codex_state.read_corpus(conn).edges[0].status == ""


# --------------------------------------------------------- runtime count helper

def test_counts_report_runtime_row_counts_never_a_hardcoded_constant():
    conn = _live_conn()
    assert codex_state.counts(conn) == {"threads": 2, "edges": 1}


def test_counts_are_zero_when_the_tables_are_absent():
    assert codex_state.counts(_conn()) == {"threads": 0, "edges": 0}


# --------------------------------------------------------- db-path resolution

def test_db_path_uses_an_explicit_codex_home():
    import os
    assert codex_state._db_path("/home/x/.codex") == \
        os.path.join("/home/x/.codex", "state_5.sqlite")


def test_db_path_falls_back_to_the_codex_home_env(monkeypatch):
    import os
    monkeypatch.setenv("CODEX_HOME", "/env/codex")
    assert codex_state._db_path() == os.path.join("/env/codex", "state_5.sqlite")


def test_db_path_falls_back_to_the_default_home_without_env(monkeypatch):
    import os
    monkeypatch.delenv("CODEX_HOME", raising=False)
    expected = os.path.join(os.path.expanduser("~/.codex"), "state_5.sqlite")
    assert codex_state._db_path() == expected


def test_ro_uri_is_readonly_immutable_and_escapes_uri_specials():
    uri = codex_state._ro_uri(r"C:\a?b#c\state_5.sqlite")
    assert uri.startswith("file:") and uri.endswith("?mode=ro&immutable=1")
    assert "%3f" in uri and "%23" in uri  # the ? and # inside the PATH are escaped
    assert "?b#c" not in uri              # ... so they cannot break the query string


# ------------------------------------------------------------- busy detection

def test_is_busy_detects_locked_and_busy_but_not_other_errors():
    assert codex_state._is_busy(sqlite3.OperationalError("database is locked"))
    assert codex_state._is_busy(sqlite3.OperationalError("database table is busy"))
    assert not codex_state._is_busy(
        sqlite3.OperationalError("unable to open database file"))


# ------------------------------------------------- retry-then-skip on a busy DB

def test_read_with_retry_retries_a_busy_db_then_succeeds():
    real = _live_conn()
    n = {"calls": 0}

    def opener():
        n["calls"] += 1
        if n["calls"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real

    sleeps = []
    c = codex_state._read_with_retry(opener, retries=3, retry_delay=0.05,
                                     sleep=sleeps.append)
    assert sleeps == [0.05]                       # retried exactly once
    assert set(c.threads) == {"root", "child"}    # then read the graph


def test_read_with_retry_skips_after_exhausting_retries_on_a_persistently_busy_db():
    def opener():
        raise sqlite3.OperationalError("database is locked")

    sleeps = []
    c = codex_state._read_with_retry(opener, retries=3, retry_delay=0.05,
                                     sleep=sleeps.append)
    assert c.threads == {} and c.edges == []      # skipped -> empty corpus
    assert sleeps == [0.05, 0.05, 0.05]           # retried `retries` times first


def test_read_with_retry_skips_immediately_on_a_non_busy_error():
    def opener():
        raise sqlite3.OperationalError("unable to open database file")

    sleeps = []
    c = codex_state._read_with_retry(opener, retries=3, retry_delay=0.05,
                                     sleep=sleeps.append)
    assert c.threads == {} and c.edges == []
    assert sleeps == []                           # a non-busy error is NOT retried


# ------------------------------------------------------------- load_corpus e2e

def _write_real_state_db(path):
    """Write a synthetic state_5.sqlite to `path` in the default (delete) journal mode
    so it can be reopened read-only + immutable, exactly as the adapter does."""
    conn = sqlite3.connect(str(path))
    try:
        _create_threads(conn, _LIVE_COLS)
        _create_edges(conn)
        _insert(conn, "threads", id="root", title="Root", tokens_used=10,
                created_at_ms=1, updated_at_ms=2, rollout_path="/r/root.jsonl")
        _insert(conn, "threads", id="child", title="Child", updated_at_ms=1,
                rollout_path="/r/child.jsonl")
        _insert(conn, "thread_spawn_edges", parent_thread_id="root",
                child_thread_id="child", status="completed")
        conn.commit()
    finally:
        conn.close()


def test_load_corpus_reads_a_real_state_db_end_to_end(tmp_path):
    _write_real_state_db(tmp_path / "state_5.sqlite")
    c = codex_state.load_corpus(codex_home=str(tmp_path))
    assert set(c.threads) == {"root", "child"}
    assert c.roots() == ["root"] and c.children_of("root") == ["child"]
    assert list(c.threads) == ["root", "child"]   # updated_at_ms DESC (2 then 1)


def test_load_corpus_skips_a_missing_db_and_returns_an_empty_corpus(tmp_path):
    # tmp_path exists but holds NO state_5.sqlite -> a read-only open fails (non-busy),
    # so the build gets an empty corpus instead of a crash.
    c = codex_state.load_corpus(codex_home=str(tmp_path))
    assert isinstance(c, corpus.Corpus)
    assert c.threads == {} and c.edges == []
