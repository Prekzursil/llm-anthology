"""The `llm-anthology` console entry point, end-to-end over SYNTHETIC fixtures.

These are the contract tests for the installed package: `pip install llm-anthology`
must give a working `llm_anthology <provider> <src> <out>`. Fixtures mirror each provider's real
schema but contain no real conversation content.
"""
import json
import os
import sqlite3

from llm_anthology import cli, corpus, index, loaders


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def _claude_export():
    def msg(u, parent, sender, text, ts):
        return {"uuid": u, "parent_message_uuid": parent, "sender": sender, "created_at": ts,
                "content": [{"type": "text", "text": text, "citations": []}],
                "attachments": [], "files": [], "text": ""}
    return [{"uuid": "c1", "name": "Chat A", "created_at": "2025-01-01T00:00:00Z",
             "updated_at": "2025-01-02T00:00:00Z", "account": {"uuid": "acc1"},
             "chat_messages": [msg("m1", None, "human", "hello", "2025-01-01T00:00:01Z"),
                               msg("m2", "m1", "assistant", "hi there", "2025-01-01T00:00:02Z")]}]


def _chatgpt_export():
    return [{"title": "CG A", "conversation_id": "a", "create_time": 1.0, "current_node": "n2",
             "mapping": {
                 "n0": {"id": "n0", "message": None, "parent": None, "children": ["n1"]},
                 "n1": {"id": "n1", "parent": "n0", "children": ["n2"],
                        "message": {"id": "n1", "author": {"role": "user"}, "create_time": 1.0,
                                    "content": {"content_type": "text", "parts": ["hello"]},
                                    "metadata": {}}},
                 "n2": {"id": "n2", "parent": "n1", "children": [],
                        "message": {"id": "n2", "author": {"role": "assistant"}, "create_time": 2.0,
                                    "content": {"content_type": "text", "parts": ["hi there"]},
                                    "metadata": {}}}}}]


def _gemini_records():
    return [{"verb": "Prompted", "prompt": "hello", "response_md": "hi there",
             "timestamp_iso": "2026-01-01T10:00:00", "gem": None,
             "attachments": [], "media": [], "title": "", "detail": ""}]


def test_no_command_returns_usage_exit_code():
    assert cli.main([]) == 2


def test_demo_writes_a_self_contained_html(tmp_path):
    out = str(tmp_path / "demo.html")
    assert cli.main(["demo", out]) == 0
    doc = open(out, encoding="utf-8").read()
    assert doc.lstrip().lower().startswith("<!doctype html")


def test_claude_end_to_end(tmp_path):
    src = str(tmp_path / "claude.json")
    _write(src, _claude_export())
    out = str(tmp_path / "site")
    assert cli.main(["claude", src, out]) == 0
    assert os.path.isfile(os.path.join(out, "index.html"))
    assert len(os.listdir(os.path.join(out, "html"))) == 1
    md = os.listdir(os.path.join(out, "md"))
    body = open(os.path.join(out, "md", md[0]), encoding="utf-8").read()
    assert "hello" in body and "hi there" in body


def test_claude_accepts_a_directory_of_exports(tmp_path):
    d = tmp_path / "exports"
    d.mkdir()
    _write(str(d / "a.json"), _claude_export())
    out = str(tmp_path / "site")
    assert cli.main(["claude", str(d), out]) == 0
    assert len(os.listdir(os.path.join(out, "html"))) == 1


def test_claude_directory_skips_metadata_but_keeps_design_chats(tmp_path):
    """A Claude export directory also holds users.json / memories.json /
    projects/*.json — NOT conversations; ingesting them padded a real corpus with
    ~30 empty entries. design_chats/*.json ARE real conversations (different shape)
    and must still be rendered."""
    d = tmp_path / "acct"
    d.mkdir()
    _write(str(d / "conversations.json"), _claude_export())
    _write(str(d / "users.json"), {"uuid": "u1", "full_name": "someone"})
    _write(str(d / "memories.json"), {"uuid": "m1", "summary": "x"})
    (d / "projects").mkdir()
    _write(str(d / "projects" / "p1.json"), {"uuid": "p1", "name": "a project"})
    (d / "design_chats").mkdir()
    _write(str(d / "design_chats" / "dc1.json"),
           {"uuid": "dc1", "title": "A design chat",
            "messages": [{"uuid": "m1", "role": "user", "content": {"content": "design me"}}]})

    out = str(tmp_path / "site")
    assert cli.main(["claude", str(tmp_path), out]) == 0
    names = sorted(os.listdir(os.path.join(out, "html")))
    assert len(names) == 2                                   # conversation + design chat
    bodies = "".join(open(os.path.join(out, "html", n), encoding="utf-8").read() for n in names)
    assert "design me" in bodies and "hi there" in bodies
    assert "a project" not in bodies and "someone" not in bodies


def test_claude_directory_without_conversations_json_falls_back_to_any_json(tmp_path):
    """A renamed/single export must still work — the filter is a preference, not a trap."""
    d = tmp_path / "acct"
    d.mkdir()
    _write(str(d / "my-claude-export.json"), _claude_export())
    out = str(tmp_path / "site")
    assert cli.main(["claude", str(tmp_path), out]) == 0
    assert len(os.listdir(os.path.join(out, "html"))) == 1


def test_chatgpt_end_to_end(tmp_path):
    src = str(tmp_path / "cg.json")
    _write(src, _chatgpt_export())
    out = str(tmp_path / "site")
    assert cli.main(["chatgpt", src, out]) == 0
    md = os.listdir(os.path.join(out, "md"))
    assert "hi there" in open(os.path.join(out, "md", md[0]), encoding="utf-8").read()


def test_wrong_provider_export_is_not_a_silent_success(tmp_path):
    """Content in, nothing out, exit 0 was indistinguishable from a good run.

    A Codex-shaped export fed to the ChatGPT loader parses without raising, yields
    one conversation with ZERO turns and ZERO errors, and used to exit 0. The real
    turn silently vanished and every automated caller saw success. Measured before
    the fix: conversations=1, turns=0, errors=0, exit=0.
    """
    src = str(tmp_path / "wrong.json")
    _write(src, [{"archived": False, "id": "x", "title": "codex-shaped",
                  "turns": [{"role": "user", "content": "CONTENT THAT WOULD VANISH"}]}])
    assert cli.main(["chatgpt", src, str(tmp_path / "site")]) == 3


def test_healthy_export_still_exits_zero(tmp_path):
    """Control for the test above: a fix that fails healthy runs is just a broken build."""
    src = str(tmp_path / "ok.json")
    _write(src, _chatgpt_export())
    assert cli.main(["chatgpt", src, str(tmp_path / "site")]) == 0


def test_empty_input_is_not_an_error(tmp_path):
    """Zero conversations in means nothing was LOST -- that is not a failure.

    Only content that went in and did not come out is. Guards the fix against
    over-firing on a legitimately empty export.
    """
    src = str(tmp_path / "empty.json")
    _write(src, [])
    assert cli.main(["chatgpt", src, str(tmp_path / "site")]) == 0


def test_chatgpt_project_tag_reaches_the_index(tmp_path):
    data = _chatgpt_export()
    data[0]["__project_id"] = "g-p-XYZ"
    src = str(tmp_path / "cg.json")
    _write(src, data)
    out = str(tmp_path / "site")
    assert cli.main(["chatgpt", src, out]) == 0
    assert "g-p-XYZ" in open(os.path.join(out, "index.html"), encoding="utf-8").read()


def test_chatgpt_dedupes_a_conversation_seen_twice(tmp_path):
    data = _chatgpt_export() + _chatgpt_export()      # same conversation_id twice
    src = str(tmp_path / "cg.json")
    _write(src, data)
    out = str(tmp_path / "site")
    assert cli.main(["chatgpt", src, out]) == 0
    assert len(os.listdir(os.path.join(out, "html"))) == 1


def test_gemini_provisional_grouping_is_labelled_as_such(tmp_path):
    src = str(tmp_path / "t.json")
    _write(src, _gemini_records())
    out = str(tmp_path / "site")
    assert cli.main(["gemini", src, out]) == 0
    rep = json.load(open(os.path.join(out, "_fidelity-report.json"), encoding="utf-8"))
    assert "PROVISIONAL" in rep["grouping_mode"]


def test_gemini_harvest_grouping_is_labelled_true(tmp_path):
    src = str(tmp_path / "t.json")
    _write(src, _gemini_records())
    harvest = str(tmp_path / "h.json")
    _write(harvest, [{"id": "g1", "title": "Real Title",
                      "turns": [{"role": "user", "text": "hello"}]}])
    out = str(tmp_path / "site")
    assert cli.main(["gemini", src, out, "--harvest", harvest]) == 0
    rep = json.load(open(os.path.join(out, "_fidelity-report.json"), encoding="utf-8"))
    assert "TRUE" in rep["grouping_mode"] and rep["harvest_matched_records"] == 1


def test_a_typod_harvest_path_is_REPORTED_not_silently_downgraded(tmp_path, capsys):
    """Asking for TRUE grouping and not getting it must be audible.

    `--harvest` is guarded by `harvest_path and os.path.isfile(harvest_path)`, so a path
    that does not exist falls straight through to the gap heuristic. That is a real
    downgrade — from conversation boundaries the web app actually reported, to boundaries
    inferred from timestamp gaps — and it happened with no error, stderr empty, exit 0,
    and terminal output byte-identical to a good run, because `print_report` never printed
    `grouping_mode`. The only trace was a field inside `_fidelity-report.json`, which
    nobody reads on a run that looks like it worked.

    A typo in a flag is the likeliest cause and the easiest to miss: the flag is accepted,
    the run succeeds, and the corpus is quietly grouped worse than the user asked for.
    """
    src = str(tmp_path / "t.json")
    _write(src, _gemini_records())
    out = str(tmp_path / "site")

    rc = cli.main(["gemini", src, out, "--harvest", str(tmp_path / "typo.json")])

    printed = capsys.readouterr().out
    assert "GROUPING_MODE" in printed, "the grouping mode must be visible on every run"
    assert "PROVISIONAL" in printed, "and it must say the grouping was downgraded"
    assert "ERRORS 1" in printed, "a harvest that was named but not found is an error"
    assert rc == 0, "the corpus still rendered, so this reports rather than fails"


def test_the_grouping_mode_is_printed_on_a_GOOD_harvest_run_too(tmp_path, capsys):
    """The control. A field printed only on failure is a field nobody learns to read, and
    it would leave 'no GROUPING_MODE line' ambiguous between success and an older build.
    """
    src = str(tmp_path / "t.json")
    _write(src, _gemini_records())
    harvest = str(tmp_path / "h.json")
    _write(harvest, [{"id": "g1", "title": "Real Title",
                      "turns": [{"role": "user", "text": "hello"}]}])
    out = str(tmp_path / "site")

    assert cli.main(["gemini", src, out, "--harvest", harvest]) == 0

    printed = capsys.readouterr().out
    assert "GROUPING_MODE" in printed and "TRUE" in printed
    assert "ERRORS 0" in printed


def test_missing_input_is_a_clean_error_not_a_traceback(tmp_path):
    out = str(tmp_path / "site")
    assert cli.main(["claude", str(tmp_path / "nope.json"), out]) == 1


def test_malformed_json_is_reported_not_fatal(tmp_path):
    src = str(tmp_path / "bad.json")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    out = str(tmp_path / "site")
    rc = cli.main(["claude", src, out])
    assert rc == 0                                     # reported, not a crash
    rep = json.load(open(os.path.join(out, "_fidelity-report.json"), encoding="utf-8"))
    assert any(e["stage"] == "parse" for e in rep["errors"])


# ------------------------------------------------------------------ index (cockpit)
#
# The cockpit REQUIRES a SQLite corpus index, and until this subcommand existed no
# shipped interface could create one (loaders.load_corpus had zero production callers).
# These fixtures are SYNTHETIC: a date-nested rollout tree and a state DB built under
# tmp_path. Every test either passes --codex-home explicitly or monkeypatches
# CODEX_HOME, so the owner's real ~/.codex is never read.

def _rollout(day_dir, name, lines):
    os.makedirs(day_dir, exist_ok=True)
    path = os.path.join(day_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(line + "\n" for line in lines))
    return path


def _meta(sid, ts, **kw):
    pl = {"session_id": sid, "id": sid, "timestamp": ts, "cwd": "/repo",
          "model_provider": "openai", "git": {"branch": "feat/x"}}
    pl.update(kw)
    return json.dumps({"type": "session_meta", "timestamp": ts, "payload": pl})


def _turn(role, text, ts):
    kind = "input_text" if role == "user" else "output_text"
    return json.dumps({"type": "response_item", "timestamp": ts,
                       "payload": {"type": "message", "role": role,
                                   "content": [{"type": kind, "text": text}]}})


def _sessions_tree(root):
    """Two synthetic rollouts: C1 (spawned by P1) and C2 (a root)."""
    day = os.path.join(root, "2026", "07", "24")
    _rollout(day, "rollout-2026-07-24T10-00-00-0000c1.jsonl", [
        _meta("C1", "2026-07-24T10:00:00.000Z", parent_thread_id="P1"),
        _turn("user", "alpha bravo", "2026-07-24T10:00:01.000Z"),
        _turn("assistant", "charlie delta", "2026-07-24T10:00:02.000Z")])
    _rollout(day, "rollout-2026-07-24T11-00-00-0000c2.jsonl", [
        _meta("C2", "2026-07-24T11:00:00.000Z"),
        _turn("user", "echo foxtrot", "2026-07-24T11:00:01.000Z")])
    return day


def _state_db(home):
    """A synthetic $CODEX_HOME/state_5.sqlite carrying a state-only thread + edge."""
    os.makedirs(home, exist_ok=True)
    conn = sqlite3.connect(os.path.join(home, "state_5.sqlite"))
    conn.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, model_provider TEXT, "
        "tokens_used INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER, "
        "git_branch TEXT, cwd TEXT, agent_role TEXT, agent_nickname TEXT, "
        "preview TEXT, rollout_path TEXT)")
    conn.execute("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, "
                 "child_thread_id TEXT, status TEXT)")
    conn.execute("INSERT INTO threads (id, title) VALUES ('S3', 'STATE_S3')")
    conn.execute("INSERT INTO thread_spawn_edges VALUES ('C1', 'S3', 'state')")
    conn.commit()
    conn.close()
    return os.path.join(home, "state_5.sqlite")


def test_index_builds_a_corpus_index_the_cockpit_can_open(tmp_path, capsys):
    """The whole point: produce the SQLite file `sidecar --index <path>` consumes."""
    sessions, home = tmp_path / "sessions", tmp_path / "codex_home"
    idx = tmp_path / "corpus.sqlite"
    _sessions_tree(str(sessions))
    _state_db(str(home))

    assert cli.main(["index", str(sessions), str(idx), "--codex-home", str(home)]) == 0

    out = capsys.readouterr().out
    assert "INGESTED_CONVERSATIONS 2" in out
    assert "INDEX_ROWS 2" in out
    # these two report the PERSISTED graph, not the in-memory one -- see
    # test_index_persists_the_spawn_graph_the_cockpit_renders for why that matters
    assert "INDEX_THREADS 3" in out             # C1 + C2 from rollouts, S3 from state
    assert "INDEX_EDGES 2" in out               # (P1,C1) from rollout, (C1,S3) from state
    assert "INGEST_ERRORS 0" in out

    conn = corpus.open_index(str(idx))
    try:
        assert index.count(conn) == 2
        assert [r["conversation_id"] for r in index.search(conn, "bravo")] == ["C1"]
        row = conn.execute(
            "SELECT thread_id FROM conversations WHERE conversation_id='C1'").fetchone()
        assert row["thread_id"] == "C1"
    finally:
        conn.close()


def test_index_persists_the_spawn_graph_the_cockpit_renders(tmp_path):
    """CONVERSATION COUNTS CANNOT CATCH THIS, AND THAT IS THE POINT.

    The cockpit rebuilds its spawn tree with `corpus.load_corpus(conn)`, which reads
    EXCLUSIVELY from the `threads` and `thread_spawn_edges` tables (corpus.py:324-334).
    Nothing derives the graph from conversations. So an index whose conversations landed
    but whose graph tables are empty opens as a healthy-looking stats line above a
    COMPLETELY BLANK spawn tree — the app's primary view, dead — while every
    conversation-count assertion in this file stays green.

    Measured on the artifact this command produced before the fix:
    conversations=3, conversations_fts=3, threads=0, thread_spawn_edges=0.

    `loaders.load_corpus` assembles the graph in memory with add_thread/add_edge, then
    hands the connection to `index.build_index`, which only ever calls add_conversation
    and set_checkpoint (index.py:164,168) — it never writes either graph table — and the
    connection is closed, discarding the graph. `corpus.upsert_thread` / `upsert_edge`
    (corpus.py:256,263) exist for exactly this and had zero production callers.

    This asserts on the ARTIFACT, through the same reader the cockpit uses. It is
    EXPECTED to be RED until the loaders.py fix lands; the fix belongs there, in one
    place, for every caller — not worked around in the CLI.
    """
    sessions, home = tmp_path / "sessions", tmp_path / "codex_home"
    idx = tmp_path / "corpus.sqlite"
    _sessions_tree(str(sessions))
    _state_db(str(home))

    assert cli.main(["index", str(sessions), str(idx), "--codex-home", str(home)]) == 0

    conn = corpus.open_index(str(idx))
    try:
        graph = corpus.load_corpus(conn)          # exactly what the sidecar rebuilds
    finally:
        conn.close()

    assert graph.threads, "index has ZERO threads — the cockpit's spawn tree is blank"
    assert graph.edges, "index has ZERO spawn edges — the cockpit's spawn tree is blank"
    # the merged graph, now durable: rollout nodes C1/C2 plus the state-only node S3
    assert set(graph.threads) == {"C1", "C2", "S3"}
    assert sorted((e.parent_thread_id, e.child_thread_id) for e in graph.edges) == \
        [("C1", "S3"), ("P1", "C1")]
    # present is not enough — the reloaded graph must be NAVIGABLE
    assert graph.roots() == ["C2", "P1"]         # P1 is a dangling parent root
    assert graph.children_of("P1") == ["C1"] and graph.children_of("C1") == ["S3"]
    assert graph.depth("S3") == 2


def test_index_creates_the_parent_directory_for_the_index_file(tmp_path):
    """`corpus.open_index` is a bare sqlite3.connect — it does NOT create parents, so a
    nested --out path would die with 'unable to open database file'."""
    sessions = tmp_path / "sessions"
    _sessions_tree(str(sessions))
    idx = tmp_path / "nested" / "deeper" / "corpus.sqlite"
    assert cli.main(["index", str(sessions), str(idx),
                     "--codex-home", str(tmp_path / "no_state")]) == 0
    assert os.path.isfile(str(idx))


def test_index_surfaces_ingest_errors_and_does_not_exit_zero(tmp_path, capsys):
    """A partially-ingested index that reports success is the worst outcome: the cockpit
    silently opens an incomplete corpus. The index is still WRITTEN (the build is
    resumable, so a re-run after fixing the file completes it) but the exit code says so.
    """
    sessions = tmp_path / "sessions"
    day = _sessions_tree(str(sessions))
    _rollout(day, "rollout-2026-07-24T12-00-00-0000c3.jsonl", [
        _meta("C3", "2026-07-24T12:00:00.000Z"),
        "{not json",
        _turn("user", "golf hotel", "2026-07-24T12:00:01.000Z")])

    idx = tmp_path / "corpus.sqlite"
    rc = cli.main(["index", str(sessions), str(idx),
                   "--codex-home", str(tmp_path / "no_state")])

    cap = capsys.readouterr()
    assert rc == 3
    assert "INGEST_ERRORS 1" in cap.out
    assert "stage=parse" in cap.err          # the error is surfaced, not swallowed
    assert "INDEX_ROWS 3" in cap.out         # the readable rollouts still landed


def test_index_caps_the_error_detail_so_a_broken_tree_cannot_spam(tmp_path, capsys):
    sessions = tmp_path / "sessions"
    day = os.path.join(str(sessions), "2026", "07", "24")
    extra = 3
    for i in range(cli.MAX_ERRORS_SHOWN + extra):
        _rollout(day, "rollout-2026-07-24T10-00-%02d-0000x%d.jsonl" % (i, i), [
            _meta("X%d" % i, "2026-07-24T10:00:00.000Z"), "{not json"])

    idx = tmp_path / "corpus.sqlite"
    rc = cli.main(["index", str(sessions), str(idx),
                   "--codex-home", str(tmp_path / "no_state")])

    cap = capsys.readouterr()
    assert rc == 3
    assert "INGEST_ERRORS %d" % (cli.MAX_ERRORS_SHOWN + extra) in cap.out
    assert cap.err.count("INGEST_ERROR ") == cli.MAX_ERRORS_SHOWN
    assert "and %d more" % extra in cap.err


def test_index_on_an_empty_sessions_root_is_not_an_error(tmp_path, capsys):
    """Nothing in means nothing was LOST — the same rule the render path uses."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    idx = tmp_path / "corpus.sqlite"
    assert cli.main(["index", str(sessions), str(idx),
                     "--codex-home", str(tmp_path / "no_state")]) == 0
    out = capsys.readouterr().out
    assert "INGESTED_CONVERSATIONS 0" in out and "INDEX_ROWS 0" in out
    assert os.path.isfile(str(idx))          # an empty index is still a usable artifact


def test_index_missing_sessions_root_is_a_clean_error_not_a_traceback(tmp_path):
    """Returns before any codex_home is resolved, so no live store is touched."""
    assert cli.main(["index", str(tmp_path / "nope"), str(tmp_path / "i.sqlite")]) == 1


def test_index_forwards_the_grok_and_claude_roots_it_now_accepts(tmp_path, monkeypatch):
    """The two roots reach `load_corpus`, and are NEVER defaulted when unnamed.

    `corpus.build` has taken `grok_root` and `claude_root` for a while; the CLI took
    neither, so both stores were importable from the cockpit and from nowhere on the
    command line. This pins the forward.

    SCOPE, stated because the assertion is deliberately narrow: this tests the CLI's job,
    which is threading flags, not the ingest itself — `test_loaders_corpus.py` owns what
    each root actually does and runs at 100% coverage. A wiring test that claimed to prove
    ingest would be the overclaim, so it does not.

    The None case is the half that matters most. Both roots are opt-in against private
    stores, and a default of "" would read differently from None if the guard downstream
    ever changed from truthiness to a None check — so the CLI must pass the absence
    through verbatim rather than normalising it.
    """
    seen = {}

    def fake(src, out, codex_home=None, progress=None, grok_root=None, claude_root=None):
        seen.update(src=src, codex_home=codex_home, grok_root=grok_root,
                    claude_root=claude_root)
        return corpus.Corpus(), []

    monkeypatch.setattr(loaders, "load_corpus", fake)
    sessions = tmp_path / "sessions"
    _sessions_tree(str(sessions))

    assert cli.main(["index", str(sessions), str(tmp_path / "a.sqlite"),
                     "--grok-root", "G:\\grok", "--claude-root", "C:\\cc\\projects"]) == 0
    assert seen["grok_root"] == "G:\\grok"
    assert seen["claude_root"] == "C:\\cc\\projects"

    seen.clear()
    assert cli.main(["index", str(sessions), str(tmp_path / "b.sqlite")]) == 0
    assert seen["grok_root"] is None, "an unnamed Grok store must stay unnamed"
    assert seen["claude_root"] is None, "an unnamed Claude Code store must stay unnamed"
    assert seen["codex_home"] is None


def test_index_without_codex_home_discloses_the_live_store_it_will_read(
        tmp_path, monkeypatch, capsys):
    """With no --codex-home, `load_corpus` falls back to the LIVE Codex store
    (adapters/codex_state.py:129 — $CODEX_HOME, else ~/.codex) and that read is
    otherwise SILENT: an absent DB is skipped without a word. Someone indexing an
    ARCHIVED sessions tree would get this machine's live spawn graph merged in and
    never know. So the resolved DB path is always printed.
    """
    home = tmp_path / "live_home"
    state_db = _state_db(str(home))
    monkeypatch.setenv("CODEX_HOME", str(home))      # never the owner's real ~/.codex
    sessions = tmp_path / "sessions"
    _sessions_tree(str(sessions))
    idx = tmp_path / "corpus.sqlite"

    assert cli.main(["index", str(sessions), str(idx)]) == 0

    # The path printed is resolved through codex_state._db_path — the SAME function
    # load_corpus reads through — so a path built from $CODEX_HOME can only appear here
    # if the env var was consulted. That the graph then LANDS is a separate claim, and
    # test_index_persists_the_spawn_graph_the_cockpit_renders owns it.
    out = capsys.readouterr().out
    assert "CODEX_STATE_DB " + state_db in out
    # ...and the disclosure must be TRUE. This is the half that was missing: the test
    # asserted a path was PRINTED and never that it was READ, so it stayed green while
    # the sentence became false. `loaders.py:428` guards the whole state merge with
    # `if codex_home:`, so an omitted flag reads nothing — the printed path names a store
    # this run never opened. Two harms, and the privacy one is why it matters: it points
    # at the owner's real `~/.codex` and says it was read, while a user who believes the
    # help gets an index with NO spawn graph — the cockpit's primary view — and no error.
    assert "STATE_GRAPH_MERGED no" in out, (
        "with no --codex-home the state graph is NOT merged; the line above must not "
        "imply otherwise")
