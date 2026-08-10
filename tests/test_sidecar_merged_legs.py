"""A merged conversation must be READABLE from every leg, not only recorded as merged.

THE DEFECT. `loaders._merge_resumed_leg` folds a resumed session's rollout legs into ONE
conversation, so the FTS body spans every leg. The leg list reached `meta["rollout_paths"]`
on the returned in-memory Corpus and stopped there — `_CONV_COLS` has no meta column — and
the cockpit never sees that object, because it spawns a sidecar against the index FILE. So
`conversation.get` re-parsed the single `conversations.rollout_path`, which for a merged
conversation is the LAST leg: a search could match text and open a transcript that does not
contain it. Measured on the real store: 236 conversations merged from 1189 files, the widest
spanning 66 legs.

TEST DISCIPLINE — the blind spot that let the whole class through. Four defects in this area
shared one shape: every guarding test asserted on the returned `Corpus` and never on what the
SQLite index held, and each stayed green for the entire life of its bug. So every assertion
here goes through a REOPENED connection or the sidecar RPC.
`test_the_index_records_every_leg_ON_DISK` deliberately uses a bare `sqlite3.connect` rather
than `corpus.open_index`, so it cannot pass by re-deriving anything in memory — it reads the
bytes the ingest left behind.

SYNTHETIC fixtures only: no real conversation content, thread id or path appears here. The
per-file `_write_rollout`/`_session_meta` helpers are local by the convention this suite
already follows — six other test modules define their own, and there is no conftest.
"""
import json
import os
import sqlite3

from llm_anthology import corpus, loaders, research, sidecar

#: Nonsense terms so a text assertion cannot pass by accident on shared boilerplate. Each
#: appears in exactly ONE leg, which is what makes "the reader lost a leg" observable.
LEG_ONE_ONLY = "quaggaflux"
LEG_TWO_ONLY = "zibbervane"


# ------------------------------------------------------------------- fixtures

def _write_rollout(day_dir, name, records):
    os.makedirs(day_dir, exist_ok=True)
    path = os.path.join(day_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


def _rec(rtype, payload, ts):
    return {"type": rtype, "timestamp": ts, "payload": payload}


def _session_meta(sid, ts):
    return _rec("session_meta",
                {"session_id": sid, "id": sid, "timestamp": ts, "cwd": "/repo",
                 "model_provider": "openai"}, ts)


def _user(text, ts):
    return _rec("response_item",
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": text}]}, ts)


def _assistant_with_id(text, ts, item_id):
    """An assistant turn carrying the provider's opaque item id — the shape that lets one
    id arrive twice with two different bodies (`_merge_resumed_leg`'s divergence case)."""
    return _rec("response_item",
                {"type": "message", "role": "assistant", "id": item_id,
                 "content": [{"type": "output_text", "text": text}]}, ts)


def _two_leg_store(tmp_path, first_records=None, second_records=None):
    """One resumed session C1 across TWO rollout files. Returns (index, leg1, leg2, corpus).

    By default leg two REPLAYS leg one's opening turn — the real shape, since `codex resume`
    re-states the prefix — so a reader that merely concatenated would show it twice.
    """
    sessions, home = tmp_path / "sessions", tmp_path / "no_state"
    idx = str(tmp_path / "index.sqlite")
    day = os.path.join(str(sessions), "2026", "07", "24")
    shared = _user("the shared prefix", "2026-07-24T10:00:01.000Z")
    first = _write_rollout(day, "rollout-2026-07-24T10-00-00-0000c1a.jsonl",
                           first_records or [
                               _session_meta("C1", "2026-07-24T10:00:00.000Z"),
                               shared,
                               _user(LEG_ONE_ONLY, "2026-07-24T10:00:02.000Z"),
                           ])
    second = _write_rollout(day, "rollout-2026-07-24T12-00-00-0000c1b.jsonl",
                            second_records or [
                                _session_meta("C1", "2026-07-24T12:00:00.000Z"),
                                shared,
                                _user(LEG_TWO_ONLY, "2026-07-24T12:00:01.000Z"),
                            ])
    home.mkdir()
    built, _errors = loaders.load_corpus(str(sessions), idx, codex_home=str(home))
    return idx, first, second, built


def _one_leg_store(tmp_path):
    """A session with exactly ONE rollout — the un-merged majority, which must keep the
    byte-identical behaviour it had before legs existed."""
    sessions, home = tmp_path / "sessions", tmp_path / "no_state"
    idx = str(tmp_path / "index.sqlite")
    day = os.path.join(str(sessions), "2026", "07", "24")
    only = _write_rollout(day, "rollout-2026-07-24T10-00-00-0000d1.jsonl", [
        _session_meta("D1", "2026-07-24T10:00:00.000Z"),
        _user(LEG_ONE_ONLY, "2026-07-24T10:00:01.000Z"),
    ])
    home.mkdir()
    loaders.load_corpus(str(sessions), idx, codex_home=str(home))
    return idx, only


def _read(idx, method, params):
    """Answer ONE RPC against the index FILE, through a connection this test opened —
    never against the object the ingest returned."""
    conn = corpus.open_index(idx)
    try:
        return sidecar.Sidecar(conn).dispatch(method, params)
    finally:
        conn.close()


def _text(dto):
    return " ".join(b["text"] for t in dto["turns"] for b in t["blocks"])


def _forget_bodies(idx):
    """Put the index FILE in the pre-G-4 state: rows indexed, no archived bodies.

    Since G-4, `conversation.get` serves `conversation_bodies` and re-parses `rollout_path`
    only when no body is stored — and the ingest stores the ALREADY-MERGED conversation, so
    the archive answers correctly no matter what happens to the legs afterwards. That is the
    point of the feature and it is why the three tests below have to reach the fall-back
    deliberately: their subject is the READ-TIME fold, which a stored body makes unnecessary.

    Each of them says in its own docstring what the archive now does instead, because "the
    reader no longer needs the legs" is the interesting half and deleting the old assertion
    without recording it would hide a real improvement.
    """
    conn = sqlite3.connect(idx)
    try:
        conn.execute("DELETE FROM conversation_bodies")
        conn.commit()
    finally:
        conn.close()


def _legs_on_disk(idx, cid):
    """The recorded legs, read through a connection that applies NO schema."""
    conn = sqlite3.connect(idx)
    try:
        return corpus.rollout_legs(conn, cid)
    finally:
        conn.close()


# ---------------------------------------------------------------- the leg record

def test_the_index_records_every_leg_ON_DISK(tmp_path):
    """The claim the old in-memory test could not make: the leg list survives to the FILE.

    Read back through a bare `sqlite3.connect` — not `corpus.open_index` — so nothing here
    can be satisfied by schema application or an in-memory rebuild.
    """
    idx, first, second, _built = _two_leg_store(tmp_path)
    assert _legs_on_disk(idx, "C1") == [first, second]


def test_a_single_leg_conversation_records_its_one_file(tmp_path):
    """Every conversation records its legs, merged or not, so the reader has one rule
    rather than a special case it can get wrong."""
    idx, only = _one_leg_store(tmp_path)
    assert _legs_on_disk(idx, "D1") == [only]


def test_an_unrecorded_conversation_reads_back_as_no_legs(tmp_path):
    """`rollout_legs` answers [] rather than raising for an id it has never seen — the
    same state a pre-fix index is in for EVERY conversation."""
    idx, _first, _second, _built = _two_leg_store(tmp_path)
    assert _legs_on_disk(idx, "no-such-conversation") == []


# ------------------------------------------------------------------- the reader

def test_conversation_get_renders_the_FIRST_leg_not_only_the_last(tmp_path):
    """The user-visible half. Before this, the transcript held only the last leg."""
    idx, _first, _second, _built = _two_leg_store(tmp_path)
    out = _read(idx, "conversation.get", {"id": "C1"})
    assert out["available"] is True
    body = _text(out)
    assert LEG_ONE_ONLY in body, "the first leg's text must reach the reader"
    assert LEG_TWO_ONLY in body, "and the last leg's, as before"


def test_a_search_hit_OPENS_to_a_transcript_that_contains_it(tmp_path):
    """The inconsistency stated end to end: search matched leg one, the reader could not
    show it. Both halves run against the same index file in one test, so neither can
    regress without this going red."""
    idx, _first, _second, _built = _two_leg_store(tmp_path)
    conn = corpus.open_index(idx)
    try:
        server = sidecar.Sidecar(conn)
        hits = server.dispatch("search.query", {"q": LEG_ONE_ONLY})["hits"]
        assert [h["conversation_id"] for h in hits] == ["C1"], \
            "the FTS body already spans every leg"
        out = server.dispatch("conversation.get", {"id": hits[0]["conversation_id"]})
    finally:
        conn.close()
    assert LEG_ONE_ONLY in _text(out)


def test_the_reader_folds_with_the_INGEST_policy_not_a_second_copy_of_it(tmp_path):
    """The reader must reuse `loaders.fold_leg_turns`, not re-implement it: a private copy
    is free to drift, and the drift would be invisible — both sides would still "work".

    Asserting the two produce the SAME turn sequence is the strongest available check that
    the policy is shared. A re-implementation that dropped the de-duplication would show
    the replayed prefix twice here.
    """
    idx, _first, _second, built = _two_leg_store(tmp_path)
    in_memory = [b.text for t in built.conversations[0].turns for b in t.blocks]
    out = _read(idx, "conversation.get", {"id": "C1"})
    assert [b["text"] for t in out["turns"] for b in t["blocks"]] == in_memory
    assert in_memory == ["the shared prefix", LEG_ONE_ONLY, LEG_TWO_ONLY], \
        "the replayed prefix appears ONCE, and both legs' own turns survive"


def test_the_reader_reports_how_many_legs_it_folded(tmp_path):
    idx, _first, _second, _built = _two_leg_store(tmp_path)
    out = _read(idx, "conversation.get", {"id": "C1"})
    assert out["meta"]["rollout_legs"] == 2
    assert "rollout_legs_unreadable" not in out["meta"]
    assert out["meta"]["rollout_path"] == "rollout-2026-07-24T12-00-00-0000c1b.jsonl", \
        "the resume target stays the LAST leg, basenamed, exactly as the ingest records it"


def test_a_single_leg_conversation_reads_exactly_as_before(tmp_path):
    """No leg bookkeeping on the wire for the un-merged case: `rollout_legs` absent means
    one file, which is what every conversation was before the merge existed."""
    idx, _only = _one_leg_store(tmp_path)
    out = _read(idx, "conversation.get", {"id": "D1"})
    assert out["available"] is True
    assert LEG_ONE_ONLY in _text(out)
    assert "rollout_legs" not in out["meta"]


def _lossy_two_leg_store(tmp_path):
    """One provider item id arriving twice with two different bodies: the run was cut short,
    so leg one holds only the opening of the answer and leg two holds the whole thing."""
    return _two_leg_store(
        tmp_path,
        first_records=[
            _session_meta("C1", "2026-07-24T10:00:00.000Z"),
            _assistant_with_id("short", "2026-07-24T10:00:01.000Z", "msg_run"),
        ],
        second_records=[
            _session_meta("C1", "2026-07-24T12:00:00.000Z"),
            _assistant_with_id("the whole answer, at length",
                               "2026-07-24T12:00:01.000Z", "msg_run"),
        ])


def test_a_LOSSY_reconciliation_is_visible_to_the_reader(tmp_path):
    """One provider item id, two renderings: the richer body wins and the count is
    surfaced. A reconciliation that drops characters must never be silent.

    A CORRECTED CLAIM. This docstring used to say the count is "RE-DERIVED by the reader's own
    fold rather than read from a column, so it describes what the reader actually reconciled".
    That was true when every read re-parsed, and G-4 made it false for a current index: the
    INGEST performs the fold, records `merge_divergent_turns` in `conv.meta`, and the archive
    carries it to the wire — nothing is re-derived here. The property that survives, and the
    one that actually matters, is that the count describes a fold that really happened; which
    side of the ingest/read boundary performed it is not what the assertion is about.

    The read-time re-derivation still exists for a pre-G-4 index and is covered by the twin
    below, which is where that original sentence is true.
    """
    idx, _first, _second, _built = _lossy_two_leg_store(tmp_path)
    out = _read(idx, "conversation.get", {"id": "C1"})
    assert _text(out) == "the whole answer, at length", \
        "the fuller rendering replaces the truncated one, in place"
    assert out["meta"]["merge_divergent_turns"] == 1


def test_a_LOSSY_reconciliation_is_RE_DERIVED_on_a_pre_G4_index(tmp_path):
    """The twin: with no stored body the reader folds the legs itself and must reach the same
    verdict, so the disclosure does not depend on which path answered.

    This is the assertion the test above used to make. It is kept as a separate test rather
    than folded in because the two exercise DIFFERENT code — `_reparse_conversation`'s fold
    versus `_archived_conversation`'s pass-through — and a single test could only cover one of
    them while appearing to cover the feature.
    """
    idx, _first, _second, _built = _lossy_two_leg_store(tmp_path)
    _forget_bodies(idx)
    out = _read(idx, "conversation.get", {"id": "C1"})
    assert _text(out) == "the whole answer, at length"
    assert out["meta"]["merge_divergent_turns"] == 1
    assert out["meta"]["rollout_legs"] == 2


def test_a_clean_merge_reports_NO_divergence(tmp_path):
    """The control for the assertion above: with nothing replaced, nothing is claimed."""
    idx, _first, _second, _built = _two_leg_store(tmp_path)
    out = _read(idx, "conversation.get", {"id": "C1"})
    assert "merge_divergent_turns" not in out["meta"]


# ----------------------------------------------------- degrading, not exploding

def test_an_index_that_PREDATES_the_legs_table_still_reads(tmp_path):
    """The migration promise. An index built before this change has no leg rows at all;
    `init_index` adds the table (IF NOT EXISTS, so nothing is dropped or rebuilt) and the
    reader falls back to `conversations.rollout_path` — the exact behaviour it had before.
    No user has to delete their corpus.

    Simulated by DROPPING the table and reopening, which is the state a pre-fix index is in
    the first time the new engine opens it.

    G-4 MAKES THIS SCENARIO UNREACHABLE FOR A CURRENT INDEX, and that is an improvement worth
    stating rather than a reason to delete the test. The ingest stores the already-MERGED
    conversation in `conversation_bodies`, so dropping the legs table no longer costs the
    reader anything — measured: with the body left in place this test's `LEG_ONE_ONLY not in
    body` assertion FAILS, because the first leg is served from the archive. The pre-fix
    limitation therefore only reproduces on a pre-G-4 index, which is what `_forget_bodies`
    builds, and that is the only index that can still be in this state.
    """
    idx, _first, second, _built = _two_leg_store(tmp_path)
    _forget_bodies(idx)
    raw = sqlite3.connect(idx)
    raw.execute("DROP TABLE conversation_rollouts")
    raw.commit()
    raw.close()

    out = _read(idx, "conversation.get", {"id": "C1"})
    assert out["available"] is True
    body = _text(out)
    assert LEG_TWO_ONLY in body, "the last leg still reads, as it always did"
    assert LEG_ONE_ONLY not in body, \
        "and the pre-fix limitation is honestly reproduced rather than papered over"
    assert out["meta"]["rollout_path"] == os.path.basename(second)


def test_a_CURRENT_index_reads_every_leg_even_with_the_legs_table_GONE(tmp_path):
    """The other side of the test above, and the reason the pre-fix limitation now needs a
    pre-G-4 fixture to reproduce at all.

    G-4 stores the already-MERGED conversation in `conversation_bodies`, so the read-time fold
    — and therefore `conversation_rollouts` — is no longer on the reader's critical path. Drop
    the leg table on a CURRENT index and both legs still render, because nothing is being
    folded at read time any more. Asserted so that the migration story is recorded from both
    ends: the fall-back still honestly reproduces the old limitation, and the archive removes
    the exposure that made the limitation matter.
    """
    idx, _first, _second, _built = _two_leg_store(tmp_path)
    raw = sqlite3.connect(idx)
    raw.execute("DROP TABLE conversation_rollouts")
    raw.commit()
    raw.close()

    body = _text(_read(idx, "conversation.get", {"id": "C1"}))
    assert LEG_ONE_ONLY in body and LEG_TWO_ONLY in body


def test_a_plain_REBUILD_repairs_an_index_that_predates_the_table(tmp_path):
    """...and the repair costs a rebuild, not a delete.

    The leg rows are written by `_persist_graph`, which runs on EVERY build ahead of the
    chunked conversation ingest. That placement is load-bearing: the ingest is
    checkpoint-skippable, so a rebuild over unchanged files never calls `add_conversation`
    again — a repair that lived there would never fire for exactly the users who need it.
    """
    idx, first, second, _built = _two_leg_store(tmp_path)
    raw = sqlite3.connect(idx)
    raw.execute("DROP TABLE conversation_rollouts")
    raw.commit()
    raw.close()

    loaders.load_corpus(str(tmp_path / "sessions"), idx,
                        codex_home=str(tmp_path / "no_state"))

    assert _legs_on_disk(idx, "C1") == [first, second]
    assert LEG_ONE_ONLY in _text(_read(idx, "conversation.get", {"id": "C1"}))


def test_a_MISSING_leg_is_disclosed_rather_than_silently_dropped(tmp_path):
    """A leg the user moved or deleted must not take the whole transcript down, and the
    partial render must SAY it is partial — the reader now knows how many legs it was
    supposed to fold, so silence here would be a new lie in place of the old one.

    ON A CURRENT INDEX THE LEG IS NOT MISSED AT ALL: the merged transcript is in
    `conversation_bodies`, so removing the file costs nothing and there is nothing to
    disclose. The disclosure still has to work for a pre-G-4 index whose sources have
    since moved, which is what `_forget_bodies` builds here."""
    idx, first, _second, _built = _two_leg_store(tmp_path)
    _forget_bodies(idx)
    os.remove(first)

    out = _read(idx, "conversation.get", {"id": "C1"})
    assert out["available"] is True
    assert LEG_TWO_ONLY in _text(out)
    assert LEG_ONE_ONLY not in _text(out)
    assert out["meta"]["rollout_legs"] == 2
    assert out["meta"]["rollout_legs_unreadable"] == 1


def test_when_EVERY_leg_is_gone_the_reader_stubs_with_the_first_reason(tmp_path):
    """The last remaining route to a stub, and it needs a pre-G-4 index to reach: with a
    stored body, losing every source file is exactly the case G-4 makes survivable —
    `tests/test_sidecar_bodies.py` asserts that direction."""
    idx, first, second, _built = _two_leg_store(tmp_path)
    _forget_bodies(idx)
    os.remove(first)
    os.remove(second)

    out = _read(idx, "conversation.get", {"id": "C1"})
    assert out["available"] is False
    assert out["reason"] == "rollout unavailable"
    assert out["turns"] == []


def test_re_recording_the_same_legs_is_a_no_op(tmp_path):
    """A resumed/idempotent build must not churn the table. Re-running the whole ingest
    over unchanged files leaves exactly the rows it left the first time."""
    idx, first, second, _built = _two_leg_store(tmp_path)
    loaders.load_corpus(str(tmp_path / "sessions"), idx,
                        codex_home=str(tmp_path / "no_state"))

    assert _legs_on_disk(idx, "C1") == [first, second]
    conn = sqlite3.connect(idx)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_rollouts").fetchone()[0] == 2
    finally:
        conn.close()


def test_the_LOCAL_research_tier_reads_every_leg_too(tmp_path):
    """The same defect had TWO instances in this file, and closing one would have left the
    other looking fixed. `_research_local` re-parses raw transcripts per row for the on-box
    synthesis tier; on the single `conversations.rollout_path` it fed a merged conversation's
    LAST leg only, so a local summary silently described a fraction of the corpus — 953 of
    1189 files on the measured store. It folds the legs now, through the same reader.

    The prompt is captured rather than assumed: `MockBackend.prompts` records EXACTLY what
    would reach a backend, which is the only way to see what the tier actually read.
    """
    idx, _first, _second, _built = _two_leg_store(tmp_path)
    local = research.MockBackend()
    conn = corpus.open_index(idx)
    try:
        server = sidecar.Sidecar(conn, local_backend=local)
        out = server.dispatch("research.synthesize", {"tier": "local"})
    finally:
        conn.close()
    assert out["conversation_count"] == 1
    assert len(local.prompts) == 1
    assert LEG_ONE_ONLY in local.prompts[0], "the first leg reached the local tier"
    assert LEG_TWO_ONLY in local.prompts[0]


def test_a_leg_that_disappears_from_the_source_leaves_the_record(tmp_path):
    """The record follows the ingest. Remove a leg from the store, rebuild, and the table
    reflects what the merge actually read — a stale path would send the reader at a file
    the ingest itself no longer believes in."""
    idx, first, second, _built = _two_leg_store(tmp_path)
    os.remove(first)
    loaders.load_corpus(str(tmp_path / "sessions"), idx,
                        codex_home=str(tmp_path / "no_state"))

    assert _legs_on_disk(idx, "C1") == [second]
