"""DECISION G-4, half one: the corpus becomes a SELF-CONTAINED ARCHIVE.

THE DEFECT THESE TESTS PIN. `conversations` stored METADATA ONLY (`corpus.py:179-191`) —
no body column, and a contentless FTS index that retains no text. `conversation.get`
re-parsed the transcript from `rollout_path` on EVERY read, so moving or deleting the
source files turned every conversation into an `available:false` stub. Measured on the
owner's live corpus: 11.5 MB of index over 122.1 MB of text across 1,071 conversations,
0 rows with no `rollout_path`, and 400/400 sampled paths still present — which is exactly
why it never surfaced. G-1 says the archive IS the product, so an archive that is a set of
pointers into someone else's files contradicts the foundation.

WHAT IS STORED, AND WHY IN THAT SHAPE. A new `conversation_bodies` table (a new TABLE, never
a new column — `corpus.py:249-256` is the schema's own migration rule) holding a
seekable-zstd archive per conversation, ONE FRAME PER TURN, each frame a JSON record.

  * `llm_anthology.archive` is reused rather than a second compression path hand-rolled.
    It had zero importers and was nearly deleted as dead code; it is precisely the
    random-access-into-compressed-content primitive this needs.
  * per-TURN frames, not one frame per conversation and not one frame per line: a frame is
    independently decompressible, so turn N of an 18.0 M-char conversation (the real
    corpus's largest) opens without inflating the other 17.9 M.
  * the frames hold the structured TURN, not the flat FTS body text. A flat body cannot
    reconstruct roles, uuids, timestamps, branch markers or typed blocks, so serving
    `conversation.get` from it would have replaced a missing transcript with a degraded
    one — the same class of lie, quieter.

PRIVACY: synthetic fixtures only. Every string here was invented for this file.
"""
import json
import sqlite3

import pytest

from llm_anthology import archive, corpus, ir

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


def _conv(cid="c1", turns=None, title="a synthetic title"):
    return ir.Conversation(id=cid, title=title, provider="codex",
                           turns=[] if turns is None else turns,
                           created_at="2026-01-01T00:00:00Z",
                           updated_at="2026-01-01T00:00:00Z", account="acct")


def _turn(role="human", text="a synthetic line", **kw):
    return ir.Turn(role=role, blocks=[ir.Block(type="text", text=text)], **kw)


def _rich_turns():
    """Every IR field a turn can carry, so the round-trip test proves fidelity rather
    than proving that role and text survive."""
    return [
        ir.Turn(role="human", uuid="u-1", timestamp="2026-01-01T00:00:01Z",
                blocks=[ir.Block(type="text", text="what does this do")]),
        ir.Turn(role="assistant", uuid="u-2", timestamp="2026-01-01T00:00:02Z",
                branch={"index": 1, "total": 2},
                blocks=[
                    ir.Block(type="thinking", text="considering the options"),
                    ir.Block(type="tool_use", text="Read",
                             data={"name": "Read", "input": {"path": "/synthetic/x.py"}}),
                    ir.Block(type="text", text="it reads a file",
                             citations=[{"url": "https://example.invalid/doc", "title": "d"}]),
                ]),
    ]


# ------------------------------------------------------------------ the schema

def test_the_bodies_table_is_created_by_init_index():
    conn = _open()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "conversation_bodies" in names


def test_the_body_is_a_new_TABLE_and_conversations_gained_no_column():
    """The schema's own migration rule, asserted rather than trusted: every DDL statement
    is IF NOT EXISTS, so a new TABLE appears (empty) on an index built before it while a
    new COLUMN would be silently absent and every INSERT naming it would raise."""
    conn = _open()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
    assert cols == set(corpus._CONV_COLS) | {"rowid"}
    body_cols = {r[1] for r in conn.execute("PRAGMA table_info(conversation_bodies)")}
    assert body_cols == {"conversation_id", "text_bytes", "meta", "archive"}


def test_conversation_meta_is_stored_alongside_the_turns():
    """`ir.Conversation.meta` is part of the conversation, not decoration: the adapters put
    `thread_id`, the parsed `rollout_path` and their hidden-character audit in it. A reader
    served from the archive without it would answer with an emptier conversation than a
    re-parse did — quietly, which is the failure mode this unit exists to remove."""
    conn = _open()
    conv = _conv(turns=[_turn()])
    conv.meta = {"thread_id": "t7", "rollout_path": "/synthetic/rollout-x.jsonl",
                 "hidden_char_hits": 3}
    corpus.add_conversation(conn, conv)
    assert corpus.load_conversation_body(conn, "c1")[1] == conv.meta


def test_an_absent_meta_round_trips_as_an_empty_dict_not_None():
    conn = _open()
    corpus.add_conversation(conn, _conv(turns=[_turn()]))
    assert corpus.load_conversation_body(conn, "c1")[1] == {}


# ---------------------------------------------------------------- storing a body

def test_add_conversation_stores_the_turns_as_a_seekable_zstd_archive():
    conn = _open()
    corpus.add_conversation(conn, _conv(turns=[_turn(text="one"), _turn(text="two")]))
    row = conn.execute("SELECT * FROM conversation_bodies").fetchone()
    assert row["conversation_id"] == "c1"
    reader = archive.SeekableReader(row["archive"])
    assert len(reader) == 2, "one frame per turn"
    assert json.loads(reader.frame(0))["blocks"][0]["text"] == "one"
    assert json.loads(reader.frame(1))["blocks"][0]["text"] == "two"


def test_a_single_turn_decompresses_INDEPENDENTLY_of_its_siblings():
    """The whole reason the seekable format is used rather than one zstd frame per
    conversation: each frame is a complete, self-contained zstd frame, so turn N opens
    without inflating turn N-1.

    ASSERTED BY CORRUPTION, not by a size heuristic. The first draft of this test compared
    compressed sizes and FAILED for a reason worth keeping: `"padding word " * 4000` is
    52,103 bytes of text that zstd crushes to 113, so the "big" frame came out SMALLER than
    the 105-byte needle frame and the size comparison proved nothing either way. Overwriting
    the neighbours' bytes with garbage tests the property directly — if reading frame 1
    touched frame 0 or 2 it could not possibly succeed.
    """
    conn = _open()
    big = "padding word " * 4000
    corpus.add_conversation(conn, _conv(turns=[
        _turn(text=big), _turn(text="the needle"), _turn(text=big)]))
    blob = bytearray(conn.execute(
        "SELECT archive FROM conversation_bodies").fetchone()[0])
    intact = archive.SeekableReader(bytes(blob))
    assert len(intact) == 3
    first, needle = intact._entries[0], intact._entries[1]
    assert first.decompressed_size > 50000 and needle.decompressed_size < 500, (
        "the frames must map to individual turns, not to arbitrary byte chunks")
    # scribble over frame 0 only; the seek table (and so every frame boundary) is untouched
    blob[0:first.compressed_size] = b"\xff" * first.compressed_size
    wounded = archive.SeekableReader(bytes(blob))
    assert json.loads(wounded.frame(1))["blocks"][0]["text"] == "the needle"
    with pytest.raises(Exception):
        wounded.frame(0)


def test_the_stored_body_round_trips_every_IR_FIELD_a_turn_carries():
    """Roles, uuids, timestamps, the branch marker, and per-block type/text/data/citations
    all survive. This is what makes serving `conversation.get` from the archive a real
    transcript rather than a flattened one."""
    conn = _open()
    original = _rich_turns()
    corpus.add_conversation(conn, _conv(turns=original))
    restored = corpus.load_conversation_body(conn, "c1")[0]
    assert restored == original


def test_a_conversation_with_no_turns_stores_an_empty_archive_not_a_missing_row():
    """`[]` and "never stored" must be distinguishable: the first is a conversation that
    genuinely has no turns, the second is an index built before this change. Only the
    second may fall back to re-parsing."""
    conn = _open()
    corpus.add_conversation(conn, _conv(turns=[]))
    assert corpus.load_conversation_body(conn, "c1") == ([], {})


def test_an_unstored_conversation_reads_back_None_not_an_empty_list():
    conn = _open()
    assert corpus.load_conversation_body(conn, "never-indexed") is None


def test_a_grown_conversation_REPLACES_its_stored_body():
    """A live session that gained turns must gain them in the archive too. This is the
    body-side of the re-index defect `add_conversation` already fixes for the FTS: a
    frozen body would make the archive quietly stale instead of quietly empty."""
    conn = _open()
    corpus.add_conversation(conn, _conv(turns=[_turn(text="first")]))
    corpus.add_conversation(conn, _conv(turns=[_turn(text="first"), _turn(text="second")]))
    restored = corpus.load_conversation_body(conn, "c1")[0]
    assert [b.text for t in restored for b in t.blocks] == ["first", "second"]
    assert conn.execute("SELECT COUNT(*) FROM conversation_bodies").fetchone()[0] == 1


def test_two_conversations_keep_separate_bodies():
    conn = _open()
    corpus.add_conversation(conn, _conv("c1", turns=[_turn(text="alpha only")]))
    corpus.add_conversation(conn, _conv("c2", turns=[_turn(text="beta only")]))
    assert corpus.load_conversation_body(conn, "c1")[0][0].blocks[0].text == "alpha only"
    assert corpus.load_conversation_body(conn, "c2")[0][0].blocks[0].text == "beta only"


# ------------------------------------------------------- the size accounting

def test_text_bytes_equals_the_archives_own_decompressed_size():
    """`text_bytes` is the one size fact a reader can see without a zstd decoder, so it
    must not be able to drift from the blob it describes."""
    conn = _open()
    corpus.add_conversation(conn, _conv(turns=_rich_turns()))
    row = conn.execute(
        "SELECT text_bytes, archive FROM conversation_bodies").fetchone()
    assert row["text_bytes"] == archive.SeekableReader(row["archive"]).decompressed_size


def test_the_archive_is_SMALLER_than_the_text_it_holds():
    """Compression is the reason a 122 MB corpus can be carried at all — asserted, because
    a codec silently degrading to store-only would still pass every round-trip test above."""
    conn = _open()
    # repetitive-but-realistic transcript text; 40 turns so per-frame headers are amortised
    corpus.add_conversation(conn, _conv(turns=[
        _turn(text="the assistant explained the plan in detail, step %d of many" % i)
        for i in range(40)]))
    row = conn.execute(
        "SELECT text_bytes, LENGTH(archive) AS blob FROM conversation_bodies").fetchone()
    assert row["blob"] < row["text_bytes"], (
        "stored %d bytes for %d bytes of text — the codec is not compressing"
        % (row["blob"], row["text_bytes"]))


# ----------------------------------------------- the G-1 property, at the library level

def test_the_body_needs_no_source_file_at_all():
    """The point of the whole unit: nothing about reading a stored body consults the
    filesystem, so the archive survives its sources being moved, compacted or deleted.
    `rollout_path` is deliberately set to a path that does not exist."""
    conn = _open()
    corpus.add_conversation(conn, _conv(turns=[_turn(text="survives its source")]),
                            rollout_path="/no/such/rollout-deleted.jsonl")
    restored = corpus.load_conversation_body(conn, "c1")[0]
    assert [b.text for t in restored for b in t.blocks] == ["survives its source"]
