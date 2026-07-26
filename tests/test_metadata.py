"""metadata.py — the csm app-owned metadata layer (alias / tags / notes).

SYNTHETIC fixtures ONLY. The real corpus is PRIVATE medical/pharma data; every
conversation id, rollout path, prompt and tag below is invented. Nothing here reads
$CODEX_HOME, ~/.codex or AppData: the one test that drives the REAL ingest path
points `codex_home` at an empty tmp directory, and the adapter opens a state DB
read-only + immutable anyway.

Four things are pinned:
  1. the C# semantics ported from Storage/Indexing/SessionCatalogRepository.cs and
     tests/.../SessionMetadataRepositoryTests.cs — a missing row reads as BLANKS (not
     an error), an explicit save writes verbatim (blanks clear), and a re-ingest MERGE
     never lets a blank incoming value clobber a stored one;
  2. NON-MUTATION — the headline invariant: the session file on disk is byte-identical
     after a full battery of metadata writes, and no file under the sessions tree is
     added, removed or changed;
  3. set-like tags — adding twice does not duplicate, ordering is deterministic and
     independent of insertion history;
  4. hidden-unicode sanitization on the way IN, and the PRIVACY boundary: alias / tags
     / notes must never appear in redact.MetadataView or in metadata_payload().
"""
import hashlib
import json

import pytest

from llm_anthology import corpus, ir, loaders, metadata, redact
from llm_anthology.sanitize import scan_invisibles

# --------------------------------------------------------------------- fixtures

# Every sqlite connection a test opens is tracked and closed when it ends, so the suite
# stays free of ResourceWarnings from connections the garbage collector reaps.
_OPEN = []


def _track(conn):
    _OPEN.append(conn)
    return conn


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()


@pytest.fixture
def conn(tmp_path):
    """A real on-disk index (corpus.py's schema) with MY table ensured on top."""
    return _track(metadata.open_metadata(str(tmp_path / "index.db")))


def _conv(cid="conv-1", title="synthetic title", provider="codex", thread_id=""):
    return ir.Conversation(
        id=cid, title=title, provider=provider, account="synthetic-account",
        turns=[ir.Turn("human", [ir.Block("text", text="synthetic body")])],
        meta={"thread_id": thread_id} if thread_id else {})


def _index_conv(conn, cid="conv-1", **kw):
    """Put a real conversations row behind `cid` so the JOIN entry points have one."""
    conv = _conv(cid, **kw)
    corpus.add_conversation(conn, conv, thread_id=kw.get("thread_id", ""))
    return conv


# A synthetic Codex rollout. The UUID is invented; the filename must carry one because
# the adapter falls back to it for the thread id (codex_rollout trap 6).
_UUID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
_ROLLOUT_RECORDS = [
    {"type": "session_meta", "timestamp": "2026-07-26T10:00:00Z",
     "payload": {"session_id": _UUID, "timestamp": "2026-07-26T10:00:00Z",
                 "cwd": "/synthetic/repo", "model_provider": "synthetic-provider",
                 "git": {"branch": "synthetic-branch"}}},
    {"type": "response_item", "timestamp": "2026-07-26T10:00:01Z",
     "payload": {"type": "message", "role": "user",
                 "content": [{"type": "input_text",
                              "text": "synthetic prompt about a widget"}]}},
    {"type": "response_item", "timestamp": "2026-07-26T10:00:02Z",
     "payload": {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "synthetic reply"}]}},
]


def _write_rollout(sessions_root):
    """Write the synthetic rollout into the DATE-NESTED tree, as BYTES.

    write_bytes, not write_text: a text write would apply newline translation on
    Windows, and this file's exact bytes are the thing the non-mutation test measures.
    """
    day = sessions_root / "2026" / "07" / "26"
    day.mkdir(parents=True)
    path = day / ("rollout-2026-07-26T10-00-00-%s.jsonl" % _UUID)
    body = "\n".join(json.dumps(r) for r in _ROLLOUT_RECORDS) + "\n"
    path.write_bytes(body.encode("utf-8"))
    return path


def _tree_digest(root):
    """{relative posix path: sha256 or None for a directory} for everything under
    `root` — the fingerprint the non-mutation test compares before and after."""
    out = {}
    for p in sorted(root.rglob("*")):
        key = p.relative_to(root).as_posix()
        out[key] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
    return out


# ------------------------------------------------------------------ ensure_schema

def test_ensure_schema_creates_the_table_and_is_idempotent(tmp_path):
    conn = _track(corpus.open_index(str(tmp_path / "i.db")))
    metadata.ensure_schema(conn)
    metadata.set_alias(conn, "conv-1", "Pinned session")
    metadata.ensure_schema(conn)          # re-running must not drop or reset anything
    metadata.ensure_schema(conn)
    assert metadata.get_alias(conn, "conv-1") == "Pinned session"


def test_ensure_schema_does_not_touch_corpus_tables(tmp_path):
    """MY table is additive: corpus.py's own tables are present and untouched."""
    conn = _track(metadata.open_metadata(str(tmp_path / "i.db")))
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"conversations", "threads", "thread_spawn_edges",
            "ingest_checkpoint", "conversation_metadata"} <= names


def test_open_metadata_returns_a_usable_connection(tmp_path):
    conn = _track(metadata.open_metadata(str(tmp_path / "i.db")))
    _index_conv(conn)
    assert metadata.set_notes(conn, "conv-1", "note").notes == "note"


# --------------------------------------------------- missing rows read as blanks

def test_missing_row_reads_as_empty_metadata_not_none(conn):
    """csm's SelectMetadataSql + `if (!reader.Read()) return current` treats absence as
    blanks (alias TEXT NOT NULL reads as '' through ListSessionsSql). Absence is NOT an
    error and NOT None — the UI binds to a uniform object."""
    meta = metadata.get_metadata(conn, "never-seen")
    assert meta == metadata.Metadata(conversation_id="never-seen")
    assert meta.alias == "" and meta.tags == () and meta.notes == ""
    assert meta.is_empty is True
    assert metadata.get_alias(conn, "never-seen") == ""
    assert metadata.get_tags(conn, "never-seen") == ()
    assert metadata.get_notes(conn, "never-seen") == ""


def test_clear_metadata_on_a_missing_row_is_a_silent_noop(conn):
    assert metadata.clear_metadata(conn, "never-seen").is_empty is True


def test_metadata_does_not_require_an_indexed_conversation(conn):
    """Deliberately NO foreign key to conversations(conversation_id): metadata must
    outlive a rebuilt index. csm stores alias/tags/notes as COLUMNS on the sessions row,
    so deleting the catalog row destroys hand-authored notes; here it cannot."""
    metadata.set_alias(conn, "not-yet-ingested", "Pinned session")
    assert metadata.get_alias(conn, "not-yet-ingested") == "Pinned session"


# ------------------------------------------------------------ set / get / clear

def test_alias_tags_notes_round_trip(conn):
    """The SessionMetadataRepositoryTests case: alias + 2 tags + notes persist."""
    stored = metadata.set_metadata(conn, "conv-1", alias="Pinned session",
                                   tags=["important", "renderer"],
                                   notes="Keep for regression checks")
    assert stored == metadata.Metadata("conv-1", "Pinned session",
                                       ("important", "renderer"),
                                       "Keep for regression checks")
    read = metadata.get_metadata(conn, "conv-1")
    assert read == stored
    assert len(read.tags) == 2
    assert "regression" in read.notes
    assert read.is_empty is False


def test_set_metadata_leaves_unnamed_fields_alone(conn):
    """None means LEAVE UNCHANGED. csm cannot express a partial edit (SaveMetadataAsync
    always writes all three); the cockpit edits one field at a time and must not blank
    the other two."""
    metadata.set_metadata(conn, "conv-1", alias="A", tags=["t"], notes="N")
    metadata.set_metadata(conn, "conv-1")                       # all three None
    assert metadata.get_metadata(conn, "conv-1") == metadata.Metadata("conv-1", "A", ("t",), "N")
    metadata.set_alias(conn, "conv-1", "B")
    assert metadata.get_metadata(conn, "conv-1") == metadata.Metadata("conv-1", "B", ("t",), "N")
    metadata.set_notes(conn, "conv-1", "M")
    assert metadata.get_metadata(conn, "conv-1") == metadata.Metadata("conv-1", "B", ("t",), "M")
    metadata.set_tags(conn, "conv-1", ["u"])
    assert metadata.get_metadata(conn, "conv-1") == metadata.Metadata("conv-1", "B", ("u",), "M")


def test_explicit_blank_clears_a_field(conn):
    """An explicit '' / () CLEARS — csm's SaveMetadataAsync writes what it is given,
    so passing blanks is how the UI clears a field."""
    metadata.set_metadata(conn, "conv-1", alias="A", tags=["t"], notes="N")
    assert metadata.clear_alias(conn, "conv-1") == metadata.Metadata("conv-1", "", ("t",), "N")
    assert metadata.clear_notes(conn, "conv-1") == metadata.Metadata("conv-1", "", ("t",), "")
    assert metadata.clear_tags(conn, "conv-1").is_empty is True
    assert metadata.get_metadata(conn, "conv-1").is_empty is True


def test_all_blank_metadata_is_stored_as_absence(conn):
    """An all-blank metadata leaves NO row, so a cleared conversation reads identically
    to one that was never annotated (absence == blanks) and the table keeps no residue."""
    metadata.set_metadata(conn, "conv-1", alias="A", tags=["t"], notes="N")
    assert conn.execute("SELECT count(*) FROM conversation_metadata").fetchone()[0] == 1
    metadata.set_metadata(conn, "conv-1", alias="   ", tags=[" ", ""], notes="\t")
    assert conn.execute("SELECT count(*) FROM conversation_metadata").fetchone()[0] == 0
    assert metadata.get_metadata(conn, "conv-1").is_empty is True


def test_clear_metadata_drops_the_whole_row(conn):
    metadata.set_metadata(conn, "conv-1", alias="A", tags=["t"], notes="N")
    assert metadata.clear_metadata(conn, "conv-1") == metadata.Metadata("conv-1")
    assert conn.execute("SELECT count(*) FROM conversation_metadata").fetchone()[0] == 0


def test_set_is_an_upsert_not_a_duplicate(conn):
    for alias in ("first", "second", "third"):
        metadata.set_alias(conn, "conv-1", alias)
    assert conn.execute("SELECT count(*) FROM conversation_metadata").fetchone()[0] == 1
    assert metadata.get_alias(conn, "conv-1") == "third"


# --------------------------------------------------------------- set-like tags

def test_tags_are_set_like_and_deterministically_ordered(conn):
    """Adding twice does not duplicate; the stored order is derived from the tag text,
    not from insertion history, so two different insertion orders agree."""
    a = metadata.set_tags(conn, "conv-a", ["renderer", "important", "renderer"])
    b = metadata.set_tags(conn, "conv-b", ["important", "renderer", "important"])
    assert a.tags == b.tags == ("important", "renderer")


def test_add_tags_unions_without_duplicating(conn):
    metadata.set_tags(conn, "conv-1", ["important"])
    assert metadata.add_tags(conn, "conv-1", ["renderer"]).tags == ("important", "renderer")
    assert metadata.add_tags(conn, "conv-1", ["important"]).tags == ("important", "renderer")
    assert metadata.add_tags(conn, "conv-1", []).tags == ("important", "renderer")


def test_add_tags_keeps_the_stored_casing(conn):
    """Dedup is case-insensitive and FIRST-seen wins; the stored tag comes first, so
    re-adding it with different casing does not rewrite what the user typed."""
    metadata.set_tags(conn, "conv-1", ["Renderer"])
    assert metadata.add_tags(conn, "conv-1", ["renderer", "RENDERER"]).tags == ("Renderer",)


def test_remove_tags_is_case_insensitive_and_tolerates_absent_tags(conn):
    metadata.set_tags(conn, "conv-1", ["Important", "renderer"])
    assert metadata.remove_tags(conn, "conv-1", ["IMPORTANT"]).tags == ("renderer",)
    assert metadata.remove_tags(conn, "conv-1", ["never-applied"]).tags == ("renderer",)
    assert metadata.remove_tags(conn, "conv-1", ["renderer"]).tags == ()


def test_a_bare_string_is_one_tag_not_a_character_sequence(conn):
    """set_tags(conn, cid, "important") must not become 9 single-character tags."""
    assert metadata.set_tags(conn, "conv-1", "important").tags == ("important",)


def test_tags_are_trimmed_blanks_dropped_and_internal_whitespace_collapsed(conn):
    """csm's SplitLines uses RemoveEmptyEntries | TrimEntries. A newline INSIDE a tag is
    collapsed too, because '\\n' is the csm wire separator — a tag carrying one would
    split into two on the round trip."""
    stored = metadata.set_tags(conn, "conv-1", ["  spaced  ", "", "   ", "two\nlines",
                                                "tabs\t\tin\there"])
    assert stored.tags == ("spaced", "tabs in here", "two lines")
    raw = conn.execute("SELECT tags FROM conversation_metadata").fetchone()[0]
    assert raw == "spaced\ntabs in here\ntwo lines"       # csm wire format
    assert metadata.get_tags(conn, "conv-1") == stored.tags   # lossless round trip


def test_tag_ordering_is_case_insensitive_with_an_exact_tiebreak(conn):
    stored = metadata.set_tags(conn, "conv-1", ["beta", "Alpha", "gamma", "BETA-2"])
    assert stored.tags == ("Alpha", "beta", "BETA-2", "gamma")


# ----------------------------------------------------- the re-ingest merge path

def test_merge_metadata_keeps_stored_values_when_incoming_is_blank(conn):
    """csm's MergeExistingMetadataAsync: on re-upsert a null/whitespace incoming alias,
    an EMPTY incoming tag list and blank incoming notes all yield to what is stored."""
    metadata.set_metadata(conn, "conv-1", alias="Pinned session", tags=["important"],
                          notes="Keep for regression checks")
    merged = metadata.merge_metadata(conn, "conv-1", alias="   ", tags=[], notes="")
    assert merged == metadata.Metadata("conv-1", "Pinned session", ("important",),
                                       "Keep for regression checks")


def test_merge_metadata_takes_incoming_values_when_they_are_present(conn):
    metadata.set_metadata(conn, "conv-1", alias="old", tags=["old"], notes="old")
    merged = metadata.merge_metadata(conn, "conv-1", alias="new", tags=["new"], notes="new")
    assert merged == metadata.Metadata("conv-1", "new", ("new",), "new")


def test_merge_metadata_on_a_missing_row_stores_the_incoming_values(conn):
    merged = metadata.merge_metadata(conn, "conv-1", alias="A", tags=["t"], notes="N")
    assert merged == metadata.Metadata("conv-1", "A", ("t",), "N")
    assert metadata.merge_metadata(conn, "conv-2").is_empty is True


def test_re_ingesting_a_conversation_cannot_clobber_metadata(conn):
    """The structural improvement over csm: alias/tags/notes live in a SEPARATE table
    keyed by conversation_id, so corpus.add_conversation — which csm's merge exists to
    defend against — cannot reach them at all."""
    conv = _index_conv(conn, "conv-1")
    metadata.set_metadata(conn, "conv-1", alias="Pinned session", tags=["important"],
                          notes="Keep for regression checks")
    corpus.add_conversation(conn, conv)                     # idempotent re-ingest
    corpus.add_conversation(conn, _conv("conv-1", title="a different title"))
    assert metadata.get_metadata(conn, "conv-1") == metadata.Metadata(
        "conv-1", "Pinned session", ("important",), "Keep for regression checks")


# ------------------------------------------------------------------ sanitization

def test_free_text_is_sanitized_on_the_way_in(conn):
    """The corpus is known to carry hidden-unicode prompt-injection payloads. Every
    free-text field is passed through sanitize.sanitize_for_copy, which STRIPS the
    flagged codepoints, before it is stored."""
    payload = "Pinned​session\U000e0041︁"          # ZWSP + TAG block + VS2
    stored = metadata.set_metadata(conn, "conv-1", alias=payload,
                                   tags=["imp​ortant", "‮renderer"],
                                   notes="notes‍here")
    assert stored.alias == "Pinnedsession"
    assert stored.tags == ("important", "renderer")
    assert stored.notes == "noteshere"
    for value in (stored.alias, stored.notes) + stored.tags:
        assert scan_invisibles(value) == []
    row = conn.execute("SELECT alias, tags, notes, tags_key, search_key "
                       "FROM conversation_metadata").fetchone()
    for column in row:
        assert scan_invisibles(column) == []


def test_a_tag_that_is_only_an_invisible_payload_is_dropped(conn):
    assert metadata.set_tags(conn, "conv-1", ["​​", "real"]).tags == ("real",)


def test_free_text_is_trimmed(conn):
    stored = metadata.set_metadata(conn, "conv-1", alias="  Pinned session  ",
                                   notes="\n Keep this \n")
    assert stored.alias == "Pinned session"
    assert stored.notes == "Keep this"


def test_non_string_free_text_becomes_blank(conn):
    """sanitize_for_copy coerces a non-str to '', so a bad RPC payload cannot land a
    non-text value in a TEXT NOT NULL column."""
    assert metadata.set_metadata(conn, "conv-1", alias=None, notes=17).is_empty is True
    assert metadata.set_metadata(conn, "conv-1", alias=object(), tags=[3, "ok"],
                                 notes=[]).tags == ("ok",)


# ----------------------------------------------------------------- search / filter

def test_find_by_tag_returns_only_exact_tag_holders(conn):
    metadata.set_tags(conn, "conv-1", ["important", "renderer"])
    metadata.set_tags(conn, "conv-2", ["important"])
    metadata.set_tags(conn, "conv-3", ["importantly"])       # a PREFIX, not the tag
    hits = metadata.find_by_tag(conn, "important")
    assert [m.conversation_id for m in hits] == ["conv-1", "conv-2"]
    assert metadata.find_by_tag(conn, "renderer")[0].tags == ("important", "renderer")


def test_find_by_tag_is_case_insensitive_and_normalizes_the_needle(conn):
    metadata.set_tags(conn, "conv-1", ["Renderer"])
    for probe in ("renderer", "RENDERER", "  Renderer  "):
        assert [m.conversation_id for m in metadata.find_by_tag(conn, probe)] == ["conv-1"]


def test_find_by_tag_treats_sql_wildcards_as_literal_text(conn):
    """instr(), not LIKE: under LIKE a tag of '%' would match every row."""
    metadata.set_tags(conn, "conv-1", ["100%_done"])
    metadata.set_tags(conn, "conv-2", ["unrelated"])
    assert [m.conversation_id for m in metadata.find_by_tag(conn, "100%_done")] == ["conv-1"]
    assert metadata.find_by_tag(conn, "%") == []
    assert metadata.find_by_tag(conn, "_") == []


def test_find_by_tag_with_a_blank_tag_matches_nothing(conn):
    metadata.set_tags(conn, "conv-1", ["important"])
    metadata.set_alias(conn, "conv-2", "no tags at all")
    assert metadata.find_by_tag(conn, "") == []
    assert metadata.find_by_tag(conn, "   ") == []


def test_search_metadata_spans_alias_tags_and_notes(conn):
    """The analogue of csm recomputing `combined_text` from alias || tags || notes on
    every metadata write — that recompute is what makes a session findable."""
    metadata.set_alias(conn, "conv-1", "Pinned session")
    metadata.set_tags(conn, "conv-2", ["regression-suite"])
    metadata.set_notes(conn, "conv-3", "Keep for regression checks")
    assert [m.conversation_id for m in metadata.search_metadata(conn, "Pinned")] == ["conv-1"]
    assert [m.conversation_id for m in metadata.search_metadata(conn, "regression")] \
        == ["conv-2", "conv-3"]
    assert metadata.search_metadata(conn, "absent") == []


def test_search_metadata_is_case_insensitive_beyond_ascii(conn):
    """Case folding happens in PYTHON on both sides. SQLite's lower() is ASCII-only and
    would silently fail to match 'Ä' against 'ä'."""
    metadata.set_alias(conn, "conv-1", "ÄRZTLICHE Notiz")
    assert [m.conversation_id for m in metadata.search_metadata(conn, "ärztliche")] == ["conv-1"]


def test_search_metadata_with_blank_text_matches_nothing(conn):
    metadata.set_alias(conn, "conv-1", "Pinned session")
    assert metadata.search_metadata(conn, "") == []
    assert metadata.search_metadata(conn, "  ​ ") == []


def test_search_conversations_joins_the_display_columns(conn):
    _index_conv(conn, "conv-1", title="synthetic renderer work", thread_id="thread-1")
    metadata.set_metadata(conn, "conv-1", alias="Pinned session", tags=["important"],
                          notes="Keep for regression checks")
    rows = metadata.search_conversations(conn, text="pinned")
    assert len(rows) == 1
    assert rows[0]["conversation_id"] == "conv-1"
    assert rows[0]["title"] == "synthetic renderer work"
    assert rows[0]["thread_id"] == "thread-1"
    assert rows[0]["provider"] == "codex"
    assert rows[0]["alias"] == "Pinned session"
    assert rows[0]["tags"] == "important"
    assert rows[0]["notes"] == "Keep for regression checks"


def test_search_conversations_filters_by_tag_and_by_both(conn):
    _index_conv(conn, "conv-1")
    _index_conv(conn, "conv-2")
    metadata.set_metadata(conn, "conv-1", alias="Pinned session", tags=["important"])
    metadata.set_metadata(conn, "conv-2", alias="Other session", tags=["important"])
    assert [r["conversation_id"] for r in metadata.search_conversations(conn, tag="important")] \
        == ["conv-1", "conv-2"]
    assert [r["conversation_id"] for r in
            metadata.search_conversations(conn, text="pinned", tag="important")] == ["conv-1"]
    assert metadata.search_conversations(conn, text="pinned", tag="absent-tag") == []


def test_search_conversations_with_no_filter_returns_nothing(conn):
    """csm's SearchAsync returns [] for a blank query rather than the whole catalogue."""
    _index_conv(conn, "conv-1")
    metadata.set_alias(conn, "conv-1", "Pinned session")
    assert metadata.search_conversations(conn) == []
    assert metadata.search_conversations(conn, text="   ", tag="  ") == []


def test_search_conversations_skips_metadata_with_no_indexed_conversation(conn):
    """An INNER JOIN, like csm's SearchSql. Metadata for a conversation the index does
    not know is still readable through get_metadata / search_metadata."""
    metadata.set_alias(conn, "ghost", "Pinned session")
    assert metadata.search_conversations(conn, text="pinned") == []
    assert [m.conversation_id for m in metadata.search_metadata(conn, "pinned")] == ["ghost"]


def test_find_by_thread_resolves_through_the_conversations_table(conn):
    """ONE key (conversation_id) plus a thread-scoped LOOKUP. On the Codex path
    codex_rollout sets Conversation.id AND ThreadMeta.id to the same session id, and
    index.build_index copies conv.meta['thread_id'] into the conversations row, so a
    thread resolves to its conversations with a join instead of a second key."""
    _index_conv(conn, "conv-1", thread_id="thread-1")
    _index_conv(conn, "conv-2", thread_id="thread-2")
    metadata.set_alias(conn, "conv-1", "Pinned session")
    metadata.set_alias(conn, "conv-2", "Other session")
    assert [m.alias for m in metadata.find_by_thread(conn, "thread-1")] == ["Pinned session"]
    assert metadata.find_by_thread(conn, "thread-absent") == []
    assert metadata.find_by_thread(conn, "") == []


def test_tag_counts_is_a_collapsed_deterministic_facet(conn):
    assert metadata.tag_counts(conn) == {}
    metadata.set_tags(conn, "conv-1", ["beta", "alpha"])
    metadata.set_tags(conn, "conv-2", ["Beta"])             # same tag, other casing
    metadata.set_tags(conn, "conv-3", ["beta"])
    metadata.set_alias(conn, "conv-4", "no tags at all")    # a row that yields no tag
    assert metadata.tag_counts(conn) == {"alpha": 1, "Beta": 3}
    assert list(metadata.tag_counts(conn)) == ["alpha", "Beta"]


# ---------------------------------------------------- NON-MUTATION (the headline)

def test_session_file_is_byte_identical_after_metadata_writes(tmp_path):
    """THE headline invariant: metadata is APP-OWNED and is never written back into the
    session files. The synthetic rollout is ingested through the REAL loader, then a
    full battery of metadata writes runs, and every byte under the sessions tree is
    re-hashed. `codex_home` points at an empty tmp directory, so nothing reads the
    owner's real store.
    """
    sessions_root = tmp_path / "sessions"
    rollout = _write_rollout(sessions_root)
    before_tree = _tree_digest(sessions_root)
    before_stat = rollout.stat()

    corp, errors = loaders.load_corpus(
        str(sessions_root), str(tmp_path / "index.db"),
        codex_home=str(tmp_path / "empty-codex-home"))
    assert errors == []
    assert [c.id for c in corp.conversations] == [_UUID]

    conn = _track(metadata.open_metadata(str(tmp_path / "index.db")))
    metadata.set_alias(conn, _UUID, "Pinned session")
    metadata.set_tags(conn, _UUID, ["important", "renderer", "important"])
    metadata.set_notes(conn, _UUID, "Keep for regression checks")
    metadata.add_tags(conn, _UUID, ["renderer", "urgent"])
    metadata.remove_tags(conn, _UUID, ["urgent"])
    metadata.merge_metadata(conn, _UUID, alias="", tags=[], notes="merged notes")
    assert metadata.find_by_tag(conn, "renderer")[0].conversation_id == _UUID
    assert metadata.search_conversations(conn, text="pinned")[0]["conversation_id"] == _UUID
    metadata.clear_notes(conn, _UUID)
    metadata.clear_metadata(conn, _UUID)
    conn.commit()

    assert _tree_digest(sessions_root) == before_tree
    after_stat = rollout.stat()
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    # and the file still parses to the same conversation it did before
    assert rollout.read_bytes().decode("utf-8").splitlines() == [
        json.dumps(r) for r in _ROLLOUT_RECORDS]


def test_metadata_writes_add_no_file_anywhere_near_the_session_store(tmp_path):
    """A second, mechanically different probe of the same invariant: the ONLY file the
    metadata layer may create is the index DB the caller named (plus sqlite's own WAL
    sidecars) — never a sidecar next to a session."""
    sessions_root = tmp_path / "sessions"
    _write_rollout(sessions_root)
    before = sorted(p.relative_to(sessions_root).as_posix()
                    for p in sessions_root.rglob("*"))
    conn = _track(metadata.open_metadata(str(tmp_path / "index.db")))
    metadata.set_metadata(conn, _UUID, alias="Pinned session", tags=["important"],
                          notes="Keep for regression checks")
    conn.commit()
    assert sorted(p.relative_to(sessions_root).as_posix()
                  for p in sessions_root.rglob("*")) == before


# ---------------------------------------------------------------------- PRIVACY

def test_alias_tags_notes_are_absent_from_the_cloud_metadata_view():
    """redact.py's allowlist is the cloud boundary and free text is FORBIDDEN to cross
    (owner decision 2026-07-25). This unit deliberately did NOT add its fields to
    MetadataView; this test fails the moment somebody does."""
    from dataclasses import fields
    names = {f.name for f in fields(redact.MetadataView)}
    assert names.isdisjoint({"alias", "tags", "notes", "aliases", "metadata"})


def test_metadata_never_reaches_the_cloud_payload(tmp_path):
    """An end-to-end leak probe, not an assertion about a mock: store distinctive alias
    / tag / notes tokens, build the exact payload the research plane may send, and prove
    none of the tokens appear anywhere in it."""
    conn = _track(metadata.open_metadata(str(tmp_path / "i.db")))
    conv = _index_conv(conn, "conv-1", thread_id="thread-1")
    metadata.set_metadata(conn, "conv-1", alias="ALIASTOKEN", tags=["TAGTOKEN"],
                          notes="NOTESTOKEN")
    corp = corpus.Corpus(conversations=[conv])
    blob = json.dumps(redact.metadata_payload(corp))
    for token in ("ALIASTOKEN", "TAGTOKEN", "NOTESTOKEN"):
        assert token not in blob
    assert metadata.get_alias(conn, "conv-1") == "ALIASTOKEN"      # still stored locally


def test_metadata_module_makes_no_network_or_filesystem_reach():
    """LOCAL-ONLY by construction: the module imports no network, subprocess or
    session-store-discovery machinery, and never opens a file itself — every entry point
    takes a caller-supplied sqlite connection."""
    import inspect
    src = inspect.getsource(metadata)
    for forbidden in ("import socket", "import urllib", "import requests", "http",
                      "import subprocess", "expanduser", "CODEX_HOME", "os.environ",
                      "open("):
        assert forbidden not in src, forbidden
