"""Grok Build session store (~/.grok/sessions/<enc-cwd>/<id>/) -> IR + spawn graph.

SYNTHETIC fixtures only. The real store on this machine holds PRIVATE MEDICAL and
PHARMACEUTICAL conversations, so not one byte of it is read here or by the adapter's
tests; every fixture below is invented text. The schema under test comes from
`.scratch/GROK-SCHEMA.md`, which was extracted by a structure-only probe (key names,
protocol discriminants, value types, string LENGTHS — never values).

Schema under test (spec-measured):
  updates.jsonl line   {method, params{_meta, sessionId, update}, timestamp}
                       — a JSON-RPC envelope; the ACP discriminant is NESTED at
                         params.update.sessionUpdate, NOT at the top level.
  summary.json         info{cwd,id} + generated_title/created_at/last_active_at/...
                       — the session id is info.id, not the directory name.
  subagents/<id>/meta.json  parent_session_id + child_session_id + status
                       — a directed spawn edge with its state. OPTIONAL (1 of 6
                         sampled sessions had it).

Each trap the spec names gets a named test: the JSON-RPC nesting a top-level reader
sees nothing through, the 2795:35 tool-to-message ratio that must not become turns,
the `_chunk` streaming that must coalesce, the `*.lock` files sitting beside the real
ones, the optional subagents directory, the ISO->epoch-ms conversion, and the
malformed line / missing summary / unreadable session that must cost one session and
be logged rather than abort the sweep.
"""
import builtins
import json
import os

import pytest

from llm_anthology import corpus, discover, ir
from llm_anthology.adapters import grok


_SID = "0198c4d1-2f3a-7b41-9e02-5d6c7a8b9c0d"
_CHILD = "0198c4d1-2f3a-7b41-9e02-000000000002"
_TS = "2026-08-06T12:00:00.123456789Z"
_TS2 = "2026-08-06T12:05:30.000000000Z"
_ENC_CWD = "C%3A%5Cwork%5Crepo"


# ------------------------------------------------------------------- fixtures

def _upd(update, ts=_TS, method="session/update", sid=_SID):
    """One updates.jsonl line: the measured JSON-RPC envelope around an ACP update."""
    return {"method": method, "params": {"_meta": {}, "sessionId": sid,
                                         "update": update},
            "timestamp": ts}


def _xai(update, ts=_TS):
    """An xAI extension event — same envelope, `_x.ai/session/update` method."""
    return _upd(update, ts=ts, method="_x.ai/session/update")


def _content(text):
    """An ACP ContentBlock. The spec records `content` as a key but NOT its inner
    shape, so the adapter accepts several; this is the assumed one."""
    return {"type": "text", "text": text}


def _user(text, ts=_TS):
    return _upd({"sessionUpdate": "user_message_chunk", "_meta": {},
                 "content": _content(text)}, ts=ts)


def _agent(text, ts=_TS):
    return _upd({"sessionUpdate": "agent_message_chunk",
                 "content": _content(text)}, ts=ts)


def _thought(text, ts=_TS):
    return _upd({"sessionUpdate": "agent_thought_chunk",
                 "content": _content(text)}, ts=ts)


def _tool_call(cid="tc_1", title="read_file", kind="read", status="pending",
               raw_input=None, ts=_TS):
    return _upd({"sessionUpdate": "tool_call", "_meta": {}, "toolCallId": cid,
                 "title": title, "kind": kind, "status": status,
                 "rawInput": raw_input if raw_input is not None else {"path": "a.py"}},
                ts=ts)


def _tool_update(cid="tc_1", status="completed", raw_output=None, content=None,
                 title=None, ts=_TS):
    upd = {"sessionUpdate": "tool_call_update", "toolCallId": cid, "status": status,
           "locations": []}
    if raw_output is not None:
        upd["rawOutput"] = raw_output
    if content is not None:
        upd["content"] = content
    if title is not None:
        upd["title"] = title
    return _upd(upd, ts=ts)


def _turn_done(ts=_TS):
    return _xai({"sessionUpdate": "turn_completed", "prompt_id": "p1",
                 "stop_reason": "end_turn"}, ts=ts)


def _hook(ts=_TS):
    return _xai({"sessionUpdate": "hook_execution", "event_name": "PreToolUse",
                 "runs": [], "tool_name": "shell"}, ts=ts)


def _summary(**kw):
    pl = {"agent_name": "grok-build", "chat_format_version": 3,
          "created_at": _TS, "current_model_id": "grok-code-fast-1",
          "generated_title": "Synthetic fixture session",
          "grok_home": "/home/u/.grok", "info": {"cwd": "/work/repo", "id": _SID},
          "last_active_at": _TS2, "next_trace_turn": 2, "num_chat_messages": 2,
          "num_messages": 9, "reasoning_effort": "high", "request_id": "req-1",
          "sandbox_profile": "workspace-write", "session_kind": "primary",
          "session_summary": "a synthetic summary", "updated_at": _TS2}
    pl.update(kw)
    return pl


def _subagent(parent=_SID, child=_CHILD, status="completed", **kw):
    pl = {"child_cwd": "/work/repo", "child_session_id": child,
          "completed_at": _TS2, "description": "run the linter", "duration_ms": 4200,
          "effective_context_source": "parent", "effective_model_id": "grok-4",
          "parent_session_id": parent, "prompt": "lint it", "started_at": _TS,
          "status": status, "subagent_id": "sa-1", "subagent_type": "general",
          "tool_calls": 7, "turns": 3}
    pl.update(kw)
    return pl


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _session_on_disk(root, records=None, summary=_summary, subagents=None,
                     sid=_SID, enc_cwd=_ENC_CWD, locks=True, extra_lines=()):
    """A synthetic <root>/<enc-cwd>/<session-id>/ directory.

    `locks=True` plants the `*.lock` siblings the real store carries, filled with
    GARBAGE — so a glob that picks a lock up as data fails loudly instead of quietly.
    """
    sdir = os.path.join(root, enc_cwd, sid)
    lines = [json.dumps(r) for r in (records if records is not None else [])]
    lines.extend(extra_lines)
    _write(os.path.join(sdir, "updates.jsonl"), "\n".join(lines) + ("\n" if lines else ""))
    if summary is not None:
        _write(os.path.join(sdir, "summary.json"),
               json.dumps(summary() if callable(summary) else summary))
    for i, meta in enumerate(subagents or ()):
        _write(os.path.join(sdir, "subagents", "sa-%d" % i, "meta.json"),
               json.dumps(meta))
        if locks:
            _write(os.path.join(sdir, "subagents", "sa-%d" % i, "meta.json.lock"),
                   "NOT JSON {{{")
    if locks:
        _write(os.path.join(sdir, "summary.json.lock"), "NOT JSON {{{")
        _write(os.path.join(sdir, "updates.jsonl.lock"), "NOT JSON {{{")
    return sdir


def _doc(records, summary=None, subagents=(), session_dir=""):
    return grok.build_document(summary, records, subagents=subagents,
                               session_dir=session_dir)


def _types(conv):
    return [[b.type for b in t.blocks] for t in conv.turns]


# --------------------------------------------------------- conversation shape

def test_a_full_session_becomes_one_grok_conversation():
    doc = _doc([_user("fix the bug"), _thought("let me look at it"),
                _tool_call(), _tool_update(raw_output={"stdout": "ok"}),
                _agent("done, it is fixed"), _turn_done()], summary=_summary())
    c = doc.conversation
    assert isinstance(c, ir.Conversation)
    assert c.provider == "grok" and c.id == _SID
    assert [t.role for t in c.turns] == ["human", "assistant"]
    # the assistant turn coalesces reasoning + the tool call + its output + the reply
    assert _types(c) == [["text"], ["thinking", "tool_use", "tool_result", "text"]]
    assert c.turns[0].blocks[0].text == "fix the bug"
    assert c.turns[1].blocks[-1].text == "done, it is fixed"


def test_the_jsonrpc_envelope_is_unwrapped_and_a_top_level_discriminant_is_not():
    """Spec correction 2: a reader that looks for `sessionUpdate` at the TOP level of
    an updates.jsonl line finds nothing — measured, all 4000 sampled lines. This is
    the both-states check: the correctly nested record MUST yield a turn and the
    doc-shaped flat record MUST yield none, or the test proves nothing."""
    nested = _doc([_user("hello")]).conversation
    flat = _doc([{"sessionUpdate": "user_message_chunk", "content": _content("hello"),
                  "timestamp": _TS}]).conversation
    assert [t.role for t in nested.turns] == ["human"]
    assert flat.turns == []


def test_thread_and_conversation_ids_agree():
    doc = _doc([], summary=_summary())
    assert doc.thread_id == doc.thread.id == doc.conversation.id == _SID


# -------------------------------------------------------------- session identity

def test_the_session_id_is_summary_info_id_not_the_directory_name():
    """Spec 5: the id lives at summary.json -> info.id. The directory name is the
    session id in practice, but a top-level `id` key does not exist and trusting the
    directory would silently diverge the moment the store is copied or renamed."""
    doc = _doc([], summary=_summary(info={"cwd": "/w", "id": "the-real-id"}),
               session_dir="/store/enc/the-directory-name")
    assert doc.thread.id == "the-real-id"


def test_the_session_id_falls_back_to_the_envelope_then_the_directory_then_empty():
    """Without a summary.json there is still an id on the wire (params.sessionId) and
    then the directory name. An id of "" would collide with every other id-less
    session in the index, whose conversation_id column is UNIQUE."""
    assert _doc([_user("hi")]).thread.id == _SID
    assert _doc([], session_dir="/store/enc/dir-id").thread.id == "dir-id"
    assert _doc([]).thread.id == ""


def test_a_session_id_from_a_directory_with_a_trailing_separator():
    assert _doc([], session_dir="/store/enc/dir-id/").thread.id == "dir-id"


# --------------------------------------------------------------- chunk coalescing

def test_consecutive_chunks_of_one_message_coalesce_into_one_block():
    """`_chunk` means STREAMING. Without coalescing every message shatters into
    fragments — the single most visible way this adapter could be wrong."""
    doc = _doc([_agent("the "), _agent("answer "), _agent("is 42")])
    assert _types(doc.conversation) == [["text"]]
    assert doc.conversation.turns[0].blocks[0].text == "the answer is 42"


def test_turn_completed_flushes_so_the_next_chunk_starts_a_new_turn():
    """The settling experiment on the UNVERIFIED delimiter, run against a fixture
    built to the `turn_completed`-delimits hypothesis."""
    doc = _doc([_agent("first"), _turn_done(), _agent("second")])
    assert [t.role for t in doc.conversation.turns] == ["assistant", "assistant"]
    assert [t.blocks[0].text for t in doc.conversation.turns] == ["first", "second"]


def test_a_thought_between_two_agent_chunks_splits_them():
    """The other hypothesis: a change of chunk TYPE delimits. Both fixtures are
    synthetic and the adapter is correct under both — a run of one type coalesces,
    and anything of another type closes it."""
    doc = _doc([_agent("before"), _thought("hmm"), _agent("after")])
    assert _types(doc.conversation) == [["text", "thinking", "text"]]
    assert [b.text for b in doc.conversation.turns[0].blocks] == [
        "before", "hmm", "after"]


def test_a_user_chunk_closes_the_open_assistant_turn():
    doc = _doc([_agent("a"), _user("b"), _agent("c")])
    assert [t.role for t in doc.conversation.turns] == [
        "assistant", "human", "assistant"]


def test_harness_noise_does_not_split_a_chunk_run():
    """686 hook_execution events sit between real ones in the measured sample. If
    they flushed the accumulator, every message they interleave would shatter."""
    doc = _doc([_agent("half "), _hook(), _agent("and half")])
    assert _types(doc.conversation) == [["text"]]
    assert doc.conversation.turns[0].blocks[0].text == "half and half"


def test_a_tool_call_closes_an_open_chunk_run():
    doc = _doc([_agent("running it"), _tool_call(), _agent("done")])
    assert _types(doc.conversation) == [["text", "tool_use", "text"]]


def test_empty_and_whitespace_chunks_produce_no_block_and_no_empty_turn():
    doc = _doc([_agent("   "), _agent("")])
    assert doc.conversation.turns == []


def test_the_chunk_join_is_a_single_documented_constant():
    """The join is the one place a wrong hypothesis about `_chunk` shows up, so it is
    a module constant a reader can flip in one edit rather than a buried literal."""
    assert grok.CHUNK_JOIN == ""


# ------------------------------------------------------------------ thoughts

def test_thought_chunks_become_thinking_blocks_and_never_their_own_turn():
    doc = _doc([_thought("step one"), _thought(", step two")])
    assert _types(doc.conversation) == [["thinking"]]
    assert doc.conversation.turns[0].role == "assistant"
    assert doc.conversation.turns[0].blocks[0].text == "step one, step two"


# ---------------------------------------------------------------- tool traffic

def test_tool_events_coalesce_by_tool_call_id_and_never_create_a_turn():
    """2400 tool_call_update against 395 tool_call in the sample: each call is
    updated ~6x. One block per streamed update would be a 6x duplicate of the same
    logical call, and one TURN per event would make the corpus ~99% tool noise."""
    records = [_tool_call(cid="tc_1", status="pending")]
    records += [_tool_update(cid="tc_1", status="in_progress") for _ in range(5)]
    records.append(_tool_update(cid="tc_1", status="completed",
                                raw_output={"stdout": "fine"}))
    doc = _doc(records)
    assert [t.role for t in doc.conversation.turns] == ["assistant"]
    assert _types(doc.conversation) == [["tool_use", "tool_result"]]
    use = doc.conversation.turns[0].blocks[0]
    assert use.data["status"] == "completed"        # the LAST status wins
    assert use.data["call_id"] == "tc_1"
    assert use.data["input"] == {"path": "a.py"}
    assert doc.conversation.meta["tool_call_count"] == 1
    assert doc.conversation.meta["event_counts"]["tool_call_update"] == 6


def test_a_tool_call_with_no_output_gets_no_empty_result_block():
    doc = _doc([_tool_call(), _tool_update(status="completed")])
    assert _types(doc.conversation) == [["tool_use"]]


def test_an_orphan_tool_call_update_still_produces_a_block():
    """A resumed or truncated log can carry an update whose opening tool_call was
    never written. Dropping it would lose the only record of that call."""
    doc = _doc([_tool_update(cid="tc_9", title="grep", raw_output="found it")])
    assert _types(doc.conversation) == [["tool_use", "tool_result"]]
    assert doc.conversation.turns[0].blocks[0].data["name"] == "grep"


def test_tool_calls_without_an_id_do_not_merge_with_each_other():
    doc = _doc([_tool_call(cid="", title="one"), _tool_call(cid="", title="two")])
    assert _types(doc.conversation) == [["tool_use", "tool_use"]]
    assert [b.data["call_id"] for b in doc.conversation.turns[0].blocks] == ["", ""]


def test_a_failed_tool_call_marks_its_result_as_an_error():
    doc = _doc([_tool_call(), _tool_update(status="failed", raw_output="boom")])
    result = doc.conversation.turns[0].blocks[1]
    assert result.data["is_error"] is True
    assert result.data["content"] == "boom"


def test_tool_output_falls_back_to_content_and_to_a_json_dump():
    plain = _doc([_tool_update(cid="a", content=[_content("from content")])])
    dumped = _doc([_tool_update(cid="b", raw_output={"rows": 3})])
    both = _doc([_tool_update(cid="c", raw_output="out", content=_content("shown"))])
    assert plain.conversation.turns[0].blocks[1].data["content"] == "from content"
    assert dumped.conversation.turns[0].blocks[1].data["content"] == '{"rows": 3}'
    assert both.conversation.turns[0].blocks[1].data["content"] == "out\nshown"


def test_an_empty_tool_output_container_is_not_rendered_as_a_result():
    doc = _doc([_tool_update(cid="d", raw_output={}, content=[])])
    assert _types(doc.conversation) == [["tool_use"]]


def test_a_status_less_ping_does_not_erase_the_last_known_status():
    doc = _doc([_tool_call(status="in_progress"), _tool_update(status="")])
    assert doc.conversation.turns[0].blocks[0].data["status"] == "in_progress"


def test_a_second_output_updates_the_same_result_block_rather_than_adding_one():
    """Output can be re-reported as a call streams. A block per report would show the
    same result several times over."""
    doc = _doc([_tool_call(), _tool_update(raw_output="partial"),
                _tool_update(raw_output="final")])
    assert _types(doc.conversation) == [["tool_use", "tool_result"]]
    assert doc.conversation.turns[0].blocks[1].data["content"] == "final"


def test_a_later_update_does_not_erase_a_known_title_or_input():
    doc = _doc([_tool_call(title="read_file", raw_input={"path": "a.py"}),
                _tool_update(status="completed")])
    use = doc.conversation.turns[0].blocks[0]
    assert use.data["name"] == "read_file" and use.data["input"] == {"path": "a.py"}
    assert use.data["kind"] == "read"


# -------------------------------------------------------- signal-to-noise counting

def test_harness_and_lifecycle_events_are_counted_never_rendered():
    """hook_execution / task_* / session_recap / *_changed are the harness talking to
    itself — 866 of the 4000 sampled lines. They are counted so nothing is silently
    invisible, and kept out of the transcript so the conversation stays legible."""
    doc = _doc([_hook(),
                _xai({"sessionUpdate": "task_backgrounded", "task_id": "t1",
                      "command": "sleep", "cwd": "/w", "description": "d",
                      "output_file": "/w/o.log", "tool_call_id": "tc_1"}),
                _xai({"sessionUpdate": "task_completed", "task_snapshot": {}}),
                _xai({"sessionUpdate": "session_recap", "auto": True,
                      "summary": "recap prose"}),
                _upd({"sessionUpdate": "current_mode_update", "currentModeId": "auto"}),
                _xai({"sessionUpdate": "hooks_changed", "hooks": [],
                      "load_errors": [], "project_trusted": True}),
                _xai({"sessionUpdate": "plugins_changed", "plugins": []})])
    assert doc.conversation.turns == []
    assert doc.conversation.meta["event_counts"] == {
        "hook_execution": 1, "task_backgrounded": 1, "task_completed": 1,
        "session_recap": 1, "current_mode_update": 1, "hooks_changed": 1,
        "plugins_changed": 1}


def test_an_unrecognised_event_type_is_counted_so_a_future_event_is_visible():
    """An adapter that silently drops what it does not know cannot tell its owner
    that the format moved. The count is the signal that a new event type shipped."""
    doc = _doc([_upd({"sessionUpdate": "agent_message_v2_chunk",
                      "content": _content("hi")})])
    assert doc.conversation.turns == []
    assert doc.conversation.meta["event_counts"]["agent_message_v2_chunk"] == 1


def test_a_session_of_pure_tool_traffic_has_no_message_turns():
    doc = _doc([_tool_call(cid="t%d" % i) for i in range(20)] + [_hook()])
    assert len(doc.conversation.turns) == 1
    assert {b.type for b in doc.conversation.turns[0].blocks} == {"tool_use"}
    assert doc.conversation.meta["tool_call_count"] == 20


def test_records_that_are_not_the_measured_envelope_are_skipped():
    doc = _doc(["a string", {"no": "params"}, {"params": "not a dict"},
                {"params": {"update": "not a dict"}},
                {"params": {"update": {"no": "discriminant"}}}, _agent("real")])
    assert _types(doc.conversation) == [["text"]]


# ---------------------------------------------------------------- content shapes

@pytest.mark.parametrize("content,expected", [
    ("bare string", "bare string"),
    ({"type": "text", "text": "acp block"}, "acp block"),
    ([{"type": "text", "text": "one"}, {"type": "text", "text": "two"}], "one\ntwo"),
    ({"type": "content", "content": {"type": "text", "text": "nested"}}, "nested"),
    ({"type": "image", "data": "..."}, ""),
    (None, ""),
    (7, ""),
])
def test_every_plausible_content_shape_is_read(content, expected):
    """The spec records `content` as a KEY but not its inner shape, so the adapter
    accepts a bare string, an ACP ContentBlock, a list of them, and one level of ACP
    ToolCallContent wrapping. Guessing ONE of these and being wrong would read the
    whole store as empty."""
    assert grok._text_of(content) == expected


def test_content_nesting_is_bounded():
    deep = {"content": {"content": {"content": {"content": {"text": "buried"}}}}}
    assert grok._text_of(deep) == ""


# ------------------------------------------------------------------ timestamps

def test_iso_timestamps_convert_to_epoch_milliseconds():
    assert grok._iso_to_ms("1970-01-01T00:00:01Z") == 1000
    assert grok._iso_to_ms("1970-01-01T00:00:01+00:00") == 1000


def test_nanosecond_precision_is_truncated_not_rejected():
    """The measured `created_at` is 30 characters — the length of an RFC-3339 stamp
    with NINE fractional digits. datetime.fromisoformat rejects >6 digits on Python
    3.9, which CI pins (ci.yml:22), so an untrimmed parse would leave EVERY Grok
    session undated on the oldest supported interpreter."""
    assert len("2026-08-06T12:00:00.123456789Z") == 30
    assert grok._iso_to_ms("1970-01-01T00:00:01.123456789Z") == 1123


def test_an_unparseable_or_absent_timestamp_leaves_the_session_undated():
    """Undated is a real state the app has an affordance for, so it must not cost the
    session."""
    doc = _doc([_user("hi")], summary=_summary(created_at="whenever",
                                               last_active_at="", updated_at=""))
    assert doc.thread.created_at_ms is None
    assert [t.role for t in doc.conversation.turns] == ["human"]
    assert grok._iso_to_ms(None) is None and grok._iso_to_ms("") is None


def test_timestamps_fall_back_to_the_event_stream_when_the_summary_lacks_them():
    doc = _doc([_user("hi", ts=_TS), _agent("yo", ts=_TS2)])
    assert doc.conversation.created_at == _TS
    assert doc.conversation.updated_at == _TS2
    assert doc.thread.created_at_ms < doc.thread.updated_at_ms


def test_the_summary_dates_win_over_the_event_stream():
    doc = _doc([_user("hi", ts=_TS2)], summary=_summary())
    assert doc.conversation.created_at == _TS and doc.conversation.updated_at == _TS2


def test_a_record_without_a_timestamp_does_not_blank_the_dates():
    no_ts = _user("hi")
    no_ts.pop("timestamp")
    doc = _doc([_user("first", ts=_TS), no_ts])
    assert doc.conversation.created_at == _TS and doc.conversation.updated_at == _TS


# ----------------------------------------------------------------- metadata

def test_the_cwd_comes_from_summary_info_cwd():
    """Spec 5: the directory name is a percent-encoded cwd, but info.cwd is the value
    ITSELF — decoding a name is a guess about the encoding, reading the field is not."""
    doc = _doc([], summary=_summary(), session_dir="/store/%2Fnope/x")
    assert doc.thread.cwd == "/work/repo"
    assert doc.conversation.meta["cwd"] == "/work/repo"


def test_the_cwd_falls_back_to_the_percent_decoded_directory_name():
    doc = _doc([], session_dir=os.path.join("/store", _ENC_CWD, _SID))
    assert doc.thread.cwd == "C:\\work\\repo"
    assert _doc([]).thread.cwd == ""


def test_the_title_prefers_the_generated_title_then_the_first_user_line():
    assert _doc([], summary=_summary()).conversation.title == \
        "Synthetic fixture session"
    assert _doc([_user("do the thing\nand then more")],
                summary=_summary(generated_title="")).conversation.title == \
        "do the thing"
    assert _doc([]).conversation.title == "(untitled)"


def test_the_preview_is_the_first_user_text_not_an_assistant_or_tool_block():
    doc = _doc([_tool_call(), _agent("assistant first"), _user("the human prompt")])
    assert doc.thread.preview == "the human prompt"
    assert _doc([_agent("only the assistant")]).thread.preview == ""
    assert _doc([_thought("t"), _user("")]).thread.preview == ""


def test_the_thread_carries_the_grok_provider_and_the_model_id_in_meta():
    doc = _doc([], summary=_summary())
    assert doc.thread.model_provider == "grok"
    assert doc.thread.agent_nickname == "grok-build"
    assert doc.thread.agent_role == "primary"
    assert doc.conversation.meta["model_id"] == "grok-code-fast-1"
    assert doc.conversation.meta["reasoning_effort"] == "high"
    assert doc.conversation.meta["sandbox_profile"] == "workspace-write"


def test_tokens_used_is_zero_because_the_schema_records_no_token_count():
    """summary.json has num_messages / num_chat_messages — MESSAGE counts, not
    tokens. Deriving a token number from them would be fabrication, so the field
    stays 0 and the reported message counts are carried instead."""
    doc = _doc([_user("hi")], summary=_summary())
    assert doc.thread.tokens_used == 0
    assert doc.conversation.meta["reported_messages"] == 9
    assert doc.conversation.meta["reported_chat_messages"] == 2


def test_a_summary_whose_info_is_not_a_dict_does_not_crash():
    doc = _doc([], summary=_summary(info="nope"), session_dir="/store/enc/fallback")
    assert doc.thread.id == "fallback"


def test_the_session_directory_is_carried_as_the_source_path():
    """`rollout_path` is the UI's 'where did this come from'. For Grok the unit on
    disk is a DIRECTORY, not one file, so that is what it names."""
    doc = _doc([], summary=_summary(), session_dir="/store/enc/x")
    assert doc.thread.rollout_path == "/store/enc/x"
    assert doc.conversation.meta["session_dir"] == "/store/enc/x"


# ---------------------------------------------------------------- spawn graph

def test_spawn_edges_come_from_the_subagent_meta_json():
    """The differentiating feature: parent_session_id AND child_session_id AND status
    are all in one file, which is exactly a corpus.SpawnEdge."""
    doc = _doc([], summary=_summary(), subagents=[_subagent()])
    assert doc.edges == [corpus.SpawnEdge(_SID, _CHILD, "completed")]
    assert doc.conversation.meta["subagent_count"] == 1


def test_a_session_without_a_subagents_directory_yields_no_edge_and_no_error(tmp_path):
    """Present in only 1 of 6 sampled sessions — absence is the NORMAL case."""
    sdir = _session_on_disk(str(tmp_path), records=[_user("hi")])
    doc, errors = grok.parse_session(sdir)
    assert doc.edges == [] and errors == []
    assert doc.conversation.meta["subagent_count"] == 0


def test_a_subagent_without_a_child_id_yields_no_phantom_edge():
    """A directed edge needs both ends. Half an edge would render as a spawn that
    never happened."""
    assert _doc([], summary=_summary(), subagents=[_subagent(child="")]).edges == []
    assert _doc([], summary=_summary(),
                subagents=[_subagent(child=None)]).edges == []


def test_a_subagent_missing_its_parent_id_falls_back_to_this_session():
    """The meta lives INSIDE the parent's directory, so the containing session is the
    parent even when the field is absent."""
    doc = _doc([], summary=_summary(), subagents=[_subagent(parent="")])
    assert doc.edges == [corpus.SpawnEdge(_SID, _CHILD, "completed")]


def test_a_subagent_with_no_parent_anywhere_yields_no_edge():
    assert _doc([], subagents=[_subagent(parent="")]).edges == []


def test_a_subagent_meta_that_is_not_a_dict_is_ignored():
    assert _doc([], summary=_summary(), subagents=["nope", None]).edges == []


def test_spawn_edges_survive_the_corpus_index_round_trip(tmp_path):
    """The edge must be exactly what corpus.upsert_edge persists, or the spawn tree
    the cockpit renders is built from something this adapter never wrote."""
    doc = _doc([_user("hi")], summary=_summary(), subagents=[_subagent()])
    conn = corpus.open_index(str(tmp_path / "idx.sqlite"))
    corpus.upsert_thread(conn, doc.thread)
    for edge in doc.edges:
        corpus.upsert_edge(conn, edge)
    conn.commit()
    loaded = corpus.load_corpus(conn)
    conn.close()
    assert loaded.children_of(_SID) == [_CHILD]
    assert loaded.fan_out(_SID) == 1 and loaded.depth(_CHILD) == 1
    assert loaded.threads[_SID].title == "Synthetic fixture session"
    assert loaded.threads[_SID].model_provider == "grok"


# ------------------------------------------------------------ line-level parsing

def test_a_malformed_jsonl_line_is_skipped_and_logged_not_fatal():
    """A live log is read while it is being written, so its last line can be a
    truncated fragment. One bad line must never cost the whole session."""
    lines = [json.dumps(_user("before")), "{not json", json.dumps(_agent("after"))]
    records, errors = grok.parse_updates_lines(lines, updates_path="u.jsonl")
    assert len(records) == 2
    assert len(errors) == 1 and errors[0]["line"] == 2
    assert errors[0]["stage"] == "parse" and errors[0]["file"] == "u.jsonl"


def test_a_non_object_jsonl_line_is_skipped_and_logged():
    records, errors = grok.parse_updates_lines(["[1,2,3]", "  ", "null"])
    assert records == []
    assert [e["line"] for e in errors] == [1, 3]


def test_blank_lines_are_skipped_silently():
    records, errors = grok.parse_updates_lines(["", "   \n", json.dumps(_user("x"))])
    assert len(records) == 1 and errors == []


# --------------------------------------------------------------- session on disk

def test_parse_session_reads_summary_updates_and_subagents(tmp_path):
    sdir = _session_on_disk(str(tmp_path),
                            records=[_user("hi"), _agent("hello"), _turn_done()],
                            subagents=[_subagent()])
    doc, errors = grok.parse_session(sdir)
    assert errors == []
    assert doc.conversation.id == _SID
    assert [t.role for t in doc.conversation.turns] == ["human", "assistant"]
    assert doc.edges == [corpus.SpawnEdge(_SID, _CHILD, "completed")]
    assert doc.session_dir == sdir and doc.thread.rollout_path == sdir


def test_lock_files_beside_the_real_ones_are_never_parsed_as_data(tmp_path):
    """summary.json.lock / updates.jsonl.lock / meta.json.lock all sit in the real
    store. Every lock here holds GARBAGE, so a glob that swept one up as data would
    surface as a parse error rather than passing quietly."""
    sdir = _session_on_disk(str(tmp_path), records=[_user("hi")],
                            subagents=[_subagent()], locks=True)
    doc, errors = grok.parse_session(sdir)
    assert errors == []
    assert doc.conversation.id == _SID and len(doc.edges) == 1
    docs, errors = grok.ingest_sessions(str(tmp_path))
    assert len(docs) == 1 and errors == []


def test_a_missing_summary_json_still_ingests_the_conversation(tmp_path):
    sdir = _session_on_disk(str(tmp_path), records=[_user("hi")], summary=None)
    doc, errors = grok.parse_session(sdir)
    assert errors == []
    assert doc.conversation.id == _SID          # recovered from params.sessionId
    assert [t.role for t in doc.conversation.turns] == ["human"]


def test_a_malformed_summary_json_is_logged_and_the_session_still_ingests(tmp_path):
    sdir = _session_on_disk(str(tmp_path), records=[_user("hi")], summary=None)
    _write(os.path.join(sdir, "summary.json"), "{oops")
    doc, errors = grok.parse_session(sdir)
    assert len(errors) == 1 and errors[0]["stage"] == "summary"
    assert [t.role for t in doc.conversation.turns] == ["human"]


def test_a_summary_json_that_is_not_an_object_is_logged(tmp_path):
    sdir = _session_on_disk(str(tmp_path), records=[], summary=None)
    _write(os.path.join(sdir, "summary.json"), "[]")
    _doc, errors = grok.parse_session(sdir)
    assert len(errors) == 1 and "not a JSON object" in errors[0]["error"]


def test_a_malformed_subagent_meta_is_logged_not_fatal(tmp_path):
    sdir = _session_on_disk(str(tmp_path), records=[_user("hi")],
                            subagents=[_subagent()])
    _write(os.path.join(sdir, "subagents", "sa-bad", "meta.json"), "{oops")
    doc, errors = grok.parse_session(sdir)
    assert len(doc.edges) == 1                  # the good one survives
    assert len(errors) == 1 and errors[0]["stage"] == "subagent"


def test_a_session_directory_with_no_updates_file_still_yields_a_document(tmp_path):
    sdir = os.path.join(str(tmp_path), _ENC_CWD, _SID)
    _write(os.path.join(sdir, "summary.json"), json.dumps(_summary()))
    doc, errors = grok.parse_session(sdir)
    assert errors == [] and doc.conversation.turns == []
    assert doc.conversation.id == _SID


# --------------------------------------------------------------------- sweep

def test_ingest_sessions_walks_the_encoded_cwd_then_session_layout(tmp_path):
    _session_on_disk(str(tmp_path), records=[_user("one")], sid="s-1",
                     summary=_summary(info={"cwd": "/a", "id": "s-1"}))
    _session_on_disk(str(tmp_path), records=[_user("two")], sid="s-2",
                     enc_cwd="D%3A%5Cother",
                     summary=_summary(info={"cwd": "/b", "id": "s-2"}))
    docs, errors = grok.ingest_sessions(str(tmp_path))
    assert errors == []
    assert sorted(d.conversation.id for d in docs) == ["s-1", "s-2"]


def test_ingest_sessions_is_sorted_and_therefore_deterministic(tmp_path):
    for name in ("s-c", "s-a", "s-b"):
        _session_on_disk(str(tmp_path), records=[_user(name)], sid=name,
                         summary=_summary(info={"cwd": "/a", "id": name}))
    first = [d.conversation.id for d in grok.ingest_sessions(str(tmp_path))[0]]
    assert first == sorted(first)
    assert first == [d.conversation.id for d in grok.ingest_sessions(str(tmp_path))[0]]


def test_ingest_sessions_does_not_treat_a_subagent_directory_as_a_session(tmp_path):
    """A subagent's own transcript lives at the top level under its own cwd folder.
    Counting the nested bookkeeping directory as a session would double-ingest it."""
    sdir = _session_on_disk(str(tmp_path), records=[_user("hi")],
                            subagents=[_subagent()])
    _write(os.path.join(sdir, "subagents", "sa-0", "updates.jsonl"),
           json.dumps(_agent("child talk")) + "\n")
    docs, _errors = grok.ingest_sessions(str(tmp_path))
    assert len(docs) == 1 and docs[0].conversation.id == _SID


def test_ingest_sessions_on_a_missing_root_returns_nothing(tmp_path):
    assert grok.ingest_sessions(str(tmp_path / "nope")) == ([], [])


def test_ingest_sessions_reports_an_unreadable_session_without_aborting(tmp_path,
                                                                        monkeypatch):
    """Robustness matching codex_rollout: one unreadable session costs THAT session."""
    _session_on_disk(str(tmp_path), records=[_user("good")], sid="s-good",
                     summary=_summary(info={"cwd": "/a", "id": "s-good"}))
    bad = _session_on_disk(str(tmp_path), records=[_user("bad")], sid="s-bad",
                           summary=_summary(info={"cwd": "/a", "id": "s-bad"}))
    real_open = builtins.open

    def refuse(path, *args, **kwargs):
        if os.path.normcase(str(path)).startswith(os.path.normcase(bad)):
            raise OSError(13, "permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", refuse)
    docs, errors = grok.ingest_sessions(str(tmp_path))
    monkeypatch.undo()
    assert [d.conversation.id for d in docs] == ["s-good"]
    assert len(errors) == 1 and errors[0]["stage"] == "read"


def test_a_glob_metacharacter_in_a_path_does_not_hide_a_session(tmp_path):
    """A percent-encoded cwd is a directory NAME, and a real path can carry `[`."""
    root = str(tmp_path / "root[1]")
    _session_on_disk(root, records=[_user("hi")], sid="s-x",
                     summary=_summary(info={"cwd": "/a", "id": "s-x"}))
    docs, errors = grok.ingest_sessions(root)
    assert [d.conversation.id for d in docs] == ["s-x"] and errors == []


def test_ingest_sessions_surfaces_a_bad_line_from_one_session_only(tmp_path):
    _session_on_disk(str(tmp_path), records=[_user("fine")], sid="s-ok",
                     summary=_summary(info={"cwd": "/a", "id": "s-ok"}))
    _session_on_disk(str(tmp_path), records=[_user("fine")], sid="s-torn",
                     summary=_summary(info={"cwd": "/a", "id": "s-torn"}),
                     extra_lines=["{truncated"])
    docs, errors = grok.ingest_sessions(str(tmp_path))
    assert len(docs) == 2
    assert len(errors) == 1 and errors[0]["stage"] == "parse"


# ------------------------------------------------------------------- discovery

def test_the_registry_ships_a_grok_session_store():
    spec = next(s for s in discover.PROVIDERS if s.provider == "grok")
    assert isinstance(spec, discover.StoreSpec)
    assert spec.kind == discover.KIND_SESSION_STORE
    assert spec.root == "grok_home" and spec.subdir == "sessions"


def test_discover_finds_a_grok_session_store(tmp_path):
    root = str(tmp_path / "grok" / "sessions")
    _session_on_disk(root, records=[_user("hi")])
    _session_on_disk(root, records=[_user("hi")], sid="s-2")
    roots = discover.Roots(grok_home=str(tmp_path / "grok"))
    hits = [f for f in discover.discover(roots).findings if f.provider == "grok"]
    assert len(hits) == 1
    assert hits[0].path == root                 # exactly what ingest_sessions takes
    assert hits[0].count == 2 and hits[0].detail["session_dirs"] == 2
    assert hits[0].detail["ingestable"] == 2 and hits[0].newest_mtime > 0


def test_discover_does_not_report_an_empty_grok_home(tmp_path):
    os.makedirs(str(tmp_path / "grok" / "sessions"))
    roots = discover.Roots(grok_home=str(tmp_path / "grok"))
    assert [f for f in discover.discover(roots).findings if f.provider == "grok"] == []


def test_discover_does_not_count_a_subagent_directory_as_a_session(tmp_path):
    """item_depth stops above `subagents/`, so a nested bookkeeping directory can
    never inflate the session count the UI reports."""
    root = str(tmp_path / "grok" / "sessions")
    sdir = _session_on_disk(root, records=[_user("hi")], subagents=[_subagent()])
    _write(os.path.join(sdir, "subagents", "sa-0", "updates.jsonl"), "{}\n")
    roots = discover.Roots(grok_home=str(tmp_path / "grok"))
    hit = [f for f in discover.discover(roots).findings if f.provider == "grok"][0]
    assert hit.count == 1


def test_default_roots_prefers_grok_home_env(tmp_path):
    home = str(tmp_path / "home")
    explicit = str(tmp_path / "elsewhere")
    assert discover.default_roots(home=home,
                                  env={"GROK_HOME": explicit}).grok_home == explicit
    assert discover.default_roots(home=home, env={}).grok_home == \
        os.path.join(home, ".grok")


def test_a_nonlocal_grok_home_is_rejected():
    with pytest.raises(ValueError):
        discover.discover(discover.Roots(grok_home="\\\\server\\share"))
