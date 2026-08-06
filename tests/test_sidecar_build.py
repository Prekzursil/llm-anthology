"""The corpus.build RPC surface — in-app ingest, run off the request thread.

Before this surface existed the app could DISPLAY a corpus but never CREATE one:
``loaders.load_corpus`` was the only function that builds an index and it had zero
production callers. These tests pin the four properties that make wiring it to a
synchronous, single-pipe, mutex-guarded Rust client safe:

  * NON-BLOCKING. ``corpus.build`` returns a job handle immediately; the ingest runs on a
    background thread. A blocking build would hold the client mutex for minutes and freeze
    every other RPC, so "returns while the work is still running" is the contract, tested
    with an injected runner rather than a sleep.
  * THREAD-CONFINED SQLITE. ``corpus.open_index`` builds its connection with sqlite3's
    default ``check_same_thread=True``, so touching ``self.conn`` off-thread raises
    ProgrammingError. The worker is handed a PATH and opens its own connection; the test
    below drives a REAL thread and asserts no such error escapes.
  * ``codex_home`` REQUIRED and never defaulted, exactly as ``dedup.scan`` requires it —
    ``loaders.load_corpus`` with no ``codex_home`` falls back to the LIVE Codex store, so
    an ingest of private data must be something the caller named. Both it and
    ``sessions_root`` are refused when UNC (the Windows SMB/NTLM hash-leak class) or
    relative.
  * ERRORS SURFACE. ``load_corpus`` returns ``(result, errors)``; the per-file ingest log
    reaches the wire (basenamed + sanitized) instead of being dropped.

Determinism: no test sleeps. The background execution seam is injected — a synchronous
runner for the happy paths, a DEFERRED runner that hands the test the worker callable so
it controls the interleaving, and the real thread runner joined explicitly.

Synthetic rollouts written into tmp_path only; no test reads a real Codex store.
"""
import json
import os
import sqlite3
import threading

import pytest

from llm_anthology import corpus, ir, sidecar
from llm_anthology.corpus import ThreadMeta

# A zero-width space (U+200B) — the hidden-unicode payload sanitize_for_copy must strip
# from anything crossing the wire.
ZW = "​"

_OPEN = []


def _track(conn):
    _OPEN.append(conn)
    return conn


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()


# --------------------------------------------------------------------- fixtures

def _write_rollout(day_dir, name, records):
    os.makedirs(day_dir, exist_ok=True)
    path = os.path.join(day_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


def _rec(rtype, payload, ts):
    return {"type": rtype, "timestamp": ts, "payload": payload}


def _session_meta(sid, ts, **kw):
    payload = {"session_id": sid, "id": sid, "timestamp": ts, "cwd": "/repo",
               "model_provider": "openai", "git": {"branch": "feat/x"}}
    payload.update(kw)
    return _rec("session_meta", payload, ts)


def _user(text, ts):
    return _rec("response_item",
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": text}]}, ts)


def _sessions_tree(root, n=2):
    """A DATE-NESTED synthetic sessions tree: C1 (spawned by P1) plus n-1 root threads."""
    day = os.path.join(root, "2026", "07", "24")
    _write_rollout(day, "rollout-2026-07-24T10-00-00-0000c1.jsonl", [
        _session_meta("C1", "2026-07-24T10:00:00.000Z", parent_thread_id="P1",
                      thread_source="spawn"),
        _user("alpha bravo", "2026-07-24T10:00:01.000Z"),
    ])
    for i in range(1, n):
        _write_rollout(day, "rollout-2026-07-24T1%d-00-00-0000c%d.jsonl" % (i, i + 1), [
            _session_meta("C%d" % (i + 1), "2026-07-24T1%d:00:00.000Z" % i),
            _user("charlie delta", "2026-07-24T1%d:00:01.000Z" % i),
        ])
    return root


def _codex_home(tmp_path):
    """An EMPTY but real codex home. codex_state.load_corpus skips a missing state DB by
    design, so this exercises the explicit-home path without inventing a state schema."""
    home = str(tmp_path / "codex_home")
    os.makedirs(home, exist_ok=True)
    return home


def _server(tmp_path, runner=None):
    """A sidecar over a real ON-DISK index (the worker reopens it by path, so an
    in-memory database cannot stand in here)."""
    path = str(tmp_path / "index.db")
    conn = _track(corpus.open_index(path))
    return sidecar.Sidecar(conn, build_runner=runner), path


def _deferred():
    """A runner that CAPTURES the worker instead of running it, so a test drives the
    interleaving explicitly — the job is observably 'running' until the test says go."""
    pending = []
    return pending, pending.append


def _sync(fn):
    """Run the worker inline: ``corpus.build`` returns with the job already finished."""
    fn()


def _build_params(tmp_path, n=2):
    root = _sessions_tree(str(tmp_path / "sessions"), n=n)
    return {"sessions_root": root, "codex_home": _codex_home(tmp_path)}


# -------------------------------------------------------- multi-provider sources

def _grok_root(tmp_path):
    """A minimal Grok Build store: <enc-cwd>/<session-id>/ with summary.json + updates.jsonl."""
    sess = tmp_path / "grok" / "C%3A%5Cwork" / "1111-2222"
    sess.mkdir(parents=True)
    (sess / "summary.json").write_text(json.dumps({
        "info": {"id": "grok-sess-1", "cwd": "C:/work"},
        "generated_title": "synthetic grok session",
        "created_at": "2026-07-24T10:00:00.000000Z",
        "last_active_at": "2026-07-24T10:05:00.000000Z",
    }), encoding="utf-8")
    (sess / "updates.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"method": "session/update", "timestamp": "2026-07-24T10:00:01.000000Z",
         "params": {"sessionId": "grok-sess-1", "update": {
             "sessionUpdate": "user_message_chunk",
             "content": {"type": "text", "text": "synthetic prompt"}}}},
        {"method": "session/update", "timestamp": "2026-07-24T10:00:02.000000Z",
         "params": {"sessionId": "grok-sess-1", "update": {
             "sessionUpdate": "agent_message_chunk",
             "content": {"type": "text", "text": "synthetic reply"}}}},
    ]) + "\n", encoding="utf-8")
    return str(tmp_path / "grok")


def test_build_accepts_a_GROK_ONLY_source_with_no_codex_sessions_root(tmp_path):
    """A machine can hold a Grok store and no Codex store — and the owner's does.

    `sessions_root` used to be required, so importing Grok alone meant inventing a Codex path;
    `ingest_sessions` would then glob nothing and the build would report success for a store
    that does not exist. Naming only `grok_root` must work.
    """
    srv, _ = _server(tmp_path, runner=_sync)
    out = srv.dispatch("corpus.build", {
        "grok_root": _grok_root(tmp_path), "codex_home": _codex_home(tmp_path)})

    assert out["state"] == "running"          # the start reply is an ACCEPTANCE, not a result
    status = srv.dispatch("corpus.build_status", {})
    assert status["state"] == "done", status
    assert status["indexed_conversations"] == 1, status
    assert status["errors"] == [], status
    # And the Grok session must actually be in the graph the sidecar now serves.
    assert [n["id"] for n in srv.dispatch("graph.roots", {})] == ["grok-sess-1"]


def test_build_accepts_grok_with_codex_home_OMITTED_entirely(tmp_path):
    """The real Grok-only shape: a grok_root and NOTHING else.

    Making `codex_home` optional was not enough on its own — `_reject_nonlocal_path` still
    ran on the empty string, which is not an absolute path, so omitting it raised
    "codex_home must be an absolute local path" and the case the parameter had just been
    made optional for still failed. The guard now runs only when the value is named.
    """
    srv, _ = _server(tmp_path, runner=_sync)
    out = srv.dispatch("corpus.build", {"grok_root": _grok_root(tmp_path)})

    assert out["state"] == "running"
    status = srv.dispatch("corpus.build_status", {})
    assert status["state"] == "done", status
    assert status["errors"] == [], status
    assert [n["id"] for n in srv.dispatch("graph.roots", {})] == ["grok-sess-1"]


def test_build_still_rejects_a_UNC_codex_home_when_it_IS_named(tmp_path):
    """Optional must not mean unguarded: a named home is still an SMB/NTLM vector."""
    srv, _ = _server(tmp_path, runner=_sync)
    with pytest.raises(sidecar.RpcError) as excinfo:
        srv.dispatch("corpus.build", {"grok_root": _grok_root(tmp_path),
                                      "codex_home": r"\\evil.example\share"})
    assert excinfo.value.code == -32602


def test_build_requires_at_least_one_named_source(tmp_path):
    """Naming neither root is a caller bug and must be refused, not silently ingest nothing."""
    srv, _ = _server(tmp_path, runner=_sync)
    with pytest.raises(sidecar.RpcError) as excinfo:
        srv.dispatch("corpus.build", {"codex_home": _codex_home(tmp_path)})
    assert excinfo.value.code == -32602
    assert "at least one source" in str(excinfo.value.message)


def test_build_rejects_a_grok_root_that_does_not_exist(tmp_path):
    """The same silent-no-op guard `sessions_root` gets: grok.ingest_sessions globs, so a
    typo'd root yields zero docs and zero errors — a perfectly 'successful' build of nothing."""
    srv, _ = _server(tmp_path, runner=_sync)
    with pytest.raises(sidecar.RpcError) as excinfo:
        srv.dispatch("corpus.build", {"grok_root": str(tmp_path / "nope"),
                                      "codex_home": _codex_home(tmp_path)})
    assert excinfo.value.code == -32602
    assert "grok_root must be an existing directory" in str(excinfo.value.message)


def test_build_rejects_a_unc_grok_root(tmp_path):
    """UNC is an outbound SMB/NTLM vector, and the guard must fire BEFORE any isdir() —
    os.path.isdir on a UNC path reaches over SMB itself, so a 'not a directory' wording
    would mean the leak already happened."""
    srv, _ = _server(tmp_path, runner=_sync)
    with pytest.raises(sidecar.RpcError) as excinfo:
        srv.dispatch("corpus.build", {"grok_root": r"\\evil.example\share",
                                      "codex_home": _codex_home(tmp_path)})
    assert excinfo.value.code == -32602
    assert "existing directory" not in str(excinfo.value.message), \
        "a UNC path must be refused by the path guard, not by an isdir() that already touched it"


def test_build_rejects_a_non_string_grok_root(tmp_path):
    """Absent means 'not this source'; a wrong TYPE is a caller bug and stays an error."""
    srv, _ = _server(tmp_path, runner=_sync)
    with pytest.raises(sidecar.RpcError) as excinfo:
        srv.dispatch("corpus.build", {"sessions_root": _sessions_tree(str(tmp_path / "s"), n=1),
                                      "grok_root": 42,
                                      "codex_home": _codex_home(tmp_path)})
    assert excinfo.value.code == -32602


# ------------------------------------------------------------------ path safety

def test_build_rejects_unc_and_relative_sessions_root(tmp_path):
    """A UNC sessions_root would coerce an outbound SMB/NTLM auth before any read.

    The SPECIFIC reason is asserted, not just the -32602. The guard has to fire BEFORE the
    is-a-directory check, because ``os.path.isdir`` on a UNC path itself reaches out over
    SMB — so a rejection worded "must be an existing directory" would mean the leak already
    happened. Matching on the code alone passes either way and proves nothing.
    """
    srv, _ = _server(tmp_path, runner=_sync)
    home = _codex_home(tmp_path)
    for bad, reason in (("\\\\evil.example\\share\\sessions", "UNC"),
                        ("//evil.example/share/sessions", "UNC"),
                        ("relative/sessions", "absolute")):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch("corpus.build", {"sessions_root": bad, "codex_home": home})
        assert ei.value.code == -32602
        assert "sessions_root" in ei.value.message
        assert reason in ei.value.message
        assert "directory" not in ei.value.message


def test_build_rejects_unc_and_relative_codex_home(tmp_path):
    """codex_home is guarded on the same edge — dedup.scan's rule, same reason."""
    srv, _ = _server(tmp_path, runner=_sync)
    root = _sessions_tree(str(tmp_path / "sessions"))
    for bad, reason in (("\\\\evil.example\\share", "UNC"),
                        ("relative/home", "absolute")):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch("corpus.build", {"sessions_root": root, "codex_home": bad})
        assert ei.value.code == -32602
        assert "codex_home" in ei.value.message
        assert reason in ei.value.message


def test_build_requires_a_SOURCE_by_name(tmp_path):
    """At least one SOURCE root must be named; a bare codex_home is not a source.

    This used to require BOTH sessions_root and codex_home, for a privacy reason:
    `loaders.load_corpus(codex_home=None)` fell through to the owner's LIVE Codex store, so
    omitting it would have slurped real private sessions. That fallback is GONE — an unnamed
    home now means "no state graph to merge" — so the requirement moved to what it was
    really protecting: an ingest must be pointed at something the caller named.
    """
    srv, _ = _server(tmp_path, runner=_sync)
    for params in ({}, {"codex_home": _codex_home(tmp_path)}):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch("corpus.build", params)
        assert ei.value.code == -32602
        assert "at least one source" in str(ei.value.message)


def test_omitting_codex_home_does_NOT_read_the_live_store(tmp_path, monkeypatch):
    """The privacy property itself, asserted directly instead of via a required argument.

    The old guarantee was structural — codex_home was mandatory, so the live-store fallback
    could not be reached. Now that it is optional, that structural argument is gone and the
    property needs its own test: an unnamed home must SKIP the state merge entirely, never
    fall through to `~/.codex`. An automated probe really did read the owner's real sessions
    through that fallback, which is why this is pinned rather than assumed.
    """
    called = []

    def must_not_be_called(home):
        called.append(home)
        raise AssertionError(f"codex_state.load_corpus was reached with {home!r}")

    monkeypatch.setattr(sidecar.loaders.codex_state, "load_corpus", must_not_be_called)

    srv, _ = _server(tmp_path, runner=_sync)
    srv.dispatch("corpus.build", {"grok_root": _grok_root(tmp_path)})

    status = srv.dispatch("corpus.build_status", {})
    assert status["state"] == "done", status
    assert called == [], "the live Codex store must never be consulted for an unnamed home"


def test_build_rejects_a_sessions_root_that_is_not_a_directory(tmp_path):
    """ingest_sessions GLOBS: a missing root yields (0 docs, 0 errors) and would report a
    perfectly 'successful' build of nothing. A typo must be an error, not a silent no-op."""
    srv, _ = _server(tmp_path, runner=_sync)
    missing = str(tmp_path / "nope")
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("corpus.build",
                     {"sessions_root": missing, "codex_home": _codex_home(tmp_path)})
    assert ei.value.code == -32602
    assert "directory" in ei.value.message


def test_build_and_status_require_a_corpus():
    for method in ("corpus.build", "corpus.build_status"):
        with pytest.raises(sidecar.RpcError) as ei:
            sidecar.Sidecar(None).dispatch(method, {})
        assert ei.value.code == sidecar.CORPUS_NOT_INDEXED


def test_build_refuses_an_in_memory_index(tmp_path):
    """The worker reopens the index BY PATH on its own connection. A second connect to
    ':memory:' is a DIFFERENT database, so the ingest would land nowhere — refuse loudly."""
    conn = _track(sqlite3.connect(":memory:"))
    conn.row_factory = sqlite3.Row
    corpus.init_index(conn)
    srv = sidecar.Sidecar(conn, build_runner=_sync)
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("corpus.build", _build_params(tmp_path))
    assert ei.value.code == sidecar.BUILD_UNAVAILABLE


# ------------------------------------------------------------------- happy path

def test_build_ingests_a_synthetic_tree_and_reports_done(tmp_path):
    srv, _ = _server(tmp_path, runner=_sync)
    started = srv.dispatch("corpus.build", _build_params(tmp_path, n=3))
    assert started["state"] == "running"        # the START reply is always 'running'
    assert started["job_id"] == "build-1"
    assert isinstance(started["started_ms"], int)

    status = srv.dispatch("corpus.build_status", {})
    assert status["state"] == "done"
    assert status["job_id"] == "build-1"
    assert status["indexed_conversations"] == 3
    assert status["errors"] == []
    assert isinstance(status["finished_ms"], int)
    assert "error" not in status


def test_build_adopts_the_new_graph_so_the_ui_sees_it(tmp_path):
    """build_index persists CONVERSATIONS only — it writes no threads/edges row — so the
    graph exists solely as the Corpus load_corpus returns. If the sidecar did not adopt
    that object, graph.roots would stay empty forever after a successful build."""
    srv, _ = _server(tmp_path, runner=_sync)
    assert srv.dispatch("graph.roots", {}) == []
    srv.dispatch("corpus.build", _build_params(tmp_path, n=2))
    srv.dispatch("corpus.build_status", {})
    roots = srv.dispatch("graph.roots", {})
    assert [n["id"] for n in roots] == ["C2", "P1"]


def test_a_completed_build_is_adopted_without_polling_status(tmp_path):
    """Adoption hangs off dispatch, not off build_status, so a UI that never polls still
    stops showing a stale graph the moment it makes any other call."""
    srv, _ = _server(tmp_path, runner=_sync)
    srv.dispatch("corpus.build", _build_params(tmp_path, n=2))
    assert [n["id"] for n in srv.dispatch("graph.roots", {})] == ["C2", "P1"]


def test_a_build_persists_the_graph_to_the_index_not_just_memory(tmp_path):
    """A build's SUCCESS CRITERION is a non-empty GRAPH, not a conversation count.

    ``index.build_index`` writes conversations only (index.py:162-168). If the graph is not
    persisted, ``corpus.stats`` reports conversations while ``graph.roots`` /
    ``graph.timeline`` / ``graph.children`` all come back empty — the app's primary view
    blank on a "successful" build. Asserted through a SEPARATE connection to the index file,
    so no amount of in-memory state inside the sidecar can satisfy it.
    """
    srv, index_path = _server(tmp_path, runner=_sync)
    srv.dispatch("corpus.build", _build_params(tmp_path, n=3))
    srv.dispatch("corpus.build_status", {})

    probe = _track(sqlite3.connect(index_path))
    probe.row_factory = sqlite3.Row
    assert probe.execute("SELECT COUNT(*) FROM threads").fetchone()[0] > 0
    assert probe.execute("SELECT COUNT(*) FROM thread_spawn_edges").fetchone()[0] > 0
    reread = corpus.load_corpus(probe)
    assert sorted(reread.roots()) == ["C2", "C3", "P1"]
    assert reread.edges


def test_an_incremental_build_keeps_the_previously_ingested_graph(tmp_path):
    """The in-app flow is "open an existing corpus, then ingest MORE sessions into it", so a
    second build must ADD to the graph rather than replace the view with only its own run.

    ``loaders.load_corpus`` returns a Corpus assembled from THIS run alone (loaders.py:281)
    while ``_persist_graph`` upserts into the tables the previous run already populated
    (loaders.py:346-350) — so adopting the RETURNED object would silently drop every
    previously-ingested thread while the index on disk still held it. The sidecar re-reads
    the graph from the index instead, which is the only source that carries the union.
    """
    srv, _ = _server(tmp_path, runner=_sync)
    home = _codex_home(tmp_path)

    first = str(tmp_path / "sessions-a")
    _write_rollout(os.path.join(first, "2026", "07", "24"), "rollout-a.jsonl", [
        _session_meta("A1", "2026-07-24T10:00:00.000Z"),
        _user("alpha", "2026-07-24T10:00:01.000Z")])
    srv.dispatch("corpus.build", {"sessions_root": first, "codex_home": home})
    assert [n["id"] for n in srv.dispatch("graph.roots", {})] == ["A1"]

    second = str(tmp_path / "sessions-b")
    _write_rollout(os.path.join(second, "2026", "07", "25"), "rollout-b.jsonl", [
        _session_meta("B1", "2026-07-25T10:00:00.000Z"),
        _user("bravo", "2026-07-25T10:00:01.000Z")])
    srv.dispatch("corpus.build", {"sessions_root": second, "codex_home": home})
    assert [n["id"] for n in srv.dispatch("graph.roots", {})] == ["A1", "B1"]


def test_a_failed_build_still_refreshes_a_graph_it_already_committed(tmp_path, monkeypatch):
    """``_persist_graph`` commits the graph BEFORE the long conversation ingest
    (loaders.py:310-311), so a build can fail with the graph already on disk. The live view
    must follow the index in that case too — refreshing only on success would leave the
    sidecar reporting a graph the file no longer matches."""
    def persist_then_die(sessions_root, index_path, codex_home, grok_root=""):
        conn = corpus.open_index(index_path)
        try:
            corpus.upsert_thread(conn, ThreadMeta(id="PARTIAL"))
            conn.commit()
        finally:
            conn.close()
        raise RuntimeError("died after the graph commit")

    monkeypatch.setattr(sidecar.loaders, "load_corpus", persist_then_die)
    srv, _ = _server(tmp_path, runner=_sync)
    srv.dispatch("corpus.build", _build_params(tmp_path))
    assert srv.dispatch("corpus.build_status", {})["state"] == "failed"
    assert [n["id"] for n in srv.dispatch("graph.roots", {})] == ["PARTIAL"]


def test_status_is_idle_before_any_build(tmp_path):
    srv, _ = _server(tmp_path, runner=_sync)
    status = srv.dispatch("corpus.build_status", {})
    assert status == {"state": "idle", "indexed_conversations": 0, "errors": []}


# --------------------------------------------------- concurrency + live progress

def test_a_second_build_while_one_runs_is_rejected(tmp_path):
    """One ingest at a time. Two concurrent builds would race the same index file and the
    same job slot, so the second is refused rather than queued."""
    pending, runner = _deferred()
    srv, _ = _server(tmp_path, runner=runner)
    srv.dispatch("corpus.build", _build_params(tmp_path))

    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("corpus.build", _build_params(tmp_path))
    assert ei.value.code == sidecar.BUILD_IN_PROGRESS
    assert srv.dispatch("corpus.build_status", {})["state"] == "running"

    pending[0]()                                       # let the captured worker finish
    assert srv.dispatch("corpus.build_status", {})["state"] == "done"


def test_status_reports_the_live_indexed_count_while_running(tmp_path):
    """Progress is real, not simulated: the count is read from the index on the request
    thread, so it tracks the worker's committed chunks. Zero while the job is pending,
    non-zero once its commits land — measured without a sleep."""
    pending, runner = _deferred()
    srv, _ = _server(tmp_path, runner=runner)
    srv.dispatch("corpus.build", _build_params(tmp_path, n=3))

    mid = srv.dispatch("corpus.build_status", {})
    assert mid["state"] == "running" and mid["indexed_conversations"] == 0
    assert "finished_ms" not in mid

    pending[0]()
    assert srv.dispatch("corpus.build_status", {})["indexed_conversations"] == 3


def test_a_new_build_is_allowed_once_the_previous_finished(tmp_path):
    srv, _ = _server(tmp_path, runner=_sync)
    srv.dispatch("corpus.build", _build_params(tmp_path))
    second = srv.dispatch("corpus.build", _build_params(tmp_path))
    assert second["job_id"] == "build-2"
    assert srv.dispatch("corpus.build_status", {})["job_id"] == "build-2"


def test_status_rejects_a_stale_job_id(tmp_path):
    """The optional job_id lets a UI prove it is reading the job it started, so a poll
    that raced a newer build gets an error instead of the wrong job's progress."""
    srv, _ = _server(tmp_path, runner=_sync)
    srv.dispatch("corpus.build", _build_params(tmp_path))
    assert srv.dispatch("corpus.build_status", {"job_id": "build-1"})["job_id"] == "build-1"
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("corpus.build_status", {"job_id": "build-9"})
    assert ei.value.code == -32602
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("corpus.build_status", {"job_id": 7})
    assert ei.value.code == -32602


def test_status_job_id_on_an_idle_engine_is_rejected(tmp_path):
    srv, _ = _server(tmp_path, runner=_sync)
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("corpus.build_status", {"job_id": "build-1"})
    assert ei.value.code == -32602


# ------------------------------------------------------------ error surfacing

def test_per_source_errors_reach_the_wire_basenamed_and_sanitized(tmp_path):
    """load_corpus returns (result, errors); a corrupt rollout must be REPORTED, not
    swallowed — and its ``file`` must cross as a bare basename with hidden unicode stripped.

    The payload rides in the FILENAME (``rollout-brok<U+200B>en.jsonl``) because that is a
    value which genuinely reaches the wire: a JSONDecodeError's repr carries the position,
    not the offending bytes, so a ZW planted in the file BODY could never fire.

    Asserted on the VALUE, never on a ``json.dumps`` blob. Both blob checks that were here
    first were vacuous: dumps escapes a backslash to ``\\\\`` and U+200B to the ASCII text
    ``\\u200b``, so ``str(tmp_path) not in blob`` and ``ZW not in blob`` were TRUE with the
    path and the payload fully intact — measured, both survived a mutation that deleted the
    basename call outright. The single equality below fires on either regression.
    """
    root = str(tmp_path / "sessions")
    day = os.path.join(root, "2026", "07", "24")
    os.makedirs(day, exist_ok=True)
    with open(os.path.join(day, "rollout-brok" + ZW + "en.jsonl"), "w",
              encoding="utf-8") as fh:
        fh.write('{"type":"session_meta","payload":{"session_id":"OK"}}\n')
        fh.write("{not json at all\n")

    srv, _ = _server(tmp_path, runner=_sync)
    srv.dispatch("corpus.build",
                 {"sessions_root": root, "codex_home": _codex_home(tmp_path)})
    status = srv.dispatch("corpus.build_status", {})

    assert status["state"] == "done"            # a bad FILE does not fail the RUN
    assert status["errors"], "a malformed rollout line must be reported"
    for err in status["errors"]:
        assert err["file"] == "rollout-broken.jsonl"   # basenamed AND sanitized
        assert os.sep not in err["file"] and "/" not in err["file"]
        for value in err.values():
            assert ZW not in str(value)
        assert err["stage"] == "parse"


def test_a_crashing_ingest_is_reported_not_swallowed(tmp_path, monkeypatch):
    """An exception inside the worker thread has nowhere to propagate — unhandled it would
    leave the job 'running' forever. It must land as a terminal 'failed' state."""
    def boom(*a, **kw):
        raise RuntimeError("disk on fire" + ZW)

    monkeypatch.setattr(sidecar.loaders, "load_corpus", boom)
    srv, _ = _server(tmp_path, runner=_sync)
    srv.dispatch("corpus.build", _build_params(tmp_path))
    status = srv.dispatch("corpus.build_status", {})
    assert status["state"] == "failed"
    assert "RuntimeError" in status["error"] and "disk on fire" in status["error"]
    assert ZW not in status["error"]
    assert isinstance(status["finished_ms"], int)


def test_a_build_can_be_retried_after_a_failure(tmp_path, monkeypatch):
    """A failed job must not wedge the slot: build_index checkpoints and commits per
    chunk, so a re-run RESUMES rather than restarting, and the retry has to be reachable."""
    monkeypatch.setattr(sidecar.loaders, "load_corpus",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("nope")))
    srv, _ = _server(tmp_path, runner=_sync)
    srv.dispatch("corpus.build", _build_params(tmp_path))
    assert srv.dispatch("corpus.build_status", {})["state"] == "failed"

    monkeypatch.undo()
    srv.dispatch("corpus.build", _build_params(tmp_path, n=2))
    ok = srv.dispatch("corpus.build_status", {})
    assert ok["state"] == "done" and ok["job_id"] == "build-2"
    assert "error" not in ok


def test_the_sessions_root_echo_is_sanitized(tmp_path):
    """The caller's own path is echoed back so the UI can label the job (the same choice
    maintenance.plan makes for its roots) — but it is still run through the sanitizer."""
    root = str(tmp_path / ("sess" + ZW + "ions"))
    _sessions_tree(root)
    srv, _ = _server(tmp_path, runner=_sync)
    started = srv.dispatch("corpus.build",
                           {"sessions_root": root, "codex_home": _codex_home(tmp_path)})
    assert ZW not in started["sessions_root"]
    assert ZW not in srv.dispatch("corpus.build_status", {})["sessions_root"]


# ------------------------------------------- corpus.create (the bootstrap verb)
#
# The Rust open_corpus now REFUSES a path that is not an existing file, so "open" can never
# resurrect a moved corpus as an empty one. That split leaves no way to make a NEW corpus
# in-app, because the path a user would name does not exist yet. corpus.create is the
# explicit CREATE verb that closes it: create-then-open, with open still refusing to create.

def test_create_makes_an_openable_empty_index(tmp_path):
    path = str(tmp_path / "new-corpus.db")
    out = sidecar.Sidecar(None).dispatch("corpus.create", {"index_path": path})
    assert out == {"index_path": path, "created": True}
    assert os.path.isfile(path)                  # open_corpus checks is_file, so it must be

    probe = _track(sqlite3.connect(path))
    probe.row_factory = sqlite3.Row
    assert corpus.load_corpus(probe).threads == {}
    assert probe.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_create_works_with_no_corpus_attached(tmp_path):
    """The bootstrap constraint: this is the ONE data method that must answer on an engine
    holding no index. A user creating their FIRST corpus has nothing open by definition, so
    requiring a corpus here would make the method unreachable exactly when it is needed."""
    path = str(tmp_path / "first.db")
    assert sidecar.Sidecar(None).dispatch("corpus.create", {"index_path": path})["created"]
    assert sidecar.Sidecar(None).dispatch("health.ping", {})["corpus_ready"] is False


def test_create_refuses_to_clobber_an_existing_file(tmp_path):
    """Create must not overwrite, for the same reason open must not create: a corpus the
    user already has must never be silently replaced by an empty one."""
    path = str(tmp_path / "exists.db")
    srv = sidecar.Sidecar(None)
    srv.dispatch("corpus.create", {"index_path": path})
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("corpus.create", {"index_path": path})
    assert ei.value.code == sidecar.CORPUS_EXISTS

    # A distinct code, not a generic -32602: the UI can offer "open it instead?" only if it
    # can tell "already there" apart from "that path is bad".
    with pytest.raises(sidecar.RpcError) as bad:
        srv.dispatch("corpus.create", {"index_path": "relative.db"})
    assert bad.value.code == -32602


def test_create_refuses_unc_relative_and_a_missing_parent(tmp_path):
    """Same path guard as every other caller-supplied path here — a UNC target would emit
    outbound SMB/NTLM. A missing parent is refused rather than mkdir'd: materialising an
    arbitrary directory tree from a caller-named path is more surface than this needs."""
    srv = sidecar.Sidecar(None)
    for bad, reason in (("\\\\evil.example\\share\\c.db", "UNC"),
                        ("//evil.example/share/c.db", "UNC"),
                        ("relative/c.db", "absolute")):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch("corpus.create", {"index_path": bad})
        assert ei.value.code == -32602
        assert "index_path" in ei.value.message and reason in ei.value.message

    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("corpus.create",
                     {"index_path": str(tmp_path / "no" / "such" / "dir" / "c.db")})
    assert ei.value.code == -32602
    assert "parent directory" in ei.value.message

    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("corpus.create", {})
    assert ei.value.code == -32602


def test_create_then_build_is_the_full_new_corpus_journey(tmp_path):
    """The whole point of the verb, driven end to end: create an index at a named path,
    attach a sidecar to it exactly as open_corpus would, ingest into it, and land a
    populated graph. This is what a "New corpus…" flow would do."""
    path = str(tmp_path / "journey.db")
    sidecar.Sidecar(None).dispatch("corpus.create", {"index_path": path})

    srv = sidecar.Sidecar(_track(corpus.open_index(path)), build_runner=_sync)
    srv.dispatch("corpus.build", _build_params(tmp_path, n=2))
    status = srv.dispatch("corpus.build_status", {})
    assert status["state"] == "done" and status["indexed_conversations"] == 2
    assert [n["id"] for n in srv.dispatch("graph.roots", {})] == ["C2", "P1"]


# -------------------------------------------------- the REAL background thread

def test_the_default_runner_runs_the_build_on_a_daemon_thread(tmp_path):
    """The production seam itself: a real thread, joined explicitly (a barrier, not a
    sleep). It must be a DAEMON so a half-finished ingest cannot wedge interpreter exit."""
    seen = {}

    def body():
        seen["thread"] = threading.current_thread()

    thread = sidecar._default_build_runner(body)
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert thread.daemon
    assert seen["thread"] is thread
    assert seen["thread"] is not threading.current_thread()


def test_a_real_threaded_build_never_touches_the_request_thread_connection(tmp_path):
    """The sqlite trap, driven for real. corpus.open_index uses sqlite3's default
    check_same_thread=True, so ANY use of self.conn from the worker raises
    ProgrammingError. The worker gets a PATH and opens its own connection instead — so a
    genuine end-to-end threaded build must complete clean."""
    threads = []

    def capturing(fn):
        thread = sidecar._default_build_runner(fn)
        threads.append(thread)
        return thread

    srv, _ = _server(tmp_path, runner=capturing)
    srv.dispatch("corpus.build", _build_params(tmp_path, n=3))
    threads[0].join(timeout=60)
    assert not threads[0].is_alive()

    status = srv.dispatch("corpus.build_status", {})
    assert status["state"] == "done", "worker failed: %r" % status.get("error")
    assert status["indexed_conversations"] == 3
    assert [n["id"] for n in srv.dispatch("graph.roots", {})] == ["C2", "C3", "P1"]


def test_the_request_thread_sees_a_concurrent_workers_committed_rows(tmp_path, monkeypatch):
    """``indexed_conversations`` is claimed to CLIMB while a build runs. That rests on the
    request thread seeing rows another thread's separate connection committed, so it is
    driven here rather than inferred from WAL being enabled.

    Deterministic via two Events — a barrier, not a sleep: the worker commits one row and
    blocks; the request thread polls and must SEE it; only then is the worker released. The
    timeouts are failsafes against a hang, never a timing assumption."""
    committed, resume = threading.Event(), threading.Event()

    def fake_ingest(sessions_root, index_path, codex_home, grok_root=""):
        conn = corpus.open_index(index_path)         # the WORKER's own connection
        try:
            corpus.add_conversation(
                conn, ir.Conversation(id="mid", title="t", provider="codex"),
                thread_id="", rollout_path="mid.jsonl")
            conn.commit()
            committed.set()
            resume.wait(timeout=30)
        finally:
            conn.close()
        return corpus.Corpus(), []

    monkeypatch.setattr(sidecar.loaders, "load_corpus", fake_ingest)
    threads = []

    def capturing(fn):
        thread = sidecar._default_build_runner(fn)
        threads.append(thread)
        return thread

    srv, _ = _server(tmp_path, runner=capturing)
    srv.dispatch("corpus.build", _build_params(tmp_path))

    assert committed.wait(timeout=30), "the worker never reached its commit"
    mid = srv.dispatch("corpus.build_status", {})
    assert mid["state"] == "running"
    assert mid["indexed_conversations"] == 1     # the concurrent commit IS visible

    resume.set()
    threads[0].join(timeout=30)
    assert not threads[0].is_alive()
    assert srv.dispatch("corpus.build_status", {})["state"] == "done"


def test_build_and_status_round_trip_over_the_real_ndjson_wire(tmp_path):
    """Every other test here calls ``dispatch`` and never SERIALIZES. The cockpit reads one
    ``\\n``-terminated line per response, so each value in these results has to survive
    json.dumps and must not embed a newline — a non-serializable object would otherwise
    reach the client as a -32603 at the transport rather than as a result."""
    srv, _ = _server(tmp_path, runner=_sync)
    started = srv.handle_line(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "corpus.build",
         "params": _build_params(tmp_path, n=2)}))
    assert started["result"]["state"] == "running"

    status = srv.handle_line(json.dumps(
        {"jsonrpc": "2.0", "id": 2, "method": "corpus.build_status", "params": {}}))
    line = sidecar._dumps(status)
    assert "\n" not in line                       # exactly one frame for read_line
    decoded = json.loads(line)
    assert decoded["id"] == 2
    assert decoded["result"]["state"] == "done"
    assert decoded["result"]["indexed_conversations"] == 2

    refused = srv.handle_line(json.dumps(
        {"jsonrpc": "2.0", "id": 3, "method": "corpus.build",
         "params": {"sessions_root": "rel", "codex_home": "rel"}}))
    assert refused["error"]["code"] == -32602
    assert "\n" not in sidecar._dumps(refused)


def test_the_request_thread_stays_answerable_while_a_build_runs(tmp_path):
    """The whole point of the job model: the client is a single mutex-guarded pipe, so a
    build that blocked its RPC would freeze the UI. Other methods must answer normally
    while a job is outstanding."""
    pending, runner = _deferred()
    srv, _ = _server(tmp_path, runner=runner)
    srv.dispatch("corpus.build", _build_params(tmp_path))
    assert srv.dispatch("health.ping", {})["ok"] is True
    assert srv.dispatch("corpus.stats", {})["conversations"] == 0
    pending[0]()
    assert srv.dispatch("corpus.stats", {})["conversations"] == 2
