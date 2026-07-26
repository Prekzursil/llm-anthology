"""Codex task export (codex.json) -> IR.

A THIRD ChatGPT-family shape that shares NOTHING with conversations.json: no
`mapping`, no `current_node`, no `messages`. Feeding it to the ChatGPT adapter
yields a silently EMPTY corpus — the same trap that lost the Claude design_chats.

Schema (probed from the real 20 MB export: 97 threads / 204 turns):
  thread          archived(bool), id, title, turns[]
  user turn       custom_instructions(str|null), id, input_items[], role="user"
  assistant turn  branch(str), branch_name(str|null), external_pull_request_id(str),
                  id, output_items[], previous_turn_id(str), pull_request_status(str),
                  role="assistant", turn_status(str)
  input_items[].type   message 102 · pull_request_info 3 · ide_context 1 ·
                       prior_conversation 1
  output_items[].type  message 80 · partial_repo_snapshot 80 · pr 62
  content part .content_type  text 461 · repo_file_citation 477 ·
                       terminal_chunk_citation 7 · image_asset_pointer_citation 6

Turns map 1:1 — a user turn and its assistant reply are SEPARATE ir.Turns, exactly
as in claude.py / gemini.py. All 97 threads start with role=user and roles strictly
alternate, so no pairing or tree walk is needed.

Five measured traps this adapter is built around:

 1. turn_status ships a LEAKED PYTHON ENUM REPR — the literal JSON strings are
    'TaskTurnStatusEnum.FAILED' etc. `status == "FAILED"` therefore matches 0 of
    102 turns and every one of the 16 failed tasks would render as a success. The
    value is normalised (`rsplit('.', 1)[-1]`) before any comparison.
    (pull_request_status is NOT prefixed — the leak is specific to turn_status.)
 2. 22 assistant turns (16 FAILED + 4 IN_PROGRESS + 2 CANCELLED) have
    output_items == [] and so produce zero natural blocks. chatgpt.py's
    `if not blocks: continue` idiom would delete 22 of 204 turns and leave 22 of
    97 threads (23%) showing a prompt with no reply. Turns are emitted
    UNCONDITIONALLY, with a synthesized status block.
 3. turn['branch'] is a git branch NAME (str 102/102), not the IR's
    {"index","total"} dict. Assigning it to ir.Turn.branch raises TypeError at
    render_html's `turn.branch["index"]` — a TOTAL render failure, not a silent
    one. It is kept in Conversation.meta and on the pr/status blocks instead.
 4. repo_file_citation parts carry NO 'text' key (477/477) — reading part['text']
    drops all of them. They are inline anchors on the preceding prose run, which
    is what ir.Block.citations exists for.
 5. partial_repo_snapshot holds its bodies one nesting level below where a reader
    stops: file.line_range_contents[].content is a LIST of line strings —
    274,077 lines / 10.8 MB, roughly HALF the export. `file.contents` is null
    298/298, so treating the snapshot as structural noise discards all of it.

There are NO timestamps anywhere in the file (a recursive key scan for
time/_at/date/created/updated/stamp returns zero hits), so times are left EMPTY
rather than invented.

external_pull_request_id is a PLACEHOLDER, not proof a PR exists: all 67
'not_created' turns share ONE 4-char value while the 32 'created' turns carry 32
distinct ones. PR presence is decided by the pr ITEM, never by that field or by
pull_request_status (28 'not_created' turns do carry a pr item).
"""
import os

from llm_anthology import ir

_COMPLETED = "COMPLETED"
_FAILED = "FAILED"
# output_diff's structural metadata; every key is emitted, nulls included, so an
# absent value is explicit rather than inferred from its absence.
_DIFF_STAT_KEYS = ("base_commit_sha", "repo_id", "type", "files_modified",
                   "lines_added", "lines_removed", "additional_parent_commit_shas",
                   "preapply_patch_base_commit_sha")


def _s(x):
    """Coerce a display field to str. Codex nulls several string fields
    (custom_instructions 71/102, branch_name 70/102, missing_reason 296/298,
    output_diff.diff 58/62, pre_apply_patch 62/62)."""
    return x if isinstance(x, str) else ""


def parse_export(data):
    """A whole codex.json (a list of threads, or a single thread dict) -> [Conversation]."""
    threads = data if isinstance(data, list) else [data]
    return [parse_conversation(t) for t in threads if isinstance(t, dict)]


def parse_conversation(thread):
    turns, turn_meta = [], []
    for t in (thread.get("turns") or []):
        if not isinstance(t, dict):
            continue
        if _s(t.get("role")) == "user":
            role, blocks = "human", _user_blocks(t)
        else:
            role, blocks = "assistant", _assistant_blocks(t)
            turn_meta.append(_provenance(t))
        # UNCONDITIONAL: 22 assistant turns carry zero output items, and dropping a
        # block-less turn would delete their thread's entire reply side.
        turns.append(ir.Turn(role=role, blocks=blocks, uuid=_s(t.get("id")),
                             timestamp="", branch=None))
    branches = [m["branch"] for m in turn_meta if m["branch"]]
    return ir.Conversation(
        id=_s(thread.get("id")),
        title=_s(thread.get("title")) or "(untitled)",
        provider="codex",
        turns=turns,
        created_at="", updated_at="",
        # archived is RECORDED, not obeyed: it is False 97/97, so filtering on it
        # is untestable here and would silently empty a future export.
        meta={"archived": bool(thread.get("archived")),
              "branch": branches[0] if branches else "",
              "turn_meta": turn_meta},
    )


def _status(turn):
    """'TaskTurnStatusEnum.FAILED' -> 'FAILED'; a bare 'FAILED' passes through."""
    return _s(turn.get("turn_status")).rsplit(".", 1)[-1]


def _provenance(turn):
    """Per-turn provenance. ir.Turn has no free-form field and the order is already
    positional, so these travel in Conversation.meta (and on the status/pr blocks)
    rather than being dropped."""
    return {"id": _s(turn.get("id")), "turn_status": _status(turn),
            "pull_request_status": _s(turn.get("pull_request_status")),
            "branch": _s(turn.get("branch")),
            "branch_name": _s(turn.get("branch_name")),
            "previous_turn_id": _s(turn.get("previous_turn_id")),
            "external_pull_request_id": _s(turn.get("external_pull_request_id"))}


# ------------------------------------------------------------------ user inputs

def _user_blocks(turn):
    blocks = []
    ci = _s(turn.get("custom_instructions"))
    if ci.strip():
        # Real content, but a system preamble — NOT prose the user typed into this
        # thread. `unknown` prints a truthful label plus the full payload, so
        # nothing is lost and nothing is misattributed.
        blocks.append(ir.Block("unknown", data={"orig_type": "custom_instructions",
                                                "x_raw": ci}))
    for item in (turn.get("input_items") or []):
        blocks.extend(_input_blocks(item))
    return blocks


def _input_blocks(item):
    if not isinstance(item, dict):
        return []
    itype = _s(item.get("type"))
    if itype == "message":
        return _content_blocks(item.get("content"))
    if itype == "pull_request_info":
        return _pr_info_blocks(item)
    # ide_context.context is a plain STRING with unclear semantics (n=1), and
    # prior_conversation.conversation quotes 354 messages of a DIFFERENT thread —
    # splicing those into this thread's turns would fabricate its history, while
    # dropping them would lose the largest embedded prose payload. Preserve both
    # verbatim under a truthful label, along with any future item type.
    return [ir.Block("unknown", data={"orig_type": itype, "x_raw": item})]


def _pr_info_blocks(item):
    """A PR title + description is authored prose a reader must see (and that must
    sit under the fidelity gate); the refs are structural."""
    blocks = [ir.Block("text", text=_s(item.get(k)))
              for k in ("title", "body") if _s(item.get(k)).strip()]
    blocks.append(ir.Block("tool_use", text="", data={
        "name": "pull_request_info",
        "input": {"base_ref": item.get("base_ref"), "head_ref": item.get("head_ref"),
                  "merge_commit_sha": item.get("merge_commit_sha")}}))
    return blocks


# --------------------------------------------------------------- message content

def _cite_repo_file(part):
    return "%s:L%s-L%s" % (_s(part.get("path")), part.get("line_range_start"),
                           part.get("line_range_end"))


def _cite_terminal(part):
    # the referenced terminal output is NOT in the export — a title-only pill says
    # so honestly instead of fabricating content
    return "terminal %s:L%s-L%s" % (_s(part.get("terminal_chunk_id")),
                                    part.get("line_range_start"),
                                    part.get("line_range_end"))


_CITE_LABEL = {"repo_file_citation": _cite_repo_file,
               "terminal_chunk_citation": _cite_terminal}


def _content_blocks(content):
    """message.content is a LIST of parts (102/102 in, 80/80 out).

    Measured part order is text|cite|cite|text|cite… — the citation parts are inline
    anchors annotating the PRECEDING prose run, so they attach to that block rather
    than becoming standalone noise.
    """
    if not isinstance(content, list):
        return []
    blocks, last_text = [], None
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = _s(part.get("content_type"))
        if ptype == "text":
            body = _s(part.get("text"))
            if body.strip():                       # 3 whitespace-only parts on disk
                last_text = ir.Block("text", text=body)
                blocks.append(last_text)
        elif ptype in _CITE_LABEL:
            label = _CITE_LABEL[ptype](part)
            if last_text is None:                  # no prose to annotate
                blocks.append(ir.Block("file", text=label, data={"file_name": label}))
            else:
                last_text.citations.append({"title": label})
        elif ptype == "image_asset_pointer_citation":
            blocks.append(_image_block(part))
        else:
            blocks.append(ir.Block("unknown", data={"orig_type": ptype, "x_raw": part}))
    return blocks


def _image_block(part):
    """Keep the FULL sediment:// pointer. Taking the basename (chatgpt.py's idiom)
    would make render_html emit <img src="../media/NAME"> for media this corpus does
    not ship — 6 broken loads; a scheme keeps it on the inert chip branch."""
    ptr = _s(part.get("asset_pointer"))
    return ir.Block("media", text=ptr,
                    data={"path": ptr, "width": part.get("width"),
                          "height": part.get("height"),
                          "size_bytes": part.get("size_bytes")})


# --------------------------------------------------------------- assistant output

def _assistant_blocks(turn):
    blocks = []
    status = _status(turn)
    if status != _COMPLETED:
        blocks.append(_status_block(turn, status))
    for item in (turn.get("output_items") or []):
        blocks.extend(_output_blocks(item, turn))
    return blocks


def _status_block(turn, status):
    """tool_result is the ONLY block type with an error affordance — is_error drives
    render_html's `class="tool err"` + ⚠️ head and render_md's '⚠️ error' — so a
    FAILED task is visibly distinguishable. IN_PROGRESS/CANCELLED are states, not
    failures, so they are named but not flagged."""
    return ir.Block("tool_result", text="",
                    data={"name": "task " + status, "is_error": status == _FAILED,
                          "content": _provenance(turn)})


def _output_blocks(item, turn):
    if not isinstance(item, dict):
        return []
    otype = _s(item.get("type"))
    if otype == "message":
        return _content_blocks(item.get("content"))
    if otype == "partial_repo_snapshot":
        return _snapshot_blocks(item)
    if otype == "pr":
        return _pr_blocks(item, turn)
    return [ir.Block("unknown", data={"orig_type": otype, "x_raw": item})]


# ------------------------------------------------------------------- pr items

def _pr_blocks(item, turn):
    blocks = [ir.Block("text", text=_s(item.get(k)))
              for k in ("pr_title", "pr_message") if _s(item.get(k)).strip()]
    patch = _s(item.get("pre_apply_patch"))
    if patch.strip():                              # null 62/62 today
        blocks.append(ir.Block("code", text=patch, data={"language": "diff"}))
    blocks.extend(_diff_blocks(item.get("output_diff")))
    blocks.append(ir.Block("tool_use", text="", data={
        "name": "pull_request",
        "input": {"pull_request_status": _s(turn.get("pull_request_status")),
                  "external_pull_request_id": _s(turn.get("external_pull_request_id")),
                  "branch": _s(turn.get("branch")),
                  "branch_name": _s(turn.get("branch_name")),
                  "previous_turn_id": _s(turn.get("previous_turn_id"))}}))
    return blocks


def _diff_blocks(od):
    """output_diff is a nested DICT 62/62, not a flat string."""
    if not isinstance(od, dict):
        return [ir.Block("unknown", data={"orig_type": "output_diff", "x_raw": od})]
    blocks = []
    msg = _s(od.get("commit_message"))
    if msg.strip():                                # distinct from pr_message in 61/62
        blocks.append(ir.Block("text", text=msg))
    diff = _s(od.get("diff"))
    external = od.get("external_storage_diff")
    if diff.strip():
        # code, not text: the markdown pass would mangle leading +/-/# AND drag
        # every diff token into the prose fidelity gate
        blocks.append(ir.Block("code", text=diff, data={"language": "diff"}))
    elif isinstance(external, dict):
        # THE content-absence fact: 58 of 62 PR diffs were OFFLOADED, so no diff
        # text is in the export at all. `file` renders '(no bytes in export)'; an
        # empty code block would instead imply 'no changes'.
        blocks.append(ir.Block("file", text="diff (external storage)", data={
            "file_name": "diff (external storage: file_id=%s, ttl=%s)"
                         % (external.get("file_id"), external.get("ttl"))}))
    blocks.append(ir.Block("tool_use", text="", data={
        "name": "pr_diff_stats", "input": {k: od.get(k) for k in _DIFF_STAT_KEYS}}))
    return blocks


# ------------------------------------------------------------- repo snapshots

def _snapshot_blocks(item):
    """298 file excerpts / 10.8 MB of repo source — the export's largest payload and
    the code its citations point into. `attachment` is the only branch that shows a
    NAME while keeping the body COLLAPSED in a <details>, and it stays out of the
    prose gate (source code is not prose)."""
    blocks = []
    for f in (item.get("files") or []):
        if not isinstance(f, dict):
            continue
        path = _s(f.get("path"))
        reason = _s(f.get("missing_reason"))
        if reason:
            # `file` would print a fixed '(no bytes in export)' and lose the reason
            blocks.append(ir.Block("tool_result", text="", data={
                "name": "repo_snapshot " + path, "is_error": True, "content": reason}))
            continue
        body, ranges = _snapshot_body(f)
        blocks.append(ir.Block("attachment", text=path, data={
            "file_name": path, "file_type": _ext(path), "file_size": len(body),
            "extracted_content": body, "ranges": ranges}))
    return blocks


def _snapshot_body(f):
    """file.contents is null 298/298, so the real bytes live one level down in
    line_range_contents[].content — a LIST of line strings. A snapshot is a set of
    DISJOINT excerpts, so each range is prefixed with its own marker; concatenating
    bare would imply contiguous code."""
    ranges, parts = [], []
    for lr in (f.get("line_range_contents") or []):
        if not isinstance(lr, dict):
            continue
        start, end = lr.get("line_range_start"), lr.get("line_range_end")
        ranges.append({"line_range_start": start, "line_range_end": end,
                       "contains_end_of_file": bool(lr.get("contains_end_of_file"))})
        parts.append("L%s-L%s\n%s" % (start, end, _lines(lr.get("content"))))
    contents = _s(f.get("contents"))
    # prefer an explicit whole-file body if a future export ever populates it
    return (contents or "\n".join(parts)), ranges


def _lines(content):
    if isinstance(content, list):
        return "\n".join(x for x in content if isinstance(x, str))
    return _s(content)


def _ext(path):
    """'' for the 8 dotfiles in the corpus (splitext gives a dotfile no extension)."""
    return os.path.splitext(path)[1].lstrip(".")
