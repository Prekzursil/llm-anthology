"""Provider loaders: raw export files -> (conversations, errors[, extra]).

Each provider differs only in how its export is read and grouped; everything from
the IR onward is shared (see llm_anthology.build). Loading errors are COLLECTED and handed
to the build layer rather than raised, so one unreadable file cannot cost the corpus.

Gemini's grouping lives here because Takeout's activity log has no conversation id:
a web-app harvest gives TRUE grouping, otherwise a clearly-labelled provisional
time-gap heuristic is used. That label is propagated into the report so a reader
never mistakes the heuristic for ground truth.

`load_corpus` (the cockpit ingest) is the one MULTI-PROVIDER loader: it merges the LIVE
stores — Codex rollouts, an optional Grok session store, and the Codex state graph — into
one Corpus and one index. Adding a provider there is one entry in its `todo` table plus
one optional root argument; the merge, edge de-duplication, id-collision and error-
attribution policies are shared and are documented on that function.
"""
import glob
import json
import os
import re
from datetime import datetime, timedelta

from llm_anthology import corpus, index
from llm_anthology.adapters import (chatgpt, claude, codex, codex_rollout, codex_state,
                                    gemini, grok)

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


#: The LABEL each document source carries on every `errors` entry it produces, so a
#: reader of that flat list can tell WHICH store failed — the entries are otherwise
#: shaped alike (file/line/stage/error) and indistinguishable. It is also the identity
#: the collision check compares: a repeated thread id from the SAME source is a resumed
#: (or copied) session, which is normal and already handled; a repeated id from a
#: DIFFERENT source is a cross-provider collision.
CODEX_ROLLOUT_SOURCE = "codex-rollout"
GROK_SOURCE = "grok"


def load_corpus(sessions_root, index_path, codex_home=None, progress=None,
                grok_root=None):
    """Build the cockpit Corpus and its FTS5 index in one pass, over one or more providers.

    Ingests the Codex rollout logs under `sessions_root` (the DATE-NESTED
    YYYY/MM/DD/rollout-*.jsonl tree) for conversation content and the per-thread graph
    node each carries; OPTIONALLY ingests a Grok Build session store under `grok_root`
    (the `<enc-cwd>/<session-id>/` tree, whose `subagents/` metas are the only other
    source of spawn edges in this repository); MERGES the live Codex state DB spawn graph
    ($CODEX_HOME/state_5.sqlite — opened read-only + immutable, retried-then-skipped if
    busy or absent); and builds the contentless FTS5 index at `index_path` over every
    ingested conversation, whatever its provider.

    `grok_root` IS NEVER DEFAULTED. Omit it and no Grok store is read at all — there is
    deliberately no `~/.grok` fallback, unlike `codex_home`, whose fallback to the LIVE
    store is how an automated probe once read the owner's real sessions (which is why
    `sidecar.py`'s `corpus.build` RPC requires `codex_home` explicitly). A Grok store
    holds private material; reading one has to be something the caller named.

    MERGE POLICY, in ingest order, so "which source wins for what" stays answerable:
      1. Codex rollouts claim their thread ids first. Their metadata (a title / preview
         from the ACTUAL first prompt) WINS over every later source, as before.
      2. Grok claims only ids no earlier source claimed, and brings its own thread
         metadata and spawn edges verbatim for those.
      3. The Codex state DB fills in threads no earlier source covered — unchanged, it
         remains the gap-filler — and contributes its authoritative spawn edges.
      4. Spawn edges are de-duplicated by (parent, child) across ALL sources; the first
         source to declare an edge owns its `status`.

    CROSS-PROVIDER THREAD-ID COLLISION. `Corpus.threads` is keyed by thread id and
    `conversations.conversation_id` is UNIQUE, so a Grok session id equal to a Codex
    thread id would REPLACE that node (re-pointing its subtree) while
    `corpus.add_conversation` — idempotent by conversation_id — silently discarded the
    incoming conversation. Both failures are invisible. Codex thread ids and Grok session
    ids are both UUID-shaped in practice, so a genuine clash is vanishingly unlikely, but
    it CANNOT be ruled out: every id has non-UUID fallbacks (a rollout filename, a Grok
    session DIRECTORY NAME, and ultimately "") and a copied or hand-edited store can
    produce anything. So the ids are kept VERBATIM — namespacing them would silently
    diverge the id the app shows from the id the provider shows — and the second claimant
    is REFUSED with an error entry naming both sources. It contributes no conversation,
    no thread and no edge; attaching its subtree to another provider's thread would be
    worse than dropping it, and the drop is now reported rather than silent.

    Returns (corpus, errors): `errors` is the per-source parse/read log, every entry
    tagged with its `source` (the state read never raises — it skips), so a partial
    corpus never costs the build.
    """
    result = corpus.Corpus()
    sources, errors = [], []
    seen_edges, claimed_by = set(), {}
    # thread id -> the Conversation already admitted for it, so a LATER rollout of the
    # same thread can be merged into it rather than overwriting it in the index. See
    # `_merge_resumed_leg`.
    admitted = {}

    # The document-producing sources, in INGEST ORDER (see the merge policy above). Each
    # entry is (label, adapter MODULE, the doc attribute naming its unit on disk, root).
    # The module is held rather than its bound `ingest_sessions` so the call stays late-
    # bound. The attribute differs because the units differ — a Codex session is one
    # FILE, a Grok session is a DIRECTORY — and that string becomes the
    # ingest_checkpoint key, so it is declared per source rather than guessed.
    #
    # Every source is OPT-IN by naming its root, Codex included. `sessions_root` stays the
    # first positional argument for compatibility, but an empty one now means "no Codex"
    # rather than "scan nothing and call it a Codex ingest".
    #
    # This matters for a real case, not a hypothetical: a machine can hold a Grok store and
    # no Codex store at all, and the discovery panel reports exactly that ("this finding
    # names no Codex home"). With Codex unconditional, importing Grok alone was impossible —
    # the caller had to invent a Codex path to get past a required argument, and
    # `ingest_sessions` would then glob nothing and report a perfectly successful ingest of
    # it. Symmetry removes both the impossibility and the silent no-op.
    todo = []
    if sessions_root:
        todo.append((CODEX_ROLLOUT_SOURCE, codex_rollout, "rollout_path", sessions_root))
    if grok_root:
        todo.append((GROK_SOURCE, grok, "session_dir", grok_root))

    for label, adapter, path_attr, root in todo:
        docs, source_errors = _ingest_docs(adapter, root, label)
        errors.extend(source_errors)
        for doc in docs:
            errors.extend(_admit(result, doc, label, path_attr, claimed_by, seen_edges,
                                 sources, admitted))

    # The Codex state graph is merged ONLY when a home was named. Two reasons, and the
    # second is the important one:
    #   * a Grok-only ingest has no Codex home to merge, and demanding one made that case
    #     impossible to express;
    #   * `codex_state.load_corpus(None)` falls back to the LIVE Codex store, and an
    #     automated probe really did read the owner's real private sessions that way. An
    #     unnamed home now means "no state graph", never "go find one".
    if codex_home:
        state = codex_state.load_corpus(codex_home)
        for meta in state.threads.values():
            if meta.id not in result.threads:           # earlier sources take priority
                result.add_thread(meta)
        for edge in state.edges:
            _add_edge(result, edge, seen_edges)

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


def _ingest_docs(adapter, root, label):
    """One adapter's `ingest_sessions(root)` -> (docs, errors), every error attributed.

    PER-SOURCE ISOLATION. Both adapters already survive a bad line, a bad file and a bad
    session internally, so `ingest_sessions` is not expected to raise — but a failure
    NEITHER of them models (an unreadable root, a shape the walk cannot handle) would
    propagate out of `load_corpus` and zero an ingest of the OTHER providers that was
    entirely healthy. One broken store must cost only its own source. Catching Exception
    is therefore deliberate, and is the same call the `sidecar.py` build worker makes for
    the same reason: the alternative is losing a corpus that was fine.
    """
    try:
        docs, errors = adapter.ingest_sessions(root)
    except Exception as exc:                # noqa: BLE001 — deliberate, see the docstring
        return [], [{"source": label, "file": root, "stage": "ingest",
                     "error": repr(exc)}]
    return docs, [dict(err, source=label) for err in errors]


def _admit(result, doc, label, path_attr, claimed_by, seen_edges, sources, admitted):
    """Fold ONE parsed document into `result`, or refuse it as a collision.

    Returns the error entries the attempt produced: empty when it was admitted, one
    collision entry when its thread id already belongs to a DIFFERENT source. A repeated
    id from the SAME source is admitted, because that is a resumed session (a second
    rollout for one thread) or a copied store — but it is MERGED into the conversation it
    continues, not appended as a second record under the same id (`_merge_resumed_leg`).

    THE CLAIM THIS DOCSTRING USED TO MAKE was that a same-source repeat needed nothing
    doing, because "the thread upserts and the conversation dedupes by id, exactly as
    before this function existed". The second half died when `corpus.add_conversation`
    was changed to re-index rather than early-return (corpus.py:318-331): dedupes-by-id
    became OVERWRITES-by-id. Two rollouts of one resumed session then meant two records
    with one `conversations.conversation_id`, and the later simply replaced the earlier —
    2 files in, 1 row out, the older leg's turns unsearchable, 0 errors, exit 0.

    Measured on the owner's live store before the fix: 236 of 1069 session ids spanned
    more than one rollout file, covering 1189 files, so 953 files — 47.1% of the store —
    were being dropped silently. None of the 236 was a benign replay (156 fully disjoint,
    78 partially overlapping), and one id spanned 66 files.

    A BLANK id neither claims nor collides — see the comment on the guard below.
    """
    thread_id = doc.thread_id
    # A blank id is not an identity, so it can neither claim the key nor collide on it.
    # Without this guard the first source to yield an id-less session OWNED the `""` key and
    # every LATER source's id-less session was refused as a collision and never ingested —
    # silent cross-source data loss, reported as "thread id '' is already held by
    # codex-rollout", which reads as an id conflict when neither session had an id at all.
    # `codex_rollout` really does derive `thread_id == ""` for a rollout with no
    # `session_meta` and no UUID in its filename (`codex_rollout.py:296` ->
    # `_id_from_path`), so the blank is a real input, not a defensive hypothetical.
    #
    # `dedup.py:339-345` settled the same question for its own map and states the rule this
    # follows: "an id that identifies nothing maps onto nothing."
    #
    # SKIPPED rather than keyed by path. `claimed_by` has exactly one reader — the check
    # immediately below — so nothing downstream needs blank-id dedup, and a path key could
    # never collide anyway (each source contributes a distinct unit on disk). Keying blanks
    # by path would add an entry with no reader and imply a collision class that does not
    # exist.
    #
    # The real guard is untouched: two DIFFERENT sources claiming the same NON-blank id are
    # still refused, because that genuinely re-points a subtree and drops a conversation.
    if thread_id:
        holder = claimed_by.get(thread_id)
        if holder is not None and holder != label:
            return [{"source": label, "file": getattr(doc, path_attr),
                     "stage": "thread-id-collision",
                     "error": "thread id %r is already held by %s; this %s session was NOT "
                              "ingested" % (thread_id, holder, label)}]
        if holder is not None:
            # SAME source, same id: a resumed session's next leg. Merge, never append.
            _merge_resumed_leg(result, admitted[thread_id], doc, path_attr, sources)
            return []
        claimed_by[thread_id] = label

    conv = doc.conversation
    conv.meta["thread_id"] = thread_id                  # link the FTS row to its thread
    conv.meta["rollout_paths"] = [getattr(doc, path_attr)]
    if thread_id:
        admitted[thread_id] = conv
    result.conversations.append(conv)
    result.add_thread(doc.thread)
    for edge in doc.edges:
        _add_edge(result, edge, seen_edges)
    sources.append(index.IndexSource(file=getattr(doc, path_attr),
                                     content_hash=_fingerprint(conv), records=[conv]))
    return []


def _turn_key(turn):
    """The identity of a turn for merge purposes.

    The provider's opaque item id when there is one — `codex_rollout.py:274,283` reads
    `payload["id"]` into `Turn.uuid`. Roughly a third of real rollout items carry no such
    id, and `grok.py:311` never sets one at all, so a uuid-only key would treat every
    id-less turn as unique and re-append the whole replayed prefix. An id that identifies
    nothing maps onto nothing (the rule `dedup.py:339-345` and `_admit` already follow),
    so those fall back to their own content: role, stamp and block text.
    """
    if turn.uuid:
        return ("id", turn.uuid)
    return ("body", turn.role, turn.timestamp,
            tuple((b.type, b.text) for b in turn.blocks))


def _turn_weight(turn):
    """How much a rendering of a turn actually carries, for choosing between two of them.

    Characters rather than block count: the measured divergence had the FEWER blocks and
    the MORE text (35 blocks / 17,012 chars against 94 / 12,044), so counting blocks picks
    the emptier one.
    """
    return sum(len(b.text) for b in turn.blocks)


def _merge_resumed_leg(result, prior, doc, path_attr, sources):
    """Fold a LATER rollout of an already-admitted thread into the conversation it
    continues, rather than letting it overwrite that conversation in the index.

    The incoming leg is filtered against every turn admitted so far. The FIRST leg is
    never filtered at all — it is admitted whole by `_admit` and never passes through
    here — so a rollout's own internal repeats survive exactly as the adapter emitted
    them; only a LATER leg re-stating something already held is dropped.

    ONE PROVIDER ID, TWO RENDERINGS. `Turn.uuid` marks where a turn STARTS, not how far
    it extends: `codex_rollout.py:283` opens an assistant turn with the id of the first
    item in a run and then accumulates the rest of the run into it. A resumed leg can cut
    that run differently, so the same id can arrive carrying a different body. MEASURED:
    3 such turns across the 236 real merges, all in one 66-leg thread, and all DIVERGENT
    — neither body was a prefix of the other (94 blocks/12,044 chars against 35
    blocks/17,012 chars for the same id). Keeping whichever arrived first would silently
    truncate a message by thousands of characters, which is a smaller copy of the very
    bug this function exists to remove. The RICHER rendering wins instead, and the count
    is recorded in `meta["merge_divergent_turns"]` so a lossy reconciliation is visible
    rather than silent. Unioning the two bodies was rejected: the legs chunk the same
    prose differently, so a union duplicates text on screen instead of losing it.

    WHICH SIDE WINS, per field, and why:
      * turns          — appended in file order, minus anything already present. 156 of
                         the 236 measured real overlaps are fully disjoint, so this is
                         mostly pure concatenation; 78 partially overlap, which is what
                         the filter is for.
      * title/preview  — the FIRST leg's. `_assemble` derives the title from the first
        /created_at      user message IN ITS OWN FILE, so a resumed leg's title is the
                         resume prompt, not what the conversation is actually about.
      * updated_at     — the LATEST leg's; the conversation now extends to it.
      * tokens_used    — the maximum. Codex reports a per-rollout cumulative figure, so
                         taking the max is right whether it accumulates across legs or
                         restarts; summing would double-count the former.
      * rollout_path   — the LATEST leg's, because that is the file `codex resume`
                         continues and the one the reader should open. The full list is
                         kept in `meta["rollout_paths"]` so the earlier legs stay visible
                         rather than being silently folded away.
    """
    # key -> POSITION, not the turn itself: a divergent rendering has to replace the one
    # already held, and `list.index` would find the wrong element whenever two turns
    # compare equal as dataclasses.
    seen = {}
    for i, turn in enumerate(prior.turns):
        seen.setdefault(_turn_key(turn), i)

    divergent = 0
    for turn in doc.conversation.turns:
        key = _turn_key(turn)
        pos = seen.get(key)
        if pos is None:
            seen[key] = len(prior.turns)
            prior.turns.append(turn)
        elif key[0] == "id" and _turn_weight(turn) > _turn_weight(prior.turns[pos]):
            prior.turns[pos] = turn                 # same item, fuller rendering
            divergent += 1
        # A body-keyed repeat is byte-identical by construction, so there is nothing to
        # choose between the two copies and the one already held stands.

    path = getattr(doc, path_attr)
    later = doc.conversation
    prior.updated_at = max(prior.updated_at, later.updated_at)
    prior.meta["rollout_paths"] = prior.meta.get("rollout_paths", []) + [path]
    prior.meta["rollout_path"] = path
    prior.meta["tokens_used"] = max(prior.meta.get("tokens_used", 0) or 0,
                                    later.meta.get("tokens_used", 0) or 0)
    if divergent:
        prior.meta["merge_divergent_turns"] = (
            prior.meta.get("merge_divergent_turns", 0) + divergent)

    # The thread node is upserted by id, so a bare `add_thread` would let the resumed
    # leg's title/preview replace the original's. Carry only the fields that genuinely
    # advance with the session.
    #
    # Subscripted, not `.get()` with a None fallback: reaching here means `claimed_by`
    # already holds this id for this source, and the admit that set it also ran
    # `result.add_thread(doc.thread)` — `RolloutDoc.thread_id` is an alias of
    # `thread.id` (codex_rollout.py:96,103), so the node is present by construction. A
    # `if node is None: add_thread(...)` fallback was written here first and proved
    # unreachable (coverage found it, nothing could exercise it). Restoring it would
    # convert a broken invariant into a silent divergence — the resumed leg's title
    # quietly replacing the original's — where a KeyError says so immediately.
    node = result.threads[doc.thread.id]
    node.updated_at_ms = max(node.updated_at_ms or 0, doc.thread.updated_at_ms or 0)
    node.tokens_used = max(node.tokens_used or 0, doc.thread.tokens_used or 0)
    node.rollout_path = doc.thread.rollout_path or node.rollout_path

    # One IndexSource per FILE regardless, so every leg is checkpointed and a resumed
    # ingest stays resumable. Each carries the SAME merged conversation object, so
    # whichever sources the checkpoint replays, the row written is the complete one.
    sources.append(index.IndexSource(file=path, content_hash=_fingerprint(prior),
                                     records=[prior]))


def _add_edge(result, edge, seen_edges):
    """Add one spawn edge unless (parent, child) was already declared by any source. The
    first declaration owns the edge's `status`; a later duplicate is dropped."""
    key = (edge.parent_thread_id, edge.child_thread_id)
    if key not in seen_edges:
        seen_edges.add(key)
        result.add_edge(edge)


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
