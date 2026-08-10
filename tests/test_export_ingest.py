"""loaders.ingest_exports — the route from a DOWNLOADED provider export INTO the corpus.

WHY THIS FILE EXISTS. The engine held TWO DISJOINT PIPELINES and no route between them:
`load_claude` / `load_chatgpt` / `load_gemini` / `load_codex` rendered HTML+Markdown and
were called from exactly one place (the four render subcommands), while `load_corpus` —
the only producer of the SQLite index the desktop app reads — ingested LIVE SESSION
STORES and nothing else. So the product could not import the one artifact every user
actually has: a downloaded export. That is the defect these tests pin closed.

SYNTHETIC FIXTURES ONLY. Every export here is hand-built under tmp_path in the measured
SHAPE of a real one (a ChatGPT `mapping`/`current_node` graph, a Claude `chat_messages`
tree, a Claude `design_chats` document, a Codex task thread, a Takeout activity record)
and carries nothing but nonsense words. No real conversation content, id or path appears,
and no live store is read: `ingest_exports` globs only the paths a test names.

WHAT IS DELIBERATELY *NOT* ASSERTED. An imported export produces ZERO spawn-graph nodes
and ZERO edges, because an export format carries no parent/child session relationship —
only a live agent store does. `test_an_imported_export_adds_no_graph_node_or_edge` pins
that as the EXPECTED state rather than a gap, so nobody later "fixes" it by fabricating
edges.
"""
import json
import os

import pytest

from llm_anthology import cli, corpus, index, loaders
from llm_anthology.adapters import chatgpt as chatgpt_adapter


# ------------------------------------------------------------------- fixtures

def _write(path, doc):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path


def _chatgpt_conv(cid, text):
    """One conversations.json record in the measured shape: a `mapping` of nodes whose
    active path is resolved from `current_node`."""
    return {
        "id": cid, "title": "title %s" % cid,
        "create_time": 1700000000.0, "update_time": 1700000060.0,
        "current_node": "n2",
        "mapping": {
            "n1": {"id": "n1", "parent": None, "children": ["n2"],
                   "message": {"id": "m1", "author": {"role": "user"},
                               "create_time": 1700000000.0,
                               "content": {"content_type": "text", "parts": [text]}}},
            "n2": {"id": "n2", "parent": "n1", "children": [],
                   "message": {"id": "m2", "author": {"role": "assistant"},
                               "create_time": 1700000060.0,
                               "content": {"content_type": "text",
                                           "parts": ["reply to " + text]}}},
        },
    }


def _claude_conv(cid, text):
    """One claude.ai export record: chat_messages with parent links."""
    return {
        "uuid": cid, "name": "name %s" % cid,
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:01:00Z",
        "account": {"uuid": "acct-1"},
        "chat_messages": [
            {"uuid": "cm1", "parent_message_uuid": None, "sender": "human",
             "created_at": "2026-01-01T00:00:00Z",
             "content": [{"type": "text", "text": text}]},
            {"uuid": "cm2", "parent_message_uuid": "cm1", "sender": "assistant",
             "created_at": "2026-01-01T00:00:30Z",
             "content": [{"type": "text", "text": "reply to " + text}]},
        ],
    }


def _claude_design_chat(cid, text):
    """A design_chats/*.json document — a top-level OBJECT, not an array, and a DIFFERENT
    shape (`messages` + a content dict). Feeding one through parse_conversation yields a
    silently empty conversation, which is why load_claude branches on it."""
    return {"uuid": cid, "title": "design %s" % cid,
            "created_at": "2026-02-02T00:00:00Z",
            "messages": [{"role": "user", "uuid": "dm1",
                          "content": {"content": text}}]}


def _codex_thread(tid, text):
    """One codex.json task thread."""
    return {
        "id": tid, "title": "task %s" % tid, "archived": False,
        "turns": [
            {"role": "user", "id": "u1",
             "input_items": [{"type": "message",
                              "content": [{"content_type": "text", "text": text}]}]},
            {"role": "assistant", "id": "a1", "branch": "main", "branch_name": None,
             "turn_status": "TaskTurnStatusEnum.COMPLETED",
             "output_items": [{"type": "message",
                               "content": [{"content_type": "text",
                                            "text": "reply to " + text}]}]},
        ],
    }


def _gemini_records(*prompts):
    """Takeout 'Gemini Apps' activity records, one per prompt, 1 minute apart."""
    return [{"prompt": p, "response_md": "reply to " + p, "gem": None,
             "timestamp_iso": "2026-03-03T10:%02d:00" % i}
            for i, p in enumerate(prompts)]


def _ids(index_path):
    conn = corpus.open_index(index_path)
    try:
        return sorted(r[0] for r in conn.execute(
            "SELECT conversation_id FROM conversations"))
    finally:
        conn.close()


def _row(index_path, cid):
    conn = corpus.open_index(index_path)
    try:
        conn.row_factory = None
        return conn.execute(
            "SELECT provider, title, turn_count, thread_id, rollout_path "
            "FROM conversations WHERE conversation_id=?", (cid,)).fetchone()
    finally:
        conn.close()


def _search(index_path, query):
    conn = corpus.open_index(index_path)
    try:
        return [r["conversation_id"] for r in index.search(conn, query)]
    finally:
        conn.close()


# ------------------------------------------------------ the route (TASK A)

def test_a_chatgpt_export_FILE_becomes_searchable_in_the_corpus_index(tmp_path):
    """THE DEFECT, closed: a downloaded conversations.json now reaches the index."""
    src = _write(str(tmp_path / "conversations.json"),
                 [_chatgpt_conv("c-1", "alpha bravo"), _chatgpt_conv("c-2", "charlie")])
    idx = str(tmp_path / "corpus.sqlite")

    files, errors = loaders.ingest_exports(idx, [("chatgpt", src, "")])

    assert errors == []
    assert [f["conversations"] for f in files] == [2]
    assert _ids(idx) == ["c-1", "c-2"]
    assert _search(idx, "bravo") == ["c-1"]
    assert _row(idx, "c-1")[0] == "chatgpt"      # the adapter's own provider label
    assert _row(idx, "c-1")[2] == 2              # both turns indexed


def test_a_sharded_chatgpt_export_DIRECTORY_contributes_every_shard(tmp_path):
    """A real ChatGPT Data Export ships conversations-000.json ... -NNN.json; a directory
    must contribute every shard AND a legacy single conversations.json beside them."""
    d = str(tmp_path / "export")
    _write(os.path.join(d, "conversations-000.json"), [_chatgpt_conv("s-0", "alpha")])
    _write(os.path.join(d, "conversations-001.json"), [_chatgpt_conv("s-1", "bravo")])
    _write(os.path.join(d, "conversations.json"), [_chatgpt_conv("s-2", "charlie")])
    idx = str(tmp_path / "corpus.sqlite")

    files, errors = loaders.ingest_exports(idx, [("chatgpt", d, "")])

    assert errors == []
    assert _ids(idx) == ["s-0", "s-1", "s-2"]
    assert len(files) == 3, "one report row per shard, so a dead shard is visible"


def test_a_conversation_repeated_across_shards_is_indexed_once_and_counted(tmp_path):
    """load_chatgpt dedupes by id across shards; the streaming route must too."""
    d = str(tmp_path / "export")
    _write(os.path.join(d, "conversations-000.json"), [_chatgpt_conv("dup", "alpha")])
    _write(os.path.join(d, "conversations-001.json"), [_chatgpt_conv("dup", "alpha")])
    idx = str(tmp_path / "corpus.sqlite")

    files, errors = loaders.ingest_exports(idx, [("chatgpt", d, "")])

    assert errors == []
    assert _ids(idx) == ["dup"]
    assert [(f["conversations"], f["duplicates"]) for f in files] == [(1, 0), (0, 1)]


def test_the_chatgpt_projects_file_is_a_second_export_in_the_same_dedup_pool(tmp_path):
    """`--chatgpt-projects` mirrors the render path's `--projects`: a SECOND export file
    whose project-tagged conversations are merged into one pool, deduped by id."""
    main = _write(str(tmp_path / "conversations.json"), [_chatgpt_conv("p-1", "alpha")])
    proj = _write(str(tmp_path / "projects.json"),
                  [_chatgpt_conv("p-1", "alpha"), _chatgpt_conv("p-2", "bravo")])
    idx = str(tmp_path / "corpus.sqlite")

    files, errors = loaders.ingest_exports(idx, [("chatgpt", main, proj)])

    assert errors == []
    assert _ids(idx) == ["p-1", "p-2"]
    assert sum(f["duplicates"] for f in files) == 1


def test_a_claude_export_reaches_the_index_and_so_does_a_design_chat(tmp_path):
    """Both Claude shapes: the conversations.json ARRAY and the design_chats OBJECT."""
    d = str(tmp_path / "claude")
    _write(os.path.join(d, "conversations.json"), [_claude_conv("cl-1", "alpha bravo")])
    _write(os.path.join(d, "design_chats", "dc.json"),
           _claude_design_chat("dc-1", "charlie delta"))
    # NOT conversations: a real export dir also carries these, and wrapping each as an
    # empty conversation is what once padded a 212-conversation corpus with junk.
    _write(os.path.join(d, "users.json"), {"uuid": "u"})
    idx = str(tmp_path / "corpus.sqlite")

    files, errors = loaders.ingest_exports(idx, [("claude", d, "")])

    assert errors == []
    assert _ids(idx) == ["cl-1", "dc-1"]
    assert _search(idx, "charlie") == ["dc-1"]
    assert [f["conversations"] for f in files] == [1, 1]


def test_a_codex_task_export_reaches_the_index(tmp_path):
    src = _write(str(tmp_path / "codex.json"),
                 [_codex_thread("t-1", "alpha bravo"), _codex_thread("t-2", "charlie")])
    idx = str(tmp_path / "corpus.sqlite")

    files, errors = loaders.ingest_exports(idx, [("codex", src, "")])

    assert errors == []
    assert _ids(idx) == ["t-1", "t-2"]
    assert _search(idx, "bravo") == ["t-1"]
    assert [f["conversations"] for f in files] == [2]


def test_a_single_document_export_is_read_as_one_conversation(tmp_path):
    """A harvested or hand-saved export is a top-level OBJECT, not an array. json.load
    wrapped it in a list; the streaming reader must not drop it."""
    src = _write(str(tmp_path / "one.json"), _chatgpt_conv("solo", "alpha"))
    idx = str(tmp_path / "corpus.sqlite")

    files, errors = loaders.ingest_exports(idx, [("chatgpt", src, "")])

    assert errors == [] and _ids(idx) == ["solo"]
    assert [f["conversations"] for f in files] == [1]


def test_a_gemini_transcript_reaches_the_index_and_DISCLOSES_its_grouping_mode(tmp_path):
    """Takeout carries no conversation id, so grouping is a labelled PROVISIONAL time-gap
    heuristic unless a web-app harvest is supplied. The ingest must say which it used —
    a corpus whose conversation boundaries were inferred must not look like ground truth.
    """
    src = _write(str(tmp_path / "transcript.json"), _gemini_records("alpha", "bravo"))
    idx = str(tmp_path / "corpus.sqlite")

    files, errors = loaders.ingest_exports(idx, [("gemini", src, "")])

    assert errors == []
    assert files[0]["grouping_mode"] == "gap-heuristic (PROVISIONAL)"
    assert files[0]["conversations"] == 1        # one group: both records within the gap
    assert _search(idx, "bravo")


def test_a_gemini_harvest_upgrades_the_grouping_and_is_reported(tmp_path):
    src = _write(str(tmp_path / "transcript.json"), _gemini_records("alpha", "bravo"))
    harvest = _write(str(tmp_path / "harvest.json"),
                     [{"id": "h1", "title": "First", "turns": [
                         {"role": "user", "text": "alpha"}]},
                      {"id": "h2", "title": "Second", "turns": [
                          {"role": "user", "text": "bravo"}]}])
    idx = str(tmp_path / "corpus.sqlite")

    files, errors = loaders.ingest_exports(idx, [("gemini", src, harvest)])

    assert errors == []
    assert files[0]["grouping_mode"] == "harvest (TRUE grouping)"
    assert sorted(_ids(idx)) == ["h1", "h2"]


def test_a_named_gemini_harvest_that_is_absent_is_reported_not_silently_downgraded(
        tmp_path):
    """load_gemini's harvest-error path: a NAMED but missing harvest falls back to the
    provisional heuristic and says so. That report must survive into the ingest errors —
    a flag typo is the likeliest cause and the hardest to notice, because it IS accepted.
    """
    src = _write(str(tmp_path / "transcript.json"), _gemini_records("alpha"))
    idx = str(tmp_path / "corpus.sqlite")

    files, errors = loaders.ingest_exports(
        idx, [("gemini", src, str(tmp_path / "typo.json"))])

    assert [e["stage"] for e in errors] == ["harvest"]
    assert "PROVISIONAL" in errors[0]["error"]
    assert files[0]["grouping_mode"] == "gap-heuristic (PROVISIONAL)"
    assert _ids(idx) == ["grp001"], "the corpus is still built, just provisionally grouped"


def test_an_export_and_a_session_store_land_in_ONE_index(tmp_path):
    """The whole point of promoting exports to first-class inputs: one searchable corpus
    across both kinds of source, not two."""
    day = str(tmp_path / "sessions" / "2026" / "07" / "24")
    os.makedirs(day)
    with open(os.path.join(day, "rollout-2026-07-24T10-00-00-0000aa.jsonl"), "w",
              encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "session_meta", "timestamp": "2026-07-24T10:00:00Z",
                             "payload": {"session_id": "S1", "id": "S1",
                                         "timestamp": "2026-07-24T10:00:00Z"}}) + "\n")
        fh.write(json.dumps({"type": "response_item",
                             "timestamp": "2026-07-24T10:00:01Z",
                             "payload": {"type": "message", "role": "user", "content": [
                                 {"type": "input_text", "text": "sessiontext"}]}}) + "\n")
    src = _write(str(tmp_path / "conversations.json"), [_chatgpt_conv("x-1", "exporttext")])
    idx = str(tmp_path / "corpus.sqlite")

    result, session_errors = loaders.load_corpus(str(tmp_path / "sessions"), idx)
    files, export_errors = loaders.ingest_exports(idx, [("chatgpt", src, "")])

    assert session_errors == [] and export_errors == []
    assert len(result.conversations) == 1 and files[0]["conversations"] == 1
    assert _ids(idx) == ["S1", "x-1"]
    assert _search(idx, "sessiontext") == ["S1"]
    assert _search(idx, "exporttext") == ["x-1"]


# --------------------------------------------------- graph honesty (no fabrication)

def test_an_imported_export_adds_no_graph_node_or_edge(tmp_path):
    """EXPECTED, not a gap. An export format carries no parent/child session
    relationship, so an honest ingest contributes zero spawn-graph nodes and zero edges.
    Fabricating either would put a tree on screen that the source data does not contain.
    """
    src = _write(str(tmp_path / "conversations.json"),
                 [_chatgpt_conv("g-1", "alpha"), _chatgpt_conv("g-2", "bravo")])
    idx = str(tmp_path / "corpus.sqlite")

    loaders.ingest_exports(idx, [("chatgpt", src, "")])

    conn = corpus.open_index(idx)
    try:
        graph = corpus.load_corpus(conn)
        assert graph.threads == {} and graph.edges == []
        assert index.count(conn) == 2, "the conversations DID land; only the graph is empty"
    finally:
        conn.close()
    assert _row(idx, "g-1")[3] == "", "an imported conversation claims no thread"


def test_no_session_store_READER_is_ever_pointed_at_an_export_file(tmp_path):
    """`conversations.rollout_path` stays EMPTY for an imported conversation, and this is
    load-bearing rather than tidiness.

    The sidecar dispatches its re-parser on the `provider` column, and `codex` names TWO
    different things: a Codex ROLLOUT (JSONL, one session per file) and a Codex TASK
    EXPORT (a JSON array of threads). MEASURED: handing `codex.json` to
    `codex_rollout.parse_rollout_file` returns a doc with ZERO turns plus a
    'line is not a JSON object' parse error — so a stored path would make the reader show
    an EMPTY transcript for a conversation that search can match. An empty path instead
    yields the honest 'rollout unavailable' stub. The source file is still recorded, as
    the conversation's single rollout LEG (see the provenance test below).

    A provider the app has NO session reader for keeps its path, where the column is most
    useful and can never be dispatched on. The asymmetry is the whole point, and the
    invariant test below is what keeps it correct as the reader table changes.
    """
    codex_src = _write(str(tmp_path / "codex.json"), [_codex_thread("t-1", "alpha")])
    chat_src = _write(str(tmp_path / "conversations.json"),
                      [_chatgpt_conv("c-1", "alpha")])
    idx = str(tmp_path / "corpus.sqlite")

    loaders.ingest_exports(idx, [("codex", codex_src, ""), ("chatgpt", chat_src, "")])

    assert _row(idx, "t-1")[4] == "", "codex names a wired rollout reader — no path"
    assert _row(idx, "c-1")[4] == chat_src, "chatgpt names no reader — keep the path"


def test_the_export_file_is_recorded_as_the_conversations_single_rollout_leg(tmp_path):
    """Provenance is not dropped: the file each imported conversation was parsed from is
    written to `conversation_rollouts`. A ONE-entry leg list is inert for the reader
    (`_reparse_conversation` falls through to the column below two legs), so recording it
    cannot re-create the wrong-reader hazard above — it is a record, not a dispatch."""
    src = _write(str(tmp_path / "conversations.json"), [_chatgpt_conv("l-1", "alpha")])
    idx = str(tmp_path / "corpus.sqlite")

    loaders.ingest_exports(idx, [("chatgpt", src, "")])

    conn = corpus.open_index(idx)
    try:
        assert corpus.rollout_legs(conn, "l-1") == [src]
    finally:
        conn.close()


def test_the_provider_labels_an_export_produces_agree_with_the_readers_the_app_wires():
    """The INVARIANT behind the two tests above, checked against the sidecar's own table
    rather than restated by hand: for every export provider, either the app has NO
    session-store reader for that label (so a stored path could never be dispatched), or
    that label is on the no-path list. A future reader wired for `chatgpt` — or a fifth
    export provider added here — turns this red instead of silently mis-reading a file.
    """
    from llm_anthology import sidecar

    for provider in loaders.EXPORT_PROVIDERS:
        assert (provider not in sidecar._REPARSERS
                or provider in loaders.EXPORT_PATH_COLLIDES_WITH_A_READER), (
            "%r now has a session-store reader in sidecar._REPARSERS; add it to "
            "loaders.EXPORT_PATH_COLLIDES_WITH_A_READER or that reader will be handed an "
            "export file" % provider)
    for provider in loaders.EXPORT_PATH_COLLIDES_WITH_A_READER:
        assert provider in sidecar._REPARSERS, (
            "%r is on the no-path list but nothing in sidecar._REPARSERS would read it — "
            "a suppression nothing spends reads as evidence the hazard was checked"
            % provider)


# ------------------------------------------------------- streaming (TASK B / G-7)

def test_an_array_export_is_STREAMED_and_never_whole_file_loaded(tmp_path, monkeypatch):
    """The deterministic streaming proof. `_load_json` is the only whole-file reader in
    this module, so breaking it and requiring the ingest to still succeed proves the
    array path never materialises the document — no memory threshold, no flake."""
    src = _write(str(tmp_path / "conversations.json"),
                 [_chatgpt_conv("st-%d" % i, "alpha%d" % i) for i in range(5)])
    idx = str(tmp_path / "corpus.sqlite")

    def refuse(path):
        raise AssertionError("whole-file json.load on %s — the array path must stream"
                             % path)

    monkeypatch.setattr(loaders, "_load_json", refuse)
    files, errors = loaders.ingest_exports(idx, [("chatgpt", src, "")])

    assert errors == []
    assert files[0]["conversations"] == 5


def test_records_are_committed_in_chunks_so_peak_memory_is_conversation_sized(tmp_path):
    """The second half of the streaming claim: records reach DISK as the stream is read,
    rather than being accumulated and written at the end. The progress callback fires per
    committed chunk (the same contract `index.build_index` offers), and by the first call
    the index already holds rows while the file is still being read."""
    src = _write(str(tmp_path / "conversations.json"),
                 [_chatgpt_conv("ch-%d" % i, "alpha%d" % i) for i in range(5)])
    idx = str(tmp_path / "corpus.sqlite")
    seen = []

    def progress(path, offset):
        conn = corpus.open_index(idx)
        try:
            seen.append((os.path.basename(path), offset, index.count(conn)))
        finally:
            conn.close()

    loaders.ingest_exports(idx, [("chatgpt", src, "")], progress=progress,
                           chunk_size=2)

    assert [(o, n) for _f, o, n in seen] == [(2, 2), (4, 4), (5, 5)]


def test_the_ingest_holds_only_one_conversation_at_a_time(tmp_path, monkeypatch):
    """The claim G-7 rests on, measured structurally rather than against a byte threshold:
    the number of parsed conversations held at once is CONSTANT in the size of the export.

    Every parsed conversation is tracked by a weak reference, and each new parse counts how
    many earlier ones are still reachable after a forced collection. A route that
    accumulated — as `load_chatgpt` does, holding every raw dict AND every parsed
    Conversation before returning a list — would climb 1, 2, 3, 4, 5 across six records.

    THE BOUND IS ONE, NOT ZERO, and the reason is structural rather than sloppy: the
    consumer holds the previous `(conv, err)` pair in its loop variables until `next()`
    returns, and `next()` is what parses the following record. So exactly one predecessor
    is alive at the moment of a parse, whatever the export's size — which is the O(1) claim
    this test exists to make, stated at the value it actually measures.
    """
    import gc
    import weakref

    src = _write(str(tmp_path / "conversations.json"),
                 [_chatgpt_conv("w-%d" % i, "alpha%d" % i) for i in range(6)])
    idx = str(tmp_path / "corpus.sqlite")
    alive, held, real = [], [], chatgpt_adapter.parse_conversation

    def tracking(raw):
        conv = real(raw)
        gc.collect()
        held.append(len([ref for ref in alive if ref() is not None]))
        alive.append(weakref.ref(conv))
        return conv

    monkeypatch.setattr(chatgpt_adapter, "parse_conversation", tracking)
    files, errors = loaders.ingest_exports(idx, [("chatgpt", src, "")], chunk_size=2)

    assert errors == [] and files[0]["conversations"] == 6
    assert len(held) == 6, "every record really was parsed"
    assert max(held) <= 1, (
        "live earlier conversations per parse was %r — anything that grows with the record "
        "number means this route accumulates the export" % (held,))


# ------------------------------------------------------- fail-closed reporting

def test_a_named_export_that_resolves_to_NO_FILE_is_an_error_not_a_silent_success(
        tmp_path):
    """The recurring defect class in this tree: a glob that matches nothing reports a
    perfectly successful ingest of it. A named source must never do that."""
    idx = str(tmp_path / "corpus.sqlite")
    empty = tmp_path / "empty_dir"
    empty.mkdir()

    files, errors = loaders.ingest_exports(idx, [("chatgpt", str(empty), "")])

    assert files == []
    assert [e["stage"] for e in errors] == ["resolve"]
    assert "no chatgpt export file" in errors[0]["error"]


def test_an_unknown_provider_is_refused_by_name(tmp_path):
    idx = str(tmp_path / "corpus.sqlite")
    files, errors = loaders.ingest_exports(idx, [("grok", "anything", "")])
    assert files == []
    assert [e["stage"] for e in errors] == ["spec"]
    assert "grok" in errors[0]["error"]


def test_a_second_path_for_a_provider_that_has_none_is_refused_not_ignored(tmp_path):
    """A silently ignored flag is the shape load_gemini's harvest guard exists for: the
    flag IS accepted, nothing happens, and the exit code says success."""
    src = _write(str(tmp_path / "codex.json"), [_codex_thread("t-1", "alpha")])
    idx = str(tmp_path / "corpus.sqlite")

    files, errors = loaders.ingest_exports(idx, [("codex", src, "second.json")])

    assert files == []
    assert [e["stage"] for e in errors] == ["spec"]
    assert "takes no second path" in errors[0]["error"]


def test_a_truncated_export_reports_a_parse_error_and_keeps_what_it_already_wrote(
        tmp_path):
    """A streamed ingest that dies mid-file must not lose the records it already
    committed, and must not report success."""
    src = str(tmp_path / "conversations.json")
    body = json.dumps([_chatgpt_conv("k-1", "alpha"), _chatgpt_conv("k-2", "bravo")])
    with open(src, "w", encoding="utf-8") as fh:
        fh.write(body[:body.index('{"id": "k-2"')] + '{"id": "k-2", "map')
    idx = str(tmp_path / "corpus.sqlite")

    files, errors = loaders.ingest_exports(idx, [("chatgpt", src, "")], chunk_size=1)

    assert [e["stage"] for e in errors] == ["parse"]
    assert _ids(idx) == ["k-1"], "the record read before the break still landed"
    assert files[0]["conversations"] == 1


def test_an_empty_file_is_a_parse_error_rather_than_an_empty_success(tmp_path):
    src = str(tmp_path / "conversations.json")
    open(src, "w").close()
    idx = str(tmp_path / "corpus.sqlite")

    files, errors = loaders.ingest_exports(idx, [("chatgpt", src, "")])

    assert [e["stage"] for e in errors] == ["parse"]
    assert files[0]["conversations"] == 0


def test_one_unreadable_record_does_not_cost_the_others(tmp_path, monkeypatch):
    """Per-record isolation, exactly as the render loaders collect rather than raise."""
    src = _write(str(tmp_path / "conversations.json"),
                 [_chatgpt_conv("ok-1", "alpha"), _chatgpt_conv("bad", "bravo"),
                  _chatgpt_conv("ok-2", "charlie")])
    idx = str(tmp_path / "corpus.sqlite")
    real = chatgpt_adapter.parse_conversation

    def explode(raw):
        if raw.get("id") == "bad":
            raise ValueError("synthetic adapter failure")
        return real(raw)

    monkeypatch.setattr(chatgpt_adapter, "parse_conversation", explode)
    files, errors = loaders.ingest_exports(idx, [("chatgpt", src, "")])

    assert _ids(idx) == ["ok-1", "ok-2"]
    assert [e["stage"] for e in errors] == ["adapt"]
    assert files[0]["conversations"] == 2


def test_a_non_dict_record_is_skipped_the_way_parse_export_skips_it(tmp_path):
    src = _write(str(tmp_path / "conversations.json"),
                 ["not a conversation", _chatgpt_conv("d-1", "alpha")])
    idx = str(tmp_path / "corpus.sqlite")

    files, errors = loaders.ingest_exports(idx, [("chatgpt", src, "")])

    assert errors == [] and _ids(idx) == ["d-1"]
    assert files[0]["conversations"] == 1


def test_a_record_with_no_id_is_reported_rather_than_indexed_under_a_blank_key(tmp_path):
    """`conversations.conversation_id` is UNIQUE, so two id-less records would overwrite
    each other on disk — the same defect `_admit` fills a synthetic id in for. An export
    record has no path to key on, so it is refused and named instead of silently merged."""
    src = _write(str(tmp_path / "conversations.json"),
                 [_chatgpt_conv("", "alpha"), _chatgpt_conv("", "bravo"),
                  _chatgpt_conv("i-1", "charlie")])
    idx = str(tmp_path / "corpus.sqlite")

    files, errors = loaders.ingest_exports(idx, [("chatgpt", src, "")])

    assert _ids(idx) == ["i-1"]
    assert [e["stage"] for e in errors] == ["identity", "identity"]
    assert files[0]["conversations"] == 1


def test_an_export_may_not_overwrite_a_conversation_another_provider_already_holds(
        tmp_path):
    """`add_conversation` is idempotent BY ID and overwrites, so an export record whose id
    equals a session conversation's id would replace it — invisibly. Mirrors the
    cross-provider refusal `_admit` already makes for thread ids: the second claimant is
    refused, named, and contributes nothing."""
    idx = str(tmp_path / "corpus.sqlite")
    session = _write(str(tmp_path / "codex.json"), [_codex_thread("shared", "alpha")])
    loaders.ingest_exports(idx, [("codex", session, "")])

    clash = _write(str(tmp_path / "conversations.json"),
                   [_chatgpt_conv("shared", "bravo")])
    files, errors = loaders.ingest_exports(idx, [("chatgpt", clash, "")])

    assert [e["stage"] for e in errors] == ["conversation-id-collision"]
    assert "codex" in errors[0]["error"] and "chatgpt" in errors[0]["error"]
    assert _row(idx, "shared")[0] == "codex", "the incumbent is untouched"
    assert files[0]["conversations"] == 0


# --------------------------------------------------------- resumable / idempotent

def test_re_ingesting_an_UNCHANGED_export_writes_nothing(tmp_path):
    """The checkpoint contract `build_index` offers, extended to a streamed export: an
    unchanged file is fully skipped, so a rebuild is cheap and adds no duplicate row."""
    src = _write(str(tmp_path / "conversations.json"),
                 [_chatgpt_conv("r-1", "alpha"), _chatgpt_conv("r-2", "bravo")])
    idx = str(tmp_path / "corpus.sqlite")

    loaders.ingest_exports(idx, [("chatgpt", src, "")])
    files, errors = loaders.ingest_exports(idx, [("chatgpt", src, "")])

    assert errors == []
    assert files[0]["conversations"] == 0, "an unchanged export re-writes nothing"
    assert _ids(idx) == ["r-1", "r-2"]


def test_a_CHANGED_export_is_re_read_and_the_grown_conversation_re_indexed(tmp_path):
    """The failure this repo has already been bitten by one layer up: a session that GREW
    after it was first indexed never became searchable."""
    src = str(tmp_path / "conversations.json")
    _write(src, [_chatgpt_conv("r-1", "alpha")])
    idx = str(tmp_path / "corpus.sqlite")
    loaders.ingest_exports(idx, [("chatgpt", src, "")])

    _write(src, [_chatgpt_conv("r-1", "alpha"), _chatgpt_conv("r-2", "bravonew")])
    files, errors = loaders.ingest_exports(idx, [("chatgpt", src, "")])

    assert errors == []
    assert files[0]["conversations"] == 2
    assert _search(idx, "bravonew") == ["r-2"]


def test_an_interrupted_ingest_resumes_at_the_last_committed_chunk(tmp_path):
    """A 728 MB export cannot afford to restart from record zero, so the checkpoint has to
    survive an abort mid-file."""
    src = _write(str(tmp_path / "conversations.json"),
                 [_chatgpt_conv("a-%d" % i, "alpha%d" % i) for i in range(6)])
    idx = str(tmp_path / "corpus.sqlite")

    class Stop(Exception):
        pass

    def die(path, offset):
        if offset >= 4:
            raise Stop()

    with pytest.raises(Stop):
        loaders.ingest_exports(idx, [("chatgpt", src, "")], progress=die, chunk_size=2)
    assert _ids(idx) == ["a-0", "a-1", "a-2", "a-3"]

    files, errors = loaders.ingest_exports(idx, [("chatgpt", src, "")], chunk_size=2)
    assert errors == []
    assert files[0]["conversations"] == 2, "only the un-committed tail is re-written"
    assert len(_ids(idx)) == 6


def test_a_file_that_produced_an_ERROR_is_re_read_on_the_next_run(tmp_path):
    """A checkpoint past a failure would make the SECOND run report zero errors and exit
    zero while the export is still broken — a silent success. So a file that errored is
    never checkpointed past the failure."""
    src = _write(str(tmp_path / "conversations.json"),
                 [_chatgpt_conv("", "alpha"), _chatgpt_conv("e-1", "bravo")])
    idx = str(tmp_path / "corpus.sqlite")

    first = loaders.ingest_exports(idx, [("chatgpt", src, "")])[1]
    second = loaders.ingest_exports(idx, [("chatgpt", src, "")])[1]

    assert [e["stage"] for e in first] == ["identity"]
    assert [e["stage"] for e in second] == ["identity"], (
        "the unfixed export must keep reporting, run after run")


# ---------------------------------------------------- the resolver may not drift

def test_the_export_file_resolver_reads_exactly_what_the_render_loaders_read(tmp_path):
    """A DRIFT GATE, not a restatement. `export_files` resolves one CLI argument to the
    files it stands for; `load_claude` and `load_codex` each carry their own copy of that
    logic inline, and the two must not diverge — a resolver that missed
    `design_chats/*.json` would silently drop real conversations from the corpus while the
    render path still showed them.

    Measured by recording which paths each render loader actually opens, rather than by
    re-deriving the glob here (which would only assert this file against itself).
    """
    d = str(tmp_path / "store")
    _write(os.path.join(d, "conversations.json"), [_claude_conv("cl-1", "alpha")])
    _write(os.path.join(d, "design_chats", "dc.json"),
           _claude_design_chat("dc-1", "bravo"))
    _write(os.path.join(d, "users.json"), {"uuid": "u"})
    _write(os.path.join(d, "nested", "codex.json"), [_codex_thread("t-1", "charlie")])

    opened = []
    real = loaders._load_json

    def record(path):
        opened.append(path)
        return real(path)

    for provider, loader in (("claude", loaders.load_claude),
                             ("codex", loaders.load_codex)):
        opened[:] = []
        orig, loaders._load_json = loaders._load_json, record
        try:
            loader(d)
        finally:
            loaders._load_json = orig
        assert sorted(loaders.export_files(provider, d)) == sorted(opened), provider


def test_the_resolver_honours_a_single_FILE_argument_for_every_provider(tmp_path):
    """A path to one file is taken as-is for every provider — a renamed export, one
    extracted shard, or a Takeout transcript."""
    one = _write(str(tmp_path / "whatever.json"), [])
    for provider in loaders.EXPORT_PROVIDERS:
        assert loaders.export_files(provider, one) == [one], provider
        assert loaders.export_files(provider, str(tmp_path / "nope.json")) == [], provider


# ------------------------------------------------------------------- the CLI

def test_the_index_subcommand_ingests_an_export_and_reports_it(tmp_path, capsys):
    src = _write(str(tmp_path / "conversations.json"),
                 [_chatgpt_conv("c-1", "alpha bravo")])
    idx = str(tmp_path / "corpus.sqlite")

    assert cli.main(["index", idx, "--chatgpt-export", src]) == 0

    out = capsys.readouterr().out
    assert "EXPORT_CONVERSATIONS 1" in out
    assert "conversations=1" in out
    assert _search(idx, "bravo") == ["c-1"]


def test_the_index_subcommand_takes_every_export_flag(tmp_path, monkeypatch):
    """Wiring only — `ingest_exports` owns what each provider does. Pins that all four
    providers plus the two second-path flags reach the ingest as specs, in the documented
    (provider, path, aux) shape."""
    seen = {}

    def fake(index_path, specs, progress=None, chunk_size=None):
        seen["specs"] = list(specs)
        return [], []

    monkeypatch.setattr(loaders, "ingest_exports", fake)
    src = _write(str(tmp_path / "any.json"), [])

    assert cli.main(["index", str(tmp_path / "i.sqlite"),
                     "--chatgpt-export", src, "--chatgpt-projects", src,
                     "--claude-export", src, "--codex-export", src,
                     "--gemini-export", src, "--gemini-harvest", src]) == 0
    assert seen["specs"] == [("chatgpt", src, src), ("claude", src, ""),
                             ("codex", src, ""), ("gemini", src, src)]


def test_the_index_subcommand_still_takes_a_sessions_root_and_both_at_once(
        tmp_path, capsys):
    """The positional sessions root is now OPTIONAL, and naming both sources builds one
    index from both."""
    day = str(tmp_path / "sessions" / "2026" / "07" / "24")
    os.makedirs(day)
    with open(os.path.join(day, "rollout-2026-07-24T10-00-00-0000aa.jsonl"), "w",
              encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "session_meta", "timestamp": "2026-07-24T10:00:00Z",
                             "payload": {"session_id": "S1", "id": "S1"}}) + "\n")
    src = _write(str(tmp_path / "conversations.json"), [_chatgpt_conv("c-1", "alpha")])
    idx = str(tmp_path / "corpus.sqlite")

    assert cli.main(["index", str(tmp_path / "sessions"), idx,
                     "--chatgpt-export", src]) == 0

    out = capsys.readouterr().out
    assert "INGESTED_CONVERSATIONS 1" in out and "EXPORT_CONVERSATIONS 1" in out
    assert "INDEX_ROWS 2" in out
    assert _ids(idx) == ["S1", "c-1"]


def test_the_session_conversation_count_says_which_half_it_counts(tmp_path, capsys):
    """INGESTED_CONVERSATIONS counts SESSION-store conversations only, so an export-only
    import legitimately prints 0 beside a non-zero INDEX_ROWS. Unlabelled, a successful
    import reads as a failed one."""
    src = _write(str(tmp_path / "conversations.json"), [_chatgpt_conv("c-1", "alpha")])

    assert cli.main(["index", str(tmp_path / "i.sqlite"), "--chatgpt-export", src]) == 0

    out = capsys.readouterr().out
    assert "INGESTED_CONVERSATIONS 0 (session stores; exports are counted above)" in out
    assert "EXPORT_CONVERSATIONS 1" in out and "INDEX_ROWS 1" in out


def test_the_index_subcommand_refuses_to_build_from_NO_source_at_all(tmp_path, capsys):
    """`index <one-path>` used to be an argparse error (out_index missing). With the
    sessions root optional that spelling now parses, so the guard has to be explicit —
    and it must refuse BEFORE creating a file, or a typo silently writes an sqlite index
    over the path the user meant as their sessions tree."""
    target = tmp_path / "sessions"
    assert cli.main(["index", str(target)]) == 2
    assert "name at least one source" in capsys.readouterr().err
    assert not target.exists(), "a refused build must create nothing"


def test_the_index_subcommand_reports_a_missing_export_path_as_a_clean_error(tmp_path):
    """Same rule the sessions root already had: a named source that does not exist is an
    error, not a successful ingest of nothing."""
    assert cli.main(["index", str(tmp_path / "i.sqlite"),
                     "--chatgpt-export", str(tmp_path / "nope.json")]) == 1
    assert not (tmp_path / "i.sqlite").exists()


def test_the_index_subcommand_exits_three_when_an_export_partially_failed(
        tmp_path, capsys):
    """The exit-3 rule already in force for session ingest errors: content went in and did
    not come out, so a caller scripting `index && open-the-cockpit` must not read it as
    success."""
    src = _write(str(tmp_path / "conversations.json"),
                 [_chatgpt_conv("", "alpha"), _chatgpt_conv("ok", "bravo")])
    idx = str(tmp_path / "corpus.sqlite")

    rc = cli.main(["index", idx, "--chatgpt-export", src])

    cap = capsys.readouterr()
    assert rc == 3
    assert "INGEST_ERRORS 1" in cap.out
    assert "stage=identity" in cap.err
    assert _ids(idx) == ["ok"], "the readable record still landed"


def test_the_index_subcommand_discloses_a_gemini_grouping_mode(tmp_path, capsys):
    src = _write(str(tmp_path / "transcript.json"), _gemini_records("alpha"))

    assert cli.main(["index", str(tmp_path / "i.sqlite"), "--gemini-export", src]) == 0
    assert "grouping_mode=gap-heuristic (PROVISIONAL)" in capsys.readouterr().out
