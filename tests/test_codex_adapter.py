"""Codex task export (codex.json) -> IR.

SYNTHETIC fixtures mirroring the schema probed from the real 20 MB export
(97 threads / 204 turns, 2026-07-20). No real conversation content appears here.

Schema under test:
  thread{archived, id, title, turns[]}
  user turn{custom_instructions, id, input_items[], role="user"}
  assistant turn{branch, branch_name, external_pull_request_id, id, output_items[],
                 previous_turn_id, pull_request_status, role="assistant", turn_status}

The measured traps each get a named test: the leaked enum repr in turn_status, the
22 assistant turns with zero output_items, the git-branch STRING that must never
reach ir.Turn.branch, the citation parts that carry no 'text' key, and the repo
snapshot whose bodies live one nesting level below `contents`.
"""
import json
import os

from llm_anthology import build, cli, ir, loaders, render_html, render_md
from llm_anthology.adapters import codex


# --------------------------------------------------------------------- fixtures

def _user_turn(tid="task_u1", parts=None, items=None, custom_instructions=None):
    if items is None:
        items = [{"type": "message", "role": "user",
                  "content": parts if parts is not None else [
                      {"content_type": "text", "text": "the question"}]}]
    return {"id": tid, "role": "user", "custom_instructions": custom_instructions,
            "input_items": items}


def _asst_turn(tid="task_a1", items=None, status="TaskTurnStatusEnum.COMPLETED",
               pr_status="not_created", branch="codex/feature", branch_name=None):
    if items is None:
        items = [{"type": "message", "role": "assistant",
                  "content": [{"content_type": "text", "text": "the answer"}]}]
    return {"id": tid, "role": "assistant", "output_items": items,
            "turn_status": status, "pull_request_status": pr_status,
            "branch": branch, "branch_name": branch_name,
            "previous_turn_id": "task_u1", "external_pull_request_id": "abcd"}


def _thread(turns=None, tid="task_t1", title="A task", archived=False):
    return {"archived": archived, "id": tid, "title": title,
            "turns": turns if turns is not None else [_user_turn(), _asst_turn()]}


def _blocks(conv):
    return [b for t in conv.turns for b in t.blocks]


def _kinds(conv):
    return [b.type for b in _blocks(conv)]


def _snapshot(path="src/app.py", lrcs=None, missing_reason=None, contents=None):
    if lrcs is None:
        lrcs = [{"line_range_start": 1, "line_range_end": 2,
                 "contains_end_of_file": False, "content": ["line one", "line two"]}]
    return {"type": "partial_repo_snapshot",
            "files": [{"path": path, "contents": contents,
                       "missing_reason": missing_reason, "line_range_contents": lrcs}]}


def _pr(output_diff=None, pre_apply_patch=None, title="Add a thing",
        message="This PR adds a thing."):
    if output_diff is None:
        output_diff = _output_diff()
    return {"type": "pr", "pr_title": title, "pr_message": message,
            "pre_apply_patch": pre_apply_patch, "output_diff": output_diff}


def _output_diff(diff=None, external=None, commit_message="commit the thing"):
    return {"diff": diff, "external_storage_diff": external,
            "commit_message": commit_message, "base_commit_sha": "deadbeef",
            "repo_id": "r-1", "type": "OutputDiffType.GIT", "files_modified": 2,
            "lines_added": 10, "lines_removed": 3,
            "additional_parent_commit_shas": None,
            "preapply_patch_base_commit_sha": None}


# ------------------------------------------------------- conversation-level shape

def test_thread_becomes_one_conversation_with_the_codex_provider():
    c = codex.parse_conversation(_thread())
    assert isinstance(c, ir.Conversation)
    assert c.provider == "codex" and c.id == "task_t1" and c.title == "A task"


def test_untitled_thread_gets_a_placeholder_title():
    c = codex.parse_conversation(_thread(title=""))
    assert c.title == "(untitled)"


def test_archived_is_mapped_to_meta_never_used_as_a_filter():
    """No thread is archived in the corpus, so FILTERING on it is untestable and
    would silently empty a future export. It is recorded, not obeyed."""
    c = codex.parse_conversation(_thread(archived=True))
    assert c.meta["archived"] is True and len(c.turns) == 2


def test_user_and_assistant_turns_stay_separate_never_folded_into_one():
    c = codex.parse_conversation(_thread())
    assert [t.role for t in c.turns] == ["human", "assistant"]
    assert [t.uuid for t in c.turns] == ["task_u1", "task_a1"]


def test_no_timestamps_are_invented():
    """A recursive key scan for time/_at/date/created/updated/stamp over the real
    file returns ZERO hits — the export carries no times at all."""
    c = codex.parse_conversation(_thread())
    assert c.created_at == "" and c.updated_at == ""
    assert all(t.timestamp == "" for t in c.turns)


def test_git_branch_string_never_reaches_turn_branch():
    """turn['branch'] is a git branch NAME (str 102/102). ir.Turn.branch is
    {index,total} and render_html subscripts branch["index"] — assigning the str
    raises TypeError and kills the render of EVERY conversation."""
    c = codex.parse_conversation(_thread())
    assert all(t.branch is None for t in c.turns)
    # preserved, just not there. Pinned FIELD-BY-FIELD: an existence-anywhere check
    # on json.dumps(c.meta) is satisfied by turn_meta[].branch independently, so it
    # asserts nothing about meta["branch"] itself.
    assert c.meta["branch"] == "codex/feature"
    assert [m["branch"] for m in c.meta["turn_meta"]] == ["codex/feature"]
    render_html.render_conversation_html(c)               # must not raise


def test_conversation_branch_is_the_first_non_empty_turn_branch():
    """The first assistant turn deliberately carries NO branch, so only a
    first-NON-EMPTY selection can produce the asserted value — a first-element or a
    constant-empty implementation both fail here."""
    c = codex.parse_conversation(_thread(turns=[
        _user_turn(), _asst_turn(tid="a1", branch=""),
        _user_turn(tid="u2"), _asst_turn(tid="a2", branch="codex/second")]))
    assert c.meta["branch"] == "codex/second"
    assert [m["branch"] for m in c.meta["turn_meta"]] == ["", "codex/second"]


def test_turn_provenance_is_preserved_in_conversation_meta():
    c = codex.parse_conversation(_thread())
    tm = c.meta["turn_meta"][0]
    assert tm["previous_turn_id"] == "task_u1"
    assert tm["external_pull_request_id"] == "abcd"
    assert tm["pull_request_status"] == "not_created"


def test_thread_with_no_assistant_turn_still_builds_meta():
    c = codex.parse_conversation(_thread(turns=[_user_turn()]))
    assert c.meta["branch"] == "" and c.meta["turn_meta"] == []


def test_parse_export_handles_a_list_of_threads():
    out = codex.parse_export([_thread(tid="a", title="A"), _thread(tid="b", title="B")])
    assert [c.title for c in out] == ["A", "B"]


def test_parse_export_wraps_a_single_thread_dict_and_skips_junk():
    assert [c.id for c in codex.parse_export(_thread(tid="solo"))] == ["solo"]
    assert codex.parse_export(["not a dict", 42]) == []


def test_non_dict_turn_is_skipped_not_fatal():
    c = codex.parse_conversation(_thread(turns=["a string", _user_turn()]))
    assert len(c.turns) == 1


# ----------------------------------------------------------------- turn_status

def test_enum_prefixed_status_is_normalised_not_compared_raw():
    """The literal JSON value is 'TaskTurnStatusEnum.FAILED'. `status == "FAILED"`
    matches 0 of 102 turns, so all 16 failed tasks would render as successes."""
    c = codex.parse_conversation(_thread(turns=[
        _user_turn(), _asst_turn(status="TaskTurnStatusEnum.FAILED", items=[])]))
    b = c.turns[1].blocks[0]
    assert b.type == "tool_result"
    assert b.data["name"] == "task FAILED" and b.data["is_error"] is True


def test_failed_turn_with_zero_output_items_still_emits_its_turn():
    """22 assistant turns (16 FAILED + 4 IN_PROGRESS + 2 CANCELLED) have
    output_items == []. chatgpt.py's `if not blocks: continue` idiom would delete
    22 of 204 turns and leave 22 of 97 threads showing a prompt with no reply."""
    c = codex.parse_conversation(_thread(turns=[
        _user_turn(), _asst_turn(status="TaskTurnStatusEnum.FAILED", items=[])]))
    assert [t.role for t in c.turns] == ["human", "assistant"]
    assert len(c.turns[1].blocks) >= 1


def test_in_progress_and_cancelled_are_labelled_but_are_not_errors():
    for status, name in (("TaskTurnStatusEnum.IN_PROGRESS", "task IN_PROGRESS"),
                         ("TaskTurnStatusEnum.CANCELLED", "task CANCELLED")):
        c = codex.parse_conversation(_thread(turns=[
            _user_turn(), _asst_turn(status=status, items=[])]))
        b = c.turns[1].blocks[0]
        assert b.data["name"] == name and b.data["is_error"] is False


def test_completed_turn_carries_no_status_block():
    c = codex.parse_conversation(_thread())
    assert [b.type for b in c.turns[1].blocks] == ["text"]


def test_an_unprefixed_status_value_also_normalises():
    """The prefix is a leaked enum repr; a future export emitting a bare value
    must not start reporting every task as an unknown state."""
    c = codex.parse_conversation(_thread(turns=[
        _user_turn(), _asst_turn(status="FAILED", items=[])]))
    assert c.turns[1].blocks[0].data["name"] == "task FAILED"


def test_the_provenance_copy_of_the_status_is_normalised_too_not_just_the_header():
    """The status is read TWICE — once for the block header and once inside
    _provenance, which render_html JSON-dumps into all 22 status blocks. Asserting
    only the header leaves the second call site blind, and a raw read there prints
    the literal 'TaskTurnStatusEnum.FAILED' on the page: the exact leak this
    module's trap exists to kill, with the header still reading correctly."""
    c = codex.parse_conversation(_thread(turns=[
        _user_turn(), _asst_turn(status="TaskTurnStatusEnum.FAILED", items=[])]))
    assert c.meta["turn_meta"][0]["turn_status"] == "FAILED"
    html = render_html.render_conversation_html(c)
    assert '"turn_status": "FAILED"' in html               # not merely absent
    assert "TaskTurnStatusEnum" not in html


# ------------------------------------------------------------------ user inputs

def test_message_text_parts_become_text_blocks():
    c = codex.parse_conversation(_thread(turns=[_user_turn()]))
    b = c.turns[0].blocks[0]
    assert b.type == "text" and b.text == "the question"


def test_whitespace_only_text_part_is_skipped():
    c = codex.parse_conversation(_thread(turns=[_user_turn(parts=[
        {"content_type": "text", "text": "   "},
        {"content_type": "text", "text": "real"}])]))
    assert [b.text for b in _blocks(c)] == ["real"]


def test_non_list_message_content_is_tolerated():
    c = codex.parse_conversation(_thread(turns=[_user_turn(items=[
        {"type": "message", "role": "user", "content": "not a list"}])]))
    assert _blocks(c) == []


def test_non_dict_content_part_is_skipped():
    c = codex.parse_conversation(_thread(turns=[_user_turn(
        parts=["a bare string", {"content_type": "text", "text": "real"}])]))
    assert [b.text for b in _blocks(c)] == ["real"]


def test_custom_instructions_are_preserved_but_not_attributed_as_user_prose():
    """A system preamble is real content, but rendering it as text would
    misattribute it to something the user typed into this thread."""
    c = codex.parse_conversation(_thread(turns=[
        _user_turn(custom_instructions="always use tabs")]))
    b = c.turns[0].blocks[0]
    assert b.type == "unknown" and b.data["orig_type"] == "custom_instructions"
    assert b.data["x_raw"] == "always use tabs"


def test_null_custom_instructions_emit_nothing():
    c = codex.parse_conversation(_thread(turns=[_user_turn(custom_instructions=None)]))
    assert _kinds(c) == ["text"]


def test_pull_request_info_yields_prose_plus_a_structural_tool_use():
    c = codex.parse_conversation(_thread(turns=[_user_turn(items=[
        {"type": "pull_request_info", "title": "PR title", "body": "PR body",
         "base_ref": "main", "head_ref": "codex/x", "merge_commit_sha": None}])]))
    blocks = _blocks(c)
    assert [b.type for b in blocks] == ["text", "text", "tool_use"]
    assert [b.text for b in blocks[:2]] == ["PR title", "PR body"]
    assert blocks[2].data["input"]["base_ref"] == "main"


def test_pull_request_info_without_prose_still_emits_its_refs():
    c = codex.parse_conversation(_thread(turns=[_user_turn(items=[
        {"type": "pull_request_info", "title": "", "body": None,
         "base_ref": "main", "head_ref": "h", "merge_commit_sha": None}])]))
    assert _kinds(c) == ["tool_use"]


def test_ide_context_string_is_preserved_verbatim():
    """context is a plain STRING (n=1) with unclear semantics — preserve it under a
    truthful label rather than guess a render."""
    c = codex.parse_conversation(_thread(turns=[_user_turn(items=[
        {"type": "ide_context", "context": "open file: a.py"}])]))
    b = c.turns[0].blocks[0]
    assert b.type == "unknown" and b.data["orig_type"] == "ide_context"
    assert b.data["x_raw"]["context"] == "open file: a.py"


def test_prior_conversation_is_preserved_not_spliced_into_this_threads_turns():
    """It holds 354 messages of a DIFFERENT thread quoted as context. Splicing them
    into Conversation.turns would fabricate this thread's history; dropping them
    would lose the largest embedded prose payload."""
    quoted = [{"role": "assistant", "type": "message",
               "content": [{"content_type": "text", "text": "earlier reply"}]}]
    c = codex.parse_conversation(_thread(turns=[_user_turn(items=[
        {"type": "prior_conversation", "conversation": quoted, "diff": None,
         "prior_task_id": None}])]))
    assert len(c.turns) == 1                          # not turned into extra turns
    b = c.turns[0].blocks[0]
    assert b.type == "unknown" and b.data["orig_type"] == "prior_conversation"
    assert b.data["x_raw"]["conversation"] == quoted


def test_unknown_input_item_type_is_preserved():
    c = codex.parse_conversation(_thread(turns=[_user_turn(items=[
        {"type": "some_future_item", "payload": 1}])]))
    assert _kinds(c) == ["unknown"]


def test_non_dict_input_item_is_skipped():
    c = codex.parse_conversation(_thread(turns=[_user_turn(items=["junk"])]))
    assert _blocks(c) == []


# -------------------------------------------------------------------- citations

def test_repo_file_citation_annotates_the_preceding_text_block():
    """477 repo_file_citation parts carry NO 'text' key; reading part['text'] with a
    default drops every one of them. Measured order is text|cite|cite|text|cite."""
    c = codex.parse_conversation(_thread(turns=[_user_turn(parts=[
        {"content_type": "text", "text": "see this"},
        {"content_type": "repo_file_citation", "path": "src/a.py",
         "line_range_start": 10, "line_range_end": 20}])]))
    blocks = _blocks(c)
    assert [b.type for b in blocks] == ["text"]
    assert blocks[0].citations == [{"title": "src/a.py:L10-L20"}]


def test_repo_file_citation_with_no_preceding_prose_becomes_a_named_chip():
    c = codex.parse_conversation(_thread(turns=[_user_turn(parts=[
        {"content_type": "repo_file_citation", "path": "src/a.py",
         "line_range_start": 1, "line_range_end": 2}])]))
    b = _blocks(c)[0]
    assert b.type == "file" and b.data["file_name"] == "src/a.py:L1-L2"


def test_terminal_chunk_citation_annotates_the_preceding_text_block():
    """The referenced terminal output is NOT in the export — a title-only pill
    states that honestly instead of fabricating the content."""
    c = codex.parse_conversation(_thread(turns=[_user_turn(parts=[
        {"content_type": "text", "text": "ran it"},
        {"content_type": "terminal_chunk_citation", "terminal_chunk_id": "ab12cd",
         "line_range_start": 3, "line_range_end": 9}])]))
    assert _blocks(c)[0].citations == [{"title": "terminal ab12cd:L3-L9"}]


def test_terminal_chunk_citation_with_no_preceding_prose_becomes_a_chip():
    c = codex.parse_conversation(_thread(turns=[_user_turn(parts=[
        {"content_type": "terminal_chunk_citation", "terminal_chunk_id": "z9",
         "line_range_start": 1, "line_range_end": 4}])]))
    assert _blocks(c)[0].type == "file"


def test_image_citation_keeps_the_full_pointer_so_no_broken_img_is_emitted():
    """Taking the basename (chatgpt.py's idiom) makes render_html emit
    <img src="../media/NAME"> for media this corpus does not ship — 6 broken loads.
    A pointer with a scheme keeps it on the inert chip branch."""
    ptr = "sediment://file_00001"
    c = codex.parse_conversation(_thread(turns=[_user_turn(parts=[
        {"content_type": "image_asset_pointer_citation", "asset_pointer": ptr,
         "width": 100, "height": 50, "size_bytes": 999}])]))
    b = _blocks(c)[0]
    assert b.type == "media" and b.data["path"] == ptr and b.data["width"] == 100
    html = render_html.render_conversation_html(c)
    assert "<img" not in html and ptr in html


def test_unknown_content_part_type_is_preserved():
    c = codex.parse_conversation(_thread(turns=[_user_turn(parts=[
        {"content_type": "some_future_part", "x": 1}])]))
    b = _blocks(c)[0]
    assert b.type == "unknown" and b.data["orig_type"] == "some_future_part"


# ------------------------------------------------------------- output: snapshots

def test_repo_snapshot_becomes_a_collapsed_attachment_with_range_markers():
    """file.line_range_contents[].content is a LIST of line strings — 274,077 lines
    / 10.8 MB, roughly half the export. A snapshot is a set of DISJOINT excerpts, so
    each range keeps its own marker; concatenating bare implies contiguous code."""
    c = codex.parse_conversation(_thread(turns=[_user_turn(), _asst_turn(items=[
        _snapshot(lrcs=[{"line_range_start": 1, "line_range_end": 2,
                         "contains_end_of_file": False, "content": ["aa", "bb"]},
                        {"line_range_start": 40, "line_range_end": 41,
                         "contains_end_of_file": True, "content": ["yy", "zz"]}])])]))
    b = c.turns[1].blocks[0]
    assert b.type == "attachment" and b.data["file_name"] == "src/app.py"
    assert b.data["file_type"] == "py"
    body = b.data["extracted_content"]
    assert "L1-L2" in body and "L40-L41" in body
    assert "aa\nbb" in body and "yy\nzz" in body
    assert b.data["ranges"][1]["contains_end_of_file"] is True


def test_attachment_file_size_measures_its_body_so_md_prints_real_byte_counts():
    """render_md falls back to a literal '?' whenever file_size is falsy, so a size
    that does not measure the body puts '? bytes' on all 296 attachments — the
    symptom the field exists to prevent. Pinned RELATIONALLY to the body rather than
    to a magic number, so it rejects any constant and survives fixture edits."""
    c = codex.parse_conversation(_thread(turns=[
        _user_turn(), _asst_turn(items=[_snapshot()])]))
    b = c.turns[1].blocks[0]
    assert b.data["file_size"] == len(b.data["extracted_content"]) > 0
    md = render_md.render_conversation_md(c)
    assert "(py, %d bytes)" % b.data["file_size"] in md
    assert "? bytes" not in md


def test_snapshot_body_survives_a_non_string_line_and_a_non_list_content():
    c = codex.parse_conversation(_thread(turns=[_user_turn(), _asst_turn(items=[
        _snapshot(lrcs=[{"line_range_start": 1, "line_range_end": 3,
                         "content": ["ok", 12345, "fine"]},
                        {"line_range_start": 5, "line_range_end": 5,
                         "content": "already flat"},
                        "not a dict"])])]))
    body = c.turns[1].blocks[0].data["extracted_content"]
    assert "ok\nfine" in body and "already flat" in body


def test_snapshot_prefers_an_explicit_contents_string_when_a_future_export_has_one():
    """contents is null 298/298 today, so this branch is unexercised by the corpus —
    but a future export that populates it must not render an empty attachment."""
    c = codex.parse_conversation(_thread(turns=[_user_turn(), _asst_turn(items=[
        _snapshot(contents="WHOLE FILE BODY")])]))
    assert c.turns[1].blocks[0].data["extracted_content"] == "WHOLE FILE BODY"


def test_snapshot_missing_reason_becomes_a_visible_error_not_a_silent_gap():
    """`file` would print a fixed '(no bytes in export)' and lose the actual reason."""
    c = codex.parse_conversation(_thread(turns=[_user_turn(), _asst_turn(items=[
        _snapshot(missing_reason="unknown_error")])]))
    b = c.turns[1].blocks[0]
    assert b.type == "tool_result" and b.data["is_error"] is True
    assert b.data["content"] == "unknown_error"
    assert "src/app.py" in b.data["name"]


def test_extensionless_snapshot_path_yields_an_empty_file_type():
    c = codex.parse_conversation(_thread(turns=[_user_turn(), _asst_turn(items=[
        _snapshot(path=".gitignore")])]))
    assert c.turns[1].blocks[0].data["file_type"] == ""


def test_non_dict_snapshot_file_entry_is_skipped():
    c = codex.parse_conversation(_thread(turns=[_user_turn(), _asst_turn(items=[
        {"type": "partial_repo_snapshot", "files": ["junk"]}])]))
    assert c.turns[1].blocks == []


# ------------------------------------------------------------------- output: pr

def test_pr_emits_title_message_commit_message_and_stats():
    c = codex.parse_conversation(_thread(turns=[_user_turn(), _asst_turn(items=[_pr()])]))
    blocks = c.turns[1].blocks
    texts = [b.text for b in blocks if b.type == "text"]
    assert texts == ["Add a thing", "This PR adds a thing.", "commit the thing"]
    stats = [b for b in blocks if b.data.get("name") == "pr_diff_stats"][0]
    assert stats.type == "tool_use"
    assert stats.data["input"]["lines_added"] == 10
    assert "preapply_patch_base_commit_sha" in stats.data["input"]


def test_pr_metadata_block_carries_the_turn_level_pr_fields():
    c = codex.parse_conversation(_thread(turns=[_user_turn(), _asst_turn(items=[_pr()])]))
    meta = [b for b in c.turns[1].blocks if b.data.get("name") == "pull_request"][0]
    assert meta.data["input"]["external_pull_request_id"] == "abcd"
    assert meta.data["input"]["branch"] == "codex/feature"


def test_pr_metadata_input_dict_is_pinned_key_for_key():
    """render_html JSON-dumps this whole dict onto the page for all 62 pr items, but
    a per-key subscript assertion is structurally blind to a rename of a key it never
    reads. Exact-dict equality compares KEY SETS as well as values, so any rename,
    addition or drop among the five fails. Non-default values throughout, so no
    hardcoded constant and no value read from the wrong field can satisfy it."""
    c = codex.parse_conversation(_thread(turns=[
        _user_turn(), _asst_turn(items=[_pr()], pr_status="created",
                                 branch_name="codex/bn")]))
    meta = [b for b in c.turns[1].blocks if b.data.get("name") == "pull_request"][0]
    assert meta.data["input"] == {"pull_request_status": "created",
                                  "external_pull_request_id": "abcd",
                                  "branch": "codex/feature",
                                  "branch_name": "codex/bn",
                                  "previous_turn_id": "task_u1"}
    html = render_html.render_conversation_html(c)
    assert '"pull_request_status": "created"' in html


def test_pr_without_prose_fields_still_emits_its_structure():
    """Empty title/message/commit_message and no diff of either kind: the PR must
    still surface as its two structural blocks, not vanish."""
    c = codex.parse_conversation(_thread(turns=[_user_turn(), _asst_turn(items=[
        _pr(title="", message=None, output_diff=_output_diff(commit_message=""))])]))
    blocks = c.turns[1].blocks
    assert [b.type for b in blocks] == ["tool_use", "tool_use"]
    assert [b.data["name"] for b in blocks] == ["pr_diff_stats", "pull_request"]


def test_inline_diff_becomes_a_code_block_not_prose():
    """Routing a diff to `text` would let the markdown pass mangle leading +/-/#
    AND drag every diff token into the prose fidelity gate."""
    c = codex.parse_conversation(_thread(turns=[_user_turn(), _asst_turn(items=[
        _pr(output_diff=_output_diff(diff="--- a\n+++ b\n+added"))])]))
    code = [b for b in c.turns[1].blocks if b.type == "code"][0]
    assert code.data["language"] == "diff" and "+added" in code.text


def test_externalised_diff_is_declared_absent_not_rendered_as_empty():
    """58 of 62 PR diffs were OFFLOADED — no diff text is in the export at all. An
    empty code block would instead imply 'no changes'."""
    c = codex.parse_conversation(_thread(turns=[_user_turn(), _asst_turn(items=[
        _pr(output_diff=_output_diff(
            external={"file_id": "f-77", "ttl": 3600}))])]))
    f = [b for b in c.turns[1].blocks if b.type == "file"][0]
    assert "f-77" in f.data["file_name"] and "3600" in f.data["file_name"]
    assert "no bytes in export" in render_html.render_conversation_html(c)


def test_diff_absent_entirely_emits_neither_code_nor_a_file_chip():
    c = codex.parse_conversation(_thread(turns=[_user_turn(), _asst_turn(items=[
        _pr(output_diff=_output_diff(diff=None, external=None))])]))
    kinds = [b.type for b in c.turns[1].blocks]
    assert "code" not in kinds and "file" not in kinds


def test_pre_apply_patch_becomes_a_diff_code_block_when_populated():
    """Null 62/62 today; the branch must exist so a populated future export is
    not dropped."""
    c = codex.parse_conversation(_thread(turns=[_user_turn(), _asst_turn(items=[
        _pr(pre_apply_patch="--- x\n+++ y")])]))
    assert any(b.type == "code" and "+++ y" in b.text for b in c.turns[1].blocks)


def test_output_diff_that_is_not_a_dict_is_preserved_as_unknown():
    c = codex.parse_conversation(_thread(turns=[_user_turn(), _asst_turn(items=[
        _pr(output_diff="a flat diff string")])]))
    unk = [b for b in c.turns[1].blocks if b.type == "unknown"][0]
    assert unk.data["orig_type"] == "output_diff"
    assert unk.data["x_raw"] == "a flat diff string"


def test_a_not_created_status_does_not_suppress_a_pr_item():
    """28 'not_created' turns DO carry a pr item and 1 'externally_created' turn does
    not — the status does not predict presence, so presence decides."""
    c = codex.parse_conversation(_thread(turns=[
        _user_turn(), _asst_turn(pr_status="not_created", items=[_pr()])]))
    assert any(b.type == "text" and b.text == "Add a thing" for b in c.turns[1].blocks)


# ---------------------------------------------------------------- output: order

def test_output_items_keep_their_source_order():
    """Observed orders include pr|message|snapshot — the reply text must keep its
    position relative to the PR rather than be re-sorted."""
    c = codex.parse_conversation(_thread(turns=[_user_turn(), _asst_turn(items=[
        _pr(title="PRTITLE", message=""),
        {"type": "message", "role": "assistant",
         "content": [{"content_type": "text", "text": "REPLYTEXT"}]},
        _snapshot()])]))
    texts = [b.text for b in c.turns[1].blocks if b.type == "text"]
    assert texts.index("PRTITLE") < texts.index("REPLYTEXT")
    assert c.turns[1].blocks[-1].type == "attachment"


def test_unknown_output_item_type_is_preserved():
    c = codex.parse_conversation(_thread(turns=[_user_turn(), _asst_turn(items=[
        {"type": "some_future_output", "payload": [1, 2]}])]))
    assert c.turns[1].blocks[0].data["orig_type"] == "some_future_output"


def test_non_dict_output_item_is_skipped():
    c = codex.parse_conversation(_thread(turns=[_user_turn(), _asst_turn(items=[42])]))
    assert c.turns[1].blocks == []


# ----------------------------------------------------------------- end-to-end

def test_a_failed_task_is_visibly_distinguishable_in_both_renderers():
    c = codex.parse_conversation(_thread(turns=[
        _user_turn(), _asst_turn(status="TaskTurnStatusEnum.FAILED", items=[])]))
    html = render_html.render_conversation_html(c)
    assert 'class="tool err"' in html and "task FAILED" in html
    md = render_md.render_conversation_md(c)
    assert "error" in md and "task FAILED" in md


def test_a_completed_task_is_not_flagged_as_an_error():
    c = codex.parse_conversation(_thread())
    assert 'class="tool err"' not in render_html.render_conversation_html(c)


def test_full_thread_survives_the_fidelity_gate(tmp_path):
    convs = codex.parse_export([_thread(turns=[
        _user_turn(custom_instructions="be brief"), _asst_turn(items=[
            {"type": "message", "role": "assistant",
             "content": [{"content_type": "text", "text": "prose that must survive"},
                         {"content_type": "repo_file_citation", "path": "a.py",
                          "line_range_start": 1, "line_range_end": 2}]},
            _snapshot(), _pr()])])])
    rep = build.render_corpus(convs, str(tmp_path), provider="codex")
    assert rep["rendered"] == 1 and rep["fidelity_passed"] == 1 and rep["errors"] == []


def test_codex_has_its_own_index_theme():
    assert build.THEMES["codex"]["title"] != build._FALLBACK_THEME["title"]


# --------------------------------------------------------------------- loaders

def _write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def test_load_codex_reads_a_single_file(tmp_path):
    src = str(tmp_path / "codex.json")
    _write(src, [_thread()])
    convs, errors = loaders.load_codex(src)
    assert len(convs) == 1 and not errors


def test_load_codex_reads_a_directory_and_skips_its_own_output(tmp_path):
    _write(str(tmp_path / "codex.json"), [_thread()])
    out = tmp_path / "site"
    out.mkdir()
    _write(str(out / "_fidelity-report.json"), {"not": "a thread"})
    convs, errors = loaders.load_codex(str(tmp_path), str(out))
    assert len(convs) == 1 and not errors


def test_load_codex_reports_malformed_json(tmp_path):
    (tmp_path / "codex.json").write_text("{not json", encoding="utf-8")
    convs, errors = loaders.load_codex(str(tmp_path))
    assert convs == [] and errors[0]["stage"] == "parse"


def test_load_codex_reports_an_adapter_failure(tmp_path, monkeypatch):
    _write(str(tmp_path / "codex.json"), [_thread()])
    monkeypatch.setattr(codex, "parse_export",
                        lambda d: (_ for _ in ()).throw(ValueError("boom")))
    convs, errors = loaders.load_codex(str(tmp_path))
    assert convs == [] and errors[0]["stage"] == "adapt"


# ------------------------------------------------------------------------- cli

def test_codex_end_to_end_through_the_cli(tmp_path):
    src = str(tmp_path / "codex.json")
    _write(src, [_thread(turns=[
        _user_turn(), _asst_turn(status="TaskTurnStatusEnum.FAILED", items=[])])])
    out = str(tmp_path / "site")
    assert cli.main(["codex", src, out]) == 0
    assert os.path.isfile(os.path.join(out, "index.html"))
    names = os.listdir(os.path.join(out, "html"))
    assert len(names) == 1
    page = open(os.path.join(out, "html", names[0]), encoding="utf-8").read()
    assert "task FAILED" in page
    rep = json.load(open(os.path.join(out, "_fidelity-report.json"), encoding="utf-8"))
    assert rep["rendered"] == 1 and rep["errors"] == []


def test_codex_cli_rejects_a_missing_path(tmp_path):
    assert cli.main(["codex", str(tmp_path / "nope.json"), str(tmp_path / "o")]) == 1
