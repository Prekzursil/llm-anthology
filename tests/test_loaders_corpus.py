"""loaders.load_corpus — the cockpit ingest entrypoint (rollouts + state graph + FTS5).

SYNTHETIC fixtures ONLY. No real conversation content, thread id, path, or DB appears
here: the state DB is built in a tmp dir and the real $CODEX_HOME is never read, because
`codex_home` is always passed explicitly, and no Grok store is read unless `grok_root`
names one explicitly — the owner's real `~/.grok` holds private material and is never
touched by this file or by the code it exercises. Every fixture mirrors the measured
shapes (date-nested rollout-*.jsonl, a threads / thread_spawn_edges state DB, and a
`<enc-cwd>/<session-id>/` Grok session directory) but never their content.

load_corpus stitches four merged work-units together:
  * codex_rollout.ingest_sessions  — conversation content + a per-thread node + spawn edge
  * grok.ingest_sessions            — the same contract for a Grok Build session store,
                                      OPT-IN via `grok_root` (never defaulted)
  * codex_state.load_corpus         — the authoritative spawn graph (threads + edges)
  * index.build_index               — the contentless FTS5 index over every conversation

The tests pin the MERGE POLICY (rollout thread metadata wins, Grok claims only ids no
earlier source claimed, the state graph fills the remaining gaps; edges are de-duplicated
by (parent, child)), the CROSS-PROVIDER COLLISION policy, per-source failure isolation,
and the end-to-end index (searchable, thread-linked, complete, idempotent on a re-run).
"""
import json
import os
import sqlite3

from llm_anthology import corpus, index, loaders
from llm_anthology.adapters import grok


# ------------------------------------------------------------------- fixtures

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
    pl = {"session_id": sid, "id": sid, "timestamp": ts, "cwd": "/repo",
          "model_provider": "openai", "git": {"branch": "feat/x"},
          "agent_nickname": "Ada", "agent_path": "architect"}
    pl.update(kw)
    return _rec("session_meta", pl, ts)


def _user(text, ts):
    return _rec("response_item",
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": text}]}, ts)


def _assistant(text, ts):
    return _rec("response_item",
                {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": text}]}, ts)


def _sessions_tree(root):
    """Two synthetic rollouts under a DATE-NESTED tree: C1 (spawned by P1) and C2 (a root)."""
    day = os.path.join(root, "2026", "07", "24")
    _write_rollout(day, "rollout-2026-07-24T10-00-00-0000c1.jsonl", [
        _session_meta("C1", "2026-07-24T10:00:00.000Z", parent_thread_id="P1",
                      thread_source="spawn"),
        _user("alpha bravo charlie", "2026-07-24T10:00:01.000Z"),
        _assistant("delta echo", "2026-07-24T10:00:02.000Z"),
    ])
    _write_rollout(day, "rollout-2026-07-24T11-00-00-0000c2.jsonl", [
        _session_meta("C2", "2026-07-24T11:00:00.000Z"),
        _user("foxtrot golf hotel", "2026-07-24T11:00:01.000Z"),
    ])


def _state_db(codex_home):
    """A synthetic state_5.sqlite: thread C1 (OVERLAPS a rollout -> must NOT clobber it),
    thread S3 (new -> merged in); edge (P1,C1) (DUPLICATES the rollout edge -> deduped),
    edge (C1,S3) (new -> merged in)."""
    os.makedirs(codex_home, exist_ok=True)
    path = os.path.join(codex_home, "state_5.sqlite")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, model_provider TEXT, "
        "tokens_used INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER, "
        "git_branch TEXT, cwd TEXT, agent_role TEXT, agent_nickname TEXT, "
        "preview TEXT, rollout_path TEXT)")
    conn.execute("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, "
                 "child_thread_id TEXT, status TEXT)")
    conn.execute("INSERT INTO threads (id, title) VALUES ('C1', 'STATE_C1')")
    conn.execute("INSERT INTO threads (id, title) VALUES ('S3', 'STATE_S3')")
    conn.execute("INSERT INTO thread_spawn_edges VALUES ('P1', 'C1', 'state')")
    conn.execute("INSERT INTO thread_spawn_edges VALUES ('C1', 'S3', 'state')")
    conn.commit()
    conn.close()
    return path


# ------------------------------------------------------------------- tests

def test_load_corpus_merges_rollouts_state_and_builds_the_index(tmp_path):
    sessions = tmp_path / "sessions"
    home = tmp_path / "codex_home"
    idx = tmp_path / "index.sqlite"
    _sessions_tree(str(sessions))
    _state_db(str(home))

    c, errors = loaders.load_corpus(str(sessions), str(idx), codex_home=str(home))

    assert errors == []
    # one conversation per rollout file
    assert {conv.id for conv in c.conversations} == {"C1", "C2"}
    # threads: the rollout nodes C1/C2 UNION the state-only node S3
    assert set(c.threads) == {"C1", "C2", "S3"}
    # rollout metadata WINS over the state row for the overlapping thread C1
    assert c.threads["C1"].title != "STATE_C1"
    assert c.threads["C1"].git_branch == "feat/x"
    # the state-only thread arrives verbatim
    assert c.threads["S3"].title == "STATE_S3"
    # edges de-duplicated by (parent, child): rollout (P1,C1) once + state (C1,S3)
    assert sorted((e.parent_thread_id, e.child_thread_id) for e in c.edges) == \
        [("C1", "S3"), ("P1", "C1")]
    # the graph is navigable across the merged nodes (P1 is a dangling parent root)
    assert c.roots() == ["C2", "P1"]
    assert c.children_of("P1") == ["C1"] and c.children_of("C1") == ["S3"]
    assert c.depth("S3") == 2

    # the FTS5 index was built: searchable, thread-linked, and complete
    conn = corpus.open_index(str(idx))
    try:
        assert index.count(conn) == 2
        hits = index.search(conn, "bravo")
        assert [r["conversation_id"] for r in hits] == ["C1"]
        row = conn.execute(
            "SELECT thread_id FROM conversations WHERE conversation_id='C1'").fetchone()
        assert row["thread_id"] == "C1"
    finally:
        conn.close()


def test_load_corpus_can_ingest_GROK_ALONE_with_no_codex_store(tmp_path):
    """A machine can hold a Grok store and no Codex store — and this one does.

    Discovery reports the owner's Grok finding as naming no Codex home, so with Codex
    unconditional the only way to import it was to invent a Codex path: `ingest_sessions`
    would then glob nothing, return zero docs and zero errors, and the ingest would report
    success for a Codex store that does not exist. An empty `sessions_root` now means "no
    Codex", so the Grok-only case is expressible instead of faked.
    """
    grok_root = tmp_path / "grok"
    idx = tmp_path / "index.sqlite"
    _grok_store(str(grok_root))

    c, errors = loaders.load_corpus("", str(idx), codex_home=str(tmp_path / "no_codex"),
                                    grok_root=str(grok_root))

    assert errors == [], f"a Grok-only ingest must not error: {errors}"
    assert c.conversations, "the Grok conversation must be ingested"
    assert set(c.threads), "the Grok thread must be present"

    # And it must reach DISK, read back the way the sidecar reads it.
    conn = corpus.open_index(str(idx))
    try:
        persisted = corpus.load_corpus(conn)
        rows = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    finally:
        conn.close()
    assert rows > 0
    assert set(persisted.threads) == set(c.threads)


def test_load_corpus_with_no_source_at_all_is_an_empty_corpus_not_a_crash(tmp_path):
    """Naming neither root is a caller mistake, but it must degrade to nothing, not raise —
    the index is still created so a later ingest has somewhere to land."""
    idx = tmp_path / "index.sqlite"

    c, errors = loaders.load_corpus("", str(idx), codex_home=str(tmp_path / "absent"))

    assert errors == []
    assert list(c.conversations) == []
    assert os.path.isfile(str(idx)), "the index must still be created"


def test_load_corpus_PERSISTS_the_spawn_graph_not_just_the_conversations(tmp_path):
    """The graph must survive in the INDEX, not only in the returned in-memory Corpus.

    This is the assertion whose absence hid a P0. The test above checks the in-memory
    `c.threads` / `c.edges` / `c.roots()` and, separately, that the index holds
    conversations — so it passes even when the graph is never written to disk. But the
    cockpit does not use the returned object: it spawns a sidecar against the index FILE
    and rebuilds the graph with `corpus.load_corpus(conn)`, which reads exclusively from
    the `threads` and `thread_spawn_edges` tables. An index with empty graph tables
    therefore reports conversations normally in `corpus.stats` while `graph.roots`,
    `graph.timeline` and `graph.children` all come back empty — the app's entire primary
    view blank, with every existing test green.

    So this asserts against a REOPENED connection, the same way the consumer reads it.
    """
    sessions = tmp_path / "sessions"
    home = tmp_path / "codex_home"
    idx = tmp_path / "index.sqlite"
    _sessions_tree(str(sessions))
    _state_db(str(home))

    in_memory, errors = loaders.load_corpus(str(sessions), str(idx), codex_home=str(home))
    assert errors == []

    conn = corpus.open_index(str(idx))
    try:
        persisted = corpus.load_corpus(conn)
    finally:
        conn.close()

    # The persisted graph must match the one load_corpus built in memory.
    assert set(persisted.threads) == set(in_memory.threads) == {"C1", "C2", "S3"}
    assert sorted((e.parent_thread_id, e.child_thread_id) for e in persisted.edges) == \
        [("C1", "S3"), ("P1", "C1")]

    # And it must be NAVIGABLE after the round-trip, since that is what the UI does with
    # it — a table holding rows that no longer form a graph would still be a failure.
    assert persisted.roots() == ["C2", "P1"]
    assert persisted.children_of("P1") == ["C1"]
    assert persisted.depth("S3") == 2

    # Merge policy must survive persistence too: rollout metadata won in memory, so the
    # row on disk must be the rollout one, not the state one.
    assert persisted.threads["C1"].git_branch == "feat/x"
    assert persisted.threads["C1"].title != "STATE_C1"
    assert persisted.threads["S3"].title == "STATE_S3"


def test_load_corpus_forwards_progress_so_a_long_ingest_can_be_reported(tmp_path):
    """`progress` must reach build_index, or a long ingest has no per-chunk hook at all.

    Two things depend on it and neither is cosmetic: the in-app build can otherwise show
    only one up-front line for a multi-minute ingest, and cancellation has nothing to
    check — the sidecar documents that absence as precisely why a cancel could not be
    honoured. Asserting the callback FIRES is the only way to know the kwarg is wired;
    accepting it and dropping it would look identical from the signature.
    """
    sessions = tmp_path / "sessions"
    home = tmp_path / "codex_home"
    idx = tmp_path / "index.sqlite"
    _sessions_tree(str(sessions))
    _state_db(str(home))

    seen = []
    loaders.load_corpus(str(sessions), str(idx), codex_home=str(home),
                        progress=lambda file, offset: seen.append((file, offset)))

    assert seen, "progress must be called at least once for a non-empty ingest"
    # build_index reports (file, offset) after each committed batch.
    assert all(isinstance(f, str) and isinstance(o, int) for f, o in seen), seen


def test_load_corpus_without_progress_still_works(tmp_path):
    """Omitting `progress` must behave exactly as before — the kwarg is additive."""
    sessions = tmp_path / "sessions"
    home = tmp_path / "codex_home"
    idx = tmp_path / "index.sqlite"
    _sessions_tree(str(sessions))
    _state_db(str(home))

    c, errors = loaders.load_corpus(str(sessions), str(idx), codex_home=str(home))

    assert errors == []
    assert set(c.threads) == {"C1", "C2", "S3"}


def test_load_corpus_re_run_does_not_duplicate_persisted_graph_rows(tmp_path):
    """Persisting the graph must stay idempotent, like the conversation ingest already is.

    `load_corpus` is documented as re-runnable ("a re-run adds no duplicate row or
    posting"), and the cockpit will re-ingest into an existing index. If graph persistence
    appended instead of upserting, every re-run would multiply the edges and the graph
    would slowly rot.
    """
    sessions = tmp_path / "sessions"
    home = tmp_path / "codex_home"
    idx = tmp_path / "index.sqlite"
    _sessions_tree(str(sessions))
    _state_db(str(home))

    loaders.load_corpus(str(sessions), str(idx), codex_home=str(home))
    loaders.load_corpus(str(sessions), str(idx), codex_home=str(home))  # replay

    conn = corpus.open_index(str(idx))
    try:
        threads = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        edges = conn.execute("SELECT COUNT(*) FROM thread_spawn_edges").fetchone()[0]
    finally:
        conn.close()

    assert threads == 3, f"expected 3 thread rows after a replay, got {threads}"
    assert edges == 2, f"expected 2 edge rows after a replay, got {edges}"


def test_load_corpus_on_empty_inputs_returns_an_empty_corpus_and_index(tmp_path):
    sessions = tmp_path / "empty_sessions"
    home = tmp_path / "no_state"          # no state_5.sqlite here -> skipped, not fatal
    idx = tmp_path / "empty_index.sqlite"
    sessions.mkdir()
    home.mkdir()

    c, errors = loaders.load_corpus(str(sessions), str(idx), codex_home=str(home))

    assert errors == []
    assert c.conversations == [] and c.threads == {} and c.edges == []
    conn = corpus.open_index(str(idx))
    try:
        assert index.count(conn) == 0
    finally:
        conn.close()


def test_load_corpus_dedupes_a_repeated_rollout_spawn_edge(tmp_path):
    """A RESUMED session writes a SECOND rollout with the same session_id AND parent, so
    two rollout docs declare the SAME (parent, child) spawn edge. It must collapse to one
    edge — the intra-rollout de-dup path (a later doc re-declaring an already-seen edge)."""
    sessions = tmp_path / "sessions"
    home = tmp_path / "no_state"
    idx = tmp_path / "index.sqlite"
    day = os.path.join(str(sessions), "2026", "07", "24")
    _write_rollout(day, "rollout-2026-07-24T10-00-00-0000c1a.jsonl", [
        _session_meta("C1", "2026-07-24T10:00:00.000Z", parent_thread_id="P1"),
        _user("first leg", "2026-07-24T10:00:01.000Z"),
    ])
    _write_rollout(day, "rollout-2026-07-24T12-00-00-0000c1b.jsonl", [
        _session_meta("C1", "2026-07-24T12:00:00.000Z", parent_thread_id="P1"),
        _user("resumed leg", "2026-07-24T12:00:01.000Z"),
    ])
    home.mkdir()

    c, errors = loaders.load_corpus(str(sessions), str(idx), codex_home=str(home))

    assert errors == []
    # both rollouts declare (P1, C1); it survives exactly once
    assert [(e.parent_thread_id, e.child_thread_id) for e in c.edges] == [("P1", "C1")]
    assert c.children_of("P1") == ["C1"]


def test_load_corpus_is_idempotent_on_a_re_run(tmp_path):
    sessions = tmp_path / "sessions"
    home = tmp_path / "codex_home"
    idx = tmp_path / "index.sqlite"
    _sessions_tree(str(sessions))
    _state_db(str(home))

    loaders.load_corpus(str(sessions), str(idx), codex_home=str(home))
    loaders.load_corpus(str(sessions), str(idx), codex_home=str(home))  # replay

    conn = corpus.open_index(str(idx))
    try:
        assert index.count(conn) == 2          # no duplicate rows on replay
        assert index.posting_count(conn) == 2  # nor duplicate FTS postings
    finally:
        conn.close()


# ------------------------------------------------- multi-provider (Grok) fixtures

# UUID-shaped, like the real ids, but wholly invented. _G1 spawned _G2; _G2 also has its
# own top-level session directory, which is how the real store records a child (see
# adapters/grok.py:_is_subagent_dir — the `subagents/<id>/` folder is bookkeeping, the
# child's transcript lives at the top level under its own cwd folder).
_G1 = "0198c4d1-2f3a-7b41-9e02-000000000001"
_G2 = "0198c4d1-2f3a-7b41-9e02-000000000002"
# NINE fractional digits, the measured Grok stamp width (adapters/grok.py:69-74).
_GROK_TS = "2026-08-06T12:00:00.123456789Z"
_GROK_TS2 = "2026-08-06T12:05:30.000000000Z"
_ENC_CWD = "C%3A%5Cwork%5Crepo"          # the percent-encoded cwd parent directory


def _write_text(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _grok_chunk(kind, text, sid, ts=_GROK_TS):
    """One updates.jsonl line: the JSON-RPC envelope whose ACP discriminant is NESTED at
    params.update.sessionUpdate — a reader that looks for it at the top level of the line
    finds nothing (adapters/grok.py:37-40)."""
    return {"method": "session/update", "timestamp": ts,
            "params": {"_meta": {}, "sessionId": sid,
                       "update": {"sessionUpdate": kind,
                                  "content": {"type": "text", "text": text}}}}


def _grok_session(root, sid, title, user_text, agent_text, child=None,
                  extra_lines=(), enc_cwd=_ENC_CWD):
    """A synthetic `<root>/<enc-cwd>/<session-id>/` Grok session DIRECTORY.

    `summary.json` + `updates.jsonl`, plus `subagents/sa-0/meta.json` when `child` names
    one. A GARBAGE-filled `*.lock` sibling is planted beside every real file, so a glob
    that mistook a lock for data would fail loudly rather than quietly
    (adapters/grok.py:60-62). The session id lives at `info.id`, NOT in the directory
    name (adapters/grok.py:65-67), so both are set to `sid` and the id-under-test is the
    field.
    """
    sdir = os.path.join(root, enc_cwd, sid)
    os.makedirs(sdir, exist_ok=True)
    lines = [json.dumps(_grok_chunk("user_message_chunk", user_text, sid)),
             json.dumps(_grok_chunk("agent_message_chunk", agent_text, sid,
                                    ts=_GROK_TS2))]
    lines.extend(extra_lines)
    _write_text(os.path.join(sdir, "updates.jsonl"), "\n".join(lines) + "\n")
    _write_text(os.path.join(sdir, "summary.json"), json.dumps({
        "agent_name": "grok-build", "chat_format_version": 3,
        "created_at": _GROK_TS, "current_model_id": "grok-code-fast-1",
        "generated_title": title, "info": {"cwd": "/work/repo", "id": sid},
        "last_active_at": _GROK_TS2, "num_chat_messages": 2, "num_messages": 2,
        "reasoning_effort": "high", "session_kind": "primary",
        "updated_at": _GROK_TS2}))
    _write_text(os.path.join(sdir, "summary.json.lock"), "NOT JSON {{{")
    _write_text(os.path.join(sdir, "updates.jsonl.lock"), "NOT JSON {{{")
    if child is not None:
        meta_dir = os.path.join(sdir, "subagents", "sa-0")
        os.makedirs(meta_dir, exist_ok=True)
        _write_text(os.path.join(meta_dir, "meta.json"), json.dumps({
            "child_session_id": child, "parent_session_id": sid,
            "status": "completed", "subagent_id": "sa-0"}))
        _write_text(os.path.join(meta_dir, "meta.json.lock"), "NOT JSON {{{")
    return sdir


def _grok_store(root):
    """The standard two-session Grok store: _G1 (which spawned _G2) and _G2 itself.
    Returns (parent_session_dir, child_session_dir)."""
    return (_grok_session(root, _G1, "Grok parent", "india juliett kilo", "lima mike",
                          child=_G2),
            _grok_session(root, _G2, "Grok child", "november oscar papa", "quebec"))


def _both_stores(tmp_path):
    """A Codex sessions tree + state DB AND a Grok store, all synthetic, all in tmp.
    Returns (sessions_root, codex_home, grok_root, index_path) as strings."""
    sessions, home = tmp_path / "sessions", tmp_path / "codex_home"
    groot, idx = tmp_path / "grok_sessions", tmp_path / "index.sqlite"
    _sessions_tree(str(sessions))
    _state_db(str(home))
    _grok_store(str(groot))
    return str(sessions), str(home), str(groot), str(idx)


def _reopen_graph(index_path):
    """The graph as the CONSUMER reads it: from a reopened connection, via the exact
    reader the sidecar uses to rebuild the cockpit's view."""
    conn = corpus.open_index(index_path)
    try:
        return corpus.load_corpus(conn)
    finally:
        conn.close()


# --------------------------------------------------------- multi-provider ingest

def test_load_corpus_ingests_a_grok_store_alongside_codex_into_ONE_corpus(tmp_path):
    """The headline: one call, one index, both providers.

    A Grok store was fully readable by `adapters/grok.py` while NOTHING in the ingest
    path called it, so the app could detect a Grok store and then refuse to import it.
    This pins that a single `load_corpus` produces one corpus holding both providers'
    conversations, both providers' thread nodes, and both providers' spawn edges.
    """
    sessions, home, groot, idx = _both_stores(tmp_path)

    c, errors = loaders.load_corpus(sessions, idx, codex_home=home, grok_root=groot)

    assert errors == []
    # one conversation per Codex rollout AND per Grok session directory
    assert {conv.id for conv in c.conversations} == {"C1", "C2", _G1, _G2}
    # threads: the Codex rollout nodes, the state-only node, and both Grok nodes
    assert set(c.threads) == {"C1", "C2", "S3", _G1, _G2}
    # each node keeps its OWN model vendor — the merge must not homogenise them, and both
    # sides must speak ONE vocabulary. This previously read
    # `== grok.GROK_PROVIDER == "grok"`, which wrote the adapter/vendor conflation down as
    # an invariant — while the Codex line directly below already asserted a real vendor,
    # so the file was describing two vocabularies in one field two lines apart.
    assert c.threads[_G1].model_provider == grok.GROK_MODEL_VENDOR == "xai"
    assert c.threads["C1"].model_provider == "openai"
    assert c.threads[_G1].model_provider != grok.GROK_PROVIDER
    # Grok's own metadata survives the merge intact
    assert c.threads[_G1].title == "Grok parent"
    assert c.threads[_G1].rollout_path == os.path.join(groot, _ENC_CWD, _G1)
    # edges from BOTH providers, still de-duplicated by (parent, child)
    assert sorted((e.parent_thread_id, e.child_thread_id) for e in c.edges) == \
        [(_G1, _G2), ("C1", "S3"), ("P1", "C1")]
    # ...and the merged graph is navigable across both
    assert c.children_of(_G1) == [_G2]
    assert c.children_of("P1") == ["C1"]
    assert c.roots() == [_G1, "C2", "P1"]

    # the FTS index covers both providers, and each row links to its own thread
    conn = corpus.open_index(idx)
    try:
        assert index.count(conn) == 4
        assert [r["conversation_id"] for r in index.search(conn, "juliett")] == [_G1]
        assert [r["conversation_id"] for r in index.search(conn, "bravo")] == ["C1"]
        row = conn.execute("SELECT thread_id, provider FROM conversations "
                           "WHERE conversation_id=?", (_G1,)).fetchone()
        assert row["thread_id"] == _G1 and row["provider"] == "grok"
    finally:
        conn.close()


def test_grok_spawn_edges_reach_DISK_not_just_the_returned_object(tmp_path):
    """Grok's spawn edges must survive to the index, asserted from a REOPENED connection.

    The repo already ate this exact P0 once: a graph that existed only in the returned
    in-memory Corpus, with every test green because every test asserted the in-memory
    copy. The cockpit never sees the returned object — it reopens the index FILE and
    rebuilds the graph with `corpus.load_corpus(conn)`. So that is what is asserted here.
    """
    sessions, home, groot, idx = _both_stores(tmp_path)

    in_memory, errors = loaders.load_corpus(sessions, idx, codex_home=home,
                                            grok_root=groot)
    assert errors == []
    persisted = _reopen_graph(idx)

    assert set(persisted.threads) == set(in_memory.threads) == \
        {"C1", "C2", "S3", _G1, _G2}
    assert sorted((e.parent_thread_id, e.child_thread_id) for e in persisted.edges) == \
        [(_G1, _G2), ("C1", "S3"), ("P1", "C1")]
    # the Grok edge is NAVIGABLE after the round-trip, and carries its status
    assert persisted.children_of(_G1) == [_G2]
    assert persisted.depth(_G2) == 1
    assert [e.status for e in persisted.edges
            if e.parent_thread_id == _G1] == ["completed"]
    # and the Grok node's model VENDOR survived persistence, not just assembly
    assert persisted.threads[_G1].model_provider == "xai"


def test_a_grok_store_is_NEVER_read_unless_grok_root_names_it(tmp_path):
    """No `~/.grok` fallback, ever — the both-states proof.

    `codex_home=None` already falls back to the LIVE Codex store, and an automated probe
    really did read the owner's real sessions that way; the `corpus.build` RPC requires
    it explicitly for exactly that reason. The Grok store holds private material, so the
    same mistake must be impossible here. A test that only checked "no Grok threads
    appear" would pass even WITH a fallback whenever the default path happens to be
    empty, so this watches the adapter entrypoint itself: it must not be called at all
    when no root was named, and must be called with EXACTLY the named root when one was.
    """
    sessions, home, groot, idx = _both_stores(tmp_path)
    seen = []

    def spy(root):
        seen.append(root)
        return [], []

    # STATE 1 — no grok_root: the adapter must never be reached, so no path (least of
    # all a default one) can be read.
    grok_ingest = grok.ingest_sessions
    grok.ingest_sessions = spy
    try:
        c, errors = loaders.load_corpus(sessions, idx, codex_home=home)
        assert seen == [], "load_corpus read a Grok store without being given one"
        assert errors == []
        assert set(c.threads) == {"C1", "C2", "S3"}          # Codex-only, unchanged

        # STATE 2 — with grok_root: the same probe FIRES, and with the named root. A
        # detector that stayed silent in both states would measure nothing.
        loaders.load_corpus(sessions, str(tmp_path / "i2.sqlite"), codex_home=home,
                            grok_root=groot)
        assert seen == [groot]
    finally:
        grok.ingest_sessions = grok_ingest


def test_a_cross_provider_thread_id_collision_is_REPORTED_and_never_overwrites(tmp_path):
    """A colliding id must cost a REPORT, not a silent overwrite.

    The Corpus is keyed by thread id and `conversations.conversation_id` is UNIQUE, so a
    Grok session id equal to a Codex thread id would (a) have its ThreadMeta REPLACE the
    Codex one in `Corpus.threads` and on disk, re-pointing that subtree, and (b) have its
    conversation silently dropped by `corpus.add_conversation`, which is idempotent by
    conversation_id and returns the existing rowid. Both are invisible. So the first
    source to claim an id keeps it and the second is refused WITH an error entry naming
    both sides.
    """
    sessions, home, groot, idx = _both_stores(tmp_path)
    # a Grok session whose info.id collides with the Codex rollout thread C1
    collider = _grok_session(groot, "C1", "GROK_C1", "sierra tango", "uniform",
                             child="C-child", enc_cwd="other%20cwd")

    c, errors = loaders.load_corpus(sessions, idx, codex_home=home, grok_root=groot)

    # the Codex thread C1 is untouched: its title, branch and provider all stand
    assert c.threads["C1"].title != "GROK_C1"
    assert c.threads["C1"].git_branch == "feat/x"
    assert c.threads["C1"].model_provider == "openai"
    # the refused session contributed no conversation and no edge
    assert {conv.id for conv in c.conversations} == {"C1", "C2", _G1, _G2}
    assert ("C1", "C-child") not in [(e.parent_thread_id, e.child_thread_id)
                                     for e in c.edges]
    # ...and the refusal is REPORTED, attributed, and points at the refused session
    collisions = [e for e in errors if e["stage"] == "thread-id-collision"]
    assert len(collisions) == 1, errors
    assert collisions[0]["source"] == "grok"
    assert collisions[0]["file"] == collider
    assert "C1" in collisions[0]["error"] and "codex-rollout" in collisions[0]["error"]
    # the non-colliding Grok sessions still ingested — one bad id costs only itself
    assert set(c.threads) == {"C1", "C2", "S3", _G1, _G2}
    # and none of it reached disk either
    assert _reopen_graph(idx).threads["C1"].model_provider == "openai"


def test_a_REPEATED_id_from_the_SAME_source_is_not_a_collision(tmp_path):
    """The detector control: a resumed session is normal, not a collision.

    A resumed Codex session writes a SECOND rollout with the same session_id, and a
    copied Grok store can repeat a session id too. Those are already handled (the thread
    upserts, the conversation dedupes by id) and must NOT be reported. A collision check
    keyed on the id alone would fire here — so this pins that it is keyed on the id AND
    the source, and would have caught a detector that flags every resume.
    """
    sessions, home = tmp_path / "sessions", tmp_path / "no_state"
    groot, idx = str(tmp_path / "grok_sessions"), str(tmp_path / "index.sqlite")
    day = os.path.join(str(sessions), "2026", "07", "24")
    _write_rollout(day, "rollout-2026-07-24T10-00-00-0000c1a.jsonl", [
        _session_meta("C1", "2026-07-24T10:00:00.000Z", parent_thread_id="P1"),
        _user("first leg", "2026-07-24T10:00:01.000Z"),
    ])
    _write_rollout(day, "rollout-2026-07-24T12-00-00-0000c1b.jsonl", [
        _session_meta("C1", "2026-07-24T12:00:00.000Z", parent_thread_id="P1"),
        _user("resumed leg", "2026-07-24T12:00:01.000Z"),
    ])
    home.mkdir()
    # the SAME Grok session id under two different cwd folders (a copied store)
    _grok_session(groot, _G1, "leg one", "victor whiskey", "xray")
    _grok_session(groot, _G1, "leg two", "yankee zulu", "alfa", enc_cwd="other%20cwd")

    c, errors = loaders.load_corpus(str(sessions), idx, codex_home=str(home),
                                    grok_root=groot)

    assert errors == [], "a resumed/repeated same-source id must not be reported"
    assert set(c.threads) == {"C1", _G1}
    assert [(e.parent_thread_id, e.child_thread_id) for e in c.edges] == [("P1", "C1")]


def test_a_broken_grok_store_does_not_cost_the_codex_ingest(tmp_path):
    """Per-source isolation: an adapter that CRASHES costs its own source and nothing else.

    `grok.ingest_sessions` survives a bad file or a bad session internally, but a failure
    it does not model would otherwise propagate out of `load_corpus` and zero a Codex
    ingest that was entirely healthy. The failure has to become one attributed error
    entry instead.
    """
    sessions, home, groot, idx = _both_stores(tmp_path)

    def boom(root):
        raise RuntimeError("grok store is unreadable")

    grok_ingest = grok.ingest_sessions
    grok.ingest_sessions = boom
    try:
        c, errors = loaders.load_corpus(sessions, idx, codex_home=home, grok_root=groot)
    finally:
        grok.ingest_sessions = grok_ingest

    # the Codex half is complete — conversations, threads, edges, and the index
    assert set(c.threads) == {"C1", "C2", "S3"}
    assert {conv.id for conv in c.conversations} == {"C1", "C2"}
    assert sorted((e.parent_thread_id, e.child_thread_id) for e in c.edges) == \
        [("C1", "S3"), ("P1", "C1")]
    conn = corpus.open_index(idx)
    try:
        assert index.count(conn) == 2
    finally:
        conn.close()
    # ...and the Grok failure is reported, attributed, and names the root it was given
    assert [(e["source"], e["stage"], e["file"]) for e in errors] == \
        [("grok", "ingest", groot)]
    assert "unreadable" in errors[0]["error"]


def test_per_source_errors_are_ATTRIBUTED_to_the_source_that_produced_them(tmp_path):
    """Every ingest error names its source, and a bad line costs only its own session.

    `errors` is a flat list mixing providers, and its entries are shaped alike
    (file/line/stage/error), so without a `source` key a reader cannot tell a broken
    rollout from a broken Grok session — which is the whole point of collecting them.
    """
    sessions, home, groot, idx = _both_stores(tmp_path)
    # a truncated tail on a Codex rollout: the shape a log read while being written has
    _write_text(os.path.join(str(sessions), "2026", "07", "24",
                             "rollout-2026-07-24T13-00-00-0000c3.jsonl"),
                json.dumps(_session_meta("C3", "2026-07-24T13:00:00.000Z")) +
                "\n{not json\n")
    # ...and the same on a Grok session's updates.jsonl
    gdir = _grok_session(groot, "0198c4d1-2f3a-7b41-9e02-000000000003", "Grok third",
                         "bravo2 charlie2", "delta2", extra_lines=("{not json",),
                         enc_cwd="third%20cwd")

    c, errors = loaders.load_corpus(sessions, idx, codex_home=home, grok_root=groot)

    by_source = {}
    for err in errors:
        by_source.setdefault(err["source"], []).append(err)
    assert set(by_source) == {"codex-rollout", "grok"}, errors
    assert [e["stage"] for e in by_source["codex-rollout"]] == ["parse"]
    assert [e["stage"] for e in by_source["grok"]] == ["parse"]
    assert by_source["grok"][0]["file"] == os.path.join(gdir, "updates.jsonl")
    # every entry still carries `file`, which sidecar._build_error indexes unguarded
    assert all(err["file"] for err in errors)
    # and BOTH sessions still ingested — the bad line cost only itself
    assert {"C3", "0198c4d1-2f3a-7b41-9e02-000000000003"} <= set(c.threads)


def test_a_grok_inclusive_re_run_duplicates_no_row(tmp_path):
    """Idempotence must hold with Grok in the mix, on the graph AND the conversations."""
    sessions, home, groot, idx = _both_stores(tmp_path)

    loaders.load_corpus(sessions, idx, codex_home=home, grok_root=groot)
    loaders.load_corpus(sessions, idx, codex_home=home, grok_root=groot)  # replay

    conn = corpus.open_index(idx)
    try:
        threads = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        edges = conn.execute("SELECT COUNT(*) FROM thread_spawn_edges").fetchone()[0]
        assert (threads, edges) == (5, 3), (threads, edges)
        assert index.count(conn) == 4          # no duplicate conversation rows
        assert index.posting_count(conn) == 4  # nor duplicate FTS postings
    finally:
        conn.close()


def test_progress_is_forwarded_for_grok_sources_too(tmp_path):
    """A Grok session must be a real `IndexSource`, not a thread node bolted on.

    `progress` firing with the Grok session DIRECTORY as its `file` is the only way to
    know the session reached `build_index` — i.e. that it is indexed, checkpointed, and
    resumable exactly like a rollout, rather than merely appearing in the graph.
    """
    sessions, home, groot, idx = _both_stores(tmp_path)
    seen = []

    loaders.load_corpus(sessions, idx, codex_home=home, grok_root=groot,
                        progress=lambda file, offset: seen.append((file, offset)))

    assert all(isinstance(f, str) and isinstance(o, int) for f, o in seen), seen
    files = [f for f, _ in seen]
    assert os.path.join(groot, _ENC_CWD, _G1) in files, files
    assert os.path.join(groot, _ENC_CWD, _G2) in files, files


def test_a_grok_only_ingest_needs_no_codex_rollouts(tmp_path):
    """Grok must stand on its own: an empty Codex tree yields a purely Grok corpus.

    A Grok ingest that only worked as a passenger on a non-empty Codex ingest would fail
    for a Grok-only user, and the empty-Codex case reports zero docs and zero errors —
    silently.
    """
    sessions, home = tmp_path / "empty_sessions", tmp_path / "no_state"
    groot, idx = str(tmp_path / "grok_sessions"), str(tmp_path / "index.sqlite")
    sessions.mkdir()
    home.mkdir()
    _grok_store(groot)

    c, errors = loaders.load_corpus(str(sessions), idx, codex_home=str(home),
                                    grok_root=groot)

    assert errors == []
    assert {conv.id for conv in c.conversations} == {_G1, _G2}
    assert set(c.threads) == {_G1, _G2}
    assert c.roots() == [_G1] and c.children_of(_G1) == [_G2]
    assert _reopen_graph(idx).children_of(_G1) == [_G2]
    conn = corpus.open_index(idx)
    try:
        assert index.count(conn) == 2
        assert [r["conversation_id"] for r in index.search(conn, "oscar")] == [_G2]
    finally:
        conn.close()
