"""Tests for llm_anthology.dedup — Codex physical-copy -> logical-session collapse.

Ported from the csm behavioural spec (SessionDeduplicatorTests.cs) and extended with the
nasty cases that spec did NOT cover: a truncated copy beside a complete one, an
unidentifiable copy, and the FALSE MERGE (two different sessions that merely look alike).

PRIVACY: every fixture here is synthetic. Nothing reads $CODEX_HOME, ~/.codex or AppData.
"""
import itertools
import json
import os
import sqlite3

from llm_anthology import corpus, dedup, ir


# --------------------------------------------------------------------- helpers

def _copy(session_id, file_path, store_kind=dedup.STORE_BACKUP,
          last_write_ms=1000, size_bytes=1000):
    return dedup.PhysicalCopy(session_id=session_id, file_path=file_path,
                              store_kind=store_kind, last_write_ms=last_write_ms,
                              size_bytes=size_bytes)


def _rollout_lines(session_id, user_text="hello", cwd="C:/work", n_extra=0):
    """A minimal but realistic rollout: a session_meta header plus messages."""
    lines = [{"type": "session_meta",
              "timestamp": "2026-03-23T10:00:00Z",
              "payload": {"session_id": session_id, "cwd": cwd,
                          "model_provider": "openai", "git": {"branch": "main"}}},
             {"type": "response_item",
              "timestamp": "2026-03-23T10:00:01Z",
              "payload": {"type": "message", "role": "user",
                          "content": [{"type": "input_text", "text": user_text}]}}]
    for i in range(n_extra):
        lines.append({"type": "response_item",
                      "timestamp": "2026-03-23T10:00:02Z",
                      "payload": {"type": "message", "role": "assistant",
                                  "content": [{"type": "output_text",
                                               "text": "reply %d" % i}]}})
    return [json.dumps(line) for line in lines]


def _write_rollout(root, name, lines):
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


UUID_A = "aaaaaaaa-1111-2222-3333-444444444444"
UUID_B = "bbbbbbbb-1111-2222-3333-444444444444"


# ------------------------------------------------- store kinds / preference rank

def test_store_rank_puts_live_first_and_ties_every_other_kind():
    assert dedup.store_rank(dedup.STORE_LIVE) == 0
    for kind in (dedup.STORE_BACKUP, dedup.STORE_MIRROR, dedup.STORE_OTHER,
                 dedup.STORE_UNKNOWN, "something-new"):
        assert dedup.store_rank(kind) == 1


def test_known_store_roots_is_pure_path_math_over_a_codex_home():
    """Port of KnownStoreLocator.GetKnownStores: live=<home>/sessions,
    backup=<home>/sessions_backup. Must NOT touch the filesystem."""
    roots = dedup.known_store_roots(os.path.join("Z:", "nope-does-not-exist"))
    kinds = [kind for _, kind in roots]
    assert kinds == [dedup.STORE_LIVE, dedup.STORE_BACKUP]
    assert os.path.basename(roots[0][0]) == "sessions"
    assert os.path.basename(roots[1][0]) == "sessions_backup"
    assert not os.path.exists(roots[0][0])


# --------------------------------------------------- the ported C# behaviour

def test_consolidate_groups_by_session_id_prefers_live_and_preserves_siblings():
    """Direct port of SessionDeduplicatorTests
    .Consolidate_GroupsBySessionId_PrefersLiveCopy_AndPreservesSiblings."""
    live = _copy("session-1", r"C:\home\.codex\sessions\2026\03\23\session-1.jsonl",
                 dedup.STORE_LIVE, last_write_ms=1_000, size_bytes=1000)
    backup = _copy("session-1",
                   r"C:\home\.codex\sessions_backup\2026\03\23\session-1.jsonl",
                   dedup.STORE_BACKUP, last_write_ms=2_000, size_bytes=1000)

    sessions = dedup.consolidate([backup, live])

    assert len(sessions) == 1
    logical = sessions[0]
    assert logical.session_id == "session-1"
    assert logical.canonical.file_path == live.file_path
    assert logical.copy_count == 2
    assert backup.file_path in [c.file_path for c in logical.copies]


def test_live_wins_even_when_a_backup_is_newer_and_larger():
    """store rank is the PRIMARY key (C# `StoreKind is Live ? 0 : 1` sorts first), so a
    newer/larger backup still loses to the live store."""
    live = _copy("s", "/live/s.jsonl", dedup.STORE_LIVE, last_write_ms=1, size_bytes=10)
    backup = _copy("s", "/bak/s.jsonl", dedup.STORE_BACKUP,
                   last_write_ms=999_999, size_bytes=999_999)
    assert dedup.consolidate([live, backup])[0].canonical.store_kind == dedup.STORE_LIVE


def test_result_is_ordered_by_session_id():
    out = dedup.consolidate([_copy("b", "/x/b.jsonl"), _copy("a", "/x/a.jsonl")])
    assert [s.session_id for s in out] == ["a", "b"]


def test_consolidate_of_nothing_is_empty():
    assert dedup.consolidate([]) == []


# ------------------------------------------------- nasty case 1: same id, many paths

def test_same_session_id_at_different_paths_collapses_to_one():
    paths = ["/live/2026/03/23/rollout-a.jsonl",
             "/bak/2026/03/23/rollout-a.jsonl",
             "/mirror/rollout-a.jsonl",
             "/somewhere/else/rollout-a-copy.jsonl"]
    kinds = [dedup.STORE_LIVE, dedup.STORE_BACKUP, dedup.STORE_MIRROR,
             dedup.STORE_OTHER]
    copies = [_copy(UUID_A, p, k) for p, k in zip(paths, kinds)]

    sessions = dedup.consolidate(copies)

    assert len(sessions) == 1
    assert sessions[0].copy_count == 4
    assert sorted(c.file_path for c in sessions[0].copies) == sorted(paths)
    assert len(sessions[0].duplicate_paths) == 3
    assert sessions[0].canonical.file_path not in sessions[0].duplicate_paths


def test_identity_is_session_id_alone_even_when_every_other_field_differs():
    a = _copy(UUID_A, "/live/rollout-1.jsonl", dedup.STORE_LIVE,
              last_write_ms=1, size_bytes=5)
    b = _copy(UUID_A, "/zzz/deep/nested/other-name.jsonl", dedup.STORE_OTHER,
              last_write_ms=88_888, size_bytes=999_999)
    assert len(dedup.consolidate([a, b])) == 1


# ------------------------------------------- nasty case 2: truncated vs complete

def test_truncated_copy_loses_to_the_complete_copy_in_the_same_store():
    """A rollout is an APPEND-ONLY log, so copies of one session are prefixes of each
    other and the LARGEST strictly dominates. The truncated copy here is NEWER, which
    is exactly the trap: mtime alone would pick the lossy file."""
    complete = _copy(UUID_A, "/bak/rollout-complete.jsonl", dedup.STORE_BACKUP,
                     last_write_ms=1_000, size_bytes=50_000)
    truncated = _copy(UUID_A, "/bak/rollout-truncated.jsonl", dedup.STORE_BACKUP,
                      last_write_ms=9_000, size_bytes=120)

    logical = dedup.consolidate([truncated, complete])[0]

    assert logical.canonical.file_path == complete.file_path
    assert logical.canonical.size_bytes == 50_000
    # ...and the truncated file is STILL retained as evidence.
    assert truncated.file_path in logical.duplicate_paths


def test_mtime_breaks_the_tie_when_store_and_size_are_equal():
    old = _copy(UUID_A, "/bak/a.jsonl", last_write_ms=1_000, size_bytes=500)
    new = _copy(UUID_A, "/bak/b.jsonl", last_write_ms=2_000, size_bytes=500)
    assert dedup.consolidate([old, new])[0].canonical.file_path == new.file_path


def test_a_missing_mtime_sorts_last_and_never_crashes_the_sort():
    known = _copy(UUID_A, "/bak/known.jsonl", last_write_ms=0, size_bytes=500)
    unknown = _copy(UUID_A, "/bak/unknown.jsonl", last_write_ms=None, size_bytes=500)
    logical = dedup.consolidate([unknown, known])[0]
    assert logical.canonical.file_path == known.file_path
    assert logical.copy_count == 2


def test_path_breaks_the_final_tie_case_insensitively_and_totally():
    """Last resort key, so the choice is never 'first one found'. Two paths that differ
    ONLY in case still get a total order (the raw path is the final key)."""
    upper = _copy(UUID_A, "/bak/B.jsonl", last_write_ms=5, size_bytes=5)
    lower = _copy(UUID_A, "/bak/a.jsonl", last_write_ms=5, size_bytes=5)
    assert dedup.consolidate([upper, lower])[0].canonical.file_path == "/bak/a.jsonl"

    same_a = _copy(UUID_A, "/bak/x.jsonl", last_write_ms=5, size_bytes=5)
    same_b = _copy(UUID_A, "/bak/X.jsonl", last_write_ms=5, size_bytes=5)
    picked = {dedup.consolidate([same_a, same_b])[0].canonical.file_path,
              dedup.consolidate([same_b, same_a])[0].canonical.file_path}
    assert picked == {"/bak/X.jsonl"}


def test_canonical_choice_is_invariant_under_every_input_permutation():
    """Determinism, proven by brute force rather than asserted."""
    copies = [
        _copy(UUID_A, "/live/a.jsonl", dedup.STORE_LIVE, 10, 100),
        _copy(UUID_A, "/bak/a.jsonl", dedup.STORE_BACKUP, 99, 900),
        _copy(UUID_B, "/bak/b.jsonl", dedup.STORE_BACKUP, 50, 500),
        _copy(UUID_B, "/mir/b.jsonl", dedup.STORE_MIRROR, 50, 500),
    ]
    expected = [(s.session_id, s.canonical.file_path,
                 tuple(c.file_path for c in s.copies))
                for s in dedup.consolidate(copies)]
    assert len(expected) == 2
    for order in itertools.permutations(copies):
        got = [(s.session_id, s.canonical.file_path,
                tuple(c.file_path for c in s.copies))
               for s in dedup.consolidate(list(order))]
        assert got == expected


# ------------------------------------------------- nasty case 3: the FALSE MERGE

def test_lookalike_sessions_never_merge():
    """THE worst failure mode. These two copies agree on EVERY observable field a fuzzy
    matcher might use — same store, same directory, byte-identical size, identical
    mtime, filenames differing by one hex character (the user ran the same prompt
    twice) — and differ ONLY in session_id. They must stay two sessions."""
    a = _copy(UUID_A, "/live/2026/03/23/rollout-2026-03-23T10-00-00-%s.jsonl" % UUID_A,
              dedup.STORE_LIVE, last_write_ms=1_711_190_000_000, size_bytes=48_921)
    b = _copy(UUID_B, "/live/2026/03/23/rollout-2026-03-23T10-00-00-%s.jsonl" % UUID_B,
              dedup.STORE_LIVE, last_write_ms=1_711_190_000_000, size_bytes=48_921)

    sessions = dedup.consolidate([a, b])

    assert len(sessions) == 2
    assert sorted(s.session_id for s in sessions) == sorted([UUID_A, UUID_B])
    for s in sessions:
        assert s.copy_count == 1
        assert s.canonical.session_id == s.session_id


def test_session_ids_differing_only_in_case_never_merge():
    """C# groups with StringComparer.Ordinal (case-SENSITIVE). A false split is a
    cosmetic duplicate; a false merge silently hides a conversation. Ordinal keeps the
    safe direction, so the port stays case-sensitive."""
    lower = _copy("abc-123", "/live/lower.jsonl", dedup.STORE_LIVE)
    upper = _copy("ABC-123", "/live/upper.jsonl", dedup.STORE_LIVE)
    assert len(dedup.consolidate([lower, upper])) == 2


def test_session_ids_differing_by_whitespace_never_merge():
    assert len(dedup.consolidate([_copy("abc", "/a.jsonl"),
                                  _copy("abc ", "/b.jsonl")])) == 2


def test_unidentified_copies_are_each_their_own_session_never_one_blob():
    """C# DROPS blank-session-id copies (`.Where(!IsNullOrWhiteSpace)`). Dropping is a
    deletion from the view, and grouping them together would be the biggest possible
    false merge, so each blank-id copy becomes its own singleton and survives."""
    a = _copy("", "/live/no-uuid-1.jsonl", dedup.STORE_LIVE)
    b = _copy("", "/live/no-uuid-2.jsonl", dedup.STORE_LIVE)
    c = _copy("   ", "/live/whitespace-id.jsonl", dedup.STORE_LIVE)

    sessions = dedup.consolidate([a, b, c])

    assert len(sessions) == 3
    assert sorted(s.canonical.file_path for s in sessions) == \
        ["/live/no-uuid-1.jsonl", "/live/no-uuid-2.jsonl", "/live/whitespace-id.jsonl"]
    assert [s.is_identified for s in sessions] == [False, False, False]
    assert all(s.copy_count == 1 for s in sessions)


def test_unidentified_copies_do_not_absorb_identified_ones():
    sessions = dedup.consolidate([_copy("", "/live/blank.jsonl"),
                                  _copy(UUID_A, "/live/real.jsonl")])
    assert len(sessions) == 2
    ids = {s.session_id for s in sessions}
    assert ids == {"", UUID_A}
    identified = [s for s in sessions if s.is_identified]
    assert len(identified) == 1 and identified[0].session_id == UUID_A


# --------------------------------------------------------- NON-DELETION guarantees

def test_consolidate_retains_every_input_copy_and_mutates_nothing():
    """Dedup is a VIEW. Every input copy must reappear in the output exactly once, the
    caller's list must be untouched, and the per-session copy tuple must be immutable."""
    copies = [_copy(UUID_A, "/live/a.jsonl", dedup.STORE_LIVE, 10, 100),
              _copy(UUID_A, "/bak/a.jsonl", dedup.STORE_BACKUP, 20, 100),
              _copy(UUID_B, "/live/b.jsonl", dedup.STORE_LIVE, 30, 300),
              _copy("", "/live/mystery.jsonl", dedup.STORE_LIVE, 40, 400)]
    before = list(copies)

    sessions = dedup.consolidate(copies)

    assert copies == before                      # input list not reordered/consumed
    emitted = [c for s in sessions for c in s.copies]
    assert len(emitted) == len(copies)
    assert sorted(c.file_path for c in emitted) == sorted(c.file_path for c in copies)
    for s in sessions:
        assert isinstance(s.copies, tuple)
        assert s.canonical in s.copies


def test_consolidate_never_touches_the_files_on_disk(tmp_path):
    """Non-deletion, proven against the real filesystem: the loser file is still there,
    byte-for-byte, after consolidation."""
    live_root = os.path.join(str(tmp_path), "sessions")
    bak_root = os.path.join(str(tmp_path), "sessions_backup")
    live = _write_rollout(live_root, "rollout-1-%s.jsonl" % UUID_A,
                          _rollout_lines(UUID_A, n_extra=3))
    bak = _write_rollout(bak_root, "rollout-1-%s.jsonl" % UUID_A,
                         _rollout_lines(UUID_A, n_extra=3))
    bak_bytes = open(bak, "rb").read()

    copies, _ = dedup.scan_stores([(live_root, dedup.STORE_LIVE),
                                   (bak_root, dedup.STORE_BACKUP)])
    sessions = dedup.consolidate(copies)

    assert len(sessions) == 1
    assert sessions[0].canonical.file_path == live
    assert os.path.exists(bak)
    assert open(bak, "rb").read() == bak_bytes
    assert os.path.exists(live)


# ------------------------------------------------- scanning real rollout files

def test_scan_store_derives_the_id_from_the_rollout_and_collapses_the_mirror(tmp_path):
    """The identity rule rides codex_rollout's own id derivation
    (session_meta.session_id -> .id -> filename UUID), so the same session written to
    two stores collapses even though the two filenames differ."""
    live_root = os.path.join(str(tmp_path), "sessions", "2026", "03", "23")
    bak_root = os.path.join(str(tmp_path), "sessions_backup", "2026", "03", "23")
    _write_rollout(live_root, "rollout-2026-03-23T10-00-00-%s.jsonl" % UUID_A,
                   _rollout_lines(UUID_A, n_extra=5))
    _write_rollout(bak_root, "rollout-copy-of-%s.jsonl" % UUID_A,
                   _rollout_lines(UUID_A))
    _write_rollout(live_root, "rollout-2026-03-23T11-00-00-%s.jsonl" % UUID_B,
                   _rollout_lines(UUID_B, user_text="hello"))

    copies, errors = dedup.scan_stores([
        (os.path.join(str(tmp_path), "sessions"), dedup.STORE_LIVE),
        (os.path.join(str(tmp_path), "sessions_backup"), dedup.STORE_BACKUP)])

    assert errors == []
    assert len(copies) == 3
    assert all(c.size_bytes > 0 for c in copies)
    assert all(c.last_write_ms is not None for c in copies)

    sessions = dedup.consolidate(copies)
    assert [s.session_id for s in sessions] == sorted([UUID_A, UUID_B])
    by_id = {s.session_id: s for s in sessions}
    assert by_id[UUID_A].copy_count == 2
    assert by_id[UUID_A].canonical.store_kind == dedup.STORE_LIVE
    assert by_id[UUID_B].copy_count == 1


def test_scan_store_falls_back_to_the_filename_uuid_when_the_header_is_gone(tmp_path):
    """A truncated/resumed log can lack session_meta (codex_rollout trap 6). The
    filename UUID still identifies it, so it merges with its complete sibling."""
    root = os.path.join(str(tmp_path), "sessions")
    full = _write_rollout(root, "rollout-full-%s.jsonl" % UUID_A,
                          _rollout_lines(UUID_A, n_extra=6))
    headerless = _write_rollout(root, "rollout-tail-%s.jsonl" % UUID_A,
                                _rollout_lines(UUID_A, n_extra=1)[1:])

    copies, errors = dedup.scan_store(root, dedup.STORE_LIVE)
    assert errors == []
    sessions = dedup.consolidate(copies)

    assert len(sessions) == 1
    assert {c.file_path for c in sessions[0].copies} == {full, headerless}
    assert sessions[0].canonical.file_path == full   # larger => not the truncated tail


def test_scan_store_yields_a_blank_id_for_a_file_with_no_recoverable_identity(tmp_path):
    root = os.path.join(str(tmp_path), "sessions")
    _write_rollout(root, "rollout-anonymous.jsonl",
                   [json.dumps({"type": "response_item", "timestamp": "",
                                "payload": {"type": "message", "role": "user",
                                            "content": [{"type": "input_text",
                                                         "text": "hi"}]}})])
    copies, errors = dedup.scan_store(root, dedup.STORE_LIVE)
    assert errors == []
    assert len(copies) == 1 and copies[0].session_id == ""
    assert dedup.consolidate(copies)[0].is_identified is False


def test_scan_store_reports_a_bad_line_without_losing_the_copy(tmp_path):
    root = os.path.join(str(tmp_path), "sessions")
    lines = _rollout_lines(UUID_A) + ['{"type": "response_item", "pay']
    _write_rollout(root, "rollout-torn-%s.jsonl" % UUID_A, lines)

    copies, errors = dedup.scan_store(root, dedup.STORE_LIVE)

    assert len(copies) == 1 and copies[0].session_id == UUID_A
    assert len(errors) == 1
    assert errors[0]["stage"] == "parse" and errors[0]["line"] == 3


def test_scan_store_on_a_missing_root_is_empty_not_an_error(tmp_path):
    copies, errors = dedup.scan_store(os.path.join(str(tmp_path), "nope"),
                                      dedup.STORE_BACKUP)
    assert copies == [] and errors == []


def test_scan_store_defaults_to_the_unknown_store_kind(tmp_path):
    root = os.path.join(str(tmp_path), "sessions")
    _write_rollout(root, "rollout-x-%s.jsonl" % UUID_A, _rollout_lines(UUID_A))
    copies, _ = dedup.scan_store(root)
    assert copies[0].store_kind == dedup.STORE_UNKNOWN


def test_scan_stores_over_no_roots_is_empty():
    assert dedup.scan_stores([]) == ([], [])


# ------------------------------------------------------------------- persistence

def _mem():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_ensure_schema_is_idempotent_and_owns_its_own_table():
    conn = _mem()
    dedup.ensure_schema(conn)
    dedup.ensure_schema(conn)          # second call must be a no-op, not an error
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert dedup.COPIES_TABLE in names
    # dedup must NOT redefine corpus.py's schema.
    assert "threads" not in names and "conversations" not in names


def test_ensure_schema_composes_with_the_corpus_index(tmp_path):
    """A dedup table added to a live corpus index leaves the corpus tables intact."""
    conn = corpus.open_index(os.path.join(str(tmp_path), "idx.sqlite"))
    dedup.ensure_schema(conn)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"threads", "thread_spawn_edges", "conversations",
            dedup.COPIES_TABLE} <= names
    corpus.upsert_thread(conn, corpus.ThreadMeta(id=UUID_A, title="still works"))
    assert conn.execute("SELECT count(*) FROM threads").fetchone()[0] == 1


def test_save_then_load_round_trips_every_copy_and_the_canonical_choice():
    conn = _mem()
    dedup.ensure_schema(conn)
    copies = [_copy(UUID_A, "/live/a.jsonl", dedup.STORE_LIVE, 10, 100),
              _copy(UUID_A, "/bak/a.jsonl", dedup.STORE_BACKUP, 20, 100),
              _copy(UUID_B, "/bak/b.jsonl", dedup.STORE_BACKUP, None, 300),
              _copy("", "/live/mystery.jsonl", dedup.STORE_LIVE, 40, 400)]
    saved = dedup.consolidate(copies)

    dedup.save_sessions(conn, saved)
    loaded = dedup.load_sessions(conn)

    assert loaded == saved
    assert conn.execute(
        "SELECT count(*) FROM %s" % dedup.COPIES_TABLE).fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM %s WHERE is_canonical=1"
                        % dedup.COPIES_TABLE).fetchone()[0] == 3


def test_save_sessions_is_idempotent_and_adds_no_duplicate_rows():
    conn = _mem()
    dedup.ensure_schema(conn)
    sessions = dedup.consolidate([_copy(UUID_A, "/live/a.jsonl", dedup.STORE_LIVE),
                                  _copy(UUID_A, "/bak/a.jsonl", dedup.STORE_BACKUP)])
    dedup.save_sessions(conn, sessions)
    dedup.save_sessions(conn, sessions)
    assert conn.execute(
        "SELECT count(*) FROM %s" % dedup.COPIES_TABLE).fetchone()[0] == 2
    assert dedup.load_sessions(conn) == sessions


def test_save_sessions_never_deletes_a_previously_recorded_copy():
    """A later scan that sees FEWER stores must not erase the evidence of the copy it
    no longer sees — the table is append/replace only."""
    conn = _mem()
    dedup.ensure_schema(conn)
    both = [_copy(UUID_A, "/live/a.jsonl", dedup.STORE_LIVE, 10, 100),
            _copy(UUID_A, "/bak/a.jsonl", dedup.STORE_BACKUP, 20, 100)]
    dedup.save_sessions(conn, dedup.consolidate(both))
    dedup.save_sessions(conn, dedup.consolidate([both[0]]))

    assert conn.execute(
        "SELECT count(*) FROM %s" % dedup.COPIES_TABLE).fetchone()[0] == 2
    assert dedup.load_sessions(conn)[0].copy_count == 2


def test_load_sessions_from_an_empty_table_is_empty():
    conn = _mem()
    dedup.ensure_schema(conn)
    assert dedup.load_sessions(conn) == []


# ---------------------------------------------------- composing with corpus.Corpus

def _conv(cid, path, title="t"):
    return ir.Conversation(id=cid, title=title, provider="codex",
                           turns=[ir.Turn("human", [ir.Block("text", text="hi")])],
                           created_at="", updated_at="", account="",
                           meta={"rollout_path": path})


def test_collapse_corpus_keeps_one_thread_and_conversation_per_logical_session():
    """The MAPPING: N physical copies of session X -> ONE ThreadMeta (id=X,
    rollout_path=canonical) and ONE Conversation (the canonical file's)."""
    src = corpus.Corpus()
    src.add_thread(corpus.ThreadMeta(id=UUID_A, rollout_path="/bak/a.jsonl"))
    src.add_thread(corpus.ThreadMeta(id=UUID_B, rollout_path="/live/b.jsonl"))
    src.conversations.extend([_conv(UUID_A, "/bak/a.jsonl"),
                              _conv(UUID_A, "/live/a.jsonl"),
                              _conv(UUID_B, "/live/b.jsonl")])
    sessions = dedup.consolidate([
        _copy(UUID_A, "/live/a.jsonl", dedup.STORE_LIVE, 10, 100),
        _copy(UUID_A, "/bak/a.jsonl", dedup.STORE_BACKUP, 20, 100),
        _copy(UUID_B, "/live/b.jsonl", dedup.STORE_LIVE, 30, 300)])

    out = dedup.collapse_corpus(src, sessions)

    assert len(out.conversations) == 2
    assert sorted(c.id for c in out.conversations) == sorted([UUID_A, UUID_B])
    kept = {c.id: c.meta["rollout_path"] for c in out.conversations}
    assert kept[UUID_A] == "/live/a.jsonl"          # canonical, not the first seen
    assert out.threads[UUID_A].rollout_path == "/live/a.jsonl"
    assert out.threads[UUID_B].rollout_path == "/live/b.jsonl"
    assert src.threads[UUID_A].rollout_path == "/bak/a.jsonl"   # source untouched
    assert len(src.conversations) == 3


def test_collapse_corpus_leaves_the_spawn_edge_graph_bit_identical():
    """The load-bearing composition claim: dedup groups BY thread id and never renames
    or merges an id, so every SpawnEdge endpoint survives and the graph helpers answer
    exactly as before."""
    root, child, grandchild = "t-root", "t-child", "t-grand"
    src = corpus.Corpus()
    for tid in (root, child, grandchild):
        src.add_thread(corpus.ThreadMeta(id=tid, rollout_path="/bak/%s.jsonl" % tid))
    src.add_edge(corpus.SpawnEdge(root, child, "completed"))
    src.add_edge(corpus.SpawnEdge(child, grandchild, "completed"))
    # every thread was written to BOTH stores => duplicate conversations
    for tid in (root, child, grandchild):
        src.conversations.append(_conv(tid, "/bak/%s.jsonl" % tid))
        src.conversations.append(_conv(tid, "/live/%s.jsonl" % tid))
    copies = []
    for tid in (root, child, grandchild):
        copies.append(_copy(tid, "/live/%s.jsonl" % tid, dedup.STORE_LIVE, 9, 900))
        copies.append(_copy(tid, "/bak/%s.jsonl" % tid, dedup.STORE_BACKUP, 9, 900))

    sessions = dedup.consolidate(copies)
    out = dedup.collapse_corpus(src, sessions)

    assert len(sessions) == 3
    assert len(src.conversations) == 6 and len(out.conversations) == 3
    assert out.edges == src.edges
    assert out.roots() == src.roots() == [root]
    assert out.children_of(root) == [child]
    assert out.children_of(child) == [grandchild]
    assert out.fan_out(root) == src.fan_out(root) == 1
    assert [out.depth(t) for t in (root, child, grandchild)] == [0, 1, 2]
    assert [src.depth(t) for t in (root, child, grandchild)] == [0, 1, 2]


def test_collapse_corpus_keeps_threads_and_conversations_dedup_never_saw():
    """Non-deletion again: a thread/conversation with no PhysicalCopy record (e.g. a
    Claude or ChatGPT conversation) passes through untouched, and a dangling spawn
    parent still roots its subtree."""
    src = corpus.Corpus()
    src.add_thread(corpus.ThreadMeta(id="claude-1", rollout_path=""))
    src.conversations.append(_conv("claude-1", ""))
    src.conversations.append(ir.Conversation(id="no-meta", title="x", provider="chatgpt",
                                             turns=[], created_at="", updated_at="",
                                             account="", meta={}))
    src.add_edge(corpus.SpawnEdge("ghost-parent", "claude-1"))

    out = dedup.collapse_corpus(src, [])

    assert set(out.threads) == {"claude-1"}
    assert [c.id for c in out.conversations] == ["claude-1", "no-meta"]
    assert out.roots() == ["ghost-parent"]
    assert out.depth("claude-1") == 1


def test_collapse_corpus_of_an_empty_corpus_is_empty():
    out = dedup.collapse_corpus(corpus.Corpus(), [])
    assert out.conversations == [] and out.threads == {} and out.edges == []


def test_collapse_corpus_ignores_an_unidentified_session():
    """A blank-id logical session has no thread id to map onto, so it changes nothing
    in the graph — but it is still in the dedup view for the UI to surface."""
    src = corpus.Corpus()
    src.add_thread(corpus.ThreadMeta(id=UUID_A, rollout_path="/bak/a.jsonl"))
    src.conversations.append(_conv(UUID_A, "/bak/a.jsonl"))
    sessions = dedup.consolidate([_copy("", "/live/mystery.jsonl", dedup.STORE_LIVE)])

    out = dedup.collapse_corpus(src, sessions)

    assert set(out.threads) == {UUID_A}
    assert out.threads[UUID_A].rollout_path == "/bak/a.jsonl"
    assert len(out.conversations) == 1
