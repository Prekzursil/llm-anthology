"""Codex CLI session rollout logs (.codex/sessions/YYYY/MM/DD/rollout-*.jsonl) -> IR.

A FOURTH Codex-family shape, unrelated to codex.json (the task export `codex.py` reads).
A rollout is an APPEND-ONLY JSONL EVENT LOG: one JSON object per line, each shaped
`{type, timestamp, payload}`. It is the on-disk transcript of a LIVE Codex CLI run and
the natural source of the cockpit's thread graph (corpus.ThreadMeta / SpawnEdge) — which
is exactly what corpus.py's docstring says "come from the Codex rollout adapter".

Schema PROBED from the real corpus (1191 files / 2.2M records, 2026-07):

  line record        {type, timestamp, payload}
  session_meta       {session_id, id, timestamp, cwd, model_provider, git{branch},
                      parent_thread_id, forked_from_id, agent_nickname, agent_path,
                      thread_source, base_instructions{text}, ...}
  response_item      {type, ...}, one of:
    message            role in {user, developer, assistant}; content is a LIST of
                       {type: input_text|output_text|encrypted_content, text}
    reasoning          summary[{type:summary_text, text}] + an OPAQUE encrypted_content
    function_call      {name, arguments, call_id, namespace}
    function_call_output {call_id, output}
    custom_tool_call   {name, input, call_id, status}
    custom_tool_call_output {call_id, output(str | list[{type,text}])}
    agent_message      inter-agent prose (multi-agent runs)
  event_msg          {type, ...}; type=token_count carries
                       info.total_token_usage.total_tokens (a CUMULATIVE session total)
  turn_context / world_state / compacted / inter_agent_communication_metadata — context
                       records that are not part of the visible transcript.

Six measured traps this adapter is built around:

 1. THE DEVELOPER ENVELOPE. role=developer response_items OUTNUMBER real user/assistant
    messages ~12:1 (2401 vs 80/198 over 40 files) — they are the base-instructions /
    AGENTS.md / environment context re-injected every turn, NOT conversation. Emitting
    them as chat bubbles would bury the actual exchange and misattribute machine context
    to a participant. They are COUNTED (meta.developer_message_count) and dropped from
    the turn stream; the real prompts survive via role=user.

 2. OPAQUE REASONING. A reasoning item's human-readable content is only its summary[]
    (type=summary_text); encrypted_content is an opaque blob. A reasoning item with no
    visible summary must NOT become an empty thinking bubble, so it emits zero blocks.

 3. THE LEAKED-LIST OUTPUT. function_call_output.output is a str, but
    custom_tool_call_output.output is a LIST of {type,text} parts. Reading it as a string
    drops the tool output entirely; it is flattened to text.

 4. DATE-NESTED DIRECTORIES. Sessions live under YYYY/MM/DD, so a flat (non-recursive)
    glob returns a FALSE ZERO. ingest_sessions recurses (**), and still finds a rollout
    written at the flat root.

 5. NO ABORT ON A BAD LINE. A rollout is often read while still being written, so its
    last line can be truncated. A malformed / non-object line is SKIPPED AND LOGGED, never
    fatal — one partial line must not cost the whole session.

 6. IDENTITY WITHOUT A HEADER. A resumed/truncated log may lack a session_meta, but every
    rollout FILENAME carries the thread UUID (rollout-<ts>-<uuid>.jsonl). The thread id
    falls back to that filename UUID, then to "".

Timestamps: every record carries a real ISO timestamp, so created_at_ms comes from the
session_meta (the session start) and updated_at_ms from the LAST record (the last logged
activity) — both real log data, left None only when unparseable. (This differs from the
legacy state DB, which structurally lacks updated_at_ms and so leaves it None.)

PRIVACY: tests use synthetic fixtures only; no real conversation content is committed.
"""
import glob
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from llm_anthology import corpus, ir

# response_item.type values with a dedicated block mapping; everything else (agent_message,
# any future type) is preserved verbatim as an `unknown` block.
# Decompression cap for a `.zst` rollout. Bounded rather than unbounded because a
# compressed file's inflated size is not knowable from its on-disk size, and ingest walks a
# whole tree: one pathological archive should fail that FILE, not exhaust memory and take
# the sweep with it. 512 MiB is far above any real rollout (the largest measured on a live
# store is a few MB) while still being a ceiling.
_MAX_ROLLOUT_BYTES = 512 * 1024 * 1024

_TEXT_PARTS = ("input_text", "output_text")
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _s(x):
    """Coerce a display field to str; a null/absent field becomes ''."""
    return x if isinstance(x, str) else ""


@dataclass
class RolloutDoc:
    """One parsed rollout file: the transcript plus the thread-graph node and any spawn
    edge it implies. `thread_id` is exposed as a convenience alias of the node id."""
    conversation: object
    thread: object
    edges: list = field(default_factory=list)
    rollout_path: str = ""

    @property
    def thread_id(self):
        return self.thread.id


# ------------------------------------------------------------------- time / id

def _iso_to_ms(s):
    """An ISO-8601 timestamp -> epoch milliseconds, or None if absent/unparseable. The
    trailing 'Z' is normalised to +00:00 because datetime.fromisoformat rejects it on the
    Python versions this project supports."""
    if not isinstance(s, str) or not s:
        return None
    t = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        return int(datetime.fromisoformat(t).timestamp() * 1000)
    except ValueError:
        return None


def _id_from_path(path):
    """The thread UUID embedded in a rollout filename, or '' if there is none."""
    m = _UUID.search(os.path.basename(_s(path)))
    return m.group(0) if m else ""


# ------------------------------------------------------------- content -> blocks

def _text_blocks(content):
    """message.content is a LIST of parts; the visible prose is the input_text/output_text
    parts. encrypted_content parts and whitespace-only text add nothing."""
    if not isinstance(content, list):
        return []
    blocks = []
    for part in content:
        if not isinstance(part, dict) or _s(part.get("type")) not in _TEXT_PARTS:
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            blocks.append(ir.Block("text", text=text))
    return blocks


def _reasoning_blocks(payload):
    """The human-readable reasoning is only summary[].text (type=summary_text). An item
    with no visible summary (opaque encrypted_content only) emits nothing."""
    parts = []
    for s in (payload.get("summary") if isinstance(payload.get("summary"), list) else []):
        if isinstance(s, dict) and _s(s.get("type")) == "summary_text":
            text = s.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
    if not parts:
        return []
    return [ir.Block("thinking", text="\n".join(parts))]


def _flatten_output(output):
    """A tool output is either a plain string (function_call_output) or a LIST of
    {type,text} parts (custom_tool_call_output). Anything else yields ''."""
    if isinstance(output, str):
        return output
    if not isinstance(output, list):
        return ""
    parts = []
    for p in output:
        if isinstance(p, dict):
            text = p.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(p, str):
            parts.append(p)
    return "\n".join(parts)


def _tool_result(payload):
    return ir.Block("tool_result", text="", data={
        "name": "output", "content": _flatten_output(payload.get("output")),
        "call_id": _s(payload.get("call_id")), "is_error": False})


def _response_blocks(payload, ptype):
    """A non-message response_item -> its block(s). Unknown types survive as `unknown`."""
    if ptype == "reasoning":
        return _reasoning_blocks(payload)
    if ptype == "function_call":
        return [ir.Block("tool_use", text=_s(payload.get("name")), data={
            "name": _s(payload.get("name")), "input": payload.get("arguments"),
            "call_id": _s(payload.get("call_id")),
            "namespace": _s(payload.get("namespace"))})]
    if ptype == "custom_tool_call":
        return [ir.Block("tool_use", text=_s(payload.get("name")), data={
            "name": _s(payload.get("name")), "input": payload.get("input"),
            "call_id": _s(payload.get("call_id")),
            "status": _s(payload.get("status"))})]
    if ptype in ("function_call_output", "custom_tool_call_output"):
        return [_tool_result(payload)]
    return [ir.Block("unknown", data={"orig_type": ptype, "x_raw": payload})]


# ----------------------------------------------------------------- token usage

def _token_total(payload):
    """The cumulative total from a token_count event, or None when any level is
    missing/non-numeric (a partial event must not zero a real running total)."""
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    ttu = info.get("total_token_usage")
    if not isinstance(ttu, dict):
        return None
    total = ttu.get("total_tokens")
    return total if isinstance(total, int) else None


# ------------------------------------------------------------- document builder

def _first_line(text, limit):
    """The first line of `text`, truncated to `limit`; '' for empty text."""
    if not text:
        return ""
    return text.splitlines()[0][:limit]


def build_document(records, rollout_path=""):
    """A sequence of already-parsed rollout records -> one RolloutDoc.

    Turn model: a role=user message is its own human turn; every assistant-side item
    (assistant message, reasoning, tool call/output, unknown) coalesces into a single
    assistant turn that a following user message flushes. role=developer messages are
    counted, not rendered (trap 1)."""
    meta_seed = None
    turns, asst = [], None
    tokens = 0
    first_ts = last_ts = None
    dev_count = 0
    first_user = ""

    for rec in records:
        rtype = _s(rec.get("type"))
        ts = rec.get("timestamp")
        if isinstance(ts, str) and ts:
            first_ts = first_ts if first_ts is not None else ts
            last_ts = ts
        payload = rec.get("payload")

        if rtype == "session_meta":
            if meta_seed is None and isinstance(payload, dict):
                meta_seed = payload
            continue
        if rtype == "event_msg":
            if isinstance(payload, dict) and _s(payload.get("type")) == "token_count":
                total = _token_total(payload)
                if total is not None:
                    tokens = total
            continue
        if rtype != "response_item" or not isinstance(payload, dict):
            continue

        ptype = _s(payload.get("type"))
        if ptype == "message":
            role = _s(payload.get("role"))
            if role == "developer":
                dev_count += 1
                continue
            if role == "user":
                if asst is not None:
                    turns.append(asst)
                    asst = None
                blocks = _text_blocks(payload.get("content"))
                if not first_user and blocks:
                    first_user = blocks[0].text
                turns.append(ir.Turn("human", blocks, uuid=_s(payload.get("id")),
                                     timestamp=_s(ts)))
                continue
            blocks = _text_blocks(payload.get("content"))
        else:
            blocks = _response_blocks(payload, ptype)

        if blocks:
            if asst is None:
                asst = ir.Turn("assistant", [], uuid=_s(payload.get("id")),
                               timestamp=_s(ts))
            asst.blocks.extend(blocks)

    if asst is not None:
        turns.append(asst)

    return _assemble(meta_seed or {}, turns, tokens, first_ts, last_ts, dev_count,
                     first_user, rollout_path)


def _assemble(ms, turns, tokens, first_ts, last_ts, dev_count, first_user, rollout_path):
    """Fold the streamed state into the Conversation, ThreadMeta and spawn edges."""
    tid = _s(ms.get("session_id")) or _s(ms.get("id")) or _id_from_path(rollout_path)
    git = ms.get("git")
    git_branch = _s(git.get("branch")) if isinstance(git, dict) else ""
    title = _first_line(first_user, 80) or _s(ms.get("agent_nickname")) or "(untitled)"
    created_ts = _s(ms.get("timestamp")) or _s(first_ts)
    model_provider = _s(ms.get("model_provider"))
    cwd = _s(ms.get("cwd"))
    agent_role = _s(ms.get("agent_path"))
    agent_nickname = _s(ms.get("agent_nickname"))
    parent = _s(ms.get("parent_thread_id"))

    thread = corpus.ThreadMeta(
        id=tid, title=title, model_provider=model_provider, tokens_used=tokens,
        created_at_ms=_iso_to_ms(created_ts), updated_at_ms=_iso_to_ms(_s(last_ts)),
        git_branch=git_branch, cwd=cwd, agent_role=agent_role,
        agent_nickname=agent_nickname, preview=first_user[:200], rollout_path=rollout_path)

    conv = ir.Conversation(
        id=tid, title=title, provider="codex", turns=turns,
        created_at=created_ts, updated_at=_s(last_ts), account="",
        meta={"rollout_path": rollout_path, "cwd": cwd,
              "model_provider": model_provider, "tokens_used": tokens,
              "agent_role": agent_role, "agent_nickname": agent_nickname,
              "git_branch": git_branch, "developer_message_count": dev_count,
              "parent_thread_id": parent, "forked_from_id": _s(ms.get("forked_from_id"))})

    edges = []
    if parent and tid:
        edges.append(corpus.SpawnEdge(parent, tid, _s(ms.get("thread_source"))))
    return RolloutDoc(conversation=conv, thread=thread, edges=edges,
                      rollout_path=rollout_path)


# ---------------------------------------------------------------- line / file / tree

def parse_rollout_lines(lines, rollout_path=""):
    """Raw text lines -> (RolloutDoc, errors). Blank lines are skipped silently; a line
    that is not valid JSON, or is valid JSON but not an object, is skipped and logged
    (trap 5). Line numbers are 1-based."""
    records, errors = [], []
    for i, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError as e:
            errors.append({"file": rollout_path, "line": i, "stage": "parse",
                           "error": repr(e)})
            continue
        if not isinstance(rec, dict):
            errors.append({"file": rollout_path, "line": i, "stage": "parse",
                           "error": "line is not a JSON object"})
            continue
        records.append(rec)
    return build_document(records, rollout_path=rollout_path), errors


def _read_rollout_lines(path):
    """The lines of one rollout, transparently decompressing a `.zst` archive.

    Codex compresses older rollouts to `rollout-*.jsonl.zst`. MEASURED on a live store:
    2043 `.zst` files and ZERO plain `.jsonl`, so a reader that handles only `.jsonl` sees
    an entirely empty history.

    A rollout `.zst` is a single zstd frame, so it is decompressed whole (`archive.py`
    describes exactly this: "opening one record means inflating the whole file"). Decoding
    is `max_output_size`-capped rather than unbounded: a zip-bomb-shaped file would
    otherwise inflate without limit while ingesting an untrusted-ish tree.
    """
    if not path.endswith(".zst"):
        with open(path, encoding="utf-8") as fh:
            return fh.readlines()

    import zstandard  # local: only the compressed path needs the dependency

    with open(path, "rb") as fh:
        blob = fh.read()
    dctx = zstandard.ZstdDecompressor()
    try:
        raw = dctx.decompress(blob)
    except zstandard.ZstdError:
        # A single-frame decompress needs the original size in the frame header; when it is
        # absent, stream instead. Capped for the same reason as above.
        try:
            with open(path, "rb") as fh:
                with dctx.stream_reader(fh) as reader:
                    raw = reader.read(_MAX_ROLLOUT_BYTES + 1)
        except zstandard.ZstdError as e:
            # Surface a CORRUPT archive as an OSError, because that is the failure class
            # ingest_sessions catches per file. Letting ZstdError escape aborted the entire
            # sweep on one bad archive — a single damaged file would cost the user their
            # whole history, which is precisely the blast radius this ingest is meant to
            # avoid. (Caught by test_ingest_sessions_reports_a_corrupt_zst_...)
            raise OSError(f"corrupt zstd rollout {path}: {e}") from e
    if len(raw) > _MAX_ROLLOUT_BYTES:
        raise OSError(
            f"rollout exceeds the {_MAX_ROLLOUT_BYTES} byte decompression cap: {path}")
    return raw.decode("utf-8", errors="replace").splitlines(keepends=True)


def parse_rollout_file(path):
    """Read one rollout (`.jsonl` or `.jsonl.zst`) -> (RolloutDoc, errors). May raise on an
    unreadable path; ingest_sessions catches that so one bad file cannot cost the sweep."""
    lines = _read_rollout_lines(path)
    return parse_rollout_lines(lines, rollout_path=path)


def ingest_sessions(root):
    """Recursively ingest a Codex sessions ROOT -> (docs, errors). Walks the DATE-NESTED
    tree for every rollout-*.jsonl AND its compressed `.zst` form (trap 4), collecting one
    RolloutDoc per file — each carrying its Conversation, thread id and rollout_path — and
    logging any file that cannot be read rather than aborting the sweep.

    BOTH suffixes are globbed because a live Codex store compresses older rollouts:
    measured 2043 `.zst` against 0 plain `.jsonl`. Globbing only `.jsonl` returned
    `docs=0, errors=0` on that store — a silent no-op that reports a perfectly successful
    ingest of nothing, which is worse than failing.
    """
    docs, errors = [], []
    patterns = [
        os.path.join(root, "**", "rollout-*.jsonl"),
        os.path.join(root, "**", "rollout-*.jsonl.zst"),
    ]
    # De-duplicated: a store holding both `x.jsonl` and `x.jsonl.zst` must ingest the
    # conversation ONCE. The plain file wins — it needs no decode and cannot be truncated
    # mid-frame — and sorting keeps the output order deterministic.
    seen, paths = set(), []
    for pattern in patterns:
        for path in sorted(glob.glob(pattern, recursive=True)):
            key = path[:-4] if path.endswith(".zst") else path
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
    for path in sorted(paths):
        try:
            doc, errs = parse_rollout_file(path)
        except OSError as e:
            errors.append({"file": path, "stage": "read", "error": repr(e)})
            continue
        docs.append(doc)
        errors.extend(errs)
    return docs, errors
