"""corpus.py — the on-disk SCHEMA VERSION marker (decision D-1).

SYNTHETIC fixtures ONLY. Every conversation, thread id and path here is made up; the
real corpus is PRIVATE and nothing in this file touches it.

WHAT IS PINNED. A future breaking change to the index has to be able to detect an
index it cannot safely open, and today nothing records which shape an index is in.
This file pins the marker and the whole OPEN POLICY around it:

  * the marker is a `schema_meta(key, value)` TABLE, not `PRAGMA user_version` — the
    schema's own migration rule (corpus.py: a NEW FACT is a new TABLE, never a new
    column) makes a table additive and migration-free, and it stays human-readable;
  * an index NEWER than this build REFUSES to open, so a downgrade cannot write into
    an archive it does not understand;
  * an OLDER index migrates in place when the delta is additive, and otherwise says
    a rebuild is required — it is never silently rebuilt;
  * an index with NO marker row — which is EVERY index that exists today — keeps
    working: it is version 1 by definition, and it is stamped on open.

WHY SOME TESTS MONKEYPATCH THE CONSTANTS. `SCHEMA_VERSION` is 1 and 1 is the first
version there is, so no real on-disk index can currently be OLDER than this build.
The older-index half of the policy therefore has no real-data instance yet, and the
constants (`SCHEMA_VERSION`, `_ADDITIVE_FROM`) are the seam that lets it be proven
now instead of the first time it matters. The pure verdict function is tested
directly with explicit arguments, so the policy itself needs no patching at all.
"""
import sqlite3

import pytest

from llm_anthology import corpus, ir


# --------------------------------------------------------------------- fixtures

_OPEN = []


def _track(conn):
    _OPEN.append(conn)
    return conn


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _conv(cid="c1", title="A synthetic title", body_text="alpha bravo"):
    return ir.Conversation(
        id=cid, title=title, provider="codex", account="acct",
        turns=[ir.Turn(role="human", blocks=[ir.Block(type="text", text=body_text)])],
        created_at="2026-01-01", updated_at="2026-01-02")


def _populate(conn):
    """Put one synthetic conversation and one thread into an index."""
    corpus.upsert_thread(conn, corpus.ThreadMeta(id="t1", title="a thread"))
    corpus.add_conversation(conn, _conv(), thread_id="t1")
    conn.commit()


def _set_marker(conn, value):
    """Write a RAW marker value, bypassing the stamp so a test can forge any state."""
    conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                 (corpus.SCHEMA_VERSION_KEY, value))
    conn.commit()


def _premarker_index(path):
    """An index built the way every index that exists TODAY was built: full schema,
    real data, and NO marker table at all. Built with the current `init_index` and
    then stripped, because that is the only way to produce the pre-marker shape from
    a build that always stamps."""
    conn = sqlite3.connect(path)
    corpus.init_index(conn)
    _populate(conn)
    conn.executescript("DROP TABLE schema_meta")
    conn.close()


# ------------------------------------------------- the shipped constants, today

def test_the_shipped_version_constants_are_todays_truth():
    """Documents the state the older-index branches are measured against: version 1
    is the first version, so there is nothing to migrate FROM yet."""
    assert corpus.SCHEMA_VERSION == 1
    assert corpus.UNSTAMPED_VERSION == 1
    assert corpus._ADDITIVE_FROM == frozenset()
    assert corpus.SCHEMA_VERSION_KEY == "schema_version"


def test_every_refusal_is_catchable_as_one_error_type():
    """A caller that only wants "this index is unusable" catches the base class."""
    assert issubclass(corpus.IndexTooNewError, corpus.IndexVersionError)
    assert issubclass(corpus.IndexRebuildRequired, corpus.IndexVersionError)
    assert issubclass(corpus.IndexVersionError, RuntimeError)


# ------------------------------------------------------- the pure verdict seam

def test_the_same_version_is_ok():
    assert corpus._version_verdict(3, 3, frozenset()) == "ok"


def test_a_newer_index_verdict_is_newer():
    assert corpus._version_verdict(4, 3, frozenset({1, 2})) == "newer"


def test_an_older_index_listed_as_additive_migrates():
    assert corpus._version_verdict(2, 3, frozenset({2})) == "migrate"


def test_an_older_index_not_listed_as_additive_needs_a_rebuild():
    assert corpus._version_verdict(1, 3, frozenset({2})) == "rebuild"


def test_the_verdict_vocabulary_is_exactly_four_words():
    """Guards a typo in a comparison: a fifth spelling would mean a branch that
    neither raises nor migrates, i.e. a silent open."""
    seen = {corpus._version_verdict(f, 3, frozenset({2})) for f in (1, 2, 3, 4)}
    assert seen == {"ok", "newer", "migrate", "rebuild"}


# --------------------------------------------------------------- the marker table

def test_init_index_creates_the_marker_table_with_the_decided_shape():
    conn = _track(corpus.init_index(sqlite3.connect(":memory:")))
    cols = {r[1]: (r[2], r[5]) for r in conn.execute("PRAGMA table_info(schema_meta)")}
    assert cols["key"] == ("TEXT", 1), "key is the PRIMARY KEY"
    assert cols["value"][0] == "TEXT"


def test_a_fresh_index_is_stamped_at_the_current_version():
    conn = _track(corpus.init_index(sqlite3.connect(":memory:")))
    assert corpus.read_schema_version(conn) == corpus.SCHEMA_VERSION


def test_the_stamp_is_COMMITTED_so_it_survives_a_reconnect(tmp_path):
    """The stamp is a write inside a function every read path calls, so leaving it in
    an open transaction would both lose the marker and hold a lock on the file."""
    path = str(tmp_path / "idx.sqlite")
    _track(corpus.open_index(path)).close()
    fresh = _track(sqlite3.connect(path))
    assert corpus.read_schema_version(fresh) == corpus.SCHEMA_VERSION


def test_read_returns_none_when_the_marker_table_is_absent():
    """A bare database is not an error — "no marker" is a legitimate answer, and it is
    what every index built before this change reports."""
    assert corpus.read_schema_version(_track(sqlite3.connect(":memory:"))) is None


def test_read_returns_none_when_the_table_exists_but_the_row_does_not():
    conn = _track(corpus.init_index(sqlite3.connect(":memory:")))
    conn.execute("DELETE FROM schema_meta")
    assert corpus.read_schema_version(conn) is None


def test_an_unreadable_marker_is_refused_rather_than_guessed():
    conn = _track(corpus.init_index(sqlite3.connect(":memory:")))
    _set_marker(conn, "not-a-number")
    with pytest.raises(corpus.IndexVersionError) as exc:
        corpus.read_schema_version(conn)
    assert "not-a-number" in str(exc.value)


# ------------------------------------------- an index that predates the marker

def test_a_premarker_index_still_reads(tmp_path):
    """THE compatibility proof: an index built WITHOUT the marker opens, and every
    conversation and thread in it is still there afterwards."""
    path = str(tmp_path / "old.sqlite")
    _premarker_index(path)

    conn = _track(corpus.open_index(path))

    assert [r["conversation_id"] for r in corpus.search(conn, "bravo")] == ["c1"]
    assert sorted(corpus.load_corpus(conn).threads) == ["t1"]


def test_a_premarker_index_is_stamped_as_version_one_on_open(tmp_path):
    path = str(tmp_path / "old.sqlite")
    _premarker_index(path)
    conn = _track(corpus.open_index(path))
    assert corpus.read_schema_version(conn) == 1


#: Every INSERT a `_SpyConn` saw aimed at `schema_meta`. A module-level list because
#: `sqlite3.Connection` is a C type with no `__dict__` — `conn.execute = spy` raises
#: AttributeError, so the seam has to be a subclass handed to `connect(factory=...)`.
_SPY_INSERTS = []


class _SpyConn(sqlite3.Connection):
    def execute(self, sql, *args):
        if "schema_meta" in sql and sql.lstrip().upper().startswith("INSERT"):
            _SPY_INSERTS.append(sql)
        return sqlite3.Connection.execute(self, sql, *args)


def test_the_spy_sees_a_marker_write_when_there_IS_one(tmp_path):
    """Detector control for the test below: a stamp that DOES happen is caught. Without
    this, an always-empty spy would report "no write" for a broken reason."""
    del _SPY_INSERTS[:]
    conn = _track(sqlite3.connect(str(tmp_path / "new.sqlite"), factory=_SpyConn))
    corpus.init_index(conn)
    assert len(_SPY_INSERTS) == 1


def test_opening_a_current_index_twice_does_not_rewrite_the_marker(tmp_path):
    """The stamp is skipped when the marker already reads the current version, so
    re-opening an up-to-date index writes nothing to it."""
    path = str(tmp_path / "idx.sqlite")
    _track(corpus.open_index(path)).close()
    del _SPY_INSERTS[:]
    corpus.init_index(_track(sqlite3.connect(path, factory=_SpyConn)))
    assert _SPY_INSERTS == []


# ---------------------------------------------------- an index NEWER than us

def test_an_index_from_a_newer_build_refuses_to_open(tmp_path):
    path = str(tmp_path / "future.sqlite")
    seed = _track(sqlite3.connect(path))
    corpus.init_index(seed)
    _set_marker(seed, str(corpus.SCHEMA_VERSION + 1))
    seed.close()

    conn = _track(sqlite3.connect(path))
    with pytest.raises(corpus.IndexTooNewError) as exc:
        corpus.init_index(conn)
    message = str(exc.value)
    assert str(corpus.SCHEMA_VERSION + 1) in message, "names the index version"
    assert str(corpus.SCHEMA_VERSION) in message, "names the build version"


def test_the_refusal_happens_BEFORE_the_schema_is_touched(tmp_path):
    """Ordering proof, and the reason the refusal exists at all: a downgrade must not
    write into an archive it does not understand. A table dropped by the newer build
    is still absent after the refused open — so `executescript` never ran."""
    path = str(tmp_path / "future.sqlite")
    seed = _track(sqlite3.connect(path))
    corpus.init_index(seed)
    seed.executescript("DROP TABLE ingest_checkpoint")
    _set_marker(seed, str(corpus.SCHEMA_VERSION + 1))
    seed.close()

    conn = _track(sqlite3.connect(path))
    with pytest.raises(corpus.IndexTooNewError):
        corpus.init_index(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ingest_checkpoint" not in tables


# ---------------------------------------------------- an index OLDER than us

def _index_at(path, version):
    conn = sqlite3.connect(path)
    corpus.init_index(conn)
    _populate(conn)
    _set_marker(conn, str(version))
    conn.close()


def test_an_older_index_with_an_additive_delta_migrates_in_place(tmp_path, monkeypatch):
    """The additive case: the missing tables are created by `init_index` itself, so
    recording the new version IS the whole migration — no rebuild, no data loss."""
    path = str(tmp_path / "v1.sqlite")
    _index_at(path, 1)
    monkeypatch.setattr(corpus, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(corpus, "_ADDITIVE_FROM", frozenset({1}))

    conn = _track(corpus.open_index(path))

    assert corpus.read_schema_version(conn) == 2, "re-stamped forward"
    assert [r["conversation_id"] for r in corpus.search(conn, "bravo")] == ["c1"]


def test_an_older_index_with_a_breaking_delta_asks_for_a_rebuild(tmp_path, monkeypatch):
    """The non-additive case — what the coming FTS rebuild will be. It reports, it
    does NOT silently rebuild, and it names both versions."""
    path = str(tmp_path / "v1.sqlite")
    _index_at(path, 1)
    monkeypatch.setattr(corpus, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(corpus, "_ADDITIVE_FROM", frozenset())

    conn = _track(sqlite3.connect(path))
    with pytest.raises(corpus.IndexRebuildRequired) as exc:
        corpus.init_index(conn)
    message = str(exc.value)
    assert "1" in message and "2" in message
    assert "rebuilt" in message.lower(), "says what the user has to DO"


def test_a_breaking_delta_leaves_the_old_index_intact(tmp_path, monkeypatch):
    """"Report that a rebuild is required" must not be "start one": the refused index
    still holds its rows, so the user can downgrade or export before rebuilding."""
    path = str(tmp_path / "v1.sqlite")
    _index_at(path, 1)
    monkeypatch.setattr(corpus, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(corpus, "_ADDITIVE_FROM", frozenset())

    conn = _track(sqlite3.connect(path))
    with pytest.raises(corpus.IndexRebuildRequired):
        corpus.init_index(conn)
    assert conn.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1
    assert corpus.read_schema_version(conn) == 1, "not re-stamped"
