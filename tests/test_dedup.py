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

from llm_anthology import corpus, dedup


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


def test_live_wins_even_when_a_backup_is_newer_and_larger_but_says_so():
    """store rank is the PRIMARY key (C# `StoreKind is Live ? 0 : 1` sorts first), so a
    newer/larger backup still loses to the live store.

    The WINNER assertion is unchanged and still right: the live store is authoritative
    and must never be demoted behind a mirror. What this test used to get wrong was
    stopping there — it asserted the pick and said nothing about the 999_989 bytes the
    pick just dropped out of the view, which is exactly the hole `has_larger_copy`
    fills. Asserting the pick WITHOUT asserting the signal is what let a 1-turn
    crash-truncated live file hide a 9-turn sibling."""
    live = _copy("s", "/live/s.jsonl", dedup.STORE_LIVE, last_write_ms=1, size_bytes=10)
    backup = _copy("s", "/bak/s.jsonl", dedup.STORE_BACKUP,
                   last_write_ms=999_999, size_bytes=999_999)
    logical = dedup.consolidate([live, backup])[0]
    assert logical.canonical.store_kind == dedup.STORE_LIVE
    assert logical.has_larger_copy is True
    assert logical.duplicate_paths == ("/bak/s.jsonl",)


def test_result_is_ordered_by_session_id():
    out = dedup.consolidate([_copy("b", "/x/b.jsonl"), _copy("a", "/x/a.jsonl")])
    assert [s.session_id for s in out] == ["a", "b"]


def test_result_order_is_by_session_id_even_when_the_paths_disagree():
    """`_session_key`'s FIRST term is the session id; the canonical path is only the
    tiebreak. A fixture whose ids and paths sort the same way cannot tell the two apart
    (drop the id term and it still passes), so here the path order is the exact REVERSE
    of the id order."""
    out = dedup.consolidate([_copy("id-a", "/x/z-sorts-last.jsonl"),
                             _copy("id-b", "/x/a-sorts-first.jsonl")])
    assert [s.session_id for s in out] == ["id-a", "id-b"]
    assert [s.canonical.file_path for s in out] == ["/x/z-sorts-last.jsonl",
                                                    "/x/a-sorts-first.jsonl"]


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


def test_an_unknown_mtime_loses_to_a_known_mtime_of_zero():
    """The unknown-mtime sentinel must sort after EVERY known mtime, including 0 — and
    the paths here are chosen so that a sentinel which merely TIES with 0 would hand the
    win to the unknown copy. Without that, `(1, 0)` and `(0, 0)` are indistinguishable:
    for any positive mtime `-ms` is already negative, so a `(0, 0)` sentinel still
    loses, and with equal paths the tiebreak hides the difference too."""
    unknown = _copy(UUID_A, "/bak/a-sorts-first.jsonl", last_write_ms=None,
                    size_bytes=500)
    known_zero = _copy(UUID_A, "/bak/z-sorts-last.jsonl", last_write_ms=0,
                       size_bytes=500)
    logical = dedup.consolidate([unknown, known_zero])[0]
    assert logical.canonical.file_path == "/bak/z-sorts-last.jsonl"


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


# ------------------- nasty case 2b: the canonical is the SMALLER copy — say so

def test_a_truncated_live_copy_is_flagged_when_a_bigger_sibling_is_demoted(tmp_path):
    """Size-DESC only breaks ties WITHIN a store, so across stores a crash-truncated
    LIVE rollout stays canonical while its complete backup sibling drops out of any view
    built from the canonical alone. Measured on real files: live 1 turn vs backup 9 turns,
    same session_id, and the rendered view showed only the 1-turn copy.

    The pick itself is deliberate (the live store is authoritative). The defect was
    doing it SILENTLY: `never hide one of the owner's conversations` is this module's
    stated doctrine, so a canonical that is smaller than a copy it demoted must be
    reported, and `has_larger_copy` is that report."""
    live_root = os.path.join(str(tmp_path), "sessions")
    bak_root = os.path.join(str(tmp_path), "sessions_backup")
    truncated = _write_rollout(live_root, "rollout-%s.jsonl" % UUID_A,
                               _rollout_lines(UUID_A))
    complete = _write_rollout(bak_root, "rollout-%s.jsonl" % UUID_A,
                              _rollout_lines(UUID_A, n_extra=8))

    copies, errors = dedup.scan_stores([(live_root, dedup.STORE_LIVE),
                                        (bak_root, dedup.STORE_BACKUP)])
    logical = dedup.consolidate(copies)[0]

    assert errors == []
    assert os.path.getsize(complete) > os.path.getsize(truncated)
    assert logical.canonical.file_path == truncated       # live still wins...
    assert logical.has_larger_copy is True                # ...but never in silence
    assert complete in logical.duplicate_paths            # and nothing is lost


def test_has_larger_copy_is_false_whenever_nothing_was_hidden():
    """The flag must mean something: it fires ONLY when a demoted copy is strictly
    bigger than the canonical, never on a lone copy and never on an exact-size tie."""
    biggest_wins = dedup.consolidate([
        _copy(UUID_A, "/live/a.jsonl", dedup.STORE_LIVE, 10, 5_000),
        _copy(UUID_A, "/bak/a.jsonl", dedup.STORE_BACKUP, 20, 100)])[0]
    assert biggest_wins.has_larger_copy is False

    same_size = dedup.consolidate([
        _copy(UUID_A, "/live/a.jsonl", dedup.STORE_LIVE, 10, 100),
        _copy(UUID_A, "/bak/a.jsonl", dedup.STORE_BACKUP, 20, 100)])[0]
    assert same_size.has_larger_copy is False

    lone = dedup.consolidate([_copy(UUID_A, "/live/only.jsonl")])[0]
    assert lone.has_larger_copy is False

    # `copies` has a default, so a hand-built session must answer rather than crash on
    # `max(())`.
    bare = dedup.LogicalSession(session_id=UUID_A,
                                canonical=_copy(UUID_A, "/live/bare.jsonl"))
    assert bare.copies == () and bare.has_larger_copy is False


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


def test_two_whitespace_only_ids_are_two_sessions_not_one_blob():
    """`_is_blank` strips, so a whitespace-only id is blank and falls back to the path
    key. Drop the `.strip()` and `"   "` becomes a truthy id that GROUPS: two unrelated
    files merge into one session and one of them vanishes from the view. Reachable —
    `codex_rollout._s` returns the raw string, so `"session_id": "   "` arrives here
    verbatim. One whitespace-id copy cannot show this; it takes two."""
    a = _copy("   ", "/live/ws-1.jsonl", dedup.STORE_LIVE)
    b = _copy("   ", "/live/ws-2.jsonl", dedup.STORE_LIVE)

    sessions = dedup.consolidate([a, b])

    assert len(sessions) == 2
    assert sorted(s.canonical.file_path for s in sessions) == ["/live/ws-1.jsonl",
                                                               "/live/ws-2.jsonl"]
    assert [s.is_identified for s in sessions] == [False, False]


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


def _meta_id_rollout(session_meta_id):
    """A rollout whose ONLY identity is `session_meta.payload.id` — no `session_id`
    key, and the caller gives it a filename with no UUID in it, so `codex_rollout`'s
    third fallback cannot rescue it either."""
    return [json.dumps({"type": "session_meta", "timestamp": "2026-03-23T10:00:00Z",
                        "payload": {"id": session_meta_id, "cwd": "C:/work"}}),
            json.dumps({"type": "response_item", "timestamp": "2026-03-23T10:00:01Z",
                        "payload": {"type": "message", "role": "user",
                                    "content": [{"type": "input_text",
                                                 "text": "hi"}]}})]


def test_scan_store_refuses_to_merge_two_files_on_a_non_uuid_id(tmp_path):
    """`codex_rollout`'s id chain is session_meta.session_id -> .id -> the FILENAME
    UUID. The filename fallback is regex-gated to a UUID shape; the `.id` fallback
    accepts ANY string, so two unrelated rollouts that both carry `id: "shared-nonuuid"`
    used to collapse into ONE logical session — a real false merge, which hides one of
    the owner's conversations behind another.

    A derived id that does not look like a Codex session id is therefore treated as no
    id at all: the same rule the filename fallback already applies, and the safe
    direction (a false split is a cosmetic duplicate; a false merge hides)."""
    root = os.path.join(str(tmp_path), "sessions")
    _write_rollout(root, "rollout-one.jsonl", _meta_id_rollout("shared-nonuuid"))
    _write_rollout(root, "rollout-two.jsonl", _meta_id_rollout("shared-nonuuid"))

    copies, errors = dedup.scan_store(root, dedup.STORE_LIVE)
    sessions = dedup.consolidate(copies)

    assert errors == []
    assert [c.session_id for c in copies] == ["", ""]
    assert len(sessions) == 2
    assert [s.is_identified for s in sessions] == [False, False]
    assert sorted(s.canonical.file_path for s in sessions) == sorted(
        c.file_path for c in copies)

    # ...and an id that merely CONTAINS a UUID is not one either: the shape check has to
    # match the whole string, or `session-<uuid>-tmp` sneaks back in as a shared key.
    embedded = os.path.join(str(tmp_path), "embedded")
    wrapped = "session-%s-tmp" % UUID_A
    _write_rollout(embedded, "rollout-three.jsonl", _meta_id_rollout(wrapped))
    _write_rollout(embedded, "rollout-four.jsonl", _meta_id_rollout(wrapped))

    more, _ = dedup.scan_store(embedded, dedup.STORE_LIVE)
    assert [c.session_id for c in more] == ["", ""]
    assert len(dedup.consolidate(more)) == 2


def test_scan_store_still_merges_on_a_uuid_shaped_id_fallback(tmp_path):
    """The gate is on the SHAPE, not on the fallback: a `session_meta.payload.id` that
    IS a UUID stays a real identity, so the two stores still collapse to one session."""
    live_root = os.path.join(str(tmp_path), "sessions")
    bak_root = os.path.join(str(tmp_path), "sessions_backup")
    _write_rollout(live_root, "rollout-live.jsonl", _meta_id_rollout(UUID_A))
    _write_rollout(bak_root, "rollout-backup.jsonl", _meta_id_rollout(UUID_A))

    copies, _ = dedup.scan_stores([(live_root, dedup.STORE_LIVE),
                                   (bak_root, dedup.STORE_BACKUP)])
    sessions = dedup.consolidate(copies)

    assert len(sessions) == 1
    assert sessions[0].session_id == UUID_A
    assert sessions[0].is_identified is True
    assert sessions[0].copy_count == 2


def test_scan_store_records_the_mtime_in_milliseconds_not_seconds(tmp_path):
    """`last_write_ms` is a MILLISECOND field (the C# `LastWriteTimeUtc` in ms) and the
    whole preference key's third term depends on the unit: in seconds, every write
    inside the same second ties and silently falls through to the path tiebreak."""
    root = os.path.join(str(tmp_path), "sessions")
    path = _write_rollout(root, "rollout-x-%s.jsonl" % UUID_A, _rollout_lines(UUID_A))

    copies, _ = dedup.scan_store(root, dedup.STORE_LIVE)

    stat = os.stat(path)
    assert copies[0].last_write_ms == int(stat.st_mtime * 1000)
    assert copies[0].last_write_ms > int(stat.st_mtime)     # ms, not seconds
    assert copies[0].size_bytes == stat.st_size


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
