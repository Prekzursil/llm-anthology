"""loaders.load_corpus — the cockpit ingest entrypoint (rollouts + state graph + FTS5).

SYNTHETIC fixtures ONLY. No real conversation content, thread id, path, or DB appears
here: the state DB is built in a tmp dir and the real $CODEX_HOME is never read, because
`codex_home` is always passed explicitly. Every fixture mirrors the Phase-0 measured
shapes (date-nested rollout-*.jsonl + a threads / thread_spawn_edges state DB) but never
their content.

load_corpus stitches three merged work-units together:
  * codex_rollout.ingest_sessions  — conversation content + a per-thread node + spawn edge
  * codex_state.load_corpus         — the authoritative spawn graph (threads + edges)
  * index.build_index               — the contentless FTS5 index over every conversation

The tests pin the MERGE POLICY (rollout thread metadata wins; the state graph fills the
gaps; edges are de-duplicated by (parent, child)) and the end-to-end index (searchable,
thread-linked, complete, and idempotent on a re-run).
"""
import json
import os
import sqlite3

from aisr import corpus, index, loaders


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
