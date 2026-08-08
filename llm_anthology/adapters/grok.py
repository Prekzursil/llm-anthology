"""Grok Build session store (~/.grok/sessions/<enc-cwd>/<session-id>/) -> IR.

xAI's coding-agent CLI keeps a LIVE local store, like Codex and unlike a downloaded
export: there is no export step, the files are written as the agent runs. One session
is a DIRECTORY, not a file — `summary.json` (metadata), `updates.jsonl` (the event
stream), and an OPTIONAL `subagents/<id>/meta.json` per spawned child. That last file
is why this adapter matters: with Codex it is the only source in this repository that
can populate `corpus.SpawnEdge`, the app's spawn graph.

Schema from `.scratch/GROK-SCHEMA.md`, extracted from a live store by a structure-only
probe (key names, protocol discriminants, value types, string LENGTHS — never values;
6 session directories, 4000 `updates.jsonl` lines, store size 2731 files):

  summary.json     agent_name, chat_format_version, created_at, current_model_id,
                   generated_title, grok_home, info{cwd,id}, last_active_at,
                   next_trace_turn, num_chat_messages, num_messages,
                   reasoning_effort, request_id, sandbox_profile, session_kind,
                   session_summary, updated_at
  updates.jsonl    one JSON-RPC envelope per line:
                   {method, params{_meta, sessionId, update}, timestamp}
                   method = `session/update` (ACP) | `_x.ai/session/update` (xAI)
                   the event type is params.update.sessionUpdate:
                     tool_call_update 2400 . hook_execution 686 . tool_call 395 .
                     agent_thought_chunk 278 . task_backgrounded 91 .
                     task_completed 88 . user_message_chunk 19 .
                     agent_message_chunk 16 . turn_completed 15 . session_recap 6 .
                     current_mode_update 2 . hooks_changed 1 . plugins_changed 1
  subagents/<id>/meta.json
                   child_cwd, child_session_id, completed_at, description,
                   duration_ms, effective_context_source, effective_model_id,
                   parent_session_id, prompt, started_at, status, subagent_id,
                   subagent_type, tool_calls, turns

Two places the PUBLIC documentation is wrong for this version, both established
against the real store, and both fatal if believed:

 A. `updates.jsonl` lines are JSON-RPC envelopes, NOT bare ACP `sessionUpdate`
    objects. A reader that looks for `sessionUpdate` at the top level finds nothing —
    measured, all 4000 sampled lines. The discriminant is nested two levels down at
    `params.update.sessionUpdate`.
 B. `summary.json` does NOT carry `parent_session_id`. The spawn relationship lives
    only in `subagents/<id>/meta.json`.

Seven traps this adapter is built around:

 1. SIGNAL TO NOISE. 35 message chunks against 2795 tool events in one 4000-line
    sample. Treating every line as a turn yields a corpus that is ~99% tool noise.
    The conversation is `user_message_chunk` + `agent_message_chunk`; everything else
    is either folded into the surrounding turn or COUNTED and dropped (see below).

 2. `_chunk` MEANS STREAMING. Consecutive chunks of one logical message coalesce into
    ONE block, or every message shatters into fragments. UNVERIFIED which delimiter
    the producer intends; this adapter closes a run on a change of chunk type, on any
    tool event, and on `turn_completed`, which is correct under both candidate
    hypotheses (see `CHUNK_JOIN` and `_Turns`).

 3. THE SPAWN GRAPH IS OPTIONAL. `subagents/` was present in 1 of 6 sampled sessions.
    A session without it must ingest normally, produce no edge, and log no error.

 4. `*.lock` FILES SIT BESIDE THE REAL ONES (`summary.json.lock`,
    `updates.jsonl.lock`). Every path here is an EXACT filename, never a prefix glob,
    so a lock can never be parsed as data.

 5. THE SESSION ID IS `summary.json` -> `info.id` — not the directory name, and there
    is no top-level `id`. Fallbacks exist (`params.sessionId`, then the directory
    name) because an id of "" collides with every other id-less session in the index,
    whose `conversation_id` column is UNIQUE.

 6. TIMESTAMPS ARE ISO STRINGS and the app works in epoch MILLISECONDS. The measured
    `created_at` is 30 characters — the length of an RFC-3339 stamp with NINE
    fractional digits — which `datetime.fromisoformat` REJECTS on Python 3.9, the
    oldest interpreter CI pins. The fraction is trimmed to 6 digits before parsing.
    An unparseable or absent stamp leaves the session undated (a state the app has a
    real affordance for), never dropped.

 7. NO ABORT ON A BAD SESSION. A live log is read while it is being written, so its
    last line can be truncated; a malformed line, a malformed `summary.json`, a
    malformed subagent meta, and an unreadable directory each cost exactly what they
    touch and are logged.

WHAT IS RENDERED, AND WHY (trap 1, stated explicitly because it is a judgement call):

  user_message_chunk / agent_message_chunk -> `text` blocks. The conversation.
  agent_thought_chunk                      -> `thinking` blocks, KEPT. This mirrors
      what `codex_rollout.py:156` does with Codex reasoning summaries, and
      `render_html.py:148` renders a thinking block inside a collapsed <details>, so
      278 thought chunks cost one disclosure triangle rather than burying the prose.
      Dropping them would discard real model output the renderer already handles.
  tool_call / tool_call_update             -> ONE `tool_use` block per distinct
      toolCallId, plus a `tool_result` only when output actually arrives. 2400
      updates against 395 calls means each call is re-reported ~6x; one block per
      event would be a 6x duplicate of the same logical call. Coalescing on
      toolCallId maps 1:1 onto the object identity the protocol itself models. Tool
      events NEVER open a turn.
  everything else (hook_execution, task_backgrounded, task_completed, session_recap,
      current_mode_update, hooks_changed, plugins_changed, and any FUTURE type)
                                           -> counted in meta['event_counts'], not
      rendered. They are the harness talking to itself. Counting rather than silently
      discarding is what lets an owner see that the format moved: a new event type
      appears as a count with no matching feature.

PRIVACY: tests use synthetic fixtures only; no real conversation content is committed,
and nothing in this module reads outside the session root it is given.
"""
import glob
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import unquote

from llm_anthology import corpus, ir

#: The ADAPTER label — which tool produced the transcript. It goes on
#: `Conversation.provider` and on the spawn-graph node; `sidecar.py:1665` surfaces it to
#: the UI as `provider`. `ThreadMeta` has no model field, so the model id travels in
#: `Conversation.meta['model_id']`.
#:
#: NOT for `ThreadMeta.model_provider` — that field is the MODEL VENDOR and takes
#: :data:`GROK_MODEL_VENDOR`. (The comment here previously defended putting this label in
#: that field on the grounds that the graph node surfaced `model_provider` as the thread's
#: provider. That WAS true when it was written — `git show a361759:llm_anthology/sidecar.py`
#: line 1390 reads `"provider": meta.model_provider` — and is no longer: `sidecar.py:1665`
#: surfaces `provider = meta.adapter` and passes `model_provider` separately. That old
#: anchor is deliberately spelled as a GIT REVISION and not as a live `<file>:<line>`,
#: because line 1390 of today's sidecar is unrelated code in `_metadata_search`: a reader
#: following it would land nowhere, and a live-looking citation into a former file state is
#: exactly the drift this repo pins against. The defence is recorded, and refuted, rather
#: than left to re-justify the same mistake.)
GROK_PROVIDER = "grok"

#: The MODEL VENDOR for `ThreadMeta.model_provider` — a DIFFERENT vocabulary from the
#: adapter label above, and `corpus.py` documents the field as the vendor: measured over
#: 250 real Codex rollouts `session_meta.model_provider` reads 'openai', never 'codex'.
#: Writing "grok" here put an adapter name in a vendor field, and it reached exports.
#:
#: INFERRED from the adapter, not read, because there is nothing to read: the measured
#: `summary.json` schema above records `current_model_id` and NO vendor field (6 live
#: session directories, structure-only probe). The `_x.ai/session/update` method namespace
#: in `updates.jsonl` names the PROTOCOL dialect, not the model's vendor, so it is not a
#: substitute. The model id that IS recorded still travels in
#: `Conversation.meta['model_id']`, so the read fact sits beside the inferred one rather
#: than being replaced by it.
#:
#: Chosen over "" deliberately — see :data:`CLAUDE_CODE_MODEL_VENDOR` in the sibling
#: adapter for the full argument; briefly, an empty vendor on every Grok node reproduces
#: the unknown-grey symptom the conflation caused, and `ThreadMeta.adapter` already carries
#: the which-tool distinction that "" would be protecting.
GROK_MODEL_VENDOR = "xai"

#: How the fragments of one streamed message are re-joined.
#:
#: EMPTY, i.e. pure concatenation, and this is the single riskiest constant here — so
#: it is a named constant a reader can flip in one edit rather than a buried literal.
#: The reasoning: if chunks are true streaming deltas, "" is the only lossless join
#: and "\n" would insert a break mid-word on every one of the ~18 thought deltas per
#: turn. If instead each chunk is a WHOLE message (the sample's 16 agent chunks
#: against 15 `turn_completed` events is suspiciously close to 1:1, so this is a live
#: possibility), "" runs two messages together — but only when one turn holds more
#: than one chunk of the same type, which that same 1:1 ratio makes rare. The failure
#: mode of "" is rare, the failure mode of "\n" is constant.
#: DETECTION: if rendered Grok text shows words jammed across a sentence boundary
#: ("...it is fixed.Now I will..."), the chunks were whole messages — set this to
#: "\n\n". UNVERIFIED against real data; settled by rendering one real session.
CHUNK_JOIN = ""

_HUMAN = "human"
_ASSISTANT = "assistant"

#: sessionUpdate -> (turn role, IR block type). These three ARE the conversation.
_CHUNK_ROLE = {
    "user_message_chunk": (_HUMAN, "text"),
    "agent_message_chunk": (_ASSISTANT, "text"),
    "agent_thought_chunk": (_ASSISTANT, "thinking"),
}
_TOOL_EVENTS = frozenset(("tool_call", "tool_call_update"))
_TURN_COMPLETED = "turn_completed"

#: The two files that identify a session DIRECTORY. Exact names, never prefixes, so
#: `summary.json.lock` and `updates.jsonl.lock` cannot be mistaken for data (trap 4).
_SESSION_FILES = ("summary.json", "updates.jsonl")

#: How far `_text_of` will follow a nested `content` wrapper before giving up. ACP
#: wraps tool output as {"type":"content","content":{...}}; three levels is well past
#: any shape the protocol defines and bounds a hostile/looping payload.
_MAX_CONTENT_DEPTH = 3

#: Trims an over-long fractional second to the 6 digits datetime accepts (trap 6).
_FRACTION = re.compile(r"(\.\d{6})\d+")


def _s(x):
    """Coerce a display field to str; a null/absent field becomes ''."""
    return x if isinstance(x, str) else ""


def _int(x):
    """Coerce a counter field to int; anything else becomes 0."""
    return x if isinstance(x, int) else 0


@dataclass
class GrokDoc:
    """One parsed session directory: the transcript, the thread-graph node, and any
    spawn edges its `subagents/` directory declared. `thread_id` is a convenience
    alias of the node id, matching `codex_rollout.RolloutDoc`."""
    conversation: object
    thread: object
    edges: list = field(default_factory=list)
    session_dir: str = ""

    @property
    def thread_id(self):
        return self.thread.id


# ------------------------------------------------------------------- time / id

def _iso_to_ms(s):
    """An ISO-8601 timestamp -> epoch milliseconds, or None if absent/unparseable.

    Two normalisations before parsing, each for a measured reason: the trailing 'Z' is
    rewritten to +00:00 because `fromisoformat` rejects it on the Python versions this
    project supports, and a fractional second longer than 6 digits is TRIMMED because
    Grok's stamps are 30 characters — nanosecond precision — which `fromisoformat`
    rejects on 3.9 (the oldest interpreter in the CI matrix). Without the trim every
    Grok session would come back undated there while passing locally on 3.14, which
    truncates silently.
    """
    if not isinstance(s, str) or not s:
        return None
    t = s[:-1] + "+00:00" if s.endswith("Z") else s
    t = _FRACTION.sub(r"\1", t, count=1)
    try:
        return int(datetime.fromisoformat(t).timestamp() * 1000)
    except ValueError:
        return None


def _dir_id(session_dir):
    """The session-id directory name, or '' — the LAST id fallback (trap 5)."""
    return os.path.basename(session_dir.rstrip("/\\"))


def _decode_cwd(session_dir):
    """The cwd recovered from the percent-encoded PARENT directory name.

    Only a fallback: `summary.json` -> `info.cwd` is the value itself, and reading a
    field beats decoding a name. UNVERIFIED that Grok's directory encoding is plain
    percent-encoding — `unquote` is a no-op on a name that carries no `%` escape, so a
    different scheme degrades to the raw name rather than to a wrong path.
    """
    return unquote(os.path.basename(os.path.dirname(session_dir.rstrip("/\\"))))


def _first_line(text, limit):
    """The first line of `text`, truncated to `limit`; '' for empty text."""
    if not text:
        return ""
    return text.splitlines()[0][:limit]


# ------------------------------------------------------------- content -> text

def _text_of(content, depth=0):
    """The visible prose inside an ACP `content` value.

    The spec records `content` as a KEY but NOT its inner shape, so every plausible
    form is accepted rather than one being guessed: a bare string, an ACP ContentBlock
    {"type":"text","text":...}, a LIST of those, and one level of ToolCallContent
    wrapping {"type":"content","content":{...}}. Committing to a single shape and
    being wrong would read the entire store as empty conversations, which is the
    failure mode this whole adapter exists to avoid.
    """
    if isinstance(content, str):
        return content
    if depth >= _MAX_CONTENT_DEPTH:
        return ""
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        return _text_of(content.get("content"), depth + 1)
    if isinstance(content, list):
        return "\n".join(p for p in (_text_of(x, depth + 1) for x in content) if p)
    return ""


# ------------------------------------------------------------ the turn machine

class _Turns:
    """Accumulates events into turns, coalescing streamed chunks (trap 2).

    Two levels of accumulation. A TURN is open for one role at a time and is closed by
    a role change or by `turn_completed`; a CHUNK RUN is open for one `sessionUpdate`
    type at a time and is closed by a different chunk type, by any non-chunk block, or
    by the turn closing. A turn that ends with no blocks is discarded rather than
    rendered as an empty bubble.

    Harness events deliberately do NOT flush: 686 `hook_execution` events interleave
    with real ones in the measured sample, and flushing on them would shatter every
    message they happen to fall inside.
    """

    def __init__(self):
        self.turns = []
        self.cur = None
        self.pending = None          # (sessionUpdate, [fragments]) or None

    def want(self, role, ts=""):
        """Ensure an open turn for `role`, closing one of the other role first."""
        if self.cur is not None and self.cur.role != role:
            self.close()
        if self.cur is None:
            self.cur = ir.Turn(role, [], timestamp=ts)

    def chunk(self, kind, text):
        """Append one streamed fragment to the run of `kind`, starting a new run (and
        closing any run of a different kind) as needed.

        Whitespace-only fragments are KEPT, not skipped: with an empty `CHUNK_JOIN` a
        lone " " delta is the space between two words, and dropping it would silently
        weld them together. Emptiness is judged once, on the joined result.
        """
        if self.pending is not None and self.pending[0] != kind:
            self.flush()
        if self.pending is None:
            self.pending = (kind, [])
        self.pending[1].append(text)

    def flush(self):
        """Close the open chunk run into one block, dropping an all-blank run."""
        if self.pending is None:
            return
        kind, parts = self.pending
        self.pending = None
        text = CHUNK_JOIN.join(parts)
        if text.strip():
            self.cur.blocks.append(ir.Block(_CHUNK_ROLE[kind][1], text=text))

    def add(self, block):
        """Append a non-chunk block, closing any open chunk run before it."""
        self.flush()
        self.cur.blocks.append(block)

    def close(self):
        """Close the open turn, keeping it only if it carries something."""
        self.flush()
        if self.cur is not None and self.cur.blocks:
            self.turns.append(self.cur)
        self.cur = None

    def done(self):
        self.close()
        return self.turns


# ---------------------------------------------------------------- tool traffic

def _tool_output(update):
    """The text a tool event reports back, from `rawOutput` and/or `content`.

    A `rawOutput` that is a structured result rather than a ContentBlock is dumped as
    JSON instead of being dropped — losing a tool's entire output because it was not
    shaped like prose is worse than showing its JSON. An EMPTY container yields
    nothing, so a `{}` placeholder does not manufacture a result block.
    """
    parts = []
    for key in ("rawOutput", "content"):
        value = update.get(key)
        text = _text_of(value)
        if not text and isinstance(value, (dict, list)) and value:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _apply_tool(turns, tools, update, anon):
    """Fold one `tool_call`/`tool_call_update` into the block for its toolCallId.

    The first sighting creates the `tool_use` block — an `update` whose opening call
    was never written (a resumed or truncated log) still gets one, because dropping it
    would lose the only record of that call. Later updates MUTATE that same block, and
    a field only overwrites when the event actually carries it, so a bare status ping
    cannot erase a title or an input that an earlier event established. A
    `tool_result` is created lazily, on the first event that brings real output, so a
    still-running or output-less call renders no empty box.

    Returns the id-less counter, which the caller carries forward.
    """
    cid = _s(update.get("toolCallId"))
    key = cid
    if not key:
        # An id-less call cannot be correlated with anything, so it must not silently
        # merge with the NEXT id-less call. The synthetic key carries a control
        # character, which no real toolCallId can contain, and never leaves this dict.
        anon += 1
        key = "\x00anon-%d" % anon

    entry = tools.get(key)
    if entry is None:
        use = ir.Block("tool_use", text="", data={
            "name": "tool", "input": None, "call_id": cid, "kind": "", "status": ""})
        turns.add(use)
        entry = tools[key] = {"use": use, "result": None}

    use = entry["use"]
    title = _s(update.get("title"))
    if title:
        use.text = title
        use.data["name"] = title
    kind = _s(update.get("kind"))
    if kind:
        use.data["kind"] = kind
    if update.get("rawInput") is not None:
        use.data["input"] = update.get("rawInput")
    status = _s(update.get("status"))
    if status:
        use.data["status"] = status

    output = _tool_output(update)
    if output:
        result = entry["result"]
        if result is None:
            result = entry["result"] = ir.Block("tool_result", text="", data={
                "name": use.data["name"], "call_id": cid, "content": "",
                "is_error": False})
            turns.add(result)
        result.data["content"] = output
        # ACP's status vocabulary is pending|in_progress|completed|failed. `status` is
        # a MEASURED key but its VALUES are not, so a vocabulary change degrades to
        # is_error=False rather than to a crash or a false alarm. UNVERIFIED; settled
        # by counting the distinct `status` strings in one real session.
        result.data["is_error"] = status == "failed"
    return anon


# ------------------------------------------------------------- document builder

def build_document(summary, records, subagents=(), session_dir=""):
    """A `summary.json` dict plus already-parsed `updates.jsonl` records -> GrokDoc.

    `records` are the RAW JSON-RPC envelopes, unwrapped here rather than by the
    caller, so correction A (the discriminant is at `params.update.sessionUpdate`, not
    at the top level) lives in one place and is covered by one test. Anything that is
    not that envelope shape is skipped: this reads a live file whose tail may be
    partial, so a record with no `params`, no `update`, or no discriminant is data to
    be survived, not a contract violation.
    """
    summary = summary if isinstance(summary, dict) else {}
    turns = _Turns()
    counts, tools = {}, {}
    first_ts = last_ts = ""
    session_hint = ""
    anon = 0

    for rec in records:
        if not isinstance(rec, dict):
            continue
        ts = _s(rec.get("timestamp"))
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        params = rec.get("params")
        if not isinstance(params, dict):
            continue
        session_hint = session_hint or _s(params.get("sessionId"))
        update = params.get("update")
        if not isinstance(update, dict):
            continue
        kind = _s(update.get("sessionUpdate"))
        if not kind:
            continue
        counts[kind] = counts.get(kind, 0) + 1

        chunk = _CHUNK_ROLE.get(kind)
        if chunk is not None:
            turns.want(chunk[0], ts)
            turns.chunk(kind, _text_of(update.get("content")))
        elif kind in _TOOL_EVENTS:
            turns.want(_ASSISTANT, ts)
            anon = _apply_tool(turns, tools, update, anon)
        elif kind == _TURN_COMPLETED:
            turns.close()
        # anything else is counted above and deliberately not rendered (trap 1)

    return _assemble(summary, turns.done(), counts, len(tools), subagents,
                     first_ts, last_ts, session_dir, session_hint)


def _first_user_text(turns):
    """The first human turn's text — the source of both the title and the preview."""
    for turn in turns:
        if turn.role == _HUMAN and turn.blocks:
            return turn.blocks[0].text
    return ""


def _edges(subagents, session_id):
    """`subagents/<id>/meta.json` dicts -> spawn edges (trap 3, correction B).

    `parent_session_id` + `child_session_id` + `status` is exactly a
    `corpus.SpawnEdge`. A meta missing its parent falls back to the CONTAINING
    session, which is the parent by construction — the file lives inside its
    directory. A meta with no child yields nothing: half an edge would render as a
    spawn that never happened.
    """
    edges = []
    for meta in subagents:
        if not isinstance(meta, dict):
            continue
        parent = _s(meta.get("parent_session_id")) or session_id
        child = _s(meta.get("child_session_id"))
        if not parent or not child:
            continue
        edges.append(corpus.SpawnEdge(parent, child, _s(meta.get("status"))))
    return edges


def _assemble(summary, turns, counts, tool_calls, subagents, first_ts, last_ts,
              session_dir, session_hint):
    """Fold the streamed state into the Conversation, ThreadMeta and spawn edges."""
    info = summary.get("info")
    info = info if isinstance(info, dict) else {}

    sid = _s(info.get("id")) or session_hint or _dir_id(session_dir)
    cwd = _s(info.get("cwd")) or _decode_cwd(session_dir)
    first_user = _first_user_text(turns)
    title = (_s(summary.get("generated_title")) or _first_line(first_user, 80)
             or "(untitled)")
    # The summary's own bookkeeping wins over the event stream: it covers activity a
    # resumed session may not have re-logged, and the stream is only a lower bound.
    created = _s(summary.get("created_at")) or first_ts
    updated = (_s(summary.get("last_active_at")) or _s(summary.get("updated_at"))
               or last_ts)

    thread = corpus.ThreadMeta(
        id=sid, title=title, model_provider=GROK_MODEL_VENDOR,
        # NO token count exists anywhere in the measured schema — num_messages and
        # num_chat_messages are MESSAGE counts. Deriving a token number from them
        # would be fabrication, so this stays 0 and the real counts travel in meta.
        tokens_used=0,
        created_at_ms=_iso_to_ms(created), updated_at_ms=_iso_to_ms(updated),
        git_branch="", cwd=cwd, agent_role=_s(summary.get("session_kind")),
        agent_nickname=_s(summary.get("agent_name")), preview=first_user[:200],
        # The unit on disk is a DIRECTORY, not one file, so that is what the UI's
        # "where did this come from" pointer names.
        rollout_path=session_dir)

    conv = ir.Conversation(
        id=sid, title=title, provider=GROK_PROVIDER, turns=turns,
        created_at=created, updated_at=updated, account="",
        meta={"session_dir": session_dir, "cwd": cwd,
              "model_id": _s(summary.get("current_model_id")),
              "agent_name": _s(summary.get("agent_name")),
              "session_kind": _s(summary.get("session_kind")),
              "reasoning_effort": _s(summary.get("reasoning_effort")),
              "sandbox_profile": _s(summary.get("sandbox_profile")),
              "reported_messages": _int(summary.get("num_messages")),
              "reported_chat_messages": _int(summary.get("num_chat_messages")),
              "event_counts": counts, "tool_call_count": tool_calls,
              "subagent_count": sum(1 for m in subagents if isinstance(m, dict))})

    return GrokDoc(conversation=conv, thread=thread,
                   edges=_edges(subagents, sid), session_dir=session_dir)


# ---------------------------------------------------------------- line / file

def parse_updates_lines(lines, updates_path=""):
    """Raw `updates.jsonl` text lines -> (records, errors).

    Blank lines are skipped silently; a line that is not valid JSON, or is valid JSON
    but not an object, is skipped and logged (trap 7). Line numbers are 1-based.
    """
    records, errors = [], []
    for i, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError as e:
            errors.append({"file": updates_path, "line": i, "stage": "parse",
                           "error": repr(e)})
            continue
        if not isinstance(rec, dict):
            errors.append({"file": updates_path, "line": i, "stage": "parse",
                           "error": "line is not a JSON object"})
            continue
        records.append(rec)
    return records, errors


def _read_lines(path):
    """The lines of a session file, or [] when it is absent. May raise OSError, which
    `ingest_sessions` turns into one error for that session."""
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.readlines()


def _load_json(path, stage, errors):
    """One JSON object file -> dict, or None. An absent file is not an error (a
    session can legitimately lack a `summary.json`); a malformed one, or one holding
    something other than an object, is logged and skipped rather than fatal."""
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    try:
        obj = json.loads(raw)
    except ValueError as e:
        errors.append({"file": path, "stage": stage, "error": repr(e)})
        return None
    if not isinstance(obj, dict):
        errors.append({"file": path, "stage": stage,
                       "error": "file is not a JSON object"})
        return None
    return obj


def _read_subagents(session_dir, errors):
    """Every `subagents/<id>/meta.json` under one session, in a stable order.

    The last path component is the LITERAL `meta.json`, so a sibling `meta.json.lock`
    cannot match (trap 4), and a session with no `subagents/` simply yields nothing —
    `glob` on a missing directory returns [], which is what makes trap 3 free rather
    than a special case. The session path is `glob.escape`d because a percent-encoded
    cwd is an arbitrary directory NAME and a real one can contain `[`.
    """
    metas = []
    pattern = os.path.join(glob.escape(session_dir), "subagents", "*", "meta.json")
    for path in sorted(glob.glob(pattern)):
        meta = _load_json(path, "subagent", errors)
        if meta is not None:
            metas.append(meta)
    return metas


def parse_session(session_dir):
    """Read one session DIRECTORY -> (GrokDoc, errors).

    May raise OSError on an unreadable path; `ingest_sessions` catches that so one bad
    session cannot cost the sweep — the same contract as
    `codex_rollout.parse_rollout_file`.
    """
    errors = []
    summary = _load_json(os.path.join(session_dir, "summary.json"), "summary", errors)
    updates = os.path.join(session_dir, "updates.jsonl")
    records, line_errors = parse_updates_lines(_read_lines(updates),
                                               updates_path=updates)
    errors.extend(line_errors)
    subagents = _read_subagents(session_dir, errors)
    return build_document(summary, records, subagents=subagents,
                          session_dir=session_dir), errors


def _is_subagent_dir(path):
    """Is this `<session>/subagents/<id>`? Such a directory is bookkeeping about a
    child whose OWN transcript lives at the top level under its own cwd folder;
    ingesting it as a session would double-count that child."""
    return os.path.basename(os.path.dirname(path.rstrip("/\\"))) == "subagents"


def _session_dirs(root):
    """Every session directory under `root`, de-duplicated and sorted.

    Anchored on EITHER identifying file rather than only `summary.json`, so a session
    missing one of them is still found, and recursive rather than depth-pinned so a
    layout change (the measured one is `<enc-cwd>/<session-id>/`) does not silently
    return zero.
    """
    base = glob.escape(root)
    seen, found = set(), []
    for name in _SESSION_FILES:
        for path in glob.glob(os.path.join(base, "**", name), recursive=True):
            session_dir = os.path.dirname(path)
            key = os.path.normcase(os.path.abspath(session_dir))
            if key in seen or _is_subagent_dir(session_dir):
                continue
            seen.add(key)
            found.append(session_dir)
    return sorted(found)


def ingest_sessions(root):
    """Recursively ingest a Grok sessions ROOT -> (docs, errors).

    One GrokDoc per session directory, each carrying its Conversation, its
    spawn-graph node and any spawn edges. A session that cannot be read is logged and
    skipped; it never aborts the sweep (trap 7). A missing root yields ([], []) — an
    honest empty result, since there is nothing there to fail on.
    """
    docs, errors = [], []
    for session_dir in _session_dirs(root):
        try:
            doc, session_errors = parse_session(session_dir)
        except OSError as e:
            errors.append({"file": session_dir, "stage": "read", "error": repr(e)})
            continue
        docs.append(doc)
        errors.extend(session_errors)
    return docs, errors
