"""Provider loaders: raw export files -> (conversations, errors[, extra]).

Each provider differs only in how its export is read and grouped; everything from
the IR onward is shared (see llm_anthology.build). Loading errors are COLLECTED and handed
to the build layer rather than raised, so one unreadable file cannot cost the corpus.

Gemini's grouping lives here because Takeout's activity log has no conversation id:
a web-app harvest gives TRUE grouping, otherwise a clearly-labelled provisional
time-gap heuristic is used. That label is propagated into the report so a reader
never mistakes the heuristic for ground truth.
"""
import glob
import json
import os
import re
from datetime import datetime, timedelta

from llm_anthology import corpus, index
from llm_anthology.adapters import chatgpt, claude, codex, codex_rollout, codex_state, gemini

GAP = timedelta(minutes=30)
_WS = re.compile(r"\s+")


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _as_list(data):
    return data if isinstance(data, list) else [data]


# --------------------------------------------------------------------------- claude

def load_claude(src, out_dir=None):
    """A single export file, or a directory tree of Claude account exports.

    A real export directory also contains users.json, memories.json, projects/*.json,
    design_chats/*.json and reflections/*.json. Those are NOT conversations, but the
    adapter will happily wrap each one as a single empty conversation — which silently
    padded a real 236-conversation corpus with ~30 junk entries. So prefer the actual
    export filename and only fall back to any *.json when none is found (a renamed or
    single-file export must still work).
    """
    convs, errors = [], []
    if os.path.isfile(src):
        files = [src]
    else:
        files = sorted(glob.glob(os.path.join(src, "**", "conversations.json"), recursive=True))
        # design_chats/*.json are real conversations in a different shape — include them
        files += sorted(glob.glob(os.path.join(src, "**", "design_chats", "*.json"),
                                  recursive=True))
        if not files:
            files = sorted(glob.glob(os.path.join(src, "**", "*.json"), recursive=True))
    if out_dir:
        # never ingest our own output (the site dir often lives inside the source dir)
        out_abs = os.path.abspath(out_dir) + os.sep
        files = [f for f in files if not os.path.abspath(f).startswith(out_abs)]
    for f in files:
        try:
            data = _load_json(f)
        except Exception as e:
            errors.append({"file": os.path.basename(f), "stage": "parse", "error": repr(e)})
            continue
        try:
            if claude.is_design_chat(data):
                convs.append(claude.parse_design_chat(data))
            else:
                convs.extend(claude.parse_export(data))
        except Exception as e:
            errors.append({"file": os.path.basename(f), "stage": "adapt", "error": repr(e)})
    return convs, errors


# ---------------------------------------------------------------------------- codex

def load_codex(src, out_dir=None):
    """A Codex task export (codex.json), or a directory holding one.

    Kept separate from load_chatgpt on purpose: codex.json is a THIRD shape with no
    mapping/current_node/messages, so the ChatGPT adapter reads it as a corpus of
    zero conversations — silently, with a success exit code.
    """
    convs, errors = [], []
    if os.path.isfile(src):
        files = [src]
    else:
        files = sorted(glob.glob(os.path.join(src, "**", "*.json"), recursive=True))
    if out_dir:
        # never ingest our own output (the site dir often lives inside the source dir)
        out_abs = os.path.abspath(out_dir) + os.sep
        files = [f for f in files if not os.path.abspath(f).startswith(out_abs)]
    for f in files:
        try:
            data = _load_json(f)
        except Exception as e:
            errors.append({"file": os.path.basename(f), "stage": "parse", "error": repr(e)})
            continue
        try:
            convs.extend(codex.parse_export(data))
        except Exception as e:
            errors.append({"file": os.path.basename(f), "stage": "adapt", "error": repr(e)})
    return convs, errors


# -------------------------------------------------------------------------- chatgpt

def _cid(c):
    return c.get("conversation_id") or c.get("id") or ""


def _chatgpt_files(p):
    """Resolve one CLI argument to the export files it stands for.

    A real ChatGPT Data Export ships the corpus SHARDED as
    conversations-000.json ... conversations-NNN.json (17 shards / 1613 conversations
    in the observed export), so a DIRECTORY must contribute every shard. Older or
    renamed exports ship a single conversations.json, which is picked up too. A path
    to one file is still honoured as-is (a harvested array, or one renamed shard).
    """
    if os.path.isdir(p):
        files = sorted(glob.glob(os.path.join(p, "conversations-*.json")))
        single = os.path.join(p, "conversations.json")
        if os.path.isfile(single):
            files.append(single)
        return files
    return [p] if os.path.isfile(p) else []


def load_chatgpt(main_path, projects_path=None):
    """The main export (a file OR an export directory) plus an optional second file
    of project-tagged conversations. A conversation appearing in more than one shard
    or file is rendered ONCE (deduped by id)."""
    errors, by_id, proj_of = [], {}, {}
    paths = []
    for p in (main_path, projects_path):
        if p:
            paths += _chatgpt_files(p)
    for path in paths:
        try:
            raw = _as_list(_load_json(path))
        except Exception as e:
            errors.append({"file": os.path.basename(path), "stage": "parse", "error": repr(e)})
            continue
        for c in raw:
            if not isinstance(c, dict):
                continue
            cid = _cid(c)
            if not cid:
                continue
            by_id.setdefault(cid, c)
            if c.get("__project_id"):
                proj_of[cid] = c["__project_id"]
    convs = []
    for craw in by_id.values():
        try:
            convs.append(chatgpt.parse_conversation(craw))
        except Exception as e:
            errors.append({"file": _cid(craw), "stage": "adapt", "error": repr(e)})
    return convs, errors, proj_of


# --------------------------------------------------------------------------- gemini

def _norm(s):
    return _WS.sub(" ", (s or "").replace(" ", " ")).strip().lower()


def _gemini_ts(rec):
    try:
        return datetime.fromisoformat(rec.get("timestamp_iso") or "")
    except (TypeError, ValueError):
        return None


def gemini_groups_from_harvest(records, harvest):
    """TRUE grouping: join each harvested web turn to its Takeout record by exact
    normalised prompt text. Unmatched Takeout records are reported, never dropped."""
    by_prompt = {}
    for i, r in enumerate(records):
        key = _norm(r.get("prompt"))
        if key:
            by_prompt.setdefault(key, []).append(i)

    groups, claimed = [], set()
    for conv in harvest:
        idxs = []
        for t in (conv.get("turns") or []):
            if (t.get("role") or "").lower() not in ("user", "human"):
                continue
            for i in by_prompt.get(_norm(t.get("text")), []):
                if i not in claimed:
                    claimed.add(i)
                    idxs.append(i)
                    break
        if idxs:
            groups.append({"id": conv.get("id") or "",
                           "title": conv.get("title") or "(untitled)",
                           "turn_idxs": sorted(idxs)})
    leftovers = [i for i in range(len(records)) if i not in claimed]
    if leftovers:
        groups.append({"id": "unmatched", "title": "(unmatched Takeout activity)",
                       "turn_idxs": leftovers})
    return groups, len(claimed)


def gemini_groups_from_gaps(records):
    """PROVISIONAL: split on a >30min gap or a Gem change. NOT ground truth."""
    groups, cur, prev_ts, prev_gem = [], [], None, None
    for i, r in enumerate(records):
        ts, gem = _gemini_ts(r), r.get("gem")
        if cur and ((prev_ts and ts and ts - prev_ts > GAP) or gem != prev_gem):
            groups.append(cur)
            cur = []
        cur.append(i)
        prev_ts, prev_gem = ts or prev_ts, gem
    if cur:
        groups.append(cur)
    return [{"id": "grp%03d" % n, "title": "(provisional group %d)" % n, "turn_idxs": g}
            for n, g in enumerate(groups, 1)]


def load_gemini(transcript_path, harvest_path=None):
    try:
        records = _load_json(transcript_path)
    except Exception as e:
        return [], [{"file": os.path.basename(transcript_path), "stage": "parse",
                     "error": repr(e)}], {}
    mode, matched = "gap-heuristic (PROVISIONAL)", 0
    if harvest_path and os.path.isfile(harvest_path):
        try:
            groups, matched = gemini_groups_from_harvest(records, _load_json(harvest_path))
            mode = "harvest (TRUE grouping)"
        except Exception as e:
            return [], [{"file": os.path.basename(harvest_path), "stage": "parse",
                         "error": repr(e)}], {}
    else:
        groups = gemini_groups_from_gaps(records)
    return gemini.parse_all(records, groups), [], {
        "grouping_mode": mode,
        "harvest_matched_records": matched,
        "source_records": len(records),
    }


# --------------------------------------------------------------- cockpit corpus

def _fingerprint(conv):
    """A lightweight, stable content fingerprint for an ingested conversation.

    Keyed on (id, updated_at, turn count) so an APPENDED-TO rollout (a later
    updated_at / more turns) re-ingests while an unchanged one is skipped. Correctness
    does not rest on it — corpus.add_conversation is idempotent by conversation_id, so
    even a fingerprint collision cannot duplicate a posting; it only drives the fast
    resume/skip path in index.build_index.
    """
    return index.hash_content(json.dumps([conv.id, conv.updated_at, len(conv.turns)]))


def load_corpus(sessions_root, index_path, codex_home=None, progress=None):
    """Build the cockpit Corpus and its FTS5 index in one pass.

    Ingests the Codex rollout logs under `sessions_root` (the DATE-NESTED
    YYYY/MM/DD/rollout-*.jsonl tree) for conversation content and the per-thread graph
    node each carries, MERGES the live Codex state DB spawn graph
    ($CODEX_HOME/state_5.sqlite — opened read-only + immutable, retried-then-skipped if
    busy or absent), and builds the contentless FTS5 index at `index_path` over every
    ingested conversation.

    Merge policy: rollout-derived thread metadata (a title / preview from the ACTUAL
    first prompt) WINS; the state graph fills in threads no rollout covered and
    contributes the authoritative spawn edges. Spawn edges are de-duplicated by
    (parent, child). The index build is resumable and idempotent, so a re-run adds no
    duplicate row or posting.

    Returns (corpus, errors): `errors` is the rollout ingest's per-file parse/read log
    (the state read never raises — it skips), so a partial corpus never costs the build.
    """
    docs, errors = codex_rollout.ingest_sessions(sessions_root)
    result = corpus.Corpus()
    sources = []
    seen_edges = set()

    for doc in docs:
        conv = doc.conversation
        conv.meta["thread_id"] = doc.thread_id          # link the FTS row to its thread
        result.conversations.append(conv)
        result.add_thread(doc.thread)
        for edge in doc.edges:
            key = (edge.parent_thread_id, edge.child_thread_id)
            if key not in seen_edges:
                seen_edges.add(key)
                result.add_edge(edge)
        sources.append(index.IndexSource(
            file=doc.rollout_path, content_hash=_fingerprint(conv), records=[conv]))

    state = codex_state.load_corpus(codex_home)
    for meta in state.threads.values():
        if meta.id not in result.threads:               # rollout metadata takes priority
            result.add_thread(meta)
    for edge in state.edges:
        key = (edge.parent_thread_id, edge.child_thread_id)
        if key not in seen_edges:
            seen_edges.add(key)
            result.add_edge(edge)

    conn = corpus.open_index(index_path)
    try:
        _persist_graph(conn, result)
        # Forward `progress` so a caller can both REPORT and INTERRUPT a long ingest.
        # `build_index` invokes it after each committed chunk (index.py), and without the
        # forward there was no per-chunk hook at all: the in-app build could show only a
        # single up-front line, and cancellation had nothing to check — `sidecar.py`
        # documents that absence as exactly why a cancel could not be honoured. Passing
        # None is identical to omitting it, since build_index defaults it to None.
        index.build_index(conn, sources, progress=progress)
    finally:
        conn.close()
    return result, errors


def _persist_graph(conn, result):
    """Write the assembled thread graph into the index.

    WHY THIS EXISTS: `index.build_index` persists only conversations — it calls
    `corpus.add_conversation` and `corpus.set_checkpoint` and never touches the `threads`
    or `thread_spawn_edges` tables. Without this, the graph `load_corpus` just built lived
    ONLY in the returned in-memory object and was discarded when this function closed the
    connection. That mattered because the cockpit never sees the returned object: it spawns
    a sidecar against the index FILE and rebuilds the graph with `corpus.load_corpus(conn)`,
    which reads exclusively from those two tables. So an index built here reported
    conversations normally via `corpus.stats` while `graph.roots` / `graph.timeline` /
    `graph.children` all came back empty — the app's entire primary view blank. Every test
    passed, because they asserted on the in-memory corpus and on the conversation rows, and
    nothing asserted that the graph survived to disk.

    Runs BEFORE the conversation ingest deliberately: `build_index` is the long, chunked,
    resumable half, so committing the (small) graph first means an interruption leaves a
    corpus that is still navigable and resumable, rather than the conversations-without-a-
    graph state this function used to produce unconditionally.

    Writes go through `index._retry`, the SAME lock-retry policy `build_index` uses for
    every write, rather than a second private copy of that policy — the module documents
    that every write is retried on transient SQLITE_LOCKED/BUSY, and a background in-app
    ingest makes contention a live concern rather than a theoretical one. `upsert_thread`
    and `upsert_edge` are INSERT OR REPLACE, so a re-run upserts instead of duplicating and
    the documented idempotence of `load_corpus` is preserved.
    """
    def do(op):
        return index._retry(op)

    for meta in result.threads.values():
        do(lambda m=meta: corpus.upsert_thread(conn, m))
    for edge in result.edges:
        do(lambda e=edge: corpus.upsert_edge(conn, e))
    do(conn.commit)
