"""Claude Code transcripts (~/.claude/projects/<slug>/) -> IR + spawn graph.

SYNTHETIC fixtures only. The real store on this machine is the owner's LARGEST
(27,770 files) and holds PRIVATE MEDICAL and PHARMACEUTICAL conversations, so not one
byte of it is read here or by the adapter's tests; every fixture below is invented
text. The schema under test comes from `.scratch/CLAUDE-CODE-SCHEMA.md`, extracted by
a structure-only probe (key names, protocol discriminants, value types, string
LENGTHS — never values; 6 files / 2500 lines).

Schema under test (spec-measured):
  projects/<slug>/<session-uuid>.jsonl                       162 SESSIONS
  projects/<slug>/<session-uuid>/subagents/agent-<id>.jsonl   \\ 12,613 CHILD
  projects/<slug>/<s>/subagents/workflows/wf_<id>/agent-<id>.jsonl / transcripts
  projects/<slug>/<session-uuid>/tool-results/*.txt          hook stdout, NOT data
  record `type`   attachment 847 . assistant 662 . user 324 . last-prompt 120 .
                  mode 117 . permission-mode 117 . ai-title 115 .
                  queue-operation 97 . system 51 . file-history-snapshot 50
                  — only `user` and `assistant` are conversation (986 of 2500).
  message         dict{content, role}; `content` is a string OR a list of Anthropic
                  content blocks (the probe refuses to print it, so BOTH are tested).
  version         2.1.170 and 2.1.220 coexist -> unknown types must not fail.

Each trap the spec names gets a named test: the 61% bookkeeping that must not become
turns, the spawn edge that is EXPLICIT IN THE PATH (both the flat and the workflows/
nesting), the `tool-results/` sibling that must never be ingested, the ISO->epoch-ms
conversion and the undated session that is valid rather than an error, the malformed
line / unreadable file that must cost one file and be logged rather than abort the
sweep, and the child-id collision that would silently DROP conversations because
`conversations.conversation_id` is UNIQUE.
"""
import builtins
import json
import os
import sqlite3
from datetime import datetime

from llm_anthology import corpus, ir, render_html, render_md
from llm_anthology.adapters import claude_code as cc


_SID = "0198c4d1-2f3a-7b41-9e02-5d6c7a8b9c0d"
_SID2 = "0198c4d1-2f3a-7b41-9e02-5d6c7a8b9c99"
#: The measured child-id shape is short hex (`a97926b10`), NOT a UUID — so it is NOT
#: unique across sessions and the adapter must qualify it.
_AGENT = "a97926b10"
_AGENT_UUID = "3f2b1a0c-4d5e-6f70-8192-a3b4c5d6e7f8"
_WF = "wf_82c5b350-9ba"
_SLUG = "C--Users-owner-work-repo"
_TS = "2026-08-06T12:00:00.123Z"          # 24 chars, exactly as measured
_TS2 = "2026-08-06T12:05:30.500Z"
_CWD = "C:/Users/owner/work/repo"


# ------------------------------------------------------------------- fixtures

def _rec(rtype, **kw):
    """One transcript line carrying the keys the spec measured on every turn."""
    rec = {"type": rtype, "timestamp": _TS, "sessionId": _SID, "cwd": _CWD,
           "gitBranch": "main", "version": "2.1.220", "isSidechain": False,
           "userType": "external", "uuid": "u-1", "parentUuid": None}
    rec.update(kw)
    return rec


def _user(content, **kw):
    return _rec("user", message={"role": "user", "content": content}, **kw)


def _asst(content, **kw):
    return _rec("assistant", message={"role": "assistant", "content": content}, **kw)


def _text(t):
    """An Anthropic text content block."""
    return [{"type": "text", "text": t}]


def _tool_result(content, tool_use_id="tu-1", is_error=False):
    return [{"type": "tool_result", "tool_use_id": tool_use_id,
             "content": content, "is_error": is_error}]


def _write(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return path


def _session_file(root, sid=_SID, slug=_SLUG, records=None):
    return _write(os.path.join(root, slug, sid + ".jsonl"),
                  [_user("hello")] if records is None else records)


def _child_file(root, sid=_SID, slug=_SLUG, agent=_AGENT, group="", records=None):
    parts = [root, slug, sid, "subagents"]
    parts.extend(p for p in group.split("/") if p)
    parts.append("agent-" + agent + ".jsonl")
    return _write(os.path.join(*parts),
                  [_user("child task")] if records is None else records)


def _doc(records, path="", layout=None):
    return cc.build_document(records, transcript_path=path, layout=layout)


def _roles(doc):
    return [t.role for t in doc.conversation.turns]


def _types(turn):
    return [b.type for b in turn.blocks]


# ------------------------------------------------------- signal to noise (trap 1)

def test_only_user_and_assistant_records_become_turns():
    """61% of a real transcript is bookkeeping. Every non-conversation type in the
    measured vocabulary is fed at once; none of them may open a turn."""
    doc = _doc([_rec("last-prompt"), _rec("mode"), _rec("permission-mode"),
                _rec("queue-operation"), _rec("file-history-snapshot"),
                _user(_text("hi")), _asst(_text("hey"))])
    assert _roles(doc) == ["human", "assistant"]


def test_a_transcript_of_pure_bookkeeping_yields_a_doc_with_zero_turns():
    """A session that never got a reply is still a real session — an empty turn list,
    not a dropped file and not an error."""
    doc = _doc([_rec("mode"), _rec("last-prompt"), _rec("ai-title", aiTitle="T")])
    assert doc.conversation.turns == []
    assert doc.conversation.title == "T"


def test_every_record_type_is_counted_even_when_it_is_not_rendered():
    doc = _doc([_rec("mode"), _rec("mode"), _user(_text("hi"))])
    counts = doc.conversation.meta["record_counts"]
    assert counts["mode"] == 2
    assert counts["user"] == 1


def test_a_record_with_no_type_is_counted_under_a_visible_placeholder():
    doc = _doc([{"timestamp": _TS}])
    assert doc.conversation.meta["record_counts"] == {"(untyped)": 1}


def test_an_unknown_record_type_is_tolerated_and_counted():
    """Two Claude Code versions coexist in one store, so the vocabulary is open."""
    doc = _doc([_rec("brand-new-2.2-type"), _user(_text("hi"))])
    assert doc.conversation.meta["record_counts"]["brand-new-2.2-type"] == 1
    assert _roles(doc) == ["human"]


def test_a_non_dict_record_is_skipped_by_the_builder():
    """build_document is public, so it defends itself rather than trusting a caller."""
    doc = _doc([None, "nope", 7, _user(_text("hi"))])
    assert _roles(doc) == ["human"]
    assert doc.conversation.meta["record_counts"] == {"user": 1}


def test_attachment_records_are_counted_by_kind_and_never_rendered():
    """847 of 2500 sampled lines. The spec records the `attachment` KEY but not its
    inner shape, so counting is the honest ceiling — rendering would need an invented
    field name."""
    doc = _doc([_rec("attachment", attachment={"type": "file"}),
                _rec("attachment", attachment={"type": "file"}),
                _rec("attachment", attachment={"no": "type"}),
                _rec("attachment", attachment="not-a-dict")])
    assert doc.conversation.turns == []
    assert doc.conversation.meta["attachment_kinds"] == {"file": 2, "(untyped)": 2}


def test_system_records_are_counted_by_subtype_and_never_rendered():
    """Hooks, compaction boundaries and stop summaries are the harness talking to
    itself; they carry no role, so they cannot become a turn without inventing one."""
    doc = _doc([_rec("system", subtype="compact_boundary"),
                _rec("system", subtype="turn_duration"),
                _rec("system")])
    assert doc.conversation.turns == []
    assert doc.conversation.meta["system_subtypes"] == {
        "compact_boundary": 1, "turn_duration": 1, "(untyped)": 1}


def test_prompt_sources_are_counted_so_injected_prompts_are_visible():
    doc = _doc([_user(_text("typed one"), promptSource="typed"),
                _user(_text("injected"), promptSource="system"),
                _user(_text("no source"))])
    assert doc.conversation.meta["prompt_sources"] == {"typed": 1, "system": 1}


def test_the_measured_claude_code_versions_are_collected():
    doc = _doc([_user(_text("a"), version="2.1.170"),
                _asst(_text("b"), version="2.1.220"),
                _rec("mode", version="")])
    assert doc.conversation.meta["versions"] == ["2.1.170", "2.1.220"]


# ------------------------------------------------------ message.content shapes

def test_message_content_may_be_a_bare_string():
    doc = _doc([_user("just a string")])
    turn = doc.conversation.turns[0]
    assert _types(turn) == ["text"]
    assert turn.blocks[0].text == "just a string"


def test_a_whitespace_only_string_content_makes_no_empty_bubble():
    doc = _doc([_user("   \n ")])
    assert doc.conversation.turns == []


def test_message_content_may_be_a_list_of_anthropic_blocks():
    doc = _doc([_asst([{"type": "thinking", "thinking": "hmm"},
                       {"type": "text", "text": "answer"},
                       {"type": "tool_use", "name": "Read", "input": {"p": "x"},
                        "id": "tu-1"}])])
    turn = doc.conversation.turns[0]
    assert _types(turn) == ["thinking", "text", "tool_use"]
    assert turn.blocks[2].data["name"] == "Read"
    assert turn.blocks[2].data["input"] == {"p": "x"}
    assert turn.blocks[2].data["id"] == "tu-1"


def test_message_content_may_be_one_bare_block_dict():
    doc = _doc([_user({"type": "text", "text": "unwrapped"})])
    assert doc.conversation.turns[0].blocks[0].text == "unwrapped"


def test_a_content_list_may_hold_bare_strings():
    doc = _doc([_user(["one", "   ", 5])])
    assert _types(doc.conversation.turns[0]) == ["text", "unknown"]


def test_content_that_is_neither_string_nor_container_yields_no_blocks():
    doc = _doc([_user(None), _user(7)])
    assert doc.conversation.turns == []


def test_a_thinking_block_may_carry_its_text_under_either_key():
    doc = _doc([_asst([{"type": "thinking", "text": "fallback"}]),
                _asst([{"type": "thinking", "thinking": "  "}])])
    assert [b.text for b in doc.conversation.turns[0].blocks] == ["fallback"]


def test_an_empty_text_block_is_dropped_rather_than_rendered_blank():
    doc = _doc([_asst([{"type": "text", "text": "  "}, {"type": "text", "text": "x"}])])
    assert [b.text for b in doc.conversation.turns[0].blocks] == ["x"]


def test_an_unknown_content_block_survives_as_an_unknown_block():
    doc = _doc([_asst([{"type": "server_tool_use_2027", "q": 1}])])
    block = doc.conversation.turns[0].blocks[0]
    assert block.type == "unknown"
    assert block.data["orig_type"] == "server_tool_use_2027"
    assert block.data["x_raw"] == {"type": "server_tool_use_2027", "q": 1}


def test_an_image_block_becomes_a_chip_and_never_carries_the_base64():
    """render_html has no base64 image path, and inlining one screenshot would put
    megabytes of payload into the export."""
    doc = _doc([_user([{"type": "image", "source": {"type": "base64",
                                                    "media_type": "image/png",
                                                    "data": "AAAABBBB"}}])])
    block = doc.conversation.turns[0].blocks[0]
    assert block.type == "attachment"
    assert block.data["media_type"] == "image/png"
    assert "AAAABBBB" not in json.dumps(block.data)


def test_a_document_block_becomes_a_chip_titled_by_its_own_title():
    doc = _doc([_user([{"type": "document", "title": "spec.pdf",
                        "source": {"type": "base64", "data": "PDF"}}])])
    block = doc.conversation.turns[0].blocks[0]
    assert block.type == "attachment"
    assert block.data["file_name"] == "spec.pdf"


def test_a_media_block_with_no_source_falls_back_to_its_type_name():
    doc = _doc([_user([{"type": "image"}])])
    assert doc.conversation.turns[0].blocks[0].data["file_name"] == "image"


# ------------------------------------------------------------- tool results

def test_a_user_record_of_pure_tool_results_folds_into_the_assistant_turn():
    """Claude Code delivers a tool RESULT as a user-role record. Rendering it as a
    human bubble would attribute machine output to the owner; the spec's
    `sourceToolAssistantUUID` says it belongs to the assistant turn that asked."""
    doc = _doc([_user(_text("do it")),
                _asst([{"type": "tool_use", "name": "Bash", "id": "tu-1"}]),
                _user(_tool_result("output text"), sourceToolAssistantUUID="u-1"),
                _asst(_text("done"))])
    assert _roles(doc) == ["human", "assistant"]
    assert _types(doc.conversation.turns[1]) == ["tool_use", "tool_result", "text"]
    assert doc.conversation.meta["tool_result_records"] == 1


def test_a_tool_result_arriving_before_any_assistant_turn_still_opens_one():
    doc = _doc([_user(_tool_result("orphan"))])
    assert _roles(doc) == ["assistant"]


def test_a_mixed_user_record_stays_human():
    doc = _doc([_user(_tool_result("out") + _text("and a question"))])
    assert _roles(doc) == ["human"]


def test_tool_result_content_that_is_a_list_of_parts_is_flattened():
    doc = _doc([_user(_tool_result([{"type": "text", "text": "a"}, "b", {"x": 1}, 9]))])
    assert doc.conversation.turns[0].blocks[0].data["content"] == "a\nb"


def test_a_base64_part_inside_a_tool_result_becomes_a_label_not_a_payload():
    """A screenshot returned by a tool must not smuggle megabytes of base64 past the
    `_MEDIA_BLOCKS` rule and into the export."""
    doc = _doc([_user(_tool_result([{"type": "text", "text": "shot:"},
                                    {"type": "image",
                                     "source": {"data": "AAAABBBB"}}]))])
    content = doc.conversation.turns[0].blocks[0].data["content"]
    assert content == "shot:\n[image]"


def test_tool_result_content_with_nothing_textual_keeps_the_raw_payload():
    """render_html json-dumps a non-string content, so the payload survives instead
    of being flattened to an empty string."""
    doc = _doc([_user(_tool_result({"structured": 1}))])
    assert doc.conversation.turns[0].blocks[0].data["content"] == {"structured": 1}
    doc2 = _doc([_user(_tool_result([{"no": "text"}]))])
    assert doc2.conversation.turns[0].blocks[0].data["content"] == [{"no": "text"}]


def test_a_failed_tool_result_is_marked_as_an_error():
    doc = _doc([_user(_tool_result("boom", is_error=True))])
    block = doc.conversation.turns[0].blocks[0]
    assert block.data["is_error"] is True
    assert block.data["tool_use_id"] == "tu-1"
    assert block.data["name"] == ""      # the Anthropic block carries no tool NAME


# --------------------------------------------------------------- turn model

def test_consecutive_assistant_records_coalesce_into_one_turn():
    """Matches codex_rollout: every assistant-side item joins one turn that the next
    human message flushes."""
    doc = _doc([_user(_text("q")), _asst(_text("part 1")), _asst(_text("part 2")),
                _user(_text("q2")), _asst(_text("part 3"))])
    assert _roles(doc) == ["human", "assistant", "human", "assistant"]
    assert len(doc.conversation.turns[1].blocks) == 2


def test_an_assistant_record_with_no_visible_block_adds_nothing():
    doc = _doc([_asst([]), _user(_text("hi"))])
    assert _roles(doc) == ["human"]


def test_a_turn_carries_its_record_uuid_and_timestamp():
    doc = _doc([_user(_text("hi"), uuid="u-9", timestamp=_TS2)])
    turn = doc.conversation.turns[0]
    assert turn.uuid == "u-9"
    assert turn.timestamp == _TS2


def test_the_first_human_text_becomes_the_title_and_the_preview():
    doc = _doc([_user(_text("Fix the flaky test\nsecond line"))])
    assert doc.conversation.title == "Fix the flaky test"
    assert doc.thread.preview == "Fix the flaky test\nsecond line"


def test_a_generated_ai_title_wins_over_the_first_prompt():
    doc = _doc([_user(_text("hi")), _rec("ai-title", aiTitle="Flaky test triage"),
                _rec("ai-title", aiTitle="ignored second")])
    assert doc.conversation.title == "Flaky test triage"


def test_a_conversation_with_no_text_at_all_is_untitled():
    doc = _doc([_asst([{"type": "tool_use", "name": "Bash", "id": "t"}])])
    assert doc.conversation.title == "(untitled)"
    assert doc.thread.preview == ""


def test_a_leading_non_text_block_does_not_hide_the_first_prompt():
    doc = _doc([_user([{"type": "image"}] + _text("real question"))])
    assert doc.conversation.title == "real question"


# ------------------------------------------------- threading (spec instruction 5)

def test_parent_uuid_branch_points_are_counted_not_walked():
    """`parentUuid` is an intra-session MESSAGE chain, distinct from the session-level
    spawn edge. Two records sharing one parent is a branch point; a null parentUuid is
    a chain HEAD, and several heads in one file are resumes, not branches."""
    doc = _doc([_user(_text("a"), uuid="1", parentUuid=None),
                _asst(_text("b"), uuid="2", parentUuid="1"),
                _asst(_text("c"), uuid="3", parentUuid="1"),
                _user(_text("d"), uuid="4", parentUuid=None)])
    assert doc.conversation.meta["branch_points"] == 1


def test_sidechain_records_are_counted_but_kept_by_default():
    doc = _doc([_user(_text("a")), _asst(_text("b"), isSidechain=True)])
    assert doc.conversation.meta["sidechain_records"] == 1
    assert _roles(doc) == ["human", "assistant"]


def test_the_sidechain_lever_drops_them_from_a_session_only(monkeypatch):
    """If the real store turns out to inline a child's turns into its PARENT file,
    flipping one constant stops the duplication — and never touches the child's own
    transcript, where those same records ARE the conversation."""
    monkeypatch.setattr(cc, "DROP_SIDECHAIN_TURNS", True)
    records = [_user(_text("a")), _asst(_text("b"), isSidechain=True)]
    assert _roles(_doc(records)) == ["human"]
    child = cc.Layout(kind="subagent", thread_id="c", parent_id=_SID, agent_id="c")
    assert _roles(_doc(records, layout=child)) == ["human", "assistant"]


def test_meta_user_records_are_counted_and_still_rendered():
    doc = _doc([_user(_text("caveat text"), isMeta=True)])
    assert doc.conversation.meta["meta_user_records"] == 1
    assert _roles(doc) == ["human"]


# --------------------------------------------------------------- timestamps

def test_iso_timestamps_become_epoch_milliseconds():
    doc = _doc([_user(_text("a"), timestamp=_TS), _asst(_text("b"), timestamp=_TS2)])
    assert doc.thread.created_at_ms == 1786017600123
    assert doc.thread.updated_at_ms == 1786017930500
    assert doc.conversation.created_at == _TS
    assert doc.conversation.updated_at == _TS2


def test_a_transcript_with_no_timestamp_is_undated_not_an_error():
    """The UI has a genuine `undated` affordance, so absence is valid."""
    doc = _doc([_user(_text("a"), timestamp=None)])
    assert doc.thread.created_at_ms is None
    assert doc.thread.updated_at_ms is None
    assert doc.conversation.created_at == ""


def test_an_unparseable_timestamp_leaves_the_session_undated():
    doc = _doc([_user(_text("a"), timestamp="last tuesday")])
    assert doc.thread.created_at_ms is None


def test_an_over_long_fractional_second_still_parses():
    """Python 3.9 (the oldest interpreter this project supports) rejects more than 6
    fractional digits, so the fraction is trimmed before parsing."""
    doc = _doc([_user(_text("a"), timestamp="2026-08-06T12:00:00.123456789Z")])
    assert doc.thread.created_at_ms == 1786017600123


class _Py39Datetime:
    """`datetime` as it behaves on Python 3.9 — the oldest interpreter
    `pyproject.toml` supports and the one this suite never actually runs on.

    Both normalisations in `_iso_to_ms` exist FOR 3.9 and are therefore invisible
    here: 3.11+ `fromisoformat` accepts a trailing 'Z' and truncates an over-long
    fraction by itself, so removing either one leaves the suite green on this
    interpreter. A mutation check proved exactly that. This stub restores the 3.9
    behaviour so the guards are measured rather than assumed.
    """

    @staticmethod
    def fromisoformat(text):
        if "Z" in text:
            raise ValueError("Invalid isoformat string: %r" % text)
        fraction = text.partition(".")[2]
        if len(fraction.split("+")[0]) > 6:
            raise ValueError("Invalid isoformat string: %r" % text)
        return datetime.fromisoformat(text)


def test_the_z_normalisation_is_what_lets_python_39_parse_a_stamp(monkeypatch):
    monkeypatch.setattr(cc, "datetime", _Py39Datetime)
    assert cc._iso_to_ms(_TS) == 1786017600123


def test_the_fraction_trim_is_what_lets_python_39_parse_a_nanosecond_stamp(monkeypatch):
    monkeypatch.setattr(cc, "datetime", _Py39Datetime)
    assert cc._iso_to_ms("2026-08-06T12:00:00.123456789Z") == 1786017600123
    assert cc._iso_to_ms("2026-08-06T12:00:00.123456789+00:00") == 1786017600123


def test_a_timestamp_without_a_trailing_z_is_accepted():
    doc = _doc([_user(_text("a"), timestamp="2026-08-06T12:00:00.123+00:00")])
    assert doc.thread.created_at_ms == 1786017600123


# ---------------------------------------------------- usage / cwd / git branch

def test_assistant_usage_is_summed_and_the_model_id_is_kept():
    """UNVERIFIED against the store: the spec measured `message` as {content, role}
    only, so `usage`/`model` are read defensively and absence costs nothing."""
    doc = _doc([_rec("assistant", message={"role": "assistant", "content": _text("a"),
                                           "model": "claude-opus-5",
                                           "usage": {"input_tokens": 10,
                                                     "output_tokens": 5}}),
                _rec("assistant", message={"role": "assistant", "content": _text("b"),
                                           "usage": {"input_tokens": 1,
                                                     "output_tokens": "bad"}}),
                _rec("assistant", message="not-a-dict")])
    assert doc.thread.tokens_used == 16
    assert doc.conversation.meta["model_id"] == "claude-opus-5"


def test_a_transcript_without_usage_reports_zero_tokens_rather_than_a_guess():
    doc = _doc([_asst(_text("a"))])
    assert doc.thread.tokens_used == 0
    assert doc.conversation.meta["model_id"] == ""


def test_cwd_and_git_branch_come_from_the_records():
    doc = _doc([_user(_text("a"))])
    assert doc.thread.cwd == _CWD
    assert doc.thread.git_branch == "main"


def test_a_missing_cwd_falls_back_to_the_raw_slug_and_never_a_decoded_path():
    """Claude Code's directory name replaces every separator with `-`, so it is NOT
    reversibly decodable — `C--Users-x` could be `C:/Users/x` or `C:/Users-x`. A wrong
    path is worse than an honest raw slug."""
    layout = cc.Layout(kind="session", thread_id=_SID, slug=_SLUG)
    doc = _doc([_user(_text("a"), cwd="")], layout=layout)
    assert doc.thread.cwd == _SLUG


def test_the_reported_session_id_is_kept_so_a_path_mismatch_is_visible():
    """Whether a CHILD transcript carries its own sessionId or its parent's is the
    spec's open question; recording it is what settles it on real data."""
    layout = cc.classify_path("/r/%s/%s/subagents/agent-%s.jsonl"
                              % (_SLUG, _SID, _AGENT))
    doc = _doc([_user(_text("a"))], layout=layout)
    assert doc.conversation.meta["reported_session_id"] == _SID
    assert doc.conversation.id != _SID


# ------------------------------------------------------- the path is the graph

def test_a_session_path_classifies_as_a_session_keyed_by_its_filename():
    layout = cc.classify_path("/root/%s/%s.jsonl" % (_SLUG, _SID))
    assert layout.kind == "session"
    assert layout.thread_id == _SID
    assert layout.slug == _SLUG
    assert layout.parent_id == ""


def test_a_flat_subagent_path_carries_both_ends_of_the_edge():
    layout = cc.classify_path("/root/%s/%s/subagents/agent-%s.jsonl"
                              % (_SLUG, _SID, _AGENT))
    assert layout.kind == "subagent"
    assert layout.parent_id == _SID
    assert layout.agent_id == _AGENT
    assert layout.workflow == ""
    assert layout.slug == _SLUG


def test_a_workflow_subagent_path_keeps_the_grouping_level():
    layout = cc.classify_path("/root/%s/%s/subagents/workflows/%s/agent-%s.jsonl"
                              % (_SLUG, _SID, _WF, _AGENT))
    assert layout.kind == "subagent"
    assert layout.parent_id == _SID
    assert layout.workflow == "workflows/" + _WF
    assert layout.agent_id == _AGENT


def test_a_windows_backslash_path_classifies_identically():
    layout = cc.classify_path("C:\\r\\%s\\%s\\subagents\\agent-%s.jsonl"
                              % (_SLUG, _SID, _AGENT))
    assert layout.parent_id == _SID
    assert layout.agent_id == _AGENT


def test_a_child_filename_without_the_agent_prefix_is_still_a_child():
    """Tolerating drift: the `agent-` prefix is a naming convention, not the signal."""
    layout = cc.classify_path("/r/%s/%s/subagents/%s.jsonl" % (_SLUG, _SID, _AGENT))
    assert layout.kind == "subagent"
    assert layout.agent_id == _AGENT


def test_a_short_hex_child_id_is_qualified_by_its_path():
    """`conversations.conversation_id` is UNIQUE, so if two sessions each hold an
    `agent-a97926b10`, a bare id would make the second one SILENTLY vanish."""
    a = cc.classify_path("/r/%s/%s/subagents/agent-%s.jsonl" % (_SLUG, _SID, _AGENT))
    b = cc.classify_path("/r/%s/%s/subagents/agent-%s.jsonl" % (_SLUG, _SID2, _AGENT))
    assert a.thread_id != b.thread_id
    assert a.thread_id == "%s/%s" % (_SID, _AGENT)


def test_two_workflows_in_one_session_cannot_collide_either():
    a = cc.classify_path("/r/%s/%s/subagents/workflows/wf_1/agent-%s.jsonl"
                         % (_SLUG, _SID, _AGENT))
    b = cc.classify_path("/r/%s/%s/subagents/workflows/wf_2/agent-%s.jsonl"
                         % (_SLUG, _SID, _AGENT))
    assert a.thread_id != b.thread_id


def test_a_uuid_child_id_is_used_verbatim():
    """A UUID is unique by construction, so qualifying it would only obscure it."""
    layout = cc.classify_path("/r/%s/%s/subagents/agent-%s.jsonl"
                              % (_SLUG, _SID, _AGENT_UUID))
    assert layout.thread_id == _AGENT_UUID


def test_a_subagents_component_with_no_parent_above_it_is_not_an_edge():
    layout = cc.classify_path("subagents/agent-x.jsonl")
    assert layout.kind == "session"
    assert layout.parent_id == ""


def test_a_bare_filename_classifies_with_an_empty_slug():
    layout = cc.classify_path("%s.jsonl" % _SID)
    assert layout.thread_id == _SID
    assert layout.slug == ""


def test_a_file_that_is_only_the_extension_falls_back_to_the_reported_id():
    doc = _doc([_user(_text("a"))], layout=cc.classify_path("/r/%s/.jsonl" % _SLUG))
    assert doc.conversation.id == _SID


def test_a_tool_results_sidecar_is_never_a_transcript():
    """`tool-results/` holds hook stdout `.txt` sidecars — real content, wrong shape."""
    assert cc.classify_path("/r/%s/%s/tool-results/x.jsonl" % (_SLUG, _SID)) is None
    assert cc.classify_path("/r/%s/%s/tool-results/deep/x.jsonl"
                            % (_SLUG, _SID)) is None


def test_a_session_transcript_emits_no_spawn_edge():
    doc = _doc([_user(_text("a"))],
               layout=cc.classify_path("/r/%s/%s.jsonl" % (_SLUG, _SID)))
    assert doc.edges == []
    assert doc.thread.agent_role == "session"


def test_a_flat_child_emits_one_subagent_edge():
    path = "/r/%s/%s/subagents/agent-%s.jsonl" % (_SLUG, _SID, _AGENT)
    doc = _doc([_user(_text("a"))], path=path, layout=cc.classify_path(path))
    assert len(doc.edges) == 1
    edge = doc.edges[0]
    assert (edge.parent_thread_id, edge.child_thread_id, edge.status) == (
        _SID, "%s/%s" % (_SID, _AGENT), "subagent")
    assert doc.thread.agent_role == "subagent"
    assert doc.thread.agent_nickname == _AGENT


def test_a_workflow_child_edge_is_labelled_workflow():
    path = ("/r/%s/%s/subagents/workflows/%s/agent-%s.jsonl"
            % (_SLUG, _SID, _WF, _AGENT))
    doc = _doc([_user(_text("a"))], path=path, layout=cc.classify_path(path))
    assert doc.edges[0].status == "workflow"
    assert doc.conversation.meta["workflow"] == "workflows/" + _WF


def test_a_child_layout_with_no_parent_yields_no_half_edge():
    """Half an edge would render as a spawn that never happened."""
    doc = _doc([_user(_text("a"))],
               layout=cc.Layout(kind="subagent", thread_id="c", agent_id="c"))
    assert doc.edges == []


def test_the_edge_is_exactly_what_corpus_upsert_edge_persists():
    path = "/r/%s/%s/subagents/agent-%s.jsonl" % (_SLUG, _SID, _AGENT)
    doc = _doc([_user(_text("a"))], path=path, layout=cc.classify_path(path))
    conn = sqlite3.connect(":memory:")
    try:
        corpus.init_index(conn)
        corpus.upsert_thread(conn, doc.thread)
        for edge in doc.edges:
            corpus.upsert_edge(conn, edge)
        assert conn.execute("SELECT parent_thread_id, child_thread_id, status "
                            "FROM thread_spawn_edges").fetchall() == [
            (_SID, "%s/%s" % (_SID, _AGENT), "subagent")]
        assert conn.execute("SELECT model_provider FROM threads").fetchone()[0] == \
            "claude-code"
    finally:
        conn.close()


def test_every_block_this_adapter_emits_actually_renders():
    """The data KEYS matter, not just the block types: `render_html._block_html` reads
    data['name'] / data['input'] / data['content'] / data['file_name'] / data['x_raw'],
    and a wrong key renders a silently blank box. One document carrying every block
    type this adapter can emit is pushed through the real renderer."""
    doc = _doc([_user(_text("a question")),
                _asst([{"type": "thinking", "thinking": "reasoning"},
                       {"type": "text", "text": "an answer"},
                       {"type": "tool_use", "name": "Grep", "input": {"q": "x"},
                        "id": "tu-1"},
                       {"type": "image", "source": {"media_type": "image/png"}},
                       {"type": "future_block_type", "payload": "kept"}]),
                _user(_tool_result("the output"))])
    html = render_html.render_conversation_html(doc.conversation)
    for expected in ("a question", "reasoning", "an answer", "Grep", '"q": "x"',
                     "image/png", "future_block_type", "kept", "the output"):
        assert expected in html, expected
    assert "<div class=\"md\"></div>" not in html      # no silently blank block
    assert "a question" in render_md.render_conversation_md(doc.conversation)


def test_the_doc_exposes_the_thread_id_alias_and_the_ir_shapes():
    doc = _doc([_user(_text("a"))],
               layout=cc.classify_path("/r/%s/%s.jsonl" % (_SLUG, _SID)))
    assert doc.thread_id == doc.thread.id == _SID
    assert isinstance(doc.conversation, ir.Conversation)
    assert isinstance(doc.thread, corpus.ThreadMeta)
    assert doc.conversation.provider == "claude-code"


# ------------------------------------------------------------- line / file layer

def test_a_malformed_line_costs_that_line_and_is_logged():
    """A transcript is read while it is being written, so its tail can be partial."""
    doc, errors = cc.parse_transcript_lines(
        [json.dumps(_user(_text("kept"))) + "\n", "\n", "{not json",
         json.dumps(_asst(_text("also kept")))], transcript_path="t.jsonl")
    assert _roles(doc) == ["human", "assistant"]
    assert len(errors) == 1
    assert errors[0]["line"] == 3
    assert errors[0]["stage"] == "parse"
    assert errors[0]["file"] == "t.jsonl"


def test_a_line_that_is_valid_json_but_not_an_object_is_logged():
    doc, errors = cc.parse_transcript_lines(["[1,2]"])
    assert doc.conversation.turns == []
    assert errors[0]["error"] == "line is not a JSON object"


def test_parse_transcript_file_classifies_from_its_own_path(tmp_path):
    root = str(tmp_path / "projects")
    path = _child_file(root)
    doc, errors = cc.parse_transcript_file(path)
    assert errors == []
    assert doc.edges[0].parent_thread_id == _SID
    assert doc.transcript_path == path
    assert doc.thread.rollout_path == path


def test_a_session_with_no_subagents_directory_ingests_normally(tmp_path):
    root = str(tmp_path / "projects")
    _session_file(root)
    docs, errors = cc.ingest_sessions(root)
    assert errors == []
    assert len(docs) == 1
    assert docs[0].edges == []


def test_a_session_and_all_its_children_are_ingested_with_their_edges(tmp_path):
    root = str(tmp_path / "projects")
    _session_file(root)
    _child_file(root, agent="a1")
    _child_file(root, agent="a2")
    _child_file(root, agent="a3", group="workflows/" + _WF)
    docs, errors = cc.ingest_sessions(root)
    assert errors == []
    assert len(docs) == 4
    edges = [e for d in docs for e in d.edges]
    assert len(edges) == 3
    assert {e.parent_thread_id for e in edges} == {_SID}
    assert len({e.child_thread_id for e in edges}) == 3
    assert sorted(e.status for e in edges) == ["subagent", "subagent", "workflow"]


def test_ingest_ignores_a_tool_results_sibling_whatever_its_extension(tmp_path):
    root = str(tmp_path / "projects")
    _session_file(root)
    os.makedirs(os.path.join(root, _SLUG, _SID, "tool-results"))
    _write(os.path.join(root, _SLUG, _SID, "tool-results", "hook.jsonl"),
           [_user(_text("hook stdout"))])
    with open(os.path.join(root, _SLUG, _SID, "tool-results", "hook.txt"), "w",
              encoding="utf-8") as fh:
        fh.write("hook stdout\n")
    docs, errors = cc.ingest_sessions(root)
    assert errors == []
    assert [d.conversation.id for d in docs] == [_SID]


def test_ingest_skips_files_that_are_not_jsonl(tmp_path):
    root = str(tmp_path / "projects")
    _session_file(root)
    with open(os.path.join(root, _SLUG, "notes.md"), "w", encoding="utf-8") as fh:
        fh.write("# notes\n")
    docs, _ = cc.ingest_sessions(root)
    assert len(docs) == 1


def test_ingest_is_deterministically_ordered(tmp_path):
    """`os.walk` is directory-at-a-time, so the sweep is NOT globally lexicographic —
    what is pinned is that two runs agree and that siblings inside one directory come
    out sorted (both levels of the walk are sorted explicitly)."""
    root = str(tmp_path / "projects")
    _session_file(root, sid=_SID2)
    _session_file(root, sid=_SID)
    _child_file(root, agent="z")
    _child_file(root, agent="a")
    first = [d.transcript_path for d in cc.ingest_sessions(root)[0]]
    assert [d.transcript_path for d in cc.ingest_sessions(root)[0]] == first
    sessions = [p for p in first if p.endswith(".jsonl") and "subagents" not in p]
    children = [os.path.basename(p) for p in first if "subagents" in p]
    assert sessions == sorted(sessions)
    assert children == ["agent-a.jsonl", "agent-z.jsonl"]


def test_an_unreadable_transcript_costs_that_file_and_never_the_sweep(tmp_path,
                                                                     monkeypatch):
    root = str(tmp_path / "projects")
    good = _session_file(root, sid=_SID)
    bad = _session_file(root, sid=_SID2)
    real_open = builtins.open

    def fake_open(path, *a, **kw):
        if str(path) == bad:
            raise OSError("permission denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)
    docs, errors = cc.ingest_sessions(root)
    assert [d.transcript_path for d in docs] == [good]
    assert len(errors) == 1
    assert errors[0]["file"] == bad
    assert errors[0]["stage"] == "read"


def test_a_missing_root_yields_an_honest_empty_result(tmp_path):
    assert cc.ingest_sessions(str(tmp_path / "nope")) == ([], [])


def test_iter_documents_yields_none_for_a_file_it_could_not_read(tmp_path,
                                                                monkeypatch):
    root = str(tmp_path / "projects")
    _session_file(root)
    real_open = builtins.open

    def fake_open(path, *a, **kw):
        if str(path).endswith(".jsonl"):
            raise OSError("gone")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert [doc for doc, _ in cc.iter_documents(root)] == [None]


def test_a_malformed_line_in_one_child_does_not_stop_the_others(tmp_path):
    root = str(tmp_path / "projects")
    _child_file(root, agent="a1")
    path = _child_file(root, agent="a2")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{truncated\n")
    docs, errors = cc.ingest_sessions(root)
    assert len(docs) == 2
    assert len(errors) == 1
    assert errors[0]["file"] == path
