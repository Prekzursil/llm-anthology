"""Claude Code transcripts (~/.claude/projects/<slug>/...jsonl) -> IR + spawn graph.

The owner's LARGEST store by a wide margin: 27,770 files, of which 162 are SESSIONS
and 12,613 are SUBAGENT transcripts. `discover.py:318` already finds it — its StoreSpec
carried a note reading "no adapter in this repository reads that shape yet", and this is
that adapter, so the note has been corrected in place (`discover.py:315-312`).

A session is ONE append-only JSONL file, unlike Grok (a directory) and like a Codex
rollout. Every line is a self-describing record `{type, timestamp, sessionId, uuid,
parentUuid, cwd, gitBranch, version, ...}`; there is NO header record, so identity and
provenance come from the PATH and from the first record that happens to carry a field.

Schema from `.scratch/CLAUDE-CODE-SCHEMA.md`, extracted from the live store by a
structure-only probe (key names, protocol discriminants, value types, string LENGTHS —
never values; 6 files / 2500 lines / 0 unparseable):

  projects/<slug>/<session-uuid>.jsonl                              162 SESSIONS
  projects/<slug>/<session-uuid>/subagents/agent-<child>.jsonl      \\  12,613 CHILD
  projects/<slug>/<s>/subagents/workflows/wf_<id>/agent-<child>.jsonl / transcripts
  projects/<slug>/<session-uuid>/tool-results/*.txt                 hook stdout only

  type            attachment 847 . assistant 662 . user 324 . last-prompt 120 .
                  mode 117 . permission-mode 117 . ai-title 115 .
                  queue-operation 97 . system 51 . file-history-snapshot 50
  message         dict{content, role} on both `user` and `assistant`
  threading       uuid . parentUuid (null at a chain head) . sessionId . isSidechain .
                  sourceToolAssistantUUID (user) . logicalParentUuid (system)
  version         2.1.170 AND 2.1.220 in one store -> the schema is NOT frozen

TWO SPEC CORRECTIONS this adapter is built on (both stated in the spec's own header,
`.scratch/CLAUDE-CODE-SCHEMA.md:25-49`, and both changing the design):

 A. 162 is the SESSION count, not a 79x undercount. The other 12,613 files are
    subagent transcripts — real conversation data that discovery reports nothing
    about. So this adapter ingests them TOO, one document each, and the store yields
    ~12,775 documents rather than 162.
 B. THE SPAWN EDGE IS EXPLICIT IN THE PATH. An earlier draft (still visible in the
    spec's Threading section at `:116-122`) said it must be INFERRED from the
    `isSidechain` boolean. It must not be: `subagents/` sits inside the PARENT
    session's directory, so parent and child are both literally in the path, and
    `workflows/wf_<id>/` adds a grouping level. `isSidechain` stays a within-file
    marker and is only COUNTED here.

Nine traps this adapter is built around:

 1. SIGNAL TO NOISE. Only `user` and `assistant` are conversation — 986 of 2500
    sampled lines, so ~61% of a transcript is bookkeeping. Everything else is COUNTED
    into `meta['record_counts']` and not rendered. Counting rather than silently
    discarding is what lets an owner see the format move: a new `type` appears as a
    count with no matching feature.

    `attachment` (847 — the single largest type) is COUNTED, NOT RENDERED. The spec
    records the `attachment` KEY but not its inner shape, and a block built on a
    guessed field name is worse than a gap because it looks like it works. What IS
    read is `attachment.type` if present, into `meta['attachment_kinds']`, purely as a
    census. UNVERIFIED what those payloads hold — settling experiment: extend
    `.scratch/jsonl_schema_probe.py` to allow-list the `attachment` sub-KEY NAMES (not
    values) and a follow-up can render them.

    `system` (51 — hooks, `compact_boundary`, stop summaries) is COUNTED by `subtype`,
    NOT RENDERED, for a structural reason as well as an editorial one: a system record
    has no `role`, so it cannot become an `ir.Turn` without inventing a speaker. The
    per-subtype census is in `meta['system_subtypes']`, so a reader can see that a
    compaction happened even though this adapter draws no marker for it. UNVERIFIED
    whether `compact_boundary` should render as a visible divider — settling
    experiment: count them per session on the real store; if they are common, add a
    between-turns marker block (the IR has no such concept today).

 2. A TOOL RESULT WEARS A USER MASK. Claude Code delivers tool output as a record of
    `type=user` whose `message.content` is a list of `tool_result` blocks — that is
    what `sourceToolAssistantUUID` ("links a tool RESULT back to the assistant turn
    that requested it") is for. Rendering those as human bubbles would attribute
    machine output to the owner, so a user record whose blocks are ALL `tool_result`
    is folded into the open ASSISTANT turn instead. A MIXED record (prose plus a
    result) stays human, because the prose is real.

 3. THE ID MUST COME FROM THE PATH, NOT FROM `sessionId`. `conversations.conversation_id`
    is UNIQUE and `corpus.py:347` (`add_conversation`) OVERWRITES a duplicate — so an id
    collision does not raise, it silently REPLACES the conversation already stored under
    that id. Two independent collision risks, both closed here:
      (That sentence used to read "treats a duplicate as already-present ... SILENTLY
      DROPS", and cited `corpus.add_conversation:278-281`. Both halves were wrong and the
      combination was invisible: the anchor pointed at `init_index`, and the FUNCTION-
      qualified citation shape is one the gate's scraper does not recognise, so nothing
      ever checked it. The claim described the pre-reindex behaviour — an early-return
      that is exactly the premise whose death silently destroyed 47% of the Codex store
      through `loaders._admit`. The DECISION here is unaffected and if anything better
      justified: overwriting loses the earlier conversation outright, where dropping at
      least kept it.)

      * a child transcript may carry its PARENT's `sessionId` (the spec's own open
        question, `:120-122`), which would collapse all ~78 children of a session into
        one row. The child id is therefore derived from its path.
      * the measured child id shape is SHORT HEX (`agent-a97926b10`), not a UUID —
        LIKELY (55-80%; evidence: the workflow/agent ids quoted in this machine's
        `rules/common/*.md`, e.g. `wf_d573a873-cf8|a97926b10`, which are harness text
        and not the store) — so it is NOT unique across sessions. A non-UUID child id
        is QUALIFIED with its parent and workflow (`<parent>/<workflow>/<agent>`); a
        UUID-shaped one is used verbatim because it is unique by construction.
    Symmetrically, a SESSION's id is its FILENAME stem, not its records' `sessionId`:
    the filename is the same token the child paths use as the parent directory, so a
    path-derived id on both ends is what guarantees the edge CONNECTS. The reported
    `sessionId` is preserved in `meta['reported_session_id']` so a mismatch is visible
    rather than silent.

 4. THE CHILD OWNS ITS EDGE — deliberately NOT grok's shape. `grok.py:626-636` has the
    PARENT read its `subagents/` directory. Doing that here would walk the 12,613-file
    subtree twice (once to build a parent's edge list, once to ingest the children as
    documents) and would give one parent a 1,238-element edge list. Instead each child
    document emits the single edge that its own path declares, so ingest is ONE walk,
    ONE read per file, and O(n) in files with nothing keyed by parent. A session
    document therefore always has `edges == []`.

 5. `parentUuid` IS AN INTRA-SESSION MESSAGE CHAIN, not a spawn edge. It is NOT walked:
    records render in FILE order, which is the true order of an append-only log. A DAG
    walk in the style of `claude.py:175` would pick an "active path" and HIDE turns,
    and nothing measured says Claude Code even writes sibling branches (a rewind may
    truncate instead). What is reported is `meta['branch_points']` — parents with more
    than one child, excluding the null head (several heads in one file are RESUMES).
    UNVERIFIED whether branches exist at all — settling experiment: read
    `branch_points` across the real store; a non-zero total is the trigger to add
    active-path selection.

 6. `tool-results/` IS NOT DATA. It holds hook-stdout `.txt` sidecars beside the real
    transcripts. Only `*.jsonl` is ingested AND any path with a `tool-results`
    component is refused outright, so a stray `.jsonl` written there can never be read
    as a conversation. (Trade-off: a project directory literally NAMED `tool-results`
    would also be skipped. Component-exact matching makes that require a whole
    directory named exactly that, not a slug merely containing the text.)

 7. TIMESTAMPS ARE 24-CHAR ISO STRINGS and the app works in epoch MILLISECONDS. There
    is no header record, so `created_at` is the FIRST record carrying a stamp and
    `updated_at` the LAST. An absent or unparseable stamp leaves the session UNDATED —
    a state the UI has a real affordance for — never dropped, never back-filled.

 8. NO ABORT ON A BAD FILE OR A BAD LINE. A transcript is read while it is being
    written, so its last line can be truncated. A malformed line, a non-object line
    and an unreadable file each cost exactly what they touch and are logged — the same
    contract as `codex_rollout.parse_rollout_file` and `grok.parse_session`.

 9. PERFORMANCE IS A FIRST-CLASS CONSTRAINT AT 12,775 FILES. `os.walk` is used instead
    of `glob.glob('**')`: it needs no `glob.escape` around real directory names (a
    percent/bracket-bearing slug is a genuine hazard — see `grok.py:632-633`), it does not
    follow directory symlinks, and it yields a stable order when sorted. One walk, one
    read per file, one pass per line, O(1) dict updates — nothing is keyed by parent,
    so nothing is quadratic in a session's 78-to-1238 children. `iter_documents` is a
    GENERATOR so a caller can stream 12,775 documents instead of holding them all;
    `ingest_sessions` keeps the list-returning contract its sibling adapters have.

PRIVACY: tests use synthetic fixtures only; no real conversation content is committed,
this module reads nothing outside the root it is given, and it was written WITHOUT
opening a single file in the owner's store — the store holds private medical and
pharmaceutical conversations, so the structure-only spec was the only input.
"""
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from llm_anthology import corpus, ir

#: The ADAPTER label — which tool produced the transcript. It goes on
#: `Conversation.provider` and on the spawn-graph node; `sidecar.py:1665` surfaces it to
#: the UI as `provider`. It matches `discover.py:318`'s StoreSpec label so the panel and
#: the ingest name one store the same way, and it is deliberately NOT "claude" —
#: `adapters/claude.py` owns that for the claude.ai account export, which is a different
#: product and a different shape.
#:
#: NOT for `ThreadMeta.model_provider` — that field is the MODEL VENDOR and takes
#: :data:`CLAUDE_CODE_MODEL_VENDOR`. See it for why the two must not be conflated.
CLAUDE_CODE_PROVIDER = "claude-code"

#: The MODEL VENDOR for `ThreadMeta.model_provider` — a DIFFERENT vocabulary from the
#: adapter label above, and `corpus.py` documents the field as the vendor: measured over
#: 250 real Codex rollouts `session_meta.model_provider` reads 'openai', never 'codex'.
#: Writing "claude-code" here put an adapter name in a vendor field, so the same value
#: arrived under two names that a consumer is entitled to treat as different vocabularies,
#: and it reached exports. Conflating them is what made every Codex node render as the
#: palette's unknown grey.
#:
#: INFERRED from the adapter, not read from the transcript, because there is nothing to
#: read: a Claude Code JSONL records `message.model` — a model ID, which this adapter does
#: read and carries in `Conversation.meta['model_id']` — and no vendor field. The
#: inference holds in the one direction that matters: Claude Code serves Anthropic models,
#: and that stays true through Bedrock/Vertex, whose ids (`us.anthropic.claude-…`) are
#: Anthropic model ids too.
#:
#: Chosen over "" deliberately. "" is not more honest here, it is merely emptier: the
#: vendor is a reliable fact about the producing tool, `ThreadMeta.adapter` already
#: carries the which-tool distinction, and an empty vendor on every Claude Code node would
#: reproduce the exact unknown-grey symptom the conflation caused.
CLAUDE_CODE_MODEL_VENDOR = "anthropic"

#: Drop `isSidechain: true` records from a SESSION transcript (never from a subagent
#: transcript, where those same records ARE the conversation).
#:
#: FALSE by default, and this is the one constant a reader may need to flip. If Claude
#: Code also INLINES a child's turns into its parent file, every sidechain turn is
#: rendered twice — once in the parent and once in the child's own transcript. Whether
#: it does is UNVERIFIED (the spec measures `isSidechain` as a key, not its
#: distribution). DETECTION: compare a session document's `meta['sidechain_records']`
#: against the number of `.jsonl` files in that session's `subagents/` directory; a
#: session with children AND a non-zero sidechain count is duplicating them. The
#: failure mode of False is visible duplication; the failure mode of True on a store
#: without child files is SILENT LOSS, so False is the safe default.
DROP_SIDECHAIN_TURNS = False

#: ir.Turn roles.
_HUMAN = "human"
_ASSISTANT = "assistant"

#: Record `type` values. `_ASSISTANT` above doubles as the assistant record type —
#: the two vocabularies happen to share that one literal, and the IR role is what it
#: is named for.
_USER = "user"
_AI_TITLE = "ai-title"
_SYSTEM = "system"
_ATTACHMENT = "attachment"

_SESSION = "session"
_SUBAGENT = "subagent"
_WORKFLOW = "workflow"

_JSONL = ".jsonl"
_SUBAGENTS_DIR = "subagents"
_TOOL_RESULTS_DIR = "tool-results"
_AGENT_PREFIX = "agent-"

#: Shown instead of an empty key so a record with no `type`, a `system` with no
#: `subtype` and an `attachment` with no inner type stay VISIBLE in the census.
_UNTYPED = "(untyped)"

#: Content-block types whose payload is base64 bytes. They become a naming chip: the
#: renderer has no base64 path (`render_html.py:191-201` needs a LOCAL relative path)
#: and an `unknown` block would dump the whole payload into the export — one pasted
#: screenshot is megabytes. The bytes stay in the source transcript, which is the
#: faithful copy; what is kept here is the media type / title.
_MEDIA_BLOCKS = ("image", "document")

_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

#: Trims an over-long fractional second to the 6 digits `datetime` accepts. The
#: measured stamp is 24 chars (millisecond precision), which parses everywhere; this
#: only guards a future version that writes nanoseconds, which `fromisoformat` REJECTS
#: on Python 3.9 — the oldest interpreter `pyproject.toml` supports.
_FRACTION = re.compile(r"(\.\d{6})\d+")


def _s(x):
    """Coerce a display field to str; a null/absent field becomes ''."""
    return x if isinstance(x, str) else ""


def _int(x):
    """Coerce a counter field to int; anything else becomes 0."""
    return x if isinstance(x, int) else 0


def _bump(counter, key):
    """One census increment. A plain dict, not Counter, so `meta` stays JSON-plain."""
    counter[key] = counter.get(key, 0) + 1


def _first_line(text, limit):
    """The first line of `text`, truncated to `limit`; '' for empty text."""
    if not text:
        return ""
    return text.splitlines()[0][:limit]


@dataclass
class ClaudeCodeDoc:
    """One parsed transcript: the conversation, the thread-graph node, and the single
    spawn edge its PATH declares (empty for a session — trap 4). `thread_id` is a
    convenience alias of the node id, matching `codex_rollout.RolloutDoc` and
    `grok.GrokDoc` so the multi-provider loader sees one contract."""
    conversation: object
    thread: object
    edges: list = field(default_factory=list)
    transcript_path: str = ""

    @property
    def thread_id(self):
        return self.thread.id


@dataclass
class Layout:
    """What a transcript's PATH says about it — the whole spawn graph (correction B).

    `kind` is `session` or `subagent`; `thread_id` is the collision-safe id from trap
    3; `parent_id` is the parent SESSION uuid (the directory that contains
    `subagents/`); `workflow` is the grouping path between `subagents/` and the file
    (`workflows/wf_<id>` in the measured layout, '' for a flat child); `agent_id` is
    the child's own id; `slug` is the encoded-cwd project directory.

    The default is an anonymous session, so `build_document` works on records alone.
    """
    kind: str = _SESSION
    thread_id: str = ""
    parent_id: str = ""
    workflow: str = ""
    agent_id: str = ""
    slug: str = ""


# ------------------------------------------------------------------- time / path

def _iso_to_ms(text):
    """An ISO-8601 timestamp -> epoch milliseconds, or None if absent/unparseable.

    Two normalisations, and BOTH exist only for Python 3.9/3.10: the trailing 'Z' is
    rewritten to +00:00 and an over-long fraction is trimmed (`_FRACTION`), because
    `fromisoformat` rejects both there while 3.11+ accepts them silently. A mutation
    check proved that removing either one leaves the suite GREEN on a modern
    interpreter, so `tests/test_claude_code.py::_Py39Datetime` stubs the 3.9 behaviour
    and pins them. Unparseable yields None, which is UNDATED, not an error (trap 7).
    """
    if not isinstance(text, str) or not text:
        return None
    t = text[:-1] + "+00:00" if text.endswith("Z") else text
    t = _FRACTION.sub(r"\1", t, count=1)
    try:
        return int(datetime.fromisoformat(t).timestamp() * 1000)
    except ValueError:
        return None


def _stem(name):
    """A filename without its `.jsonl` suffix. Deliberately not `os.path.splitext`,
    which returns ('.jsonl', '') for a file named exactly `.jsonl` and would hand back
    the extension as an id."""
    return name[:-len(_JSONL)] if name.endswith(_JSONL) else name


def _child_thread_id(parent, workflow, agent):
    """A collision-safe id for a child transcript (trap 3).

    A UUID-shaped agent id is unique by construction and is used verbatim; anything
    else is qualified by the path components that make it unique — the parent session
    and the workflow group — so `agent-a97926b10` under two different sessions, or
    under two different workflows of one session, can never be the same row.
    """
    if _UUID.search(agent):
        return agent
    return "/".join(p for p in (parent, workflow, agent) if p)


def classify_path(path):
    """A transcript path -> Layout, or None when the path is not a transcript.

    Tail-anchored, so it needs no root and survives any depth above the project slug:
    the LAST `subagents` component that has both a directory before it and a filename
    after it marks a child, and everything else is a session named by its filename.
    Backslashes are normalised, so a Windows path classifies identically.

    Returns None for anything under `tool-results` (trap 6) — the ONE deny-list, and
    the only reason this returns None at all.
    """
    parts = os.path.normpath(path).replace("\\", "/").split("/")
    if _TOOL_RESULTS_DIR in parts:
        return None
    idx = -1
    for i in range(len(parts) - 2, 0, -1):
        if parts[i] == _SUBAGENTS_DIR:
            idx = i
            break
    if idx < 0:
        return Layout(kind=_SESSION, thread_id=_stem(parts[-1]),
                      slug=parts[-2] if len(parts) > 1 else "")
    agent = _stem(parts[-1])
    if agent.startswith(_AGENT_PREFIX):
        agent = agent[len(_AGENT_PREFIX):]
    parent = parts[idx - 1]
    workflow = "/".join(parts[idx + 1:-1])
    return Layout(kind=_SUBAGENT,
                  thread_id=_child_thread_id(parent, workflow, agent),
                  parent_id=parent, workflow=workflow, agent_id=agent,
                  slug=parts[idx - 2] if idx > 1 else "")


# ------------------------------------------------------------ content -> blocks

def _flatten(content):
    """A `tool_result` block's content -> the value the renderer should show.

    A string passes through; a LIST of Anthropic parts is joined to its prose (the
    codex trap-3 shape — reading it as a string would drop the tool's entire output).
    A base64 part inside that list becomes a `[image]`/`[document]` LABEL for the same
    reason `_MEDIA_BLOCKS` exists: a screenshot returned by a tool is megabytes, and
    keeping it here would smuggle the payload past that rule and into the export.
    Anything with no showable part in it is returned UNCHANGED so
    `render_html.py:163-164` JSON-dumps it — a structured result is worth more as JSON
    than as ''.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif not isinstance(p, dict):
                continue
            elif isinstance(p.get("text"), str):
                parts.append(p["text"])
            elif _s(p.get("type")) in _MEDIA_BLOCKS:
                parts.append("[%s]" % _s(p.get("type")))
        if parts:
            return "\n".join(parts)
    return content


def _media_block(item, btype):
    """An `image`/`document` block -> a naming chip, never its base64 (`_MEDIA_BLOCKS`)."""
    source = item.get("source")
    source = source if isinstance(source, dict) else {}
    media_type = _s(source.get("media_type"))
    label = _s(item.get("title")) or media_type or btype
    return ir.Block("attachment", text=label,
                    data={"file_name": label, "orig_type": btype,
                          "media_type": media_type,
                          "source_kind": _s(source.get("type"))})


def _map_block(item):
    """One Anthropic content block -> an ir.Block, or None when it is empty.

    The mapped vocabulary matches `adapters/claude.py:238-263` (the claude.ai export
    reads the SAME message shape), so both Claude surfaces render identically. The
    spec deliberately does not print `content`, so the inner `type` vocabulary here is
    the PUBLIC Anthropic one and NOT measured — UNVERIFIED which types actually occur;
    settling experiment: extend the probe to census `message.content[].type`. Anything
    unmapped survives as an `unknown` block carrying its payload, so a type this list
    has never heard of is displayed rather than dropped.
    """
    btype = _s(item.get("type"))
    if btype == "text":
        text = _s(item.get("text"))
        return ir.Block("text", text=text) if text.strip() else None
    if btype == "thinking":
        # `thinking` is the field the API uses; `text` is accepted because a summary
        # variant writes it there, and an unrenderable empty bubble is worse than one
        # extra lookup.
        text = _s(item.get("thinking")) or _s(item.get("text"))
        return ir.Block("thinking", text=text) if text.strip() else None
    if btype == "tool_use":
        return ir.Block("tool_use", text="",
                        data={"name": _s(item.get("name")),
                              "input": item.get("input"),
                              "id": _s(item.get("id"))})
    if btype == "tool_result":
        # No `name`: the Anthropic tool_result block carries only `tool_use_id`, and
        # `render_html.py:161` already falls back to "tool". Inventing a name here
        # would put a guess in the export.
        return ir.Block("tool_result", text="",
                        data={"name": "", "content": _flatten(item.get("content")),
                              "is_error": bool(item.get("is_error")),
                              "tool_use_id": _s(item.get("tool_use_id"))})
    if btype in _MEDIA_BLOCKS:
        return _media_block(item, btype)
    return ir.Block("unknown", text="", data={"orig_type": btype, "x_raw": item})


def _content_blocks(content):
    """`message.content` -> blocks. It is a STRING or a LIST of blocks (spec `:102`),
    and both are handled; a single un-wrapped block dict and a bare string inside the
    list are accepted too, because committing to one shape and being wrong would read
    the whole store as empty conversations — the failure this adapter exists to avoid.
    """
    if isinstance(content, str):
        return [ir.Block("text", text=content)] if content.strip() else []
    if isinstance(content, dict):
        items = [content]
    elif isinstance(content, list):
        items = content
    else:
        return []
    blocks = []
    for item in items:
        if isinstance(item, dict):
            block = _map_block(item)
        elif isinstance(item, str):
            block = ir.Block("text", text=item) if item.strip() else None
        else:
            block = ir.Block("unknown", text="",
                             data={"orig_type": "", "x_raw": item})
        if block is not None:
            blocks.append(block)
    return blocks


def _first_text(blocks):
    """The first prose block's text — the source of the title and the preview. A turn
    that opens with an image or a tool call must not hide the prompt behind it."""
    for block in blocks:
        if block.type == "text":
            return block.text
    return ""


# ------------------------------------------------------------ the turn machine

class _Turns:
    """Accumulates records into turns.

    One assistant turn stays open and absorbs every assistant-side block — assistant
    prose, thinking, tool calls, and the tool RESULTS that arrive wearing a user mask
    (trap 2) — until a real human record flushes it. That is exactly
    `codex_rollout.build_document`'s model, so the two live-log adapters produce the
    same turn shape. A turn is only appended when it carries something, so nothing
    renders as an empty bubble.
    """

    def __init__(self):
        self.turns = []
        self.asst = None

    def assistant(self, blocks, uuid="", ts=""):
        if self.asst is None:
            self.asst = ir.Turn(_ASSISTANT, [], uuid=uuid, timestamp=ts)
        self.asst.blocks.extend(blocks)

    def human(self, blocks, uuid="", ts=""):
        self.flush()
        self.turns.append(ir.Turn(_HUMAN, blocks, uuid=uuid, timestamp=ts))

    def flush(self):
        if self.asst is not None:
            self.turns.append(self.asst)
            self.asst = None

    def done(self):
        self.flush()
        return self.turns


def _attachment_kind(rec):
    """An `attachment` record's inner type, for the census only (trap 1)."""
    att = rec.get("attachment")
    if isinstance(att, dict):
        return _s(att.get("type")) or _UNTYPED
    return _UNTYPED


class _Reader:
    """Folds a transcript's records into one document.

    A class rather than a long function because a headerless log means ~15 running
    accumulators: every field (cwd, gitBranch, sessionId, version, the generated
    title) is taken from the FIRST record that carries it, and the censuses grow as
    the file is read.
    """

    def __init__(self, layout):
        self.layout = layout
        self.turns = _Turns()
        self.counts = {}
        self.system = {}
        self.attachments = {}
        self.prompts = {}
        self.parents = {}
        self.versions = set()
        self.first_ts = ""
        self.last_ts = ""
        self.cwd = ""
        self.git_branch = ""
        self.reported_sid = ""
        self.ai_title = ""
        self.model_id = ""
        self.tokens = 0
        self.sidechain = 0
        self.meta_users = 0
        self.tool_results = 0
        self.first_user = ""

    # -- per record ----------------------------------------------------------

    def feed(self, rec):
        """One record. Unknown types are counted and dropped, never fatal (trap 1)."""
        rtype = _s(rec.get("type"))
        _bump(self.counts, rtype or _UNTYPED)
        ts = _s(rec.get("timestamp"))
        if ts:
            self.first_ts = self.first_ts or ts
            self.last_ts = ts
        version = _s(rec.get("version"))
        if version:
            self.versions.add(version)
        self.cwd = self.cwd or _s(rec.get("cwd"))
        self.git_branch = self.git_branch or _s(rec.get("gitBranch"))
        self.reported_sid = self.reported_sid or _s(rec.get("sessionId"))

        if rtype == _USER:
            self._turn(rec, ts, human=True)
        elif rtype == _ASSISTANT:
            self._turn(rec, ts, human=False)
        elif rtype == _SYSTEM:
            _bump(self.system, _s(rec.get("subtype")) or _UNTYPED)
        elif rtype == _ATTACHMENT:
            _bump(self.attachments, _attachment_kind(rec))
        elif rtype == _AI_TITLE:
            self.ai_title = self.ai_title or _s(rec.get("aiTitle"))

    def _turn(self, rec, ts, human):
        """A `user`/`assistant` record -> blocks on the right side of the exchange."""
        if rec.get("isSidechain") is True:
            self.sidechain += 1
            if DROP_SIDECHAIN_TURNS and self.layout.kind == _SESSION:
                return
        parent = _s(rec.get("parentUuid"))
        if parent:
            _bump(self.parents, parent)
        message = rec.get("message")
        message = message if isinstance(message, dict) else {}
        blocks = _content_blocks(message.get("content"))
        uuid = _s(rec.get("uuid"))
        if human:
            self._human(rec, blocks, uuid, ts)
        else:
            self._assistant(message, blocks, uuid, ts)

    def _human(self, rec, blocks, uuid, ts):
        if blocks and all(b.type == "tool_result" for b in blocks):
            # trap 2: machine output, not the owner speaking.
            self.tool_results += 1
            self.turns.assistant(blocks, uuid, ts)
            return
        if rec.get("isMeta") is True:
            # Harness-injected text attributed to the user (the `developer`-envelope
            # shape `codex_rollout.py:31-36` drops). It is COUNTED and still RENDERED:
            # codex earned its drop with a measured 12:1 ratio, and no equivalent
            # measurement exists here — UNVERIFIED how many user records carry it;
            # settling experiment: read `meta_user_records` across the real store.
            self.meta_users += 1
        source = _s(rec.get("promptSource"))
        if source:
            _bump(self.prompts, source)
        if not blocks:
            return
        self.first_user = self.first_user or _first_text(blocks)
        self.turns.human(blocks, uuid, ts)

    def _assistant(self, message, blocks, uuid, ts):
        # `usage` and `model` are read DEFENSIVELY. The spec measured `message` as
        # {content, role} only, so they may not exist at all — UNVERIFIED; absence
        # costs nothing (0 tokens and an empty model id, both honest) and no field
        # name is invented anywhere else. Settling experiment: extend the probe to
        # print the assistant `message` KEY SET. Per-response usage is SUMMED, unlike
        # codex's cumulative `total_token_usage` which is assigned.
        usage = message.get("usage")
        if isinstance(usage, dict):
            self.tokens += (_int(usage.get("input_tokens"))
                            + _int(usage.get("output_tokens")))
        self.model_id = self.model_id or _s(message.get("model"))
        if blocks:
            self.turns.assistant(blocks, uuid, ts)

    # -- assembly ------------------------------------------------------------

    def document(self, transcript_path):
        """The accumulated state -> Conversation + ThreadMeta + the path's edge."""
        layout = self.layout
        turns = self.turns.done()
        tid = layout.thread_id or self.reported_sid
        title = (self.ai_title or _first_line(self.first_user, 80) or "(untitled)")
        # The slug is a LOSSY encoding of the cwd (every separator became `-`), so it
        # is never decoded into a path — a wrong path is worse than a raw slug.
        cwd = self.cwd or layout.slug
        spawn_kind = _WORKFLOW if layout.workflow else _SUBAGENT
        is_child = layout.kind == _SUBAGENT

        thread = corpus.ThreadMeta(
            id=tid, title=title, model_provider=CLAUDE_CODE_MODEL_VENDOR,
            tokens_used=self.tokens,
            created_at_ms=_iso_to_ms(self.first_ts),
            updated_at_ms=_iso_to_ms(self.last_ts),
            git_branch=self.git_branch, cwd=cwd,
            agent_role=spawn_kind if is_child else _SESSION,
            agent_nickname=layout.agent_id, preview=self.first_user[:200],
            rollout_path=transcript_path)

        conv = ir.Conversation(
            id=tid, title=title, provider=CLAUDE_CODE_PROVIDER, turns=turns,
            created_at=self.first_ts, updated_at=self.last_ts, account="",
            meta={"transcript_path": transcript_path, "cwd": cwd,
                  "slug": layout.slug, "git_branch": self.git_branch,
                  "kind": layout.kind, "parent_session_id": layout.parent_id,
                  "workflow": layout.workflow, "agent_id": layout.agent_id,
                  "reported_session_id": self.reported_sid,
                  "model_id": self.model_id, "tokens_used": self.tokens,
                  "record_counts": self.counts, "system_subtypes": self.system,
                  "attachment_kinds": self.attachments,
                  "prompt_sources": self.prompts,
                  "versions": sorted(self.versions),
                  "sidechain_records": self.sidechain,
                  "meta_user_records": self.meta_users,
                  "tool_result_records": self.tool_results,
                  "branch_points": sum(1 for n in self.parents.values() if n > 1)})

        edges = []
        # trap 4: the child declares its own edge; a session declares none. A child
        # with no parent yields nothing — half an edge would render as a spawn that
        # never happened (`grok.py:516-517` makes the same call for the same reason).
        if is_child and layout.parent_id:
            edges.append(corpus.SpawnEdge(layout.parent_id, tid, spawn_kind))
        return ClaudeCodeDoc(conversation=conv, thread=thread, edges=edges,
                             transcript_path=transcript_path)


# ---------------------------------------------------------- document builder

def build_document(records, transcript_path="", layout=None):
    """Already-parsed transcript records -> one ClaudeCodeDoc.

    `layout` is what `classify_path` returned for the file; without one the records
    are read as an anonymous session identified by their own `sessionId`. A record
    that is not a dict is skipped: this is a public entry point reading a live log, so
    a partial line is data to be survived, not a contract violation.
    """
    reader = _Reader(layout if isinstance(layout, Layout) else Layout())
    for rec in records:
        if isinstance(rec, dict):
            reader.feed(rec)
    return reader.document(transcript_path)


# ---------------------------------------------------------------- line / file

def parse_transcript_lines(lines, transcript_path="", layout=None):
    """Raw JSONL text lines -> (ClaudeCodeDoc, errors).

    Blank lines are skipped silently; a line that is not valid JSON, or is valid JSON
    but not an object, is skipped and LOGGED (trap 8). Line numbers are 1-based.
    """
    records, errors = [], []
    for i, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError as e:
            errors.append({"file": transcript_path, "line": i, "stage": "parse",
                           "error": repr(e)})
            continue
        if not isinstance(rec, dict):
            errors.append({"file": transcript_path, "line": i, "stage": "parse",
                           "error": "line is not a JSON object"})
            continue
        records.append(rec)
    doc = build_document(records, transcript_path=transcript_path, layout=layout)
    return doc, errors


def parse_transcript_file(path):
    """Read one transcript -> (ClaudeCodeDoc, errors).

    Classifies from its own path, so a caller needs nothing but the path. May raise
    OSError; `iter_documents` catches that so one bad file cannot cost the sweep — the
    same contract as `codex_rollout.parse_rollout_file`. Undecodable bytes are
    REPLACED rather than fatal: one bad byte must not cost a whole session.
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    return parse_transcript_lines(lines, transcript_path=path,
                                  layout=classify_path(path))


# ---------------------------------------------------------------------- tree

def _transcripts(root):
    """Every `*.jsonl` under `root`, in a stable order (trap 9).

    `os.walk` rather than `glob.glob('**')`: no `glob.escape` is needed around real
    directory names, directory symlinks are not followed, and sorting both levels
    makes the sweep order deterministic. A missing root simply yields nothing.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(_JSONL):
                yield os.path.join(dirpath, name)


def iter_documents(root):
    """Stream (ClaudeCodeDoc | None, errors) for every transcript under `root`.

    A GENERATOR, because the measured store holds ~12,775 transcripts and a caller
    that only indexes them should not have to hold them all at once. `doc` is None
    exactly when the file could not be read, and its error is carried alongside.
    """
    for path in _transcripts(root):
        if classify_path(path) is None:
            continue                       # trap 6: a `tool-results` sidecar
        try:
            doc, errors = parse_transcript_file(path)
        except OSError as e:
            yield None, [{"file": path, "stage": "read", "error": repr(e)}]
            continue
        yield doc, errors


def ingest_sessions(root):
    """Recursively ingest a Claude Code PROJECTS root -> (docs, errors).

    One document per transcript — sessions AND the 12,613 subagent transcripts
    (correction A) — each carrying its Conversation, its spawn-graph node and the one
    spawn edge its path declares. A file that cannot be read is logged and skipped; it
    never aborts the sweep (trap 8). A missing root yields ([], []), an honest empty
    result rather than an error, since there is nothing there to fail on.
    """
    docs, errors = [], []
    for doc, doc_errors in iter_documents(root):
        errors.extend(doc_errors)
        if doc is not None:
            docs.append(doc)
    return docs, errors
