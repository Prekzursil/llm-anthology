"""Codex CLI session rollout logs (.codex/sessions/YYYY/MM/DD/rollout-*.jsonl) -> IR.

A FOURTH Codex-family shape, unrelated to codex.json (the task export the `codex`
adapter reads). A rollout is an APPEND-ONLY JSONL event log: one JSON object PER LINE,
each `{type, timestamp, payload}`. It is the on-disk transcript of a live Codex CLI
run, and the source of the cockpit's thread graph (corpus.ThreadMeta / SpawnEdge).

SYNTHETIC fixtures only, mirroring the schema PROBED from the real corpus
(1191 files / 2.2M records, 2026-07). No real conversation content appears here.

Schema under test (measured record + payload shapes):
  line record        {type, timestamp, payload}
  session_meta       {session_id, id, timestamp, cwd, model_provider, git{branch},
                      parent_thread_id, forked_from_id, agent_nickname, agent_path,
                      thread_source, ...}   (appears 1-4x/file; FIRST wins)
  response_item      {type: message|reasoning|function_call|function_call_output|
                      custom_tool_call|custom_tool_call_output|agent_message, ...}
    message.role       user | developer | assistant   (2401 developer = envelope)
    content part.type  input_text | output_text | encrypted_content
    reasoning          summary[{type:summary_text,text}] + opaque encrypted_content
  event_msg          {type: token_count|task_started|...}
    token_count        info.total_token_usage.total_tokens   (cumulative)

The traps each get a named test: the giant repeated `developer` envelope that must
NOT masquerade as user prose, the opaque `encrypted_content` reasoning that carries no
visible summary, the leaked-list `custom_tool_call_output.output`, the DATE-NESTED
directory layout a flat glob returns a false zero on, and the malformed line that must
be skipped-and-logged rather than abort the whole file.
"""
import json

import pytest

from llm_anthology import corpus, ir
from llm_anthology.adapters import codex_rollout as cr


_SID = "019f570f-3a16-7e43-a5b9-aed8e2477c5e"
_TS = "2026-07-12T19:00:48.019Z"
_TS2 = "2026-07-12T19:05:10.500Z"


# ------------------------------------------------------------------- fixtures

def _rec(rtype, payload, ts=_TS):
    return {"type": rtype, "timestamp": ts, "payload": payload}


def _session_meta(sid=_SID, ts=_TS, **kw):
    pl = {"session_id": sid, "id": sid, "timestamp": ts, "cwd": "/work/repo",
          "originator": "codex_cli", "cli_version": "0.9.0", "source": "cli",
          "model_provider": "openai", "history_mode": "full"}
    pl.update(kw)
    return _rec("session_meta", pl, ts=ts)


def _msg(role, texts, part_type=None, mid="", ts=_TS):
    pt = part_type or ("output_text" if role == "assistant" else "input_text")
    pl = {"type": "message", "role": role,
          "content": [{"type": pt, "text": t} for t in texts]}
    if mid:
        pl["id"] = mid
    return _rec("response_item", pl, ts=ts)


def _reasoning(summaries, encrypted="ENCRYPTED_OPAQUE", rid="rs_1", ts=_TS):
    pl = {"type": "reasoning", "id": rid, "encrypted_content": encrypted,
          "summary": [{"type": "summary_text", "text": s} for s in summaries]}
    return _rec("response_item", pl, ts=ts)


def _fcall(name="spawn_agent", arguments='{"x":1}', call_id="call_1",
           namespace="collaboration", ts=_TS):
    return _rec("response_item", {"type": "function_call", "id": "fc_1", "name": name,
                                  "arguments": arguments, "call_id": call_id,
                                  "namespace": namespace}, ts=ts)


def _fout(output="done", call_id="call_1", ts=_TS):
    return _rec("response_item", {"type": "function_call_output",
                                  "call_id": call_id, "output": output}, ts=ts)


def _ctool(name="exec", cinput="ls -la", call_id="call_2", status="completed", ts=_TS):
    return _rec("response_item", {"type": "custom_tool_call", "id": "ctc_1",
                                  "name": name, "input": cinput, "call_id": call_id,
                                  "status": status}, ts=ts)


def _ctout(output, call_id="call_2", ts=_TS):
    return _rec("response_item", {"type": "custom_tool_call_output",
                                  "call_id": call_id, "output": output}, ts=ts)


def _token(total=1234, ts=_TS):
    return _rec("event_msg", {"type": "token_count",
                              "info": {"total_token_usage": {"total_tokens": total}}}, ts=ts)


def _blocks(conv):
    return [b for t in conv.turns for b in t.blocks]


def _doc(records, path=""):
    return cr.build_document(records, rollout_path=path)


# ------------------------------------------------------- conversation-level shape

def test_full_rollout_becomes_one_codex_conversation():
    doc = _doc([_session_meta(),
                _msg("user", ["fix the bug"], mid="u1"),
                _reasoning(["I will inspect the file"]),
                _ctool(),
                _ctout([{"type": "input_text", "text": "output line"}]),
                _msg("assistant", ["done, it is fixed"], mid="a1"),
                _token(4096)])
    c = doc.conversation
    assert isinstance(c, ir.Conversation)
    assert c.provider == "codex" and c.id == _SID
    assert [t.role for t in c.turns] == ["human", "assistant"]
    # the assistant turn coalesces reasoning + tool + tool-output + message, in order
    assert [b.type for t in c.turns for b in t.blocks] == [
        "text", "thinking", "tool_use", "tool_result", "text"]


def test_thread_id_prefers_session_id_then_id_then_filename_then_empty():
    assert _doc([_session_meta(sid="", id="019f0000-0000-7000-8000-000000000001")]
                ).thread.id == "019f0000-0000-7000-8000-000000000001"
    assert _doc([_session_meta()]).thread.id == _SID
    # no session_meta: fall back to the UUID in the filename
    name = "rollout-2026-07-12T19-00-48-%s.jsonl" % _SID
    assert _doc([], path="/s/2026/07/12/" + name).thread.id == _SID
    # nothing to key on at all
    assert _doc([]).thread.id == ""


def test_thread_id_and_conversation_id_agree():
    doc = _doc([_session_meta()])
    assert doc.thread_id == doc.thread.id == doc.conversation.id == _SID


def test_thread_metadata_is_lifted_from_session_meta():
    doc = _doc([_session_meta(cwd="/srv/app", model_provider="openai",
                              git={"branch": "feature/x"}, agent_path="/root/worker",
                              agent_nickname="worker-3"),
                _token(9000)], path="/logs/r.jsonl")
    t = doc.thread
    assert isinstance(t, corpus.ThreadMeta)
    assert t.cwd == "/srv/app" and t.model_provider == "openai"
    assert t.git_branch == "feature/x" and t.tokens_used == 9000
    assert t.agent_role == "/root/worker" and t.agent_nickname == "worker-3"
    assert t.rollout_path == "/logs/r.jsonl" and t.created_at_ms == 1783882848019


def test_git_branch_defaults_empty_when_absent_or_not_a_dict():
    assert _doc([_session_meta()]).thread.git_branch == ""
    assert _doc([_session_meta(git="not-a-dict")]).thread.git_branch == ""
    assert _doc([_session_meta(git={"other": 1})]).thread.git_branch == ""


def test_created_from_meta_updated_from_last_record_timestamp():
    doc = _doc([_session_meta(ts=_TS),
                _msg("user", ["hi"], ts=_TS),
                _msg("assistant", ["yo"], ts=_TS2)])
    assert doc.thread.created_at_ms == 1783882848019
    assert doc.thread.updated_at_ms == 1783883110500      # from _TS2, the LAST record
    assert doc.conversation.created_at == _TS and doc.conversation.updated_at == _TS2


def test_unparseable_timestamps_leave_the_ms_fields_none():
    doc = _doc([_session_meta(ts="not-a-date"),
                _msg("user", ["hi"], ts="also-bad")])
    assert doc.thread.created_at_ms is None and doc.thread.updated_at_ms is None


def test_no_records_yields_an_empty_but_valid_conversation():
    doc = _doc([])
    assert doc.conversation.turns == [] and doc.conversation.title == "(untitled)"
    assert doc.thread.tokens_used == 0 and doc.thread.created_at_ms is None
    assert doc.edges == []


# --------------------------------------------------------- title / preview / meta

def test_title_is_the_first_line_of_the_first_user_message():
    doc = _doc([_session_meta(),
                _msg("user", ["Refactor the parser\nand add tests"])])
    assert doc.thread.title == "Refactor the parser"
    assert doc.conversation.title == "Refactor the parser"


def test_title_falls_back_to_agent_nickname_then_untitled():
    # no user message, but a sub-agent nickname is present
    assert _doc([_session_meta(agent_nickname="auditor")]).thread.title == "auditor"
    # neither a user message nor a nickname
    assert _doc([_session_meta()]).thread.title == "(untitled)"


def test_long_title_is_truncated_but_preview_keeps_more():
    long = "x" * 300
    doc = _doc([_session_meta(), _msg("user", [long])])
    assert len(doc.thread.title) == 80
    assert len(doc.thread.preview) == 200


def test_conversation_meta_carries_the_thread_provenance():
    doc = _doc([_session_meta(parent_thread_id="P", forked_from_id="F",
                              git={"branch": "b"}, agent_path="/root/w",
                              agent_nickname="nick"),
                _msg("developer", ["envelope"]),
                _token(5)], path="/logs/r.jsonl")
    m = doc.conversation.meta
    assert m["rollout_path"] == "/logs/r.jsonl" and m["cwd"] == "/work/repo"
    assert m["parent_thread_id"] == "P" and m["forked_from_id"] == "F"
    assert m["git_branch"] == "b" and m["agent_role"] == "/root/w"
    assert m["agent_nickname"] == "nick" and m["tokens_used"] == 5
    assert m["developer_message_count"] == 1


# ------------------------------------------------------------------- turn model

def test_developer_envelope_is_counted_not_rendered_as_a_turn():
    """2401 developer response_items across 40 files = the repeated base-instructions /
    AGENTS.md envelope injected each turn. Rendering it as a chat bubble would bury the
    real conversation and misattribute machine context to a participant."""
    doc = _doc([_session_meta(),
                _msg("developer", ["BASE INSTRUCTIONS ..."]),
                _msg("developer", ["AGENTS.md ..."]),
                _msg("user", ["real prompt"]),
                _msg("assistant", ["real reply"])])
    assert [t.role for t in doc.conversation.turns] == ["human", "assistant"]
    assert doc.conversation.meta["developer_message_count"] == 2


def test_turns_alternate_across_multiple_exchanges():
    doc = _doc([_session_meta(),
                _msg("user", ["q1"]), _msg("assistant", ["a1"]),
                _msg("user", ["q2"]), _msg("assistant", ["a2"])])
    assert [t.role for t in doc.conversation.turns] == [
        "human", "assistant", "human", "assistant"]
    assert [b.text for b in _blocks(doc.conversation)] == ["q1", "a1", "q2", "a2"]


def test_assistant_work_without_a_preceding_user_still_forms_a_turn():
    doc = _doc([_session_meta(), _reasoning(["thinking first"]),
                _msg("assistant", ["reply"])])
    assert [t.role for t in doc.conversation.turns] == ["assistant"]
    assert [b.type for b in doc.conversation.turns[0].blocks] == ["thinking", "text"]


def test_a_user_message_flushes_the_open_assistant_turn():
    doc = _doc([_session_meta(),
                _msg("assistant", ["a"]),   # opens an assistant turn
                _msg("user", ["b"]),        # must flush it, then a human turn
                _msg("assistant", ["c"])])
    assert [t.role for t in doc.conversation.turns] == [
        "assistant", "human", "assistant"]


def test_an_unusual_role_is_treated_as_assistant_side():
    doc = _doc([_session_meta(), _msg("tool", ["tool text"], part_type="input_text")])
    assert [t.role for t in doc.conversation.turns] == ["assistant"]
    assert doc.conversation.turns[0].blocks[0].text == "tool text"


def test_assistant_turn_uuid_and_timestamp_come_from_its_first_item():
    doc = _doc([_session_meta(), _reasoning(["r"], rid="rs_first", ts=_TS),
                _msg("assistant", ["a"], mid="a_second", ts=_TS2)])
    turn = doc.conversation.turns[0]
    assert turn.uuid == "rs_first" and turn.timestamp == _TS


def test_human_turn_uuid_comes_from_the_message_id():
    doc = _doc([_session_meta(), _msg("user", ["q"], mid="u_42")])
    assert doc.conversation.turns[0].uuid == "u_42"


# --------------------------------------------------------------- message content

def test_input_and_output_text_parts_both_become_text_blocks():
    doc = _doc([_msg("user", ["a"], part_type="input_text"),
                _msg("assistant", ["b"], part_type="output_text")])
    assert [b.text for b in _blocks(doc.conversation)] == ["a", "b"]


def test_whitespace_only_text_part_is_skipped():
    doc = _doc([_msg("user", ["   ", "real"])])
    assert [b.text for b in _blocks(doc.conversation)] == ["real"]


def test_non_string_text_and_non_dict_part_and_wrong_part_type_are_skipped():
    rec = _rec("response_item", {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": None},          # non-string text
        "a bare string",                                # non-dict part
        {"type": "encrypted_content", "text": "x"},     # not a text part type
        {"type": "input_text", "text": "kept"}]})
    doc = _doc([rec])
    assert [b.text for b in _blocks(doc.conversation)] == ["kept"]


def test_non_list_message_content_yields_no_blocks():
    rec = _rec("response_item", {"type": "message", "role": "user",
                                 "content": "not a list"})
    assert _blocks(_doc([rec]).conversation) == []


# ------------------------------------------------------------------- reasoning

def test_reasoning_summary_becomes_one_joined_thinking_block():
    doc = _doc([_reasoning(["step one", "step two"])])
    b = _blocks(doc.conversation)[0]
    assert b.type == "thinking" and b.text == "step one\nstep two"


def test_reasoning_with_only_opaque_encrypted_content_emits_nothing():
    """encrypted_content is not human-readable; a reasoning item that carries no visible
    summary must not become an empty thinking bubble."""
    doc = _doc([_reasoning([], encrypted="LONGOPAQUEBLOB")])
    assert _blocks(doc.conversation) == []


def test_reasoning_skips_malformed_summary_parts_and_non_list_summary():
    rec = _rec("response_item", {"type": "reasoning", "summary": [
        {"type": "summary_text", "text": "kept"},
        {"type": "other", "text": "wrong type"},
        {"type": "summary_text", "text": None},
        "bare"]})
    assert _blocks(_doc([rec]).conversation)[0].text == "kept"
    rec2 = _rec("response_item", {"type": "reasoning", "summary": "not-a-list"})
    assert _blocks(_doc([rec2]).conversation) == []


# ------------------------------------------------------------------- tool calls

def test_function_call_becomes_a_tool_use_with_its_arguments():
    doc = _doc([_fcall(name="spawn_agent", arguments='{"role":"x"}',
                       call_id="c9", namespace="collaboration")])
    b = _blocks(doc.conversation)[0]
    assert b.type == "tool_use" and b.text == "spawn_agent"
    assert b.data == {"name": "spawn_agent", "input": '{"role":"x"}',
                      "call_id": "c9", "namespace": "collaboration"}


def test_function_call_output_becomes_a_tool_result():
    b = _blocks(_doc([_fout(output="exit 0", call_id="c9")]).conversation)[0]
    assert b.type == "tool_result" and b.data["content"] == "exit 0"
    assert b.data["call_id"] == "c9" and b.data["is_error"] is False


def test_custom_tool_call_becomes_a_tool_use_with_its_status():
    b = _blocks(_doc([_ctool(name="exec", cinput="ls", status="completed")]).conversation)[0]
    assert b.type == "tool_use" and b.text == "exec"
    assert b.data["input"] == "ls" and b.data["status"] == "completed"


def test_custom_tool_call_output_flattens_a_list_of_text_parts():
    b = _blocks(_doc([_ctout([{"type": "input_text", "text": "line 1"},
                              {"type": "input_text", "text": "line 2"}])]).conversation)[0]
    assert b.type == "tool_result" and b.data["content"] == "line 1\nline 2"


def test_tool_output_flattening_covers_str_mixed_list_and_other():
    # plain string output
    assert _blocks(_doc([_ctout("plain string")]).conversation)[0].data["content"] == "plain string"
    # mixed list: dict-with-text, dict-without-text, bare string, and a non-str/dict item
    mixed = [{"type": "input_text", "text": "a"}, {"type": "input_text"},
             "b", 12345]
    assert _blocks(_doc([_ctout(mixed)]).conversation)[0].data["content"] == "a\nb"
    # neither str nor list -> empty
    assert _blocks(_doc([_ctout(999)]).conversation)[0].data["content"] == ""


def test_unknown_response_item_type_is_preserved_as_unknown():
    """agent_message and any future response_item type must survive verbatim rather than
    be dropped or misrendered."""
    rec = _rec("response_item", {"type": "agent_message", "author": "/root/w",
                                 "recipient": "/root", "content": []})
    b = _blocks(_doc([rec]).conversation)[0]
    assert b.type == "unknown" and b.data["orig_type"] == "agent_message"
    assert b.data["x_raw"]["author"] == "/root/w"


# ------------------------------------------------------------------- token count

def test_token_count_uses_the_last_cumulative_total():
    doc = _doc([_token(100), _token(250), _token(900)])
    assert doc.thread.tokens_used == 900


def test_token_count_tolerates_missing_or_non_numeric_levels():
    # info not a dict / total_token_usage not a dict / total_tokens not an int
    for info in (None, "x", {"total_token_usage": None},
                 {"total_token_usage": {"total_tokens": "many"}},
                 {"total_token_usage": {}}):
        doc = _doc([_rec("event_msg", {"type": "token_count", "info": info})])
        assert doc.thread.tokens_used == 0
    # a later valid count still wins over an earlier broken one
    doc = _doc([_rec("event_msg", {"type": "token_count", "info": None}), _token(42)])
    assert doc.thread.tokens_used == 42


def test_non_token_event_and_non_dict_event_payload_are_ignored():
    doc = _doc([_rec("event_msg", {"type": "task_started", "turn_id": "t"}),
                _rec("event_msg", "not-a-dict"),
                _msg("user", ["q"])])
    assert doc.thread.tokens_used == 0
    assert [t.role for t in doc.conversation.turns] == ["human"]


# ------------------------------------------------------------- spawn edges

def test_a_child_thread_yields_a_spawn_edge_to_its_parent():
    doc = _doc([_session_meta(parent_thread_id="PARENT", thread_source="spawn")])
    assert doc.edges == [corpus.SpawnEdge("PARENT", _SID, "spawn")]


def test_a_root_thread_yields_no_edge():
    assert _doc([_session_meta()]).edges == []


def test_no_edge_when_the_child_id_cannot_be_resolved():
    # parent is present but there is no session id and no filename UUID -> no child id
    doc = _doc([_session_meta(sid="", id="", parent_thread_id="PARENT")], path="/x/y.log")
    assert doc.thread.id == "" and doc.edges == []


# ------------------------------------------------- record-level robustness

def test_only_the_first_session_meta_wins():
    doc = _doc([_session_meta(sid="first", cwd="/one"),
                _session_meta(sid="second", cwd="/two")])
    assert doc.thread.id == "first" and doc.thread.cwd == "/one"


def test_session_meta_and_response_item_with_non_dict_payload_are_ignored():
    doc = _doc([_rec("session_meta", "not-a-dict"),
                _rec("response_item", 42),
                _msg("user", ["q"])])
    assert doc.thread.id == ""              # the bad session_meta contributed nothing
    assert [t.role for t in doc.conversation.turns] == ["human"]


def test_unknown_record_types_and_missing_type_are_ignored():
    doc = _doc([_rec("turn_context", {"cwd": "/x"}),
                _rec("world_state", {"full": True}),
                _rec("compacted", {"message": "..."}),
                {"payload": {"no": "type"}},        # missing top-level type
                _msg("user", ["q"])])
    assert [t.role for t in doc.conversation.turns] == ["human"]


def test_a_record_with_a_non_string_timestamp_does_not_break_time_tracking():
    doc = _doc([_session_meta(),
                _rec("response_item", {"type": "message", "role": "user",
                                       "content": [{"type": "input_text", "text": "q"}]},
                     ts=None)])
    assert doc.thread.created_at_ms == 1783882848019   # still from the session_meta


# --------------------------------------------------- iso_to_ms / id_from_path units

def test_iso_to_ms_handles_z_offset_and_rejects_junk():
    assert cr._iso_to_ms("2026-07-12T19:00:48.019Z") == 1783882848019
    assert cr._iso_to_ms("2026-07-12T19:00:48.019+00:00") == 1783882848019
    assert cr._iso_to_ms("nonsense") is None
    assert cr._iso_to_ms("") is None
    assert cr._iso_to_ms(None) is None


def test_id_from_path_extracts_the_uuid_or_returns_empty():
    name = "rollout-2026-07-12T19-00-48-%s.jsonl" % _SID
    assert cr._id_from_path("/s/2026/07/12/" + name) == _SID
    assert cr._id_from_path("/s/no-uuid-here.jsonl") == ""
    assert cr._id_from_path("") == ""


# ----------------------------------------------------------- parse_rollout_lines

def test_parse_rollout_lines_skips_blank_and_malformed_lines():
    lines = [
        json.dumps(_session_meta()),
        "",                                  # blank -> skipped silently
        "   ",                               # whitespace -> skipped silently
        "{not valid json",                   # malformed -> logged
        "42",                                # valid JSON but not an object -> logged
        json.dumps(_msg("user", ["hello"])),
    ]
    doc, errors = cr.parse_rollout_lines(lines, rollout_path="/logs/r.jsonl")
    assert [t.role for t in doc.conversation.turns] == ["human"]
    assert doc.rollout_path == "/logs/r.jsonl"
    stages = [e["stage"] for e in errors]
    assert stages == ["parse", "parse"]
    assert all(e["file"] == "/logs/r.jsonl" for e in errors)
    # 1-based line numbers: line 4 is malformed JSON, line 5 is a non-object literal
    assert errors[0]["line"] == 4 and errors[1]["line"] == 5


# ------------------------------------------------------------ parse_rollout_file

def _write_rollout(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def test_parse_rollout_file_reads_a_real_file(tmp_path):
    p = tmp_path / "2026" / "07" / "12" / ("rollout-x-%s.jsonl" % _SID)
    _write_rollout(p, [_session_meta(), _msg("user", ["hi"]), _msg("assistant", ["yo"])])
    doc, errors = cr.parse_rollout_file(str(p))
    assert errors == [] and doc.rollout_path == str(p)
    assert [t.role for t in doc.conversation.turns] == ["human", "assistant"]
    assert doc.thread.rollout_path == str(p)


# --------------------------------------------------------------- ingest_sessions

def test_ingest_sessions_recurses_the_date_tree_and_filters_non_rollouts(tmp_path):
    """codex sessions are DATE-NESTED (YYYY/MM/DD/rollout-*.jsonl). A flat, non-recursive
    glob returns a FALSE ZERO — the whole point of this test."""
    _write_rollout(tmp_path / "2026" / "07" / "12" / ("rollout-a-%s.jsonl" % _SID),
                   [_session_meta(sid="child-a", parent_thread_id="root"),
                    _msg("user", ["deep in the date tree"])])
    _write_rollout(tmp_path / "2026" / "06" / "01" / "rollout-b-000.jsonl",
                   [_session_meta(sid="child-b"), _msg("user", ["another day"])])
    # a rollout at the FLAT root must also be found (** matches zero dirs)
    _write_rollout(tmp_path / "rollout-c-000.jsonl", [_session_meta(sid="flat")])
    # non-rollout files that must be ignored
    (tmp_path / "notes.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "2026" / "rollout-not-jsonl.txt").write_text("x", encoding="utf-8")

    docs, errors = cr.ingest_sessions(str(tmp_path))
    assert errors == []
    ids = sorted(d.thread.id for d in docs)
    assert ids == ["child-a", "child-b", "flat"]
    # rollout_path is carried through for every collected conversation
    assert all(d.rollout_path and d.rollout_path.endswith(".jsonl") for d in docs)
    edges = [e for d in docs for e in d.edges]
    assert corpus.SpawnEdge("root", "child-a", "") in edges


def _write_rollout_zst(path, records):
    """Write a rollout as a single-frame `rollout-*.jsonl.zst`, the shape Codex stores."""
    import zstandard
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(r) + "\n" for r in records).encode("utf-8")
    path.write_bytes(zstandard.ZstdCompressor().compress(body))


def test_ingest_sessions_reads_ZSTD_COMPRESSED_rollouts(tmp_path):
    """A live Codex store compresses its rollouts, and reading only `.jsonl` sees nothing.

    MEASURED on a real store: 2043 `rollout-*.jsonl.zst` and ZERO plain `.jsonl`. Against
    it, ingest_sessions returned `docs=0, errors=0` — a silent no-op reporting a perfectly
    successful ingest of an entire history it could not read. Zero errors is what made it
    invisible: a failure would have been noticed.
    """
    _write_rollout_zst(tmp_path / "2026" / "07" / "12" / ("rollout-z-%s.jsonl.zst" % _SID),
                       [_session_meta(sid="compressed-a", parent_thread_id="root"),
                        _msg("user", ["stored compressed"]),
                        _msg("assistant", ["read back fine"])])

    docs, errors = cr.ingest_sessions(str(tmp_path))

    assert errors == [], f"a compressed rollout must parse cleanly: {errors}"
    assert [d.thread.id for d in docs] == ["compressed-a"]
    # The content must survive the decode, not merely the file be counted.
    assert [t.role for t in docs[0].conversation.turns] == ["human", "assistant"]
    assert docs[0].rollout_path.endswith(".jsonl.zst")
    assert corpus.SpawnEdge("root", "compressed-a", "") in docs[0].edges


def test_ingest_sessions_reads_plain_and_compressed_together_without_double_counting(tmp_path):
    """Mixed stores are the normal case: Codex compresses older rollouts and leaves recent
    ones plain. A conversation present in BOTH forms must be ingested once, not twice —
    double-counting would inflate every stat and duplicate nodes in the spawn graph."""
    day = tmp_path / "2026" / "07" / "12"
    _write_rollout(day / ("rollout-plain-%s.jsonl" % _SID), [_session_meta(sid="plain-one")])
    _write_rollout_zst(day / "rollout-old-000.jsonl.zst", [_session_meta(sid="compressed-one")])
    # The SAME rollout in both forms — the de-duplication case.
    _write_rollout(day / "rollout-both-111.jsonl", [_session_meta(sid="both")])
    _write_rollout_zst(day / "rollout-both-111.jsonl.zst", [_session_meta(sid="both")])

    docs, errors = cr.ingest_sessions(str(tmp_path))

    assert errors == []
    assert sorted(d.thread.id for d in docs) == ["both", "compressed-one", "plain-one"]
    both = [d for d in docs if d.thread.id == "both"]
    assert len(both) == 1, "a rollout present as .jsonl AND .jsonl.zst must ingest once"
    assert both[0].rollout_path.endswith(".jsonl"), \
        "the uncompressed copy should win — no decode, and it cannot be a truncated frame"


def test_read_rollout_lines_refuses_a_zst_that_inflates_past_the_cap(tmp_path, monkeypatch):
    """The decompression cap must actually FIRE, not just exist.

    A compressed file's inflated size is not knowable from its on-disk size, so ingest
    walking a whole tree could otherwise be made to exhaust memory by one pathological
    archive. The real ceiling is 512 MiB, which no honest fixture can reach, so the cap is
    lowered for this test — the assertion is about the BEHAVIOUR at the boundary, not the
    constant's value. Reported as OSError so `ingest_sessions` charges it to that one file.
    """
    p = tmp_path / ("rollout-big-%s.jsonl.zst" % _SID)
    _write_rollout_zst(p, [_session_meta(), _msg("user", ["x" * 200])])
    monkeypatch.setattr(cr, "_MAX_ROLLOUT_BYTES", 8)

    with pytest.raises(OSError) as excinfo:
        cr._read_rollout_lines(str(p))
    assert "decompression cap" in str(excinfo.value)

    # And the sweep must survive it: the oversized file is logged, not fatal.
    docs, errors = cr.ingest_sessions(str(tmp_path))
    assert docs == []
    assert len(errors) == 1 and errors[0]["stage"] == "read"


def test_ingest_sessions_reports_a_corrupt_zst_without_aborting_the_sweep(tmp_path):
    """One unreadable archive must cost that FILE, not the whole history."""
    day = tmp_path / "2026" / "07" / "12"
    (day).mkdir(parents=True, exist_ok=True)
    (day / "rollout-bad-000.jsonl.zst").write_bytes(b"this is not a zstd frame at all")
    _write_rollout_zst(day / "rollout-good-111.jsonl.zst", [_session_meta(sid="survivor")])

    docs, errors = cr.ingest_sessions(str(tmp_path))

    assert [d.thread.id for d in docs] == ["survivor"], "the good rollout must still land"
    assert len(errors) == 1 and errors[0]["stage"] == "read"
    assert errors[0]["file"].endswith("rollout-bad-000.jsonl.zst")


def test_ingest_sessions_reports_a_malformed_line_without_aborting(tmp_path):
    good = tmp_path / "2026" / "07" / "12" / "rollout-good.jsonl"
    _write_rollout(good, [_session_meta(sid="ok"), _msg("user", ["fine"])])
    bad = tmp_path / "2026" / "07" / "12" / "rollout-bad.jsonl"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(json.dumps(_session_meta(sid="partial")) + "\n{truncated",
                   encoding="utf-8")
    docs, errors = cr.ingest_sessions(str(tmp_path))
    assert sorted(d.thread.id for d in docs) == ["ok", "partial"]
    assert len(errors) == 1 and errors[0]["stage"] == "parse"


def test_ingest_sessions_reports_an_unreadable_entry_and_continues(tmp_path):
    _write_rollout(tmp_path / "2026" / "rollout-ok.jsonl", [_session_meta(sid="ok")])
    # a DIRECTORY whose name matches the glob: opening it as a file raises -> logged
    (tmp_path / "2026" / "rollout-trap.jsonl").mkdir()
    docs, errors = cr.ingest_sessions(str(tmp_path))
    assert [d.thread.id for d in docs] == ["ok"]
    assert len(errors) == 1 and errors[0]["stage"] == "read"


def test_ingest_sessions_on_an_empty_tree_returns_nothing(tmp_path):
    docs, errors = cr.ingest_sessions(str(tmp_path))
    assert docs == [] and errors == []


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
