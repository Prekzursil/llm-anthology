"""loaders.load_corpus — the cockpit ingest entrypoint (rollouts + state graph + FTS5).

SYNTHETIC fixtures ONLY. No real conversation content, thread id, path, or DB appears
here: the state DB is built in a tmp dir and the real $CODEX_HOME is never read, because
`codex_home` is always passed explicitly; no Grok store is read unless `grok_root` names
one explicitly; and no Claude Code store is read unless `claude_root` names one — the
owner's real `~/.grok` and `~/.claude` hold private material (the latter including
medical and pharmaceutical conversations) and neither is touched by this file or by the
code it exercises. Every fixture mirrors the measured shapes (date-nested
rollout-*.jsonl, a threads / thread_spawn_edges state DB, a `<enc-cwd>/<session-id>/`
Grok session directory, and a `<slug>/<session-uuid>.jsonl` Claude Code transcript tree)
but never their content.

load_corpus stitches five merged work-units together:
  * codex_rollout.ingest_sessions  — conversation content + a per-thread node + spawn edge
  * grok.ingest_sessions            — the same contract for a Grok Build session store,
                                      OPT-IN via `grok_root` (never defaulted)
  * claude_code.ingest_sessions     — the same contract for a Claude Code transcript
                                      tree, OPT-IN via `claude_root` (never defaulted)
  * codex_state.load_corpus         — the authoritative spawn graph (threads + edges)
  * index.build_index               — the contentless FTS5 index over every conversation

The tests pin the MERGE POLICY (rollout thread metadata wins, later sources claim only
ids no earlier source claimed, the state graph fills the remaining gaps; edges are
de-duplicated by (parent, child)), the CROSS-PROVIDER COLLISION policy, per-source
failure isolation, and the end-to-end index (searchable, thread-linked, complete,
idempotent on a re-run).
"""
import json
import os
import sqlite3

from llm_anthology import corpus, index, ir, loaders
from llm_anthology.adapters import claude_code, grok


def _fts_postings(conn):
    """Rows in the contentless FTS index — `index.count`'s FTS half. Equals the
    conversation count in a healthy build, so a divergence is a duplicated or orphaned
    posting, which is exactly what a replay must not produce.

    Inline `count(*)` rather than `index.posting_count`, which DECISION G-17 deleted for
    having zero production callers. The invariant it checked is unchanged and still
    asserted below; only the helper moved out of the shipped package.
    """
    return conn.execute("SELECT count(*) FROM conversations_fts").fetchone()[0]


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
        assert _fts_postings(conn) == 2  # nor duplicate FTS postings
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
    copied Grok store can repeat a session id too. Those must NOT be reported as
    collisions. A collision check keyed on the id alone would fire here — so this pins
    that it is keyed on the id AND the source, and would have caught a detector that
    flags every resume.

    WHAT THIS TEST USED TO CLAIM, AND WHY IT WAS WRONG. The docstring here said the
    repeat was "already handled (the thread upserts, the conversation dedupes by id)".
    The second half of that stopped being true when `corpus.add_conversation` was changed
    to RE-INDEX rather than early-return (corpus.py:318-331): "dedupes by id" became
    "OVERWRITES by id". This test did not notice, because it asserted only on errors,
    thread ids and edges — it never checked that both legs' CONTENT survived. It passed
    throughout the entire period the data loss was live. The content assertions now live
    in the two tests below; this one keeps its original, narrower job.
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


def test_TWO_id_less_sessions_both_reach_the_INDEX_not_just_the_corpus(tmp_path):
    """The blank-id guard in `_admit` got both id-less sessions into the in-memory corpus.
    It did not get them onto DISK.

    `codex_rollout._assemble` sets `Conversation.id = tid`, and tid is blank in exactly
    the case the guard exists for — so two id-less sessions arrive as two Conversations
    both keyed "", and `conversations.conversation_id` is UNIQUE. Result: 2 conversations
    in memory, 1 row in the index, the first one's turns unsearchable, zero errors. The
    same overwrite the resumed-session fix removed, one layer down, and the same reason it
    survived review — the test that pinned the guard asserted on the returned corpus and
    the thread ids, never on what the index actually held.

    Two id-less sessions are DIFFERENT conversations, so they cannot be merged the way a
    resumed session's legs are; they need distinct keys instead. The real store holds one
    such file today, so nothing is being lost right now — but "currently only one" is not
    a guarantee, and the failure is silent when it stops being true.
    """
    sessions, home = tmp_path / "sessions", tmp_path / "no_state"
    idx = str(tmp_path / "index.sqlite")
    day = os.path.join(str(sessions), "2026", "07", "24")
    # No session_meta and no UUID in the filename: the measured way tid comes out blank.
    _write_rollout(day, "rollout-2026-07-24T10-00-00-first.jsonl",
                   [_user("narwhalpudding", "2026-07-24T10:00:01.000Z")])
    _write_rollout(day, "rollout-2026-07-24T11-00-00-second.jsonl",
                   [_user("octopusgarden", "2026-07-24T11:00:01.000Z")])
    home.mkdir()

    c, errors = loaders.load_corpus(str(sessions), idx, codex_home=str(home))

    assert len(c.conversations) == 2, "both id-less sessions are still admitted"
    assert {conv.meta["thread_id"] for conv in c.conversations} == {""}, \
        "and neither invents a THREAD id — a blank id still claims no graph node"
    conn = corpus.open_index(idx)
    try:
        assert index.count(conn) == 2, "both must reach the index, not overwrite each other"
        assert len(corpus.search(conn, "narwhalpudding")) == 1, \
            "the FIRST id-less session must stay searchable"
        assert len(corpus.search(conn, "octopusgarden")) == 1
    finally:
        conn.close()


def test_an_id_less_session_keeps_its_key_across_a_re_run(tmp_path):
    """The synthetic key has to be STABLE, or a re-ingest appends a second row for the
    same session and the corpus grows on every build. Derived from the unit's BASENAME,
    not its full path, so that Codex moving a finished session into `archived_sessions/`
    — which it does — is not seen as a new conversation.
    """
    sessions, home = tmp_path / "sessions", tmp_path / "no_state"
    idx = str(tmp_path / "index.sqlite")
    day = os.path.join(str(sessions), "2026", "07", "24")
    _write_rollout(day, "rollout-2026-07-24T10-00-00-first.jsonl",
                   [_user("narwhalpudding", "2026-07-24T10:00:01.000Z")])
    home.mkdir()

    first, _ = loaders.load_corpus(str(sessions), idx, codex_home=str(home))
    again, _ = loaders.load_corpus(str(sessions), idx, codex_home=str(home))

    assert first.conversations[0].id == again.conversations[0].id
    conn = corpus.open_index(idx)
    try:
        assert index.count(conn) == 1, "a re-run must not add a second row"
    finally:
        conn.close()


def _user_with_id(text, ts, mid):
    """A user message carrying the provider's opaque item id (codex_rollout.py:274 reads
    `payload["id"]` into `Turn.uuid`). Needed to exercise the replay case, where the same
    item really does appear in two rollout files of one resumed session."""
    return _rec("response_item",
                {"type": "message", "role": "user", "id": mid,
                 "content": [{"type": "input_text", "text": text}]}, ts)


def test_a_RESUMED_session_keeps_BOTH_legs_of_the_conversation(tmp_path):
    """MEASURED DATA LOSS on the owner's real store, not a hypothetical.

    A resumed Codex session writes a SECOND rollout file under the SAME session_id, and
    `codex_rollout._assemble` sets `Conversation.id = session_id` (codex_rollout.py:296,
    314). Two files therefore yield two Conversations with an IDENTICAL id, both admitted
    (same source, correctly not a collision) and both written to an index whose
    `conversations.conversation_id` is UNIQUE. Since add_conversation now OVERWRITES
    instead of early-returning, the later leg REPLACED the earlier one: the earlier
    rollout's turns became unsearchable, with zero ingest errors and exit 0.

    Scale, measured over ~/.codex/sessions before this fix: 2024 rollout files, 1069
    distinct session ids, 236 of them spread across more than one file, covering 1189
    files — so 953 files (47.1% of the store) were dropped silently. Not one of the 236
    was a harmless replay in which the survivor was a superset: 156 were fully disjoint
    and 78 partially overlapping. One id spanned 66 files.

    A resumed session is ONE conversation continued, so the legs MERGE rather than
    refusing or overwriting each other.
    """
    sessions, home = tmp_path / "sessions", tmp_path / "no_state"
    idx = str(tmp_path / "index.sqlite")
    day = os.path.join(str(sessions), "2026", "07", "24")
    _write_rollout(day, "rollout-2026-07-24T10-00-00-0000c1a.jsonl", [
        _session_meta("C1", "2026-07-24T10:00:00.000Z"),
        _user("zebracrossing on the first leg", "2026-07-24T10:00:01.000Z"),
        _assistant("acknowledged the first leg", "2026-07-24T10:00:02.000Z"),
    ])
    _write_rollout(day, "rollout-2026-07-24T12-00-00-0000c1b.jsonl", [
        _session_meta("C1", "2026-07-24T12:00:00.000Z"),
        _user("quokkasandwich on the resumed leg", "2026-07-24T12:00:01.000Z"),
    ])
    home.mkdir()

    c, errors = loaders.load_corpus(str(sessions), idx, codex_home=str(home))

    assert errors == [], "a resume is not an error"
    assert [conv.id for conv in c.conversations] == ["C1"], \
        "the two legs are ONE conversation, not two rows fighting over one id"
    merged = c.conversations[0]
    assert len(merged.turns) == 3, "every turn from both legs survives the merge"

    conn = corpus.open_index(idx)
    try:
        assert index.count(conn) == 1
        # THE REGRESSION THIS PINS: before the fix the first leg matched ZERO rows.
        assert len(corpus.search(conn, "zebracrossing")) == 1, \
            "the FIRST leg must stay searchable after the session is resumed"
        assert len(corpus.search(conn, "quokkasandwich")) == 1
    finally:
        conn.close()


def test_a_RESUMED_session_does_not_DUPLICATE_a_REPLAYED_turn(tmp_path):
    """78 of the 236 real shared ids PARTIALLY overlap — the later rollout repeats some
    items of the earlier one. So a naive concatenation would show the user duplicated
    messages, which is the opposite failure to the one being fixed. Turns already present
    are dropped on merge, keyed on the provider's opaque item id.

    Only the incoming leg is filtered, and only against the turns admitted BEFORE it: a
    genuine repeat WITHIN one rollout is the adapter's output and is left exactly as-is.
    """
    sessions, home = tmp_path / "sessions", tmp_path / "no_state"
    idx = str(tmp_path / "index.sqlite")
    day = os.path.join(str(sessions), "2026", "07", "24")
    _write_rollout(day, "rollout-2026-07-24T10-00-00-0000c1a.jsonl", [
        _session_meta("C1", "2026-07-24T10:00:00.000Z"),
        _user_with_id("the shared prefix", "2026-07-24T10:00:01.000Z", "msg_shared"),
    ])
    _write_rollout(day, "rollout-2026-07-24T12-00-00-0000c1b.jsonl", [
        _session_meta("C1", "2026-07-24T12:00:00.000Z"),
        _user_with_id("the shared prefix", "2026-07-24T12:00:00.500Z", "msg_shared"),
        _user_with_id("only in the second leg", "2026-07-24T12:00:01.000Z", "msg_new"),
    ])
    home.mkdir()

    c, _ = loaders.load_corpus(str(sessions), idx, codex_home=str(home))

    merged = c.conversations[0]
    assert [t.uuid for t in merged.turns] == ["msg_shared", "msg_new"], \
        "the replayed item appears once, and the new one is appended after it"


def test_a_RESUMED_session_merges_legs_that_carry_NO_item_id(tmp_path):
    """Roughly a third of real rollout items carry no `payload["id"]`, and the Grok
    adapter never sets `Turn.uuid` at all (grok.py:311). Identity therefore cannot rest on
    the uuid alone: an id that identifies nothing maps onto nothing (the rule dedup.py and
    _admit already follow), so an id-less turn falls back to its own content.
    """
    sessions, home = tmp_path / "sessions", tmp_path / "no_state"
    idx = str(tmp_path / "index.sqlite")
    day = os.path.join(str(sessions), "2026", "07", "24")
    _write_rollout(day, "rollout-2026-07-24T10-00-00-0000c1a.jsonl", [
        _session_meta("C1", "2026-07-24T10:00:00.000Z"),
        _user("an id-less opening turn", "2026-07-24T10:00:01.000Z"),
    ])
    _write_rollout(day, "rollout-2026-07-24T12-00-00-0000c1b.jsonl", [
        _session_meta("C1", "2026-07-24T12:00:00.000Z"),
        # byte-identical replay of the opening turn: same role, stamp and text
        _user("an id-less opening turn", "2026-07-24T10:00:01.000Z"),
        _user("a genuinely new id-less turn", "2026-07-24T12:00:01.000Z"),
    ])
    home.mkdir()

    c, _ = loaders.load_corpus(str(sessions), idx, codex_home=str(home))

    merged = c.conversations[0]
    texts = [b.text for t in merged.turns for b in t.blocks]
    assert texts == ["an id-less opening turn", "a genuinely new id-less turn"], \
        "an id-less replay collapses; an id-less NEW turn survives"


def test_one_item_id_rendered_TWO_ways_keeps_the_fuller_rendering(tmp_path):
    """`Turn.uuid` marks where a turn STARTS, not how far it extends: codex_rollout.py:283
    opens an assistant turn with the first item id of a run and accumulates the rest of
    the run into it, so a resumed leg that cuts the run differently re-states the same id
    with a different body.

    MEASURED on the real store: 3 such turns across the 236 merges, all in one 66-leg
    thread, and every one DIVERGENT — neither body a prefix of the other (94 blocks /
    12,044 chars against 35 / 17,012 for the same id). Keeping whichever landed first
    would silently truncate a message by thousands of characters. This was found by
    falsifying the merge against real data AFTER the synthetic tests were already green,
    which is the only reason it is not still shipping.
    """
    sessions, home = tmp_path / "sessions", tmp_path / "no_state"
    idx = str(tmp_path / "index.sqlite")
    day = os.path.join(str(sessions), "2026", "07", "24")
    _write_rollout(day, "rollout-2026-07-24T10-00-00-0000c1a.jsonl", [
        _session_meta("C1", "2026-07-24T10:00:00.000Z"),
        # the run is cut short here — this leg saw only the opening of the answer
        _rec("response_item", {"type": "message", "role": "assistant", "id": "msg_run",
                               "content": [{"type": "output_text", "text": "short"}]},
             "2026-07-24T10:00:01.000Z"),
    ])
    _write_rollout(day, "rollout-2026-07-24T12-00-00-0000c1b.jsonl", [
        _session_meta("C1", "2026-07-24T12:00:00.000Z"),
        _rec("response_item", {"type": "message", "role": "assistant", "id": "msg_run",
                               "content": [{"type": "output_text",
                                            "text": "the whole answer, at length"}]},
             "2026-07-24T12:00:01.000Z"),
    ])
    home.mkdir()

    c, _ = loaders.load_corpus(str(sessions), idx, codex_home=str(home))

    merged = c.conversations[0]
    assert len(merged.turns) == 1, "one provider item stays one turn"
    assert [b.text for b in merged.turns[0].blocks] == ["the whole answer, at length"], \
        "the fuller rendering replaces the truncated one, in place"
    # a lossy reconciliation must be visible, not silent
    assert merged.meta["merge_divergent_turns"] == 1


def test_a_leg_that_re_states_a_turn_WORSE_does_not_shrink_it(tmp_path):
    """The mirror of the test above, and the control that proves it is choosing rather
    than just preferring whatever came last. Order the same divergence the other way —
    full rendering first, truncated second — and the full one must survive.
    """
    sessions, home = tmp_path / "sessions", tmp_path / "no_state"
    idx = str(tmp_path / "index.sqlite")
    day = os.path.join(str(sessions), "2026", "07", "24")
    _write_rollout(day, "rollout-2026-07-24T10-00-00-0000c1a.jsonl", [
        _session_meta("C1", "2026-07-24T10:00:00.000Z"),
        _rec("response_item", {"type": "message", "role": "assistant", "id": "msg_run",
                               "content": [{"type": "output_text",
                                            "text": "the whole answer, at length"}]},
             "2026-07-24T10:00:01.000Z"),
    ])
    _write_rollout(day, "rollout-2026-07-24T12-00-00-0000c1b.jsonl", [
        _session_meta("C1", "2026-07-24T12:00:00.000Z"),
        _rec("response_item", {"type": "message", "role": "assistant", "id": "msg_run",
                               "content": [{"type": "output_text", "text": "short"}]},
             "2026-07-24T12:00:01.000Z"),
    ])
    home.mkdir()

    c, _ = loaders.load_corpus(str(sessions), idx, codex_home=str(home))

    merged = c.conversations[0]
    assert [b.text for b in merged.turns[0].blocks] == ["the whole answer, at length"]
    assert "merge_divergent_turns" not in merged.meta, \
        "nothing was replaced, so nothing is reported"


def test_a_RESUMED_session_records_EVERY_leg_it_merged(tmp_path):
    """A merged conversation is stitched from more than one file, so the single
    `rollout_path` the reader opens can no longer describe where it came from. The legs
    are recorded rather than silently folded away — an ingest that quietly rewrites what
    it read is the failure mode this whole area exists to prevent.

    SCOPE OF THIS TEST, and the correction it carries. It asserts on the RETURNED corpus,
    in memory. That used to be the WHOLE story — the leg list reached no column, so the
    cockpit, which reads the index file and never this object, could not see it — and this
    docstring said so. It is no longer true: `_persist_graph` writes the legs to
    `conversation_rollouts` and `sidecar._reparse_conversation` folds them back.

    The scoping stays anyway, because an in-memory assertion still is not a disk assertion
    and reading one as the other is precisely the blind spot that hid four defects in this
    area. The DISK half is pinned in `tests/test_sidecar_merged_legs.py`, which reads the
    rows back through a bare `sqlite3.connect` and then opens the conversation over the RPC.
    """
    sessions, home = tmp_path / "sessions", tmp_path / "no_state"
    idx = str(tmp_path / "index.sqlite")
    day = os.path.join(str(sessions), "2026", "07", "24")
    first = _write_rollout(day, "rollout-2026-07-24T10-00-00-0000c1a.jsonl", [
        _session_meta("C1", "2026-07-24T10:00:00.000Z"),
        _user("leg one", "2026-07-24T10:00:01.000Z"),
    ])
    second = _write_rollout(day, "rollout-2026-07-24T12-00-00-0000c1b.jsonl", [
        _session_meta("C1", "2026-07-24T12:00:00.000Z"),
        _user("leg two", "2026-07-24T12:00:01.000Z"),
    ])
    home.mkdir()

    c, _ = loaders.load_corpus(str(sessions), idx, codex_home=str(home))

    merged = c.conversations[0]
    assert merged.meta["rollout_paths"] == [first, second]
    # the newest leg stays the resume target, because that is the one Codex continues
    assert merged.meta["rollout_path"] == second
    # and the conversation's window covers both legs, not just the first
    assert merged.updated_at == "2026-07-24T12:00:01.000Z"


def _blank_thread_id_grok_doc(session_dir, conv_id):
    """A Grok doc whose THREAD id is blank, injected rather than fixtured.

    Deliberately synthetic, and the reason is the finding: the shipped Grok adapter cannot
    produce one. `grok.py:523` derives `sid` as
    `info.id or params.sessionId or _dir_id(session_dir)`, and `_dir_id` is the directory
    basename (`grok.py:238`), which is never empty for a real session directory — grok.py:64-67
    says that fallback exists precisely so an id of "" cannot happen. So a SECOND blank-id
    source does not exist among today's two adapters, and injecting the doc is the only way
    to drive `_admit`'s handling of a blank id. Same stubbing idiom as the grok-root probe
    above.
    """
    conv = ir.Conversation(id=conv_id, title="an id-less session", provider="grok")
    return grok.GrokDoc(conversation=conv, thread=corpus.ThreadMeta(id=""),
                        session_dir=session_dir)


def test_BLANK_thread_ids_from_DIFFERENT_sources_are_BOTH_ingested(tmp_path, monkeypatch):
    """A blank thread id identifies NOTHING, so it can never be a collision.

    THE DEFECT: `_admit` claimed `doc.thread_id` with no guard for "". The first source to
    yield an id-less session therefore OWNED the `""` key, and every LATER source's id-less
    session was refused and silently never ingested — reported as "thread id '' is already
    held by codex-rollout", which reads as an id conflict when neither session had an id at
    all. `holder != label` means same-source blanks were fine, so the loss is specifically
    CROSS-source.

    The repo had already settled this question one module over: `dedup.py:339-345` excludes
    blank ids from its own map because "an id that identifies nothing maps onto nothing", and
    names codex_rollout's real blank-id case as the reason. This pins the same rule in the
    ingest path.

    REACHABILITY, stated honestly. The codex side genuinely produces `thread_id == ""`
    (`codex_rollout.py:296` falls through to `_id_from_path`, which returns "" when the
    filename carries no UUID) — asserted as a control below so this test cannot pass on a
    fixture that quietly grew an id. The grok side cannot (see the helper above), so today
    the cross-source loss is LATENT rather than live: it becomes reachable the moment any
    third adapter can emit a blank id. The `_admit` contract is wrong either way.
    """
    sessions = tmp_path / "sessions"
    day = os.path.join(str(sessions), "2026", "07", "24")
    # No session_meta, and no UUID in the filename -> a genuinely blank thread id.
    _write_rollout(day, "rollout-2026-07-24T10-00-00-plainname.jsonl", [
        _user("alpha bravo charlie", "2026-07-24T10:00:01.000Z"),
    ])
    groot = str(tmp_path / "grok_sessions")
    os.makedirs(groot, exist_ok=True)
    grok_dir = os.path.join(groot, _ENC_CWD, "no-id-session")
    monkeypatch.setattr(grok, "ingest_sessions", lambda root: (
        [_blank_thread_id_grok_doc(grok_dir, "GROK-NO-THREAD-ID")], []))

    c, errors = loaders.load_corpus(str(sessions), str(tmp_path / "index.sqlite"),
                                    grok_root=groot)

    # CONTROL: the codex fixture really is id-less. Without this the test would still pass
    # if that rollout started carrying an id, and would then be asserting nothing.
    assert "" in c.threads, "the codex fixture stopped producing a blank thread id"
    # THE REGRESSION: neither id-less session is a collision, and BOTH are ingested.
    assert [e for e in errors if e["stage"] == "thread-id-collision"] == [], errors
    # A blank THREAD id and a blank CONVERSATION id are different things, and only the
    # first stays blank. This assertion used to read `{"", "GROK-NO-THREAD-ID"}`, pinning
    # the empty conversation id — which was the overwrite bug itself, since
    # `conversations.conversation_id` is UNIQUE and a second id-less codex session would
    # have replaced this one on disk. The Grok doc is untouched because it HAS a
    # conversation id; only the codex rollout, which had none, is given a stable key.
    assert {conv.id for conv in c.conversations} == {
        "unidentified:rollout-2026-07-24T10-00-00-plainname.jsonl", "GROK-NO-THREAD-ID"}
    assert "" in c.threads, "and the blank THREAD id is still blank — no node invented"


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
        assert _fts_postings(conn) == 4  # nor duplicate FTS postings
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


# --------------------------------------------- multi-provider (Claude Code) fixtures
#
# PRIVACY, and it is the reason every value below is invented rather than sampled. The
# real Claude Code store is `~/.claude/projects`, the owner's LARGEST session store and
# the one holding private medical and pharmaceutical conversations. NOTHING in this file
# reads it: `claude_root` is a keyword argument with no default and no `~/.claude`
# fallback, exactly as `grok_root` has no `~/.grok` fallback, so there is no code path
# from these tests to that store. The SHAPES here are taken from the adapter's own
# docstring and tests, never from the store.

#: `<claude_root>/<slug>/<session-uuid>.jsonl` is a SESSION and
#: `<claude_root>/<slug>/<session-uuid>/subagents/agent-<id>.jsonl` is one of its
#: children — the layout `claude_code.classify_path` reads the whole spawn graph out of,
#: so a fixture that flattened it would exercise no edge at all.
_CC_SLUG = "C--Users-me-work-repo"
_CC_SESSION = "0198d5e6-3a4b-7c52-8f13-000000000001"
#: SHORT HEX, the measured child-id shape — deliberately NOT uuid-shaped, so the child's
#: thread id exercises the parent-qualifying branch rather than the verbatim one.
_CC_AGENT = "a97926b10"
_CC_CHILD = "%s/%s" % (_CC_SESSION, _CC_AGENT)
#: 24-char ISO stamps, the measured width.
_CC_TS = "2026-08-07T09:00:00.000Z"
_CC_TS2 = "2026-08-07T09:05:00.000Z"


def _cc_transcript(path, sid, user_text, agent_text):
    """One synthetic Claude Code JSONL transcript. There is NO header record — identity
    and provenance come from the path and from the first record carrying a field — so
    every line repeats `sessionId`/`version` the way a real append-only log does."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    records = [
        {"type": "user", "timestamp": _CC_TS, "uuid": "u-1", "parentUuid": None,
         "sessionId": sid, "cwd": "C:/Users/me/work/repo", "gitBranch": "main",
         "version": "2.1.220",
         "message": {"role": "user", "content": user_text}},
        {"type": "assistant", "timestamp": _CC_TS2, "uuid": "a-1", "parentUuid": "u-1",
         "sessionId": sid, "version": "2.1.220",
         "message": {"role": "assistant", "model": "claude-opus-5",
                     "content": [{"type": "text", "text": agent_text}]}},
    ]
    _write_text(path, "\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _claude_code_store(root):
    """The standard two-transcript store: one SESSION and one of its subagents.
    Returns (session_path, subagent_path)."""
    session = _cc_transcript(
        os.path.join(root, _CC_SLUG, _CC_SESSION + ".jsonl"), _CC_SESSION,
        "tangelo umbrella", "vermilion")
    child = _cc_transcript(
        os.path.join(root, _CC_SLUG, _CC_SESSION, "subagents",
                     "agent-%s.jsonl" % _CC_AGENT), _CC_SESSION,
        "wolfram xenon", "yarrow")
    return session, child


# ---------------------------------------------------- multi-provider (Claude Code)

def test_load_corpus_can_ingest_CLAUDE_CODE_ALONE_with_no_codex_store(tmp_path):
    """The symmetry Grok already has, for the store that had NO ingest path at all.

    `adapters/claude_code.py` exposed exactly the `ingest_sessions(root)` contract this
    loader documents and was reachable from nothing but a single-file reparse — the
    "complete, tested, called by nothing" shape. A Claude-Code-only machine is also the
    common case for this store (it is the harness's own transcript tree), so it must
    stand alone rather than only as a passenger on a Codex ingest.
    """
    croot = str(tmp_path / "claude_projects")
    idx = str(tmp_path / "index.sqlite")
    _claude_code_store(croot)

    c, errors = loaders.load_corpus("", idx, claude_root=croot)

    assert errors == []
    assert {conv.id for conv in c.conversations} == {_CC_SESSION, _CC_CHILD}
    assert set(c.threads) == {_CC_SESSION, _CC_CHILD}
    # the spawn edge the CHILD's path declares — a session declares none
    assert c.roots() == [_CC_SESSION]
    assert c.children_of(_CC_SESSION) == [_CC_CHILD]

    # ...and all of it on DISK, read back the way the sidecar reads it. An in-memory
    # assertion is not a disk assertion: the cockpit never sees the returned object.
    assert _reopen_graph(idx).children_of(_CC_SESSION) == [_CC_CHILD]
    conn = corpus.open_index(idx)
    try:
        assert index.count(conn) == 2
        assert len(corpus.search(conn, "tangelo")) == 1, "the SESSION must be searchable"
        assert len(corpus.search(conn, "wolfram")) == 1, "and so must the SUBAGENT"
        row = conn.execute("SELECT thread_id, provider FROM conversations "
                           "WHERE conversation_id=?", (_CC_SESSION,)).fetchone()
        assert row["thread_id"] == _CC_SESSION and row["provider"] == "claude-code"
    finally:
        conn.close()


def test_load_corpus_ingests_claude_code_ALONGSIDE_codex_and_grok(tmp_path):
    """One call, one index, THREE providers — and every input reaches the index.

    The per-provider tests each pass with the other providers absent, so only this one
    can catch a merge that homogenises vendors, drops a source's edges, or lets one
    provider's ids shadow another's.
    """
    sessions, home, groot, idx = _both_stores(tmp_path)
    croot = str(tmp_path / "claude_projects")
    _claude_code_store(croot)

    c, errors = loaders.load_corpus(sessions, idx, codex_home=home, grok_root=groot,
                                    claude_root=croot)

    assert errors == []
    assert {conv.id for conv in c.conversations} == {
        "C1", "C2", _G1, _G2, _CC_SESSION, _CC_CHILD}
    assert set(c.threads) == {"C1", "C2", "S3", _G1, _G2, _CC_SESSION, _CC_CHILD}
    # each provider keeps its OWN model vendor — the merge must not homogenise them
    assert c.threads[_CC_SESSION].model_provider == "anthropic"
    assert c.threads["C1"].model_provider == "openai"
    assert c.threads[_G1].model_provider == "xai"
    # edges from all three providers, still de-duplicated by (parent, child)
    assert sorted((e.parent_thread_id, e.child_thread_id) for e in c.edges) == \
        [(_G1, _G2), (_CC_SESSION, _CC_CHILD), ("C1", "S3"), ("P1", "C1")]
    assert c.roots() == [_G1, _CC_SESSION, "C2", "P1"]

    # THE INDEX, not the returned corpus: content from EVERY input must be searchable
    # on disk, because that file is the only thing the cockpit ever reads.
    conn = corpus.open_index(idx)
    try:
        assert index.count(conn) == 6
        for word, cid in (("bravo", "C1"), ("foxtrot", "C2"), ("juliett", _G1),
                          ("oscar", _G2), ("tangelo", _CC_SESSION),
                          ("wolfram", _CC_CHILD)):
            assert [r["conversation_id"] for r in index.search(conn, word)] == [cid], word
    finally:
        conn.close()
    assert _reopen_graph(idx).children_of(_CC_SESSION) == [_CC_CHILD]


def test_a_claude_code_store_is_NEVER_read_unless_claude_root_names_it(tmp_path):
    """No `~/.claude` fallback, ever — the both-states proof, and the hardest constraint
    on this whole change.

    `~/.claude` holds the owner's private conversations, including medical and
    pharmaceutical material. `codex_home=None` already falls back to the LIVE Codex store
    and an automated probe really did read the owner's real sessions that way; that is
    the measured precedent this must not repeat. A test asserting merely "no Claude Code
    threads appear" would pass EVEN WITH a fallback whenever the default path happens to
    be empty or absent, so it would measure nothing. This watches the adapter entrypoint
    itself: not called at all with no root, called with EXACTLY the named root otherwise.
    """
    sessions, home, groot, idx = _both_stores(tmp_path)
    croot = str(tmp_path / "claude_projects")
    _claude_code_store(croot)
    seen = []

    def spy(root):
        seen.append(root)
        return [], []

    cc_ingest = claude_code.ingest_sessions
    claude_code.ingest_sessions = spy
    try:
        # STATE 1 — no claude_root: the adapter must never be reached, so no path (least
        # of all a defaulted `~/.claude`) can be read.
        c, errors = loaders.load_corpus(sessions, idx, codex_home=home, grok_root=groot)
        assert seen == [], "load_corpus read a Claude Code store without being given one"
        assert errors == []
        assert set(c.threads) == {"C1", "C2", "S3", _G1, _G2}      # unchanged

        # STATE 2 — with claude_root: the same probe FIRES, and with the named root. A
        # detector silent in both states would measure nothing.
        loaders.load_corpus(sessions, str(tmp_path / "i2.sqlite"), codex_home=home,
                            grok_root=groot, claude_root=croot)
        assert seen == [croot]
    finally:
        claude_code.ingest_sessions = cc_ingest


def test_a_claude_code_thread_id_collision_is_REPORTED_and_never_overwrites(tmp_path):
    """A Claude Code transcript whose PATH-derived id equals a Codex thread id must cost
    a REPORT, not a silent overwrite.

    Reachable, not hypothetical: a session's thread id is its FILENAME STEM
    (`claude_code.classify_path`), so any transcript named `<codex-thread-id>.jsonl` —
    a rename, a copied store, a hand-made fixture — collides. `Corpus.threads` is keyed
    by thread id and `conversations.conversation_id` is UNIQUE, so the second claimant
    would re-point the first's subtree AND have its conversation silently discarded.
    """
    sessions, home, groot, idx = _both_stores(tmp_path)
    croot = str(tmp_path / "claude_projects")
    _claude_code_store(croot)
    collider = _cc_transcript(os.path.join(croot, _CC_SLUG, "C1.jsonl"), "C1",
                              "zircon periwinkle", "amaranth")

    c, errors = loaders.load_corpus(sessions, idx, codex_home=home, grok_root=groot,
                                    claude_root=croot)

    # the refusal is REPORTED, attributed, and points at the refused transcript
    collisions = [e for e in errors if e["stage"] == "thread-id-collision"]
    assert len(collisions) == 1, errors
    assert collisions[0]["source"] == "claude-code"
    assert collisions[0]["file"] == collider
    assert "C1" in collisions[0]["error"] and "codex-rollout" in collisions[0]["error"]
    # the Codex thread C1 is untouched, in memory AND on disk
    assert c.threads["C1"].model_provider == "openai"
    assert c.threads["C1"].git_branch == "feat/x"
    assert _reopen_graph(idx).threads["C1"].model_provider == "openai"
    # the refused transcript contributed no conversation — to the corpus or the index
    assert {conv.id for conv in c.conversations} == {
        "C1", "C2", _G1, _G2, _CC_SESSION, _CC_CHILD}
    conn = corpus.open_index(idx)
    try:
        assert corpus.search(conn, "zircon") == [], \
            "a refused session must not reach the index either"
        assert index.count(conn) == 6
    finally:
        conn.close()
    # the non-colliding Claude Code transcripts still ingested — one bad id costs itself
    assert {_CC_SESSION, _CC_CHILD} <= set(c.threads)


def test_a_broken_claude_code_store_does_not_cost_the_other_sources(tmp_path):
    """Per-source isolation, for the source that walks 27,770 files on the real store.

    `claude_code.ingest_sessions` survives a bad line and an unreadable file internally,
    but a failure it does not model would otherwise propagate out of `load_corpus` and
    zero a Codex+Grok ingest that was entirely healthy.
    """
    sessions, home, groot, idx = _both_stores(tmp_path)
    croot = str(tmp_path / "claude_projects")
    _claude_code_store(croot)

    def boom(root):
        raise RuntimeError("claude code store is unreadable")

    cc_ingest = claude_code.ingest_sessions
    claude_code.ingest_sessions = boom
    try:
        c, errors = loaders.load_corpus(sessions, idx, codex_home=home, grok_root=groot,
                                        claude_root=croot)
    finally:
        claude_code.ingest_sessions = cc_ingest

    assert set(c.threads) == {"C1", "C2", "S3", _G1, _G2}
    assert [(e["source"], e["stage"], e["file"]) for e in errors] == \
        [("claude-code", "ingest", croot)]
    assert "unreadable" in errors[0]["error"]


def test_a_claude_code_inclusive_re_run_duplicates_no_row(tmp_path):
    """Idempotence must hold with the third provider in the mix, on the graph AND the
    conversations — the cockpit re-ingests into an existing index."""
    sessions, home, groot, idx = _both_stores(tmp_path)
    croot = str(tmp_path / "claude_projects")
    _claude_code_store(croot)

    loaders.load_corpus(sessions, idx, codex_home=home, grok_root=groot,
                        claude_root=croot)
    loaders.load_corpus(sessions, idx, codex_home=home, grok_root=groot,
                        claude_root=croot)  # replay

    conn = corpus.open_index(idx)
    try:
        threads = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        edges = conn.execute("SELECT COUNT(*) FROM thread_spawn_edges").fetchone()[0]
        assert (threads, edges) == (7, 4), (threads, edges)
        assert index.count(conn) == 6
        assert _fts_postings(conn) == 6
    finally:
        conn.close()


def test_a_REPEATED_claude_code_thread_id_MERGES_instead_of_overwriting(tmp_path):
    """The new source flows through `_merge_resumed_leg`, so that path is exercised here.

    REACHABLE, not hypothetical: a SESSION's thread id is its filename STEM, so the same
    `<session-uuid>.jsonl` under two project slugs — a copied or moved store — repeats an
    id from the SAME source. That is not a collision (the collision rule is keyed on id
    AND source), so it merges, exactly as a repeated Grok session id under two cwd folders
    does.

    Two things would break silently without this. `_merge_resumed_leg` subscripts
    `result.threads[doc.thread.id]` with no fallback — deliberately, because a missing node
    means a broken invariant and it says so with a KeyError — which holds only because
    `ClaudeCodeDoc.thread_id` is an ALIAS of `thread.id` rather than an independently
    derived value. And `conversations.conversation_id` is UNIQUE, so without the merge the
    second file would OVERWRITE the first on disk: two files in, one row out, the first
    unsearchable, zero errors. That is the 47%-data-loss shape this area was just fixed
    for, and the reason the assertion below is against the INDEX and not the corpus.
    """
    croot = str(tmp_path / "claude_projects")
    idx = str(tmp_path / "index.sqlite")
    _cc_transcript(os.path.join(croot, _CC_SLUG, _CC_SESSION + ".jsonl"), _CC_SESSION,
                   "azimuth borealis", "cinnabar")
    _cc_transcript(os.path.join(croot, "D--other-checkout", _CC_SESSION + ".jsonl"),
                   _CC_SESSION, "dulcimer eglantine", "fennel")

    c, errors = loaders.load_corpus("", idx, claude_root=croot)

    assert errors == [], "a same-source repeat is not a collision"
    assert [conv.id for conv in c.conversations] == [_CC_SESSION], \
        "the two files are ONE conversation, not two rows fighting over one id"
    conn = corpus.open_index(idx)
    try:
        assert index.count(conn) == 1
        assert len(corpus.search(conn, "azimuth")) == 1, \
            "the FIRST transcript must stay searchable after the second is merged in"
        assert len(corpus.search(conn, "dulcimer")) == 1
    finally:
        conn.close()


def test_progress_is_forwarded_for_claude_code_sources_too(tmp_path):
    """A Claude Code transcript must be a real `IndexSource`, not a thread node bolted on.

    `progress` firing with the TRANSCRIPT PATH as its `file` is the only way to know the
    document reached `build_index` — i.e. that it is indexed, checkpointed and resumable
    like a rollout. It also pins the `path_attr` choice: the unit on disk for this
    provider is a FILE (`transcript_path`), not a directory as it is for Grok, and that
    string becomes the ingest_checkpoint key.
    """
    croot = str(tmp_path / "claude_projects")
    idx = str(tmp_path / "index.sqlite")
    session, child = _claude_code_store(croot)
    seen = []

    loaders.load_corpus("", idx, claude_root=croot,
                        progress=lambda file, offset: seen.append((file, offset)))

    files = [f for f, _ in seen]
    assert all(isinstance(f, str) and isinstance(o, int) for f, o in seen), seen
    assert session in files and child in files, files
