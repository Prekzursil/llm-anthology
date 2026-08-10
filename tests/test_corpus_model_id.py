"""DECISION D-5, the half that ships: PERSIST `model_id` as a first-class fact.

WHAT THE ADAPTERS ALREADY CAPTURE. `adapters/claude_code.py:698` puts the model the
assistant actually answered with into `Conversation.meta["model_id"]`, and
`adapters/grok.py:556` puts `summary.current_model_id` in the same key. Two adapters, one
agreed spelling, and nothing in `corpus.py` had ever heard of it.

THE PREMISE THIS UNIT WAS GIVEN WAS STALE, AND THE CORRECTION IS THE REASON THESE TESTS
LOOK THE WAY THEY DO. The unit was scoped as "the adapters capture model_id and then it is
THROWN AWAY — `add_conversation` persists only `_CONV_COLS` and `conv.meta` is never
written", i.e. unrecoverable data loss. MEASURED on this tree before writing a line of
implementation (synthetic conversation, `model_id="claude-opus-4-20250514"`, in-memory
index):

    conversation_bodies.meta          -> {"model_id":"claude-opus-4-20250514","cwd":"/tmp"}
    load_conversation_body(...)[1]    -> {"model_id": "claude-opus-4-20250514", ...}
    json_extract(meta,'$.model_id')   -> matches the row

So `conv.meta` IS written — G-4's `set_conversation_body` (`corpus.py:394`) stores it as
compact JSON beside the archive, and `model_id` round-trips. **There is no unrecoverable
loss on a schema-version-2 index, and this file does not claim one.** What was actually
wrong is narrower, and all four parts are still worth fixing:

  1. NO NAMED CONTRACT. `corpus.py` never spelled `model_id`, so the fact lived only as an
     untyped key inside a blob the adapters own privately. Either adapter renaming its key
     would drop the fact with nothing going red.
  2. NO QUERYABILITY WITHOUT A JSON DECODER. Answering "which conversations used model X"
     needed `json_extract` over every body row — which works (SQLite ships JSON1 here) but
     cannot be indexed, is not part of any contract, and reaches into another unit's table.
     `test_the_recorded_model_is_queryable_by_plain_SQL` pins the replacement.
  3. THE ONLY CONTRACTED READ PATH INFLATES THE WHOLE BODY. `load_conversation_body`
     decodes every frame of the archive to hand back `meta`, so reading one short string off
     the measured widest conversation (18.0 M chars) costs inflating all of it.
     `test_reading_the_model_touches_only_its_own_table` measures that the new reader does
     not.
  4. THE FACT DIED WITH THE BODY. Delete or fail to store a body and the model went with
     it. `test_the_model_survives_a_body_that_is_GONE` pins that it no longer does.

SHAPE: one additive TABLE, `conversation_models`, never a column on `conversations` — the
schema's own migration rule at `corpus.py:249-256`. So it simply appears, EMPTY, the first
time this build opens an index that predates it, and "no row" means "not recorded yet",
exactly as `rollout_legs` documents at `corpus.py:288-291`.
`test_an_index_built_BEFORE_this_table_still_works` is that migration, end to end.

NOT BUILT, DELIBERATELY: the cost ledger and model-reliance reporting on top of this. D-5
was split under the EVPI gate and the analysis half is DEFERRED. There is therefore no
aggregate/facet function here, and the read surface is not wired to the sidecar RPC or the
cockpit yet — `conversation_model` has no production caller in this commit, which is stated
plainly rather than dressed up: this unit closes the persistence hole, and exposing it is a
separate unit. UNVERIFIED that any UI need is met by this alone; the settling experiment is
whichever unit adds a `conversation.model` RPC and finds out whether one string per
conversation is enough.

DISTINCT FROM `threads.model_provider`, which is the VENDOR — measured 'openai' on 92.8% of
real Codex rollouts and never a model name (`corpus.py:72-76`).
`test_the_model_id_is_not_the_vendor_in_threads` keeps the two from being conflated again.

THESE TESTS WERE MUTATION-CHECKED, because a green suite is not evidence that it protects a
behaviour. Each decision below was reverted in `corpus.py` in turn and this file (plus
`test_schema_version`, `test_corpus_bodies`, `test_citation_anchors`) had to go RED. 8/8
killed, 0 survivors:

  1. the `set_conversation_model` call removed from `add_conversation` (unit fully reverted)
  2. `_MODEL_SCHEMA` dropped out of the composed `_SCHEMA` — the silent one: every DDL
     statement is `IF NOT EXISTS`, so nothing raises, the table just stops existing
  3. authoritative-ERASE on an absent model instead of the monotone rule
  4. a blank value recorded as an empty-string row
  5. a non-string value coerced with `str()` instead of dropped
  6. `MODEL_ID_KEY` renamed out from under the two adapters
  7. an extra unused column added to the new table
  8. the reader re-joined to `conversation_bodies` (inflating the archive again)

The harness that did it was deliberately NOT committed: it rewrites `llm_anthology/corpus.py`
in place, and a runnable source-mutator in a tree where other agents are working is a way to
destroy someone else's edit. The mutations are recorded here instead so the check is
reproducible by hand, which is the part that has to survive.

PRIVACY: synthetic fixtures only. Every model name below was invented for this file; none
was read off the owner's corpus.
"""
import sqlite3
from pathlib import Path

import pytest

from llm_anthology import corpus, ir

_OPEN = []


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _open(path=":memory:"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _OPEN.append(conn)
    return corpus.init_index(conn)


def _conv(cid="c1", model_id="synth-model-1", **extra_meta):
    """A synthetic conversation shaped like what the two capturing adapters emit.

    `model_id=None` means the key is ABSENT entirely, which is what every adapter that
    does not capture a model produces — not an empty string.
    """
    meta = dict(extra_meta)
    if model_id is not None:
        meta["model_id"] = model_id
    return ir.Conversation(
        id=cid, title="a synthetic title", provider="claude-code",
        turns=[ir.Turn(role="human", uuid="u-1", timestamp="2026-01-01T00:00:01Z",
                       blocks=[ir.Block(type="text", text="alpha bravo")])],
        created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:02Z",
        account="", meta=meta)


# ------------------------------------------------------------------- the schema

def test_the_models_table_is_created_by_init_index():
    conn = _open()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "conversation_models" in names


def test_the_model_is_a_new_TABLE_and_conversations_gained_no_column():
    """The schema's own migration rule (`corpus.py:249-256`), asserted rather than trusted.

    A new COLUMN on `conversations` would be silently ABSENT on every existing index and
    every INSERT naming it would raise — a migration, not a no-op. So `_CONV_COLS` must be
    untouched by this change.
    """
    conn = _open()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
    assert cols == set(corpus._CONV_COLS) | {"rowid"}
    assert "model_id" not in cols
    model_cols = {r[1] for r in conn.execute("PRAGMA table_info(conversation_models)")}
    assert model_cols == {"conversation_id", "model_id"}


def test_one_row_per_conversation():
    """`conversation_id` is the PRIMARY KEY, so a replay cannot accumulate rows. Per-TURN
    model attribution would need a different shape and belongs to the DEFERRED cost-ledger
    half of D-5, not here."""
    conn = _open()
    pk = [r[1] for r in conn.execute("PRAGMA table_info(conversation_models)") if r[5]]
    assert pk == ["conversation_id"]


# -------------------------------------------------------------- the write path

def test_add_conversation_persists_the_model_the_adapter_captured():
    """The whole unit in one assertion: the real ingest path now keeps the fact.

    Via `add_conversation` rather than the writer directly, because that is the only
    function both production callers reach — `index.py:164` and `loaders.py:1147`.
    """
    conn = _open()
    corpus.add_conversation(conn, _conv(model_id="synth-opus-9"))
    assert corpus.conversation_model(conn, "c1") == "synth-opus-9"


def test_a_conversation_with_NO_model_id_records_no_row():
    """Absence is recorded as absence, never as an empty-string row.

    An empty row would read as "recorded, and it was blank", which is a fact no adapter
    ever reported. Most adapters (codex, chatgpt, gemini, claude export) capture no model
    at all, so this is the common case, not the edge.
    """
    conn = _open()
    corpus.add_conversation(conn, _conv(model_id=None))
    assert corpus.conversation_model(conn, "c1") is None
    assert conn.execute("SELECT COUNT(*) FROM conversation_models").fetchone()[0] == 0


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_a_blank_model_id_records_no_row(value):
    """A whitespace-only value is absence too. The adapters run their strings through `_s`,
    which strips — but `meta` is a plain dict any future adapter can write, so the rule is
    enforced here rather than assumed upstream."""
    conn = _open()
    corpus.add_conversation(conn, _conv(model_id=value))
    assert corpus.conversation_model(conn, "c1") is None


@pytest.mark.parametrize("value", [5, ["synth-model-1"], {"name": "x"}, True])
def test_a_NON_STRING_model_id_is_not_recorded(value):
    """Not coerced with `str()`. A model id is a name the provider reported; manufacturing
    `"5"` or `"['synth-model-1']"` out of a wrong-typed value would put a string that no
    provider ever emitted into the one table whose whole purpose is fidelity. Dropping it
    leaves the honest answer (`None` — not recorded) and the raw value is still in the
    archived `meta` for anyone diagnosing the adapter."""
    conn = _open()
    corpus.add_conversation(conn, _conv(model_id=value))
    assert corpus.conversation_model(conn, "c1") is None


def test_a_recorded_model_is_never_the_empty_string():
    """The reader's contract has exactly two answers — a non-empty name, or None. Nothing
    writes a blank, so a caller never has to distinguish "" from None."""
    conn = _open()
    for i, value in enumerate(["synth-a", "", "  ", None, "synth-b"]):
        corpus.add_conversation(conn, _conv(cid="c%d" % i, model_id=value))
    stored = [r[0] for r in conn.execute("SELECT model_id FROM conversation_models")]
    assert stored and all(s.strip() for s in stored)


def test_the_writer_takes_the_whole_meta_dict():
    """`set_conversation_model` is handed `meta`, not a pre-extracted string, so which key
    holds the model is known in ONE place — next to the table and the column list, the same
    rule `_CONV_COLS` and `indexed_provider` exist for."""
    conn = _open()
    corpus.set_conversation_model(conn, "c9", {"model_id": "synth-direct", "cwd": "/tmp"})
    assert corpus.conversation_model(conn, "c9") == "synth-direct"


def test_a_None_meta_writes_nothing():
    """`ir.Conversation.meta` defaults to `{}`, but `set_conversation_body` already tolerates
    None and this writer is called beside it, so it tolerates None too rather than raising
    from inside an ingest."""
    conn = _open()
    corpus.set_conversation_model(conn, "c9", None)
    assert corpus.conversation_model(conn, "c9") is None


# ------------------------------------------------------------- the read contract

def test_no_row_reads_as_None_meaning_NOT_RECORDED_YET():
    """None is BOTH "this id is not in the index" and "this index predates the table". The
    caller must treat it as "not recorded", never as "this conversation had no model" — the
    same contract `rollout_legs` states at `corpus.py:288-291`."""
    conn = _open()
    assert corpus.conversation_model(conn, "never-indexed") is None


def test_the_reader_works_on_a_bare_connection_with_no_row_factory():
    """Several callers hand in a `sqlite3.connect(...)` with no `row_factory`, so the row is
    read POSITIONALLY — `row["model_id"]` would raise on a plain tuple. `indexed_provider`
    carries the same note for the same reason."""
    conn = sqlite3.connect(":memory:")
    _OPEN.append(conn)
    corpus.init_index(conn)
    corpus.add_conversation(conn, _conv(model_id="synth-bare"))
    assert corpus.conversation_model(conn, "c1") == "synth-bare"


def test_reading_the_model_touches_only_its_own_table():
    """Reason (3) from the header, MEASURED rather than asserted by eye.

    The pre-existing contracted path to this fact is `load_conversation_body`, which decodes
    every frame of the seekable archive to hand back `meta`. This reader must not: the SQL
    it executes is captured with `set_trace_callback` and must name `conversation_models`
    and nothing else.
    """
    conn = _open()
    corpus.add_conversation(conn, _conv(model_id="synth-cheap"))
    seen = []
    conn.set_trace_callback(seen.append)
    try:
        assert corpus.conversation_model(conn, "c1") == "synth-cheap"
    finally:
        conn.set_trace_callback(None)
    assert len(seen) == 1, "one statement, not a join and not a decode: %r" % (seen,)
    assert "conversation_models" in seen[0]
    assert "conversation_bodies" not in seen[0] and "archive" not in seen[0]


def test_the_model_survives_a_body_that_is_GONE():
    """Reason (4): the fact no longer dies with the archive.

    A body row can be absent for real — every index built before G-4 has none, and
    `test_sidecar_bodies.py` exercises exactly that fallback. Before this table the model
    went with it; now `load_conversation_body` answering None (the documented "fall back")
    leaves the model still readable.
    """
    conn = _open()
    corpus.add_conversation(conn, _conv(model_id="synth-durable"))
    conn.execute("DELETE FROM conversation_bodies WHERE conversation_id=?", ("c1",))
    assert corpus.load_conversation_body(conn, "c1") is None
    assert corpus.conversation_model(conn, "c1") == "synth-durable"


def test_the_recorded_model_is_queryable_by_plain_SQL():
    """Reason (2): the aggregate question needs no JSON decoder and no archive.

    This is the one thing the stored `meta` blob genuinely could not do on a contract — it
    needed `json_extract` over `conversation_bodies`, which cannot be indexed and reaches
    into another unit's table. The DEFERRED cost ledger is what would CONSUME this; the
    query itself is the affordance shipping now.
    """
    conn = _open()
    corpus.add_conversation(conn, _conv(cid="a1", model_id="synth-opus-9"))
    corpus.add_conversation(conn, _conv(cid="a2", model_id="synth-opus-9"))
    corpus.add_conversation(conn, _conv(cid="a3", model_id="synth-haiku-3"))
    corpus.add_conversation(conn, _conv(cid="a4", model_id=None))
    hits = sorted(r[0] for r in conn.execute(
        "SELECT conversation_id FROM conversation_models WHERE model_id=?",
        ("synth-opus-9",)))
    assert hits == ["a1", "a2"]
    # `tuple(row)` explicitly rather than `dict(cursor)`: the row_factory here is
    # `sqlite3.Row`, and relying on dict() unpacking it is a cross-version bet this suite
    # runs on a python matrix and cannot check locally.
    counts = dict(tuple(row) for row in conn.execute(
        "SELECT model_id, COUNT(*) FROM conversation_models GROUP BY model_id"))
    assert counts == {"synth-opus-9": 2, "synth-haiku-3": 1}


def test_the_model_id_is_not_the_vendor_in_threads():
    """`threads.model_provider` is the VENDOR ('openai' on 92.8% of measured Codex rollouts)
    and is explicitly NOT a model name (`corpus.py:72-76`). Conflating them is what made
    every Codex node render as the palette's unknown grey, so the two facts are asserted to
    live in different tables with different values."""
    conn = _open()
    corpus.upsert_thread(conn, corpus.ThreadMeta(id="t1", model_provider="synth-vendor"))
    corpus.add_conversation(conn, _conv(model_id="synth-opus-9"), thread_id="t1")
    assert corpus.load_corpus(conn).threads["t1"].model_provider == "synth-vendor"
    assert corpus.conversation_model(conn, "c1") == "synth-opus-9"


# -------------------------------------------------------- replay and re-ingest

def test_a_replay_of_the_same_ingest_is_a_no_op():
    """Idempotent, like every other writer here: the PK dedupes and a resumed build leaves
    one row with the same value."""
    conn = _open()
    for _ in range(3):
        corpus.add_conversation(conn, _conv(model_id="synth-opus-9"))
    assert conn.execute("SELECT COUNT(*) FROM conversation_models").fetchone()[0] == 1
    assert corpus.conversation_model(conn, "c1") == "synth-opus-9"


def test_a_reingest_that_REPORTS_A_DIFFERENT_model_replaces_the_row():
    """A non-empty value is authoritative over a non-empty value. A session re-parsed from a
    repaired source must read back repaired — the rule `set_conversation_body` states."""
    conn = _open()
    corpus.add_conversation(conn, _conv(model_id="synth-opus-9"))
    corpus.add_conversation(conn, _conv(model_id="synth-opus-10"))
    assert corpus.conversation_model(conn, "c1") == "synth-opus-10"
    assert conn.execute("SELECT COUNT(*) FROM conversation_models").fetchone()[0] == 1


@pytest.mark.parametrize("lost", [None, "", "   ", 5])
def test_a_reingest_that_LOST_the_model_does_not_erase_the_recorded_one(lost):
    """THE ONE PLACE THIS WRITER DELIBERATELY BREAKS FROM `set_conversation_rollouts`.

    That function REPLACES its whole list because "the ingest is authoritative": a leg the
    user deleted must disappear or the reader is sent at a file the ingest no longer
    believes in. Here the same rule would be erasure, not authority — an absent `model_id`
    is not the claim "this conversation had no model", it is the ADAPTER not capturing one
    (a partial re-parse, a truncated first assistant record, a renamed upstream key). This
    table records POSITIVE knowledge only, so it is monotone and can never lose a fact it
    once held.

    The cost, stated rather than hidden: a genuinely CORRECTED-to-nothing model cannot be
    unrecorded through this path. Not reachable by the real ingest — both capturing adapters
    take the FIRST model seen in an append-only log, so a re-parse yields the same value or
    none. Settling experiment if it ever matters: an explicit `forget_conversation_model`,
    which nothing has asked for.
    """
    conn = _open()
    corpus.add_conversation(conn, _conv(model_id="synth-opus-9"))
    corpus.add_conversation(conn, _conv(model_id=lost))
    assert corpus.conversation_model(conn, "c1") == "synth-opus-9"


# ------------------------------------------------------ an index built BEFORE this

def test_an_index_built_BEFORE_this_table_still_works():
    """The migration, end to end — the brief's explicit requirement.

    An index that predates this change is reproduced by DROPping the table from a populated
    index (the same technique `test_schema_version._premarker_index` uses to reproduce a
    pre-G-4 index). Re-opening it must: recreate the table EMPTY, answer "not recorded" for
    a conversation that IS indexed, leave search working, and record the model on the next
    ingest of that conversation. No ALTER, no rebuild, no refusal.

    This is also why `SCHEMA_VERSION` is NOT bumped by this change — see the note beside
    `_MODEL_SCHEMA` in `corpus.py`.
    """
    conn = _open()
    corpus.add_conversation(conn, _conv(model_id="synth-opus-9"))
    conn.executescript("DROP TABLE conversation_models;")
    assert corpus.conversation_model is not None       # the module still imports fine

    corpus.init_index(conn)                            # what re-opening the index does
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "conversation_models" in names
    assert conn.execute("SELECT COUNT(*) FROM conversation_models").fetchone()[0] == 0
    assert corpus.conversation_model(conn, "c1") is None, "no row means NOT RECORDED yet"
    assert [r["conversation_id"] for r in corpus.search(conn, "alpha")] == ["c1"]

    corpus.add_conversation(conn, _conv(model_id="synth-opus-9"))
    assert corpus.conversation_model(conn, "c1") == "synth-opus-9", "a rebuild repopulates"


def test_reopening_an_index_that_already_has_the_table_is_a_no_op():
    """`init_index` is idempotent and must not truncate what is already recorded — the
    `New-Item -Force` shape of bug, in SQL."""
    conn = _open()
    corpus.add_conversation(conn, _conv(model_id="synth-opus-9"))
    corpus.init_index(conn)
    assert corpus.conversation_model(conn, "c1") == "synth-opus-9"


def test_the_ddl_carries_no_percent_so_init_index_can_format_it():
    """`init_index` runs `%`-formatting over the whole concatenated schema, so a literal `%`
    anywhere in it raises at open time. `_BODIES_SCHEMA` carries the same warning in prose;
    this asserts it for both appended blocks instead."""
    assert "%" not in corpus._MODEL_SCHEMA
    assert "%" not in corpus._BODIES_SCHEMA


def test_the_composed_schema_still_contains_every_block():
    """`init_index` was changed to format ONE composed constant instead of concatenating two
    inline, so that a third block could be added without splitting a line whose position is
    cited (`corpus.py:303`, `corpus.py:347`). If a later edit drops a block from `_SCHEMA`,
    the table it creates disappears silently — every statement is `IF NOT EXISTS`, so nothing
    raises; the index just quietly stops having it."""
    assert corpus.INDEX_SCHEMA in corpus._SCHEMA
    assert corpus._BODIES_SCHEMA in corpus._SCHEMA
    assert corpus._MODEL_SCHEMA in corpus._SCHEMA


# ----------------------------------------------------- the key is a CONTRACT now

def test_both_capturing_adapters_use_the_contracted_key():
    """Reason (1) from the header, and the test that makes it stop being true.

    Before this, `corpus.py` never spelled `model_id`, so the fact travelled on an untyped
    key two adapters happened to agree on. Either of them renaming it — to `model`, `modelId`,
    `current_model_id` — would have dropped the fact with NOTHING going red, because no test
    and no schema anywhere named it. `MODEL_ID_KEY` is the contract; this asserts the two
    producers still honour the spelling.

    A SOURCE-TEXT assertion, in the same spirit as `test_citation_anchors.py`, because the
    alternative is to run both adapters over synthetic fixtures — which those adapters' own
    suites already do — and this file may not edit them (they are a sibling agent's scope).
    So it pins the SPELLING, which is the thing that would silently drift, and says so.
    """
    root = Path(__file__).resolve().parents[1] / "llm_anthology" / "adapters"
    spelling = '"%s":' % corpus.MODEL_ID_KEY
    for name in ("claude_code.py", "grok.py"):
        path = root / name
        # Not `pytest.skip`: a skip on a missing input is a green build proving nothing.
        assert path.exists(), (
            "cannot find %s — this pin verifies nothing until the path is re-pointed" % path)
        assert spelling in path.read_text(encoding="utf-8"), (
            "%s no longer writes %s into Conversation.meta. Either it stopped capturing the "
            "model (then corpus.MODEL_ID_KEY has one producer, not two, and this test should "
            "say so) or it renamed the key (then the fact is being dropped on the floor — "
            "point it back at corpus.MODEL_ID_KEY)." % (name, spelling))


def test_the_contracted_key_is_the_spelling_the_adapters_shipped():
    """Pinned as a literal, not derived: the value IS the wire contract with two adapters and
    every index already on disk. Changing it silently orphans every recorded row."""
    assert corpus.MODEL_ID_KEY == "model_id"
