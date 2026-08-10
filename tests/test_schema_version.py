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

WHY SOME TESTS MONKEYPATCH THE CONSTANTS — and why fewer of them need to now.
`SCHEMA_VERSION` was 1, and 1 being the first version there is meant no real on-disk index
could be OLDER than the build, so the older-index half of the policy had no real-data
instance and the constants were the seam that let it be proven early.

G-4 spent that seam for real: the FTS rebuild (`detail=none` -> `detail=full`) cannot be
applied in place, so `SCHEMA_VERSION` is 2, `_ADDITIVE_FROM` stays EMPTY, and a version-1
index — which is every index in existence before G-4 — now genuinely reports that a rebuild
is required. `test_a_real_pre_G4_index_is_REFUSED_with_a_rebuild_instruction` exercises that
with no patching at all. The MIGRATE branch is still hypothetical (nothing additive has
shipped yet) and still needs its patch.

The pure verdict function is tested directly with explicit arguments, so the policy itself
needs no patching in any case.
"""
import hashlib
import pathlib
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
    """An index built the way every index that existed before G-4 was built: no marker
    table, a `detail=none` contentless FTS, and no `conversation_bodies`.

    IT REALLY IS THE OLD SHAPE, not just the current shape with the marker stripped. That
    shortcut was what this fixture did while `SCHEMA_VERSION` was 1 and it was harmless
    then, because the only thing under test was the absent marker. It stopped being
    harmless the moment version 1 acquired a MEANING: an index that carries the new FTS and
    the new bodies table but no marker is a version-2 index that lost its stamp, which is a
    different situation from the one every real user has. Reverting the two G-4 changes here
    keeps the fixture honest about what is on those users' disks.
    """
    conn = sqlite3.connect(path)
    corpus.init_index(conn)
    _populate(conn)
    conn.executescript(
        "DROP TABLE schema_meta;\n"
        "DROP TABLE conversation_bodies;\n"
        # D-5's additive table postdates version 1 too, so a faithful version-1 index has
        # none. Dropping it changes no assertion here — the refusal fires on the absent
        # MARKER, before any table is read — but a fixture whose docstring promises "IT
        # REALLY IS THE OLD SHAPE" while carrying a table the old shape lacks is the quiet
        # dishonesty that same docstring was written to end.
        "DROP TABLE conversation_models;\n"
        "DROP TABLE conversations_fts;\n"
        "CREATE VIRTUAL TABLE conversations_fts USING fts5("
        "title, body, content='', detail=none);")
    conn.execute("INSERT INTO conversations_fts(rowid, title, body) "
                 "SELECT rowid, title, 'alpha bravo' FROM conversations")
    conn.commit()
    conn.close()


# ------------------------------------------------- the shipped constants, today

def test_the_shipped_version_constants_are_todays_truth():
    """Documents the state the older-index branches are measured against: version 1
    is the first version, so there is nothing to migrate FROM yet."""
    assert corpus.SCHEMA_VERSION == 2, "G-4 rebuilt the FTS table, which is not additive"
    assert corpus.UNSTAMPED_VERSION == 1, (
        "an UNMARKED index is still a 1 and must stay a 1 — this is exactly the constant "
        "whose separateness from SCHEMA_VERSION stops a version bump from skipping a real "
        "migration, and the bump to 2 is the first time that mattered")
    assert corpus._ADDITIVE_FROM == frozenset(), (
        "recreating a virtual table is not additive, so version 1 may NOT be migrated in "
        "place — it has to be reported as needing a rebuild")
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


def test_a_BRAND_NEW_database_is_not_mistaken_for_a_version_one_index(tmp_path):
    """A LATENT DEFECT THAT ONLY EXISTS ABOVE VERSION 1, and the bump to 2 is what exposed
    it: creating an index stopped working entirely.

    `check_schema_version` read an absent marker as `UNSTAMPED_VERSION` (1) unconditionally.
    That is right for an index built before the marker existed and WRONG for an empty file
    that is about to BECOME an index — and while `SCHEMA_VERSION` was also 1 the two were
    indistinguishable, so the verdict came out "ok" either way and nothing was ever wrong.
    At `SCHEMA_VERSION` 2 the same read makes a fresh `sqlite3.connect(...)` report as a
    version-1 index and `init_index` refuses it with `IndexRebuildRequired` — asking the user
    to rebuild an index that does not exist yet. Measured: 432 tests failed on the bump, all
    of them at `open_index`, none of them about the FTS change that motivated it.

    The fix is the missing half of the distinction: an absent marker means version
    `UNSTAMPED_VERSION` only when the database ALREADY HOLDS an index. So the presence of
    `conversations` is the second signal, and this test covers both directions — a bare
    database is created, and one holding an index without a marker is still refused
    (`test_a_real_pre_G4_index_is_REFUSED_with_a_rebuild_instruction`).
    """
    conn = _track(corpus.open_index(str(tmp_path / "brand-new.sqlite")))
    assert corpus.read_schema_version(conn) == corpus.SCHEMA_VERSION
    assert conn.execute(
        "SELECT count(*) FROM conversations").fetchone()[0] == 0
    # and in-memory, which is what most of the suite and every ad-hoc reader uses
    assert corpus.read_schema_version(
        _track(corpus.init_index(sqlite3.connect(":memory:")))) == corpus.SCHEMA_VERSION


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

def test_a_real_pre_G4_index_is_REFUSED_with_a_rebuild_instruction(tmp_path):
    """THE behaviour change G-4 buys, and the price it charges.

    This test previously asserted the OPPOSITE — that a pre-marker index simply opens and is
    stamped as version 1 — and it was right to, because at `SCHEMA_VERSION` 1 there was no
    difference between an unmarked index and a current one. G-4 rebuilds the FTS table, which
    `IF NOT EXISTS` cannot patch up (the option set of a virtual table is fixed at create
    time), so version 1 is now genuinely a DIFFERENT shape and opening it would half-read it:
    the schema apply would leave the old `detail=none` table in place while every new query
    assumed positions were available.

    So it is refused, by name, with both versions in the message. Recorded rather than
    quietly re-pointed, because "every existing index must be rebuilt once" is a real cost to
    a real user and the reason for it should not have to be reconstructed from a diff.
    """
    path = str(tmp_path / "old.sqlite")
    _premarker_index(path)

    conn = _track(sqlite3.connect(path))
    with pytest.raises(corpus.IndexRebuildRequired) as exc:
        corpus.init_index(conn)
    message = str(exc.value)
    assert "1" in message and str(corpus.SCHEMA_VERSION) in message
    assert "rebuilt" in message.lower(), "says what the user has to DO"


def test_the_refused_pre_G4_index_is_left_completely_alone(tmp_path):
    """A rebuild is REPORTED, never started. The old index keeps its rows, its old FTS
    table, and its absent marker, so the user can export or open it with the older build
    first. The file is compared by HASH, which also covers the `journal_mode` header the
    refusal deliberately runs ahead of."""
    path = str(tmp_path / "old.sqlite")
    _premarker_index(path)
    before = hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()

    conn = sqlite3.connect(path)
    with pytest.raises(corpus.IndexRebuildRequired):
        corpus.init_index(conn)
    conn.close()

    assert hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest() == before
    check = _track(sqlite3.connect(path))
    assert check.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1
    assert corpus.read_schema_version(check) is None, "not stamped"
    assert "detail=none" in check.execute(
        "SELECT sql FROM sqlite_master WHERE name='conversations_fts'").fetchone()[0]


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


def test_a_refused_open_does_not_write_a_single_byte(tmp_path):
    """"Before the schema is touched" is not enough: `journal_mode` is a PERSISTENT
    header setting, so enabling WAL on an archive a newer build owns is still a write
    into a shape this build does not understand. Measured with the pragma running one
    line ahead of the gate: `delete` became `wal` and the file hash changed. The gate
    therefore runs before ANY statement, and this pins the whole file."""
    path = str(tmp_path / "future.sqlite")
    seed = _track(sqlite3.connect(path))
    seed.execute("PRAGMA journal_mode=DELETE")   # the hostile case: NOT already WAL
    seed.executescript(corpus._SCHEMA_META_DDL)
    _set_marker(seed, str(corpus.SCHEMA_VERSION + 1))
    seed.close()
    before = hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()

    conn = _track(sqlite3.connect(path))
    with pytest.raises(corpus.IndexTooNewError):
        corpus.init_index(conn)
    conn.close()

    assert hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest() == before
    assert _track(sqlite3.connect(path)).execute(
        "PRAGMA journal_mode").fetchone()[0] == "delete"


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


# ============================================================ CF-22: the SURFACES
#
# The refusal above is correct and stays. What was missing is that NOTHING CAUGHT IT.
# Measured against HEAD before this section existed, with a synthetic v1 index:
#
#   python -m llm_anthology.sidecar --index old.db  -> exit 1, ZERO bytes on stdout, an
#       uncaught IndexRebuildRequired traceback. The cockpit spawns that process and gets a
#       corpse: not one JSON-RPC message, so there is nothing for the UI to render.
#   python -m llm_anthology.cli index <src> old.db  -> exit 1, same uncaught traceback.
#
# That violates decision G-2 (a user existing corpus must survive an upgrade) in the most
# user-hostile way available: a stack trace instead of a sentence. A grep for
# IndexRebuildRequired / IndexTooNewError / IndexVersionError across .py/.ts/.rs outside
# corpus.py hit only this file.
#
# WHY THERE IS NO IN-PLACE MIGRATION, AND WHY NOBODY SHOULD GO LOOKING FOR ONE. It is not
# that a v1 -> v2 migration is hard; the data it would need does not exist. The v1 FTS table
# is CONTENTLESS, so the searchable text was never stored -- only inverted postings, which do
# not reconstruct a document. conversation_bodies, the archive that WOULD hold it, is itself
# a v2 addition. A v1 index therefore cannot yield the text a v2 index needs, and the only
# route is re-ingesting the SOURCES. So the error tells the user to rebuild rather than
# offering to migrate, and recording that here is cheaper than the next person re-deriving it.

def _v1_index(tmp_path, name="old.sqlite"):
    path = str(tmp_path / name)
    _premarker_index(path)
    return path


def _drive(index_path, *lines):
    """Run sidecar.main over in-memory streams; -> the parsed reply objects."""
    import io
    import json as _json

    from llm_anthology import sidecar as sc
    stdout = io.StringIO()
    sc.main(["--index", index_path], stdin=io.StringIO("\n".join(lines) + "\n"),
            stdout=stdout)
    return [_json.loads(ln) for ln in stdout.getvalue().splitlines() if ln.strip()]


def test_the_sidecar_ANSWERS_on_a_v1_index_instead_of_dying(tmp_path):
    """THE DEFECT. The process must come up and speak JSON-RPC, not exit with a traceback.

    Sidecar(None) is already a supported state -- main own docstring says "None -> no
    corpus" -- so serving without a corpus is not a new mode, it is the existing one reached
    by a path that previously raised instead of arriving.
    """
    out = _drive(_v1_index(tmp_path), '{"jsonrpc":"2.0","id":1,"method":"health.ping"}')
    assert out, "the engine wrote NOTHING -- a spawned process that cannot answer is a corpse"
    assert out[0]["result"]["ok"] is True
    assert out[0]["result"]["corpus_ready"] is False


def test_a_corpus_call_on_a_v1_index_is_a_TYPED_error_naming_the_rebuild(tmp_path):
    """Not a generic "corpus not indexed": that sends a user looking for a missing file when
    the file is present and merely old. The code is distinct and the message is actionable."""
    from llm_anthology import sidecar as sc
    out = _drive(_v1_index(tmp_path), '{"jsonrpc":"2.0","id":1,"method":"corpus.stats"}')
    err = out[0]["error"]
    assert err["code"] == sc.INDEX_REBUILD_REQUIRED, err
    assert "rebuil" in err["message"].lower(), err
    assert "llm-anthology index" in err["message"], "names the command, not just the problem"


def test_the_v1_reason_is_visible_on_health_ping(tmp_path):
    """The cockpit calls health.ping first. Putting the reason there lets the UI say WHY the
    corpus is unavailable without provoking an error to find out."""
    out = _drive(_v1_index(tmp_path), '{"jsonrpc":"2.0","id":1,"method":"health.ping"}')
    assert "schema version 1" in out[0]["result"]["corpus_error"]


def test_a_HEALTHY_index_is_untouched_by_the_new_path(tmp_path):
    """The control. Without it, "the sidecar answers" would also pass if it had quietly
    stopped opening anything at all."""
    good = str(tmp_path / "new.sqlite")
    _track(corpus.open_index(good)).close()
    out = _drive(good, '{"jsonrpc":"2.0","id":1,"method":"health.ping"}')
    assert out[0]["result"]["corpus_ready"] is True
    assert out[0]["result"].get("corpus_error", "") == ""


def test_the_cli_reports_a_v1_index_as_a_SENTENCE_not_a_traceback(tmp_path, capsys):
    """cli index against an old index is the other spawn-a-corpse path."""
    from llm_anthology import cli
    src = tmp_path / "src"
    src.mkdir()
    code = cli.main(["index", str(src), _v1_index(tmp_path)])
    captured = capsys.readouterr()
    both = captured.err + captured.out
    assert code != 0, "a refused index is not a success"
    assert "Traceback" not in both
    assert "rebuil" in both.lower()
    assert "llm-anthology index" in both
