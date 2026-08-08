"""maintenance.py — gated, recoverable archive / move / reconcile / delete.

SYNTHETIC fixtures ONLY. Every store root is a pytest `tmp_path`, every session file
is invented bytes, and every "protected" path is a synthetic directory that merely
*spells* the protected marker (`<tmp>/.codex/sessions/...`). Nothing here reads
$CODEX_HOME, ~/.codex, AppData, or any real session store, and no test asserts on
conversation content — this module only ever moves opaque files.

What is pinned:

  * the PLANNER/EXECUTOR split is the safety property: plan_maintenance is PURE
    (asserted by snapshotting the whole store tree before/after) and returns the
    exact source -> destination pairs; execute_maintenance runs only that plan.
  * three INDEPENDENT gates, each proven in BOTH states (permitted and refused):
      1. dry-run default        — apply=False mutates nothing;
      2. typed confirmation     — exact match required (blank / mismatch / a preview
                                  that does not require one all refuse);
      3. checkpoint-before-act  — the manifest is on disk BEFORE the first move,
                                  proven by crashing shutil.move mid-batch and then
                                  RESTORING from the pending manifest.
  * path-traversal safety, adversarially: `..` segments, an absolute path outside the
    store, a UNC `\\\\host\\share` path (the Windows SMB/NTLM hash-leak class), an
    NTFS alternate-data-stream `name:stream`, and a real SYMLINK inside the store
    whose target is outside it. The symlink case is why the guard resolves paths
    physically (os.path.realpath) and not just lexically.
  * the executor never trusts a hand-built preview — the ROOTS included: a UNC or
    `..` root on the preview is refused before anything is created (os.makedirs on
    `\\\\host\\share` is an outbound SMB authentication, so those probes intercept
    makedirs and never let one leave the machine). It also re-asserts confinement,
    that every plan source is an ALLOWED target, that no source and no destination
    appears twice, that the source exists, and that the destination does not (lexists,
    so a dangling symlink cannot be written through).
  * one physical file is planned exactly once, so the typed-confirmation count cannot
    overstate what will happen and no batch can crash on its own second move.
  * BOTH confinement layers are pinned independently, which needs care: each alone
    refuses a plain outside path, so the tests assert WHICH layer fired.

Path semantics are asserted with pure PureWindowsPath rules where possible so the
suite behaves identically on Linux/macOS/Windows CI; the real moves use the
host-native tmp_path, so the mutating path is exercised on every platform.

PORTABILITY: three tests still need FILE symlinks (Developer Mode on Windows) and skip
without them, but none is uniquely load-bearing any more — measured with every one of
them deselected, maintenance.py still reports 275/0 statements, 90/0 branches, 100%.
The physical-confinement layer, whose only route is a link, is reached through a
DIRECTORY link instead: a symlink on POSIX, an NTFS junction on Windows, which needs
no elevation. The separator and lexical rules are additionally pinned by assertions
that need no link at all.
"""
import json
import os
import sqlite3
import tempfile

import pytest

from llm_anthology import maintenance as mt


# --------------------------------------------------------------------- helpers

def _can_symlink():
    """Probe once: file symlinks need Developer Mode / elevation on Windows."""
    d = tempfile.mkdtemp()
    target = os.path.join(d, "t")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("t")
    try:
        os.symlink(target, os.path.join(d, "l"))
        return True
    except OSError:                          # env capability probe, not app code
        return False


_SYMLINKS = _can_symlink()


def _link_dir(link, target):
    """Create `link` as a DIRECTORY link resolving to `target`.

    Prefers a plain symlink; on Windows falls back to an NTFS junction, which — unlike a
    file symlink — needs neither Developer Mode nor elevation. That fallback is the
    whole point: it lets the PHYSICAL confinement layer be exercised on a stock Windows
    host, where the file-symlink tests below skip and would otherwise take the only
    route to that layer with them.
    """
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        import _winapi                     # CPython-on-Windows junction, no elevation
        _winapi.CreateJunction(target, link)


def _can_link_dir():
    d = tempfile.mkdtemp()
    try:
        _link_dir(os.path.join(d, "l"), d)
        return True
    except (OSError, AttributeError, ImportError):   # env capability probe, not app code
        return False


_DIR_LINKS = _can_link_dir()


def _read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def _read_json(path):
    return json.loads(_read_bytes(path).decode("utf-8"))


def _store(tmp_path, *names):
    """Create <tmp>/store with a synthetic session file per name; return (root, paths)."""
    root = tmp_path / "store"
    paths = []
    for name in names:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(("synthetic-" + name).encode("utf-8"))
        paths.append(str(target))
    root.mkdir(parents=True, exist_ok=True)
    return str(root), paths


def _copy(path, session_id="s1", store_kind=None, is_hot=False):
    return mt.SessionCopy(
        session_id=session_id,
        file_path=path,
        store_kind=store_kind or mt.SessionStoreKind.BACKUP,
        last_write_ms=1_700_000_000_000,
        size_bytes=9,
        is_hot=is_hot,
    )


def _request(action, targets, store_root, tmp_path, *, destination_root=None,
             typed_confirmation=""):
    if destination_root is None:
        destination_root = str(tmp_path / "archive")
    return mt.MaintenanceRequest(
        action=action,
        targets=tuple(targets),
        store_root=store_root,
        checkpoint_root=str(tmp_path / "checkpoints"),
        destination_root=destination_root,
        typed_confirmation=typed_confirmation,
    )


def _tree(root):
    """A snapshot of every file under `root` as {relative path: bytes} (purity oracle)."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as fh:
                out[os.path.relpath(full, root)] = fh.read()
    return out


# ------------------------------------------------------- planner: purity + shape

def test_plan_is_pure_and_touches_nothing(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl", "b.jsonl")
    before = _tree(str(tmp_path))
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.ARCHIVE, [_copy(paths[0]), _copy(paths[1], "s2")],
                 root, tmp_path))
    assert _tree(str(tmp_path)) == before                # no mutation whatsoever
    assert not (tmp_path / "archive").exists()
    assert not (tmp_path / "checkpoints").exists()
    assert len(preview.allowed) == 2
    assert preview.blocked == ()
    assert preview.requires_checkpoint is True
    assert preview.requires_typed_confirmation is True
    assert preview.required_typed_confirmation == "ARCHIVE 2 FILES"
    assert [(m.source, m.destination) for m in preview.plan] == [
        (paths[0], str(tmp_path / "archive" / "a.jsonl")),
        (paths[1], str(tmp_path / "archive" / "b.jsonl")),
    ]


def test_plan_singular_confirmation_phrase_and_dangerous_warning(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path))
    assert preview.required_typed_confirmation == "DELETE 1 FILE"
    dangerous = [w for w in preview.warnings
                 if w.severity is mt.MaintenanceWarningSeverity.DANGEROUS]
    assert any("Dangerous maintenance target" in w.message for w in dangerous)
    info = [w for w in preview.warnings
            if w.severity is mt.MaintenanceWarningSeverity.INFO]
    assert len(info) == 1 and "1 allowed" in info[0].message


def test_plan_blocks_live_store_kind_and_protected_markers(tmp_path):
    """Ports MaintenancePlannerTests: a Live copy and a `.codex\\sessions\\` path are
    BLOCKED (Dangerous), a Backup copy elsewhere is ALLOWED."""
    root, paths = _store(tmp_path, "backup/keep.jsonl",
                         ".codex/sessions/2026/03/23/s.jsonl",
                         ".codex/codex-sqlite/x.jsonl",
                         ".codex/state_5.sqlite",
                         "live/hot.jsonl")
    targets = [
        _copy(paths[0], "keep"),
        _copy(paths[1], "marker-sessions"),
        _copy(paths[2], "marker-sqlite-dir"),
        _copy(paths[3], "marker-state-db"),
        _copy(paths[4], "live", store_kind=mt.SessionStoreKind.LIVE),
    ]
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, targets, root, tmp_path))
    assert [t.file_path for t in preview.allowed] == [paths[0]]
    assert [b.target.file_path for b in preview.blocked] == paths[1:]
    assert {b.reason for b in preview.blocked} == {"protected"}
    assert any("Protected path blocked" in w.message for w in preview.warnings)
    assert [m.source for m in preview.plan] == [paths[0]]
    assert preview.required_typed_confirmation == "DELETE 1 FILE"   # derived from ALLOWED


@pytest.mark.parametrize("spelling", ["doubled-separator", "dot-component"])
def test_plan_blocks_a_protected_path_however_it_is_spelled(tmp_path, spelling):
    """The marker test has to run on the RESOLVED path, not on the caller's string.

    `_classify` resolves a target with realpath and finds it lexically and physically
    inside the store; `_is_protected` then compares its markers against the LITERAL
    `file_path`. So any spelling that normalises to the protected file satisfies
    confinement and matches no marker. Measured before the fix: both spellings below
    reached ALLOWED and `execute_maintenance` relocated the live session file.

    This is the whole live-store defence in practice, not a second line of it — the RPC
    layer builds every target with `store_kind=UNKNOWN` (sidecar.py:1552), so the
    `SessionStoreKind.LIVE` half of the guard can never fire on a request that arrived
    over the wire. Neither spelling needs a filesystem link or any privilege.
    """
    root, paths = _store(tmp_path, ".codex/sessions/s.jsonl")
    head = os.path.join(root, ".codex")
    tail = os.path.join("sessions", "s.jsonl")
    sneaky = (head + os.sep + os.sep + tail if spelling == "doubled-separator"
              else os.path.join(head, ".", tail))
    assert sneaky != paths[0]                        # a different string...
    assert os.path.isfile(sneaky)                    # ...naming the same physical file
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(sneaky)], root, tmp_path))
    assert preview.allowed == () and preview.plan == ()
    assert [b.reason for b in preview.blocked] == ["protected"]
    assert os.path.isfile(paths[0])


@pytest.mark.skipif(not _DIR_LINKS, reason="directory links unavailable on this host")
def test_plan_blocks_a_protected_path_reached_through_a_directory_link(tmp_path):
    """The same hole by the mechanism a person creates on purpose: a directory link
    inside the store pointing at the live session directory — a symlink on POSIX, an
    NTFS junction on Windows, which needs no elevation. Confinement is satisfied because
    the link resolves to somewhere still INSIDE the store, so the marker test is the only
    thing that can refuse it, and `<store>\\innocent\\s.jsonl` spells no marker.

    Not load-bearing for coverage — the spelling cases above reach the same branch on
    every host — but it is the mechanism that makes the hole reachable without anyone
    crafting an odd path.
    """
    root, paths = _store(tmp_path, ".codex/sessions/s.jsonl")
    _link_dir(str(tmp_path / "store" / "innocent"),
              os.path.join(root, ".codex", "sessions"))
    through_link = os.path.join(root, "innocent", "s.jsonl")
    assert os.path.isfile(through_link)              # the link really reaches it
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(through_link)], root, tmp_path))
    assert [b.reason for b in preview.blocked] == ["protected"]
    assert os.path.isfile(paths[0])


@pytest.mark.parametrize("action,field", [
    (mt.MaintenanceAction.ARCHIVE, "destination_root"),
    (mt.MaintenanceAction.DELETE, "checkpoint_root"),
])
def test_plan_refuses_to_write_into_a_protected_store_path(tmp_path, action, field):
    """The marker rule guarded TARGETS only, so nothing stopped a run writing INTO the
    live store. Measured before the fix: an ARCHIVE whose destination_root is
    `<x>\\.codex\\sessions` put the session file there.

    Both roots below reach the same effective root through
    `_effective_destination_root`, and both arrive from the client on the wire
    (sidecar.py:1521-1530 only rejects them for being non-local), so both are pinned.
    This is corruption rather than loss — Codex then reads session files it never wrote —
    and a checkpoint cannot undo it, because the checkpoint is what got misplaced.
    """
    root, paths = _store(tmp_path, "a.jsonl")
    protected = str(tmp_path / ".codex" / "sessions")
    fields = dict(action=action, targets=(_copy(paths[0]),), store_root=root,
                  checkpoint_root=str(tmp_path / "checkpoints"),
                  destination_root=("" if action is mt.MaintenanceAction.DELETE
                                    else str(tmp_path / "archive")))
    fields[field] = protected
    with pytest.raises(mt.MaintenanceRefused, match="protected"):
        mt.plan_maintenance(mt.MaintenanceRequest(**fields))
    assert not os.path.exists(protected)             # the planner stayed pure
    assert os.path.isfile(paths[0])


def test_plan_measures_the_size_on_disk_and_ignores_the_callers_claim(tmp_path):
    """`size_bytes` on an ALLOWED target describes the file, not the request.

    It is the one number on the preview a caller could previously dictate: the RPC edge
    forwards `size_bytes` verbatim (`llm_anthology/sidecar.py:1557`) while never even
    reading `last_write_ms` or `is_hot` from the request. And it is not decorative — the
    cockpit's maintenance panel SUMS it and prints the total on the confirm screen beside
    the file count, so an inflated claim became the largest, most reassuring figure on the
    dialog that authorises a delete. Every other number there is derived by this planner
    from what it actually found; this one now is too.
    """
    root, paths = _store(tmp_path, "a.jsonl")
    on_disk = os.path.getsize(paths[0])
    assert on_disk > 0                                   # the fixture wrote real bytes
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE,
                 [mt.SessionCopy(session_id="a", file_path=paths[0],
                                 store_kind=mt.SessionStoreKind.BACKUP,
                                 size_bytes=999_999_999)],
                 root, tmp_path))
    assert [t.size_bytes for t in preview.allowed] == [on_disk]


def test_plan_reports_an_unmeasurable_size_as_zero_rather_than_the_claim(tmp_path):
    """A path that passes confinement but is not a readable file measures 0, not the claim.

    Confinement says nothing about EXISTENCE — a name inside the store that was never
    created satisfies both layers — so the stat can fail. 0 means UNMEASURABLE here, and it
    is safe to report because such a target cannot execute anyway: the executor refuses a
    source that is not a regular file. An honest 0 followed by a hard refusal beats
    carrying the caller's number into the confirm dialog.
    """
    root, _ = _store(tmp_path)
    ghost = str(tmp_path / "store" / "never-existed.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE,
                 [mt.SessionCopy(session_id="g", file_path=ghost,
                                 store_kind=mt.SessionStoreKind.BACKUP,
                                 size_bytes=4242)],
                 root, tmp_path))
    assert [t.size_bytes for t in preview.allowed] == [0]
    with pytest.raises(mt.MaintenanceRefused, match="not a regular file"):
        mt.execute_maintenance(preview, "DELETE 1 FILE", apply=True)


def test_plan_does_not_measure_a_target_it_refused(tmp_path, monkeypatch):
    """A REFUSED target is deliberately left unmeasured.

    It was declined precisely because it is protected or escapes the store, so stat-ing it
    would be a small instance of the very access the confinement layer exists to prevent —
    and it buys nothing, because nothing will be moved. Asserted on WHICH paths were
    stat-ed rather than on the resulting number, since a blocked target's `size_bytes` is
    never read by anything.
    """
    root, paths = _store(tmp_path, "keep.jsonl", ".codex/sessions/live.jsonl")
    measured = []
    real_getsize = os.path.getsize

    def _recording(path):
        measured.append(path)
        return real_getsize(path)

    monkeypatch.setattr(mt.os.path, "getsize", _recording)
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE,
                 [_copy(paths[0], "keep"), _copy(paths[1], "protected")], root, tmp_path))
    assert [b.reason for b in preview.blocked] == ["protected"]
    assert [os.path.normcase(p) for p in measured] == [os.path.normcase(paths[0])]


def test_plan_warns_review_on_a_hot_target(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.MOVE, [_copy(paths[0], is_hot=True)],
                 root, tmp_path))
    review = [w for w in preview.warnings
              if w.severity is mt.MaintenanceWarningSeverity.REVIEW]
    assert len(review) == 1 and "hot" in review[0].message


def test_plan_warns_review_when_the_offered_confirmation_is_stale(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path,
                 typed_confirmation="DELETE 2 FILES"))
    assert preview.required_typed_confirmation == "DELETE 1 FILE"
    assert any(w.severity is mt.MaintenanceWarningSeverity.REVIEW
               and "offered" in w.message for w in preview.warnings)


def test_plan_is_silent_when_the_offered_confirmation_already_matches(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path,
                 typed_confirmation="DELETE 1 FILE"))
    assert not [w for w in preview.warnings
                if w.severity is mt.MaintenanceWarningSeverity.REVIEW]


# ------------------------------------------- planner: effective destination roots

def test_plan_delete_quarantines_under_the_checkpoint_root(tmp_path):
    """Ports GetEffectiveDestinationRoot: Delete ignores destination_root entirely and
    quarantines into <checkpoint_root>/deleted, so a delete is recoverable."""
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path,
                 destination_root=""))
    assert preview.destination_root == str(tmp_path / "checkpoints" / "deleted")
    assert preview.plan[0].destination == str(
        tmp_path / "checkpoints" / "deleted" / "a.jsonl")


def test_plan_reconcile_uses_a_reconciled_subdirectory(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.RECONCILE, [_copy(paths[0])], root, tmp_path,
                 destination_root=str(tmp_path / "dest")))
    assert preview.destination_root == str(tmp_path / "dest" / "reconciled")


def test_plan_move_and_archive_use_the_destination_root_verbatim(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    for action in (mt.MaintenanceAction.MOVE, mt.MaintenanceAction.ARCHIVE):
        preview = mt.plan_maintenance(
            _request(action, [_copy(paths[0])], root, tmp_path,
                     destination_root=str(tmp_path / "dest")))
        assert preview.destination_root == str(tmp_path / "dest")


def test_plan_requires_a_destination_root_for_non_delete_actions(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    with pytest.raises(mt.MaintenanceRefused, match="destination_root"):
        mt.plan_maintenance(_request(mt.MaintenanceAction.ARCHIVE, [_copy(paths[0])],
                                     root, tmp_path, destination_root="   "))


# ------------------------------------------------ planner: destination collisions

def test_plan_gives_colliding_basenames_distinct_destinations(tmp_path):
    """Ports ExecuteAsync_GeneratesUniqueDestinationNames: same basename in two source
    directories must not collapse onto one destination. Deterministic `-N` suffixes
    replace the C# Guid so the plan is auditable BEFORE it runs."""
    root, paths = _store(tmp_path, "a/shared.jsonl", "b/shared.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.ARCHIVE,
                 [_copy(paths[0], "a"), _copy(paths[1], "b")], root, tmp_path))
    dests = [m.destination for m in preview.plan]
    assert dests == [str(tmp_path / "archive" / "shared.jsonl"),
                     str(tmp_path / "archive" / "shared-1.jsonl")]


def test_plan_skips_destination_names_already_on_disk(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "a.jsonl").write_bytes(b"occupied")
    (archive / "a-1.jsonl").write_bytes(b"occupied too")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.ARCHIVE, [_copy(paths[0])], root, tmp_path))
    assert preview.plan[0].destination == str(archive / "a-2.jsonl")


@pytest.mark.skipif(not _SYMLINKS, reason="file symlinks unavailable on this host")
def test_plan_treats_a_dangling_link_at_the_destination_as_occupied(tmp_path):
    """`_unique_name` probes with lexists, not exists: a dangling symlink already holds
    the name. With `exists` the planner would show the operator a destination the
    executor will then refuse — the run fails closed, but only after the operator
    confirmed a plan that could never have run."""
    root, paths = _store(tmp_path, "a.jsonl")
    archive = tmp_path / "archive"
    archive.mkdir()
    os.symlink(str(archive / "sub" / "nowhere.jsonl"), str(archive / "a.jsonl"))
    assert not os.path.exists(archive / "a.jsonl")        # dangling...
    assert os.path.lexists(archive / "a.jsonl")           # ...but the name is taken
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.ARCHIVE, [_copy(paths[0])], root, tmp_path))
    assert preview.plan[0].destination == str(archive / "a-1.jsonl")


# ------------------------------------ planner: one physical file is planned only once

def test_plan_deduplicates_two_targets_that_name_one_physical_file(tmp_path):
    """Two SessionCopy entries CAN name the same file_path — the sibling DEDUP unit
    exists precisely because one logical session has several physical copies — and both
    pass the executor's isfile pre-flight, because at check time the file is still
    there. Left alone the plan holds two moves from one source: the operator is asked to
    type "DELETE 2 FILES" for a single file, and the second move crashes."""
    root, paths = _store(tmp_path, "b.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE,
                 [_copy(paths[0], "first"), _copy(paths[0], "second")], root, tmp_path))
    assert [t.session_id for t in preview.allowed] == ["first"]
    assert [(b.reason, b.target.session_id) for b in preview.blocked] == [
        ("duplicate-target", "second")]
    assert [m.source for m in preview.plan] == [paths[0]]
    assert preview.required_typed_confirmation == "DELETE 1 FILE"     # a truthful count
    assert any(w.severity is mt.MaintenanceWarningSeverity.REVIEW
               and "Duplicate target" in w.message for w in preview.warnings)


def test_plan_treats_a_case_or_separator_variant_as_the_same_physical_file(tmp_path):
    """The de-duplication key is the same `normcase` key the executor already uses for
    its allowed-set, so it folds case and `/` on Windows and stays exact on POSIX —
    a duplicate must not slip through by being spelled differently."""
    root, paths = _store(tmp_path, "b.jsonl")
    variant = paths[0].replace(os.sep, "/") if os.sep == "\\" else paths[0]
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE,
                 [_copy(paths[0], "first"), _copy(variant, "second")], root, tmp_path))
    assert [t.session_id for t in preview.allowed] == ["first"]
    assert [b.reason for b in preview.blocked] == ["duplicate-target"]


def test_plan_blocks_a_duplicate_as_protected_when_it_is_also_protected(tmp_path):
    """A protected duplicate is reported as PROTECTED, not merely as a duplicate: the
    more dangerous reason wins, so the audit record names the real objection."""
    root, paths = _store(tmp_path, ".codex/sessions/s.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE,
                 [_copy(paths[0], "a"), _copy(paths[0], "b")], root, tmp_path))
    assert [b.reason for b in preview.blocked] == ["protected", "protected"]


def test_execute_and_restore_survive_a_duplicated_target(tmp_path):
    """The end-to-end consequence. Without de-duplication the second move raises an
    uncaught FileNotFoundError mid-batch, and restore_checkpoint then refuses the whole
    batch as an "unaccounted move" — so the file that DID move is not auto-recoverable,
    which is the entire safety argument for a DELETE being a quarantine."""
    root, paths = _store(tmp_path, "b.jsonl")
    original = _read_bytes(paths[0])
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE,
                 [_copy(paths[0], "first"), _copy(paths[0], "second")], root, tmp_path))
    result = mt.execute_maintenance(preview, preview.required_typed_confirmation,
                                    apply=True)
    assert result.executed is True and len(result.moves) == 1
    assert not os.path.exists(paths[0])
    restored = mt.restore_checkpoint(result.manifest_path, apply=True)
    assert restored.executed is True
    assert _read_bytes(paths[0]) == original


# ------------------------------------------------ planner: adversarial path safety

def test_plan_blocks_a_unc_target_path(tmp_path):
    """The Windows SMB/NTLM hash-leak class: touching \\\\host\\share coerces an
    outbound authentication. At least as strict as export.py's UNC guard."""
    root, _ = _store(tmp_path)
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE,
                 [_copy(r"\\evil\share\s.jsonl")], root, tmp_path))
    assert preview.allowed == () and preview.plan == ()
    assert [b.reason for b in preview.blocked] == ["unsafe-path"]
    assert "UNC" in preview.blocked[0].detail


def test_plan_blocks_a_protocol_relative_unc_target(tmp_path):
    root, _ = _store(tmp_path)
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE,
                 [_copy("//evil/share/s.jsonl")], root, tmp_path))
    assert [b.reason for b in preview.blocked] == ["unsafe-path"]


def test_plan_blocks_parent_traversal_segments(tmp_path):
    root, _ = _store(tmp_path)
    escape = str(tmp_path / "store") + "/../outside/secret.jsonl"
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(escape)], root, tmp_path))
    assert preview.allowed == ()
    assert [b.reason for b in preview.blocked] == ["traversal"]


def test_plan_blocks_an_absolute_path_outside_the_store_root(tmp_path):
    root, _ = _store(tmp_path)
    outside = tmp_path / "outside" / "secret.jsonl"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"not yours")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(str(outside))], root, tmp_path))
    assert [b.reason for b in preview.blocked] == ["outside-store-root"]
    assert outside.read_bytes() == b"not yours"


def test_plan_blocks_a_windows_drive_absolute_path_on_every_platform(tmp_path):
    root, _ = _store(tmp_path)
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE,
                 [_copy(r"C:\Windows\System32\drivers\etc\hosts")], root, tmp_path))
    assert [b.reason for b in preview.blocked] == ["outside-store-root"]


def test_plan_blocks_an_alternate_data_stream_basename(tmp_path):
    """`x.jsonl:evil` is an NTFS ADS: moving to it writes a hidden stream rather than
    the file the operator saw in the preview."""
    root, _ = _store(tmp_path)
    ads = str(tmp_path / "store" / "a.jsonl:evil")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(ads)], root, tmp_path))
    assert [b.reason for b in preview.blocked] == ["unsafe-path"]
    assert "stream" in preview.blocked[0].detail


def test_plan_blocks_the_store_root_itself_as_a_target(tmp_path):
    """The root IS `_within` itself, so the lexical check passes; the executor's
    is-a-regular-file gate is what stops it."""
    root, _ = _store(tmp_path)
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(root)], root, tmp_path))
    assert len(preview.allowed) == 1                        # lexically confined
    with pytest.raises(mt.MaintenanceRefused, match="not a regular file"):
        mt.execute_maintenance(preview, "DELETE 1 FILE", apply=True)


@pytest.mark.skipif(not _SYMLINKS, reason="file symlinks unavailable on this host")
def test_plan_blocks_a_symlink_that_escapes_the_store_root(tmp_path):
    """The case a LEXICAL guard cannot see: a link INSIDE the store whose real target
    is outside it. Deleting through it would move the victim file."""
    root, _ = _store(tmp_path)
    victim = tmp_path / "outside" / "secret.jsonl"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"not yours")
    link = tmp_path / "store" / "innocent.jsonl"
    os.symlink(str(victim), str(link))
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(str(link))], root, tmp_path))
    assert [b.reason for b in preview.blocked] == ["outside-store-root"]
    assert "resolved" in preview.blocked[0].detail
    assert victim.read_bytes() == b"not yours"


# -------------------------- confinement: BOTH layers are load-bearing, not just one

def test_within_rejects_a_sibling_directory_whose_name_extends_the_root(tmp_path):
    """Pins the separator suffix in `_within`: `.../store-evil` must not pass as
    `.../store`. Asserted on the predicate itself because the only end-to-end route
    into it needs a symlink, and not every host can create one — without this the rule
    is unpinned on exactly those hosts."""
    root = os.path.join(str(tmp_path), "store")
    assert mt._within(root, root) is True
    assert mt._within(os.path.join(root, "a.jsonl"), root) is True
    assert mt._within(os.path.join(str(tmp_path), "store-evil", "a.jsonl"), root) is False


def test_classify_blocks_an_outside_target_lexically_before_resolving_anything(tmp_path):
    """Pins the LEXICAL layer. The module claims two layers "because either alone is
    insufficient", but the physical layer also refuses a plain outside path — so
    deleting the lexical guard leaves the suite green unless a test observes WHICH layer
    fired. The two details are distinguishable, so it can."""
    root, _ = _store(tmp_path)
    outside = tmp_path / "outside" / "secret.jsonl"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"not yours")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(str(outside))], root, tmp_path))
    detail = preview.blocked[0].detail
    assert "is not within the store root" in detail       # layer 1, lexical
    assert "resolved to" not in detail                    # ...and not layer 2, realpath


@pytest.mark.skipif(not _DIR_LINKS, reason="directory links unavailable on this host")
def test_plan_blocks_a_directory_link_escaping_into_a_sibling_of_the_store(tmp_path):
    """End-to-end form of the separator rule, and the only route to the PHYSICAL layer's
    refusal: a directory link inside `.../store` resolving into the sibling
    `.../store-evil`. Lexically innocent, so layer 1 waves it through and only a
    separator-aware containment test can refuse it. Uses a directory link rather than a
    file symlink so it still runs — and still covers that layer — on a host without
    Developer Mode."""
    root, _ = _store(tmp_path)
    victim = tmp_path / "store-evil" / "secret.jsonl"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"not yours")
    _link_dir(str(tmp_path / "store" / "innocent"), str(tmp_path / "store-evil"))
    escaping = str(tmp_path / "store" / "innocent" / "secret.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(escaping)], root, tmp_path))
    assert [b.reason for b in preview.blocked] == ["outside-store-root"]
    assert "resolved to" in preview.blocked[0].detail       # layer 2, the realpath one
    assert victim.read_bytes() == b"not yours"


@pytest.mark.skipif(not _DIR_LINKS, reason="directory links unavailable on this host")
def test_plan_blocks_an_outside_directory_link_that_resolves_inside_the_store(tmp_path):
    """End-to-end form of the lexical rule, and the case the PHYSICAL layer structurally
    cannot catch: a link OUTSIDE the store resolving to a real file inside it. realpath
    lands in the store, so containment is satisfied and only the lexical layer can
    refuse a target the operator named by a path outside the store."""
    root, paths = _store(tmp_path, "real.jsonl")
    (tmp_path / "outside").mkdir(parents=True)
    _link_dir(str(tmp_path / "outside" / "innocent"), root)
    disguised = str(tmp_path / "outside" / "innocent" / "real.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(disguised)], root, tmp_path))
    assert [b.reason for b in preview.blocked] == ["outside-store-root"]
    assert "is not within the store root" in preview.blocked[0].detail
    assert os.path.isfile(paths[0])


@pytest.mark.parametrize("bad", [r"\\evil\share", "", "   ", "store/../elsewhere"])
def test_plan_refuses_an_unsafe_store_root_outright(tmp_path, bad):
    """A bad ROOT is a bad request, not a blockable target: fail loud, plan nothing."""
    with pytest.raises(mt.MaintenancePathError):
        mt.plan_maintenance(_request(mt.MaintenanceAction.DELETE, [], bad, tmp_path))


def test_plan_refuses_an_unsafe_checkpoint_root(tmp_path):
    root, _ = _store(tmp_path)
    request = mt.MaintenanceRequest(
        action=mt.MaintenanceAction.DELETE, targets=(), store_root=root,
        checkpoint_root=r"\\evil\share\cp", destination_root="")
    with pytest.raises(mt.MaintenancePathError, match="checkpoint root"):
        mt.plan_maintenance(request)


def test_plan_refuses_an_unsafe_destination_root(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    with pytest.raises(mt.MaintenancePathError, match="destination root"):
        mt.plan_maintenance(_request(mt.MaintenanceAction.ARCHIVE, [_copy(paths[0])],
                                     root, tmp_path,
                                     destination_root=r"\\evil\share\arch"))


def test_plan_refuses_a_nul_byte_in_a_root(tmp_path):
    root, _ = _store(tmp_path)
    with pytest.raises(mt.MaintenancePathError, match="NUL"):
        mt.plan_maintenance(_request(mt.MaintenanceAction.DELETE, [], root + "\x00x",
                                     tmp_path))


# ------------------------------------------------------- gate 1: dry-run default

def test_execute_defaults_to_a_dry_run_and_mutates_nothing(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path))
    before = _tree(str(tmp_path))
    result = mt.execute_maintenance(preview, "DELETE 1 FILE")     # no apply=
    assert result.executed is False
    # `moves` is the effective move SET (what would happen); `executed` is the only
    # signal that it happened, and no manifest is written on a dry run.
    assert result.moves == preview.plan and result.manifest_path == ""
    assert _tree(str(tmp_path)) == before
    assert not (tmp_path / "checkpoints").exists()               # not even a checkpoint


# ------------------------------------------------ gate 2: typed confirmation

def test_execute_refuses_a_mismatched_confirmation(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path))
    before = _tree(str(tmp_path))
    with pytest.raises(mt.MaintenanceRefused, match="does not match"):
        mt.execute_maintenance(preview, "delete 1 file", apply=True)   # case differs
    assert _tree(str(tmp_path)) == before


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_execute_refuses_a_blank_confirmation(tmp_path, blank):
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path))
    with pytest.raises(mt.MaintenanceRefused, match="required"):
        mt.execute_maintenance(preview, blank, apply=True)
    assert os.path.isfile(paths[0])


def test_execute_refuses_a_preview_that_does_not_require_confirmation(tmp_path):
    """Ports the C# `!preview.RequiresTypedConfirmation` guard: a preview with the gate
    switched off can never execute — it cannot be used to bypass the gate."""
    root, paths = _store(tmp_path, "a.jsonl")
    planned = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path))
    ungated = mt.MaintenancePreview(
        action=planned.action, allowed=planned.allowed, blocked=planned.blocked,
        warnings=planned.warnings, plan=planned.plan, requires_checkpoint=True,
        requires_typed_confirmation=False,
        required_typed_confirmation=planned.required_typed_confirmation,
        store_root=planned.store_root, destination_root=planned.destination_root,
        checkpoint_root=planned.checkpoint_root)
    with pytest.raises(mt.MaintenanceRefused, match="required"):
        mt.execute_maintenance(ungated, "DELETE 1 FILE", apply=True)
    assert os.path.isfile(paths[0])


# ------------------------------------------------------ executor: happy paths

def test_execute_archives_allowed_targets_and_writes_a_manifest(tmp_path):
    """Ports ExecuteAsync_ArchivesAllowedTargets_AndWritesCheckpointManifest."""
    root, paths = _store(tmp_path, "session-1.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.ARCHIVE, [_copy(paths[0], "session-1")],
                 root, tmp_path))
    result = mt.execute_maintenance(preview, "ARCHIVE 1 FILE", apply=True,
                                    now_ms=1_700_000_000_000)
    assert result.executed is True
    assert len(result.moves) == 1
    assert not os.path.exists(paths[0])
    assert (tmp_path / "archive" / "session-1.jsonl").read_bytes() \
        == b"synthetic-session-1.jsonl"
    doc = _read_json(result.manifest_path)
    assert doc["action"] == "archive" and doc["status"] == "executed"
    assert len(doc["moves"]) == 1
    assert doc["llm_anthology_maintenance_version"] == mt.MAINTENANCE_CHECKPOINT_VERSION


def test_execute_delete_moves_into_the_checkpoint_quarantine(tmp_path):
    """Ports ExecuteAsync_DeleteMovesTargetsIntoCheckpointDeletedArea: a DELETE never
    unlinks — it relocates into <checkpoint_root>/deleted."""
    root, paths = _store(tmp_path, "gone.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path,
                 destination_root=str(tmp_path / "ignored")))
    result = mt.execute_maintenance(preview, "DELETE 1 FILE", apply=True)
    assert not os.path.exists(paths[0])
    assert not (tmp_path / "ignored").exists()
    quarantined = tmp_path / "checkpoints" / "deleted" / "gone.jsonl"
    assert quarantined.read_bytes() == b"synthetic-gone.jsonl"
    assert [m.destination for m in result.moves] == [str(quarantined)]


def test_execute_reconcile_and_move_land_in_their_effective_roots(tmp_path):
    root, paths = _store(tmp_path, "r.jsonl", "m.jsonl")
    rec = mt.plan_maintenance(
        _request(mt.MaintenanceAction.RECONCILE, [_copy(paths[0])], root, tmp_path,
                 destination_root=str(tmp_path / "dest")))
    mt.execute_maintenance(rec, "RECONCILE 1 FILE", apply=True)
    assert (tmp_path / "dest" / "reconciled" / "r.jsonl").exists()
    mov = mt.plan_maintenance(
        _request(mt.MaintenanceAction.MOVE, [_copy(paths[1])], root, tmp_path,
                 destination_root=str(tmp_path / "moved")))
    mt.execute_maintenance(mov, "MOVE 1 FILE", apply=True)
    assert (tmp_path / "moved" / "m.jsonl").exists()


def test_execute_with_an_empty_plan_is_a_no_op_that_still_records(tmp_path):
    root, _ = _store(tmp_path)
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [], root, tmp_path))
    result = mt.execute_maintenance(preview, "DELETE 0 FILES", apply=True)
    assert result.executed is True and result.moves == ()
    assert _read_json(result.manifest_path)["moves"] == []


def test_execute_derives_a_manifest_name_when_no_clock_is_injected(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.ARCHIVE, [_copy(paths[0])], root, tmp_path))
    result = mt.execute_maintenance(preview, "ARCHIVE 1 FILE", apply=True)
    assert os.path.basename(result.manifest_path).endswith("-archive.json")
    assert _read_json(result.manifest_path)[
        "recorded_at_ms"] > 0


def test_execute_never_overwrites_an_existing_manifest(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl", "b.jsonl")
    first = mt.execute_maintenance(
        mt.plan_maintenance(_request(mt.MaintenanceAction.ARCHIVE, [_copy(paths[0])],
                                     root, tmp_path)),
        "ARCHIVE 1 FILE", apply=True, now_ms=42)
    second = mt.execute_maintenance(
        mt.plan_maintenance(_request(mt.MaintenanceAction.ARCHIVE, [_copy(paths[1])],
                                     root, tmp_path)),
        "ARCHIVE 1 FILE", apply=True, now_ms=42)          # same injected clock
    assert first.manifest_path != second.manifest_path
    assert os.path.isfile(first.manifest_path) and os.path.isfile(second.manifest_path)


# --------------------------------------- executor: refuses to trust a bad preview

def _forge(preview, **kw):
    fields = dict(action=preview.action, allowed=preview.allowed,
                  blocked=preview.blocked, warnings=preview.warnings,
                  plan=preview.plan, requires_checkpoint=preview.requires_checkpoint,
                  requires_typed_confirmation=preview.requires_typed_confirmation,
                  required_typed_confirmation=preview.required_typed_confirmation,
                  store_root=preview.store_root,
                  destination_root=preview.destination_root,
                  checkpoint_root=preview.checkpoint_root)
    fields.update(kw)
    return mt.MaintenancePreview(**fields)


def test_execute_refuses_a_plan_entry_that_is_not_an_allowed_target(tmp_path):
    """A forged plan smuggling a BLOCKED (protected) target past the planner."""
    root, paths = _store(tmp_path, "keep.jsonl", ".codex/sessions/s.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE,
                 [_copy(paths[0]), _copy(paths[1], "protected")], root, tmp_path))
    smuggled = preview.plan + (mt.PlannedMove(
        session_id="protected", source=paths[1],
        destination=str(tmp_path / "checkpoints" / "deleted" / "s.jsonl")),)
    with pytest.raises(mt.MaintenanceRefused, match="not an allowed target"):
        mt.execute_maintenance(_forge(preview, plan=smuggled), "DELETE 1 FILE",
                               apply=True)
    assert os.path.isfile(paths[1])


def test_execute_refuses_a_plan_source_outside_the_store_root(tmp_path):
    """Even if a forged preview lists the escaping path as ALLOWED, the executor
    re-runs confinement rather than trusting the preview."""
    root, _ = _store(tmp_path)
    outside = tmp_path / "outside" / "secret.jsonl"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"not yours")
    empty = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [], root, tmp_path))
    forged = _forge(
        empty,
        allowed=(_copy(str(outside)),),
        plan=(mt.PlannedMove(session_id="s1", source=str(outside),
                             destination=str(tmp_path / "checkpoints" / "deleted"
                                             / "secret.jsonl")),),
        required_typed_confirmation="DELETE 1 FILE")
    with pytest.raises(mt.MaintenanceRefused, match="outside-store-root"):
        mt.execute_maintenance(forged, "DELETE 1 FILE", apply=True)
    assert outside.read_bytes() == b"not yours"


def test_execute_refuses_a_plan_destination_outside_its_root(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.ARCHIVE, [_copy(paths[0])], root, tmp_path))
    escaped = (mt.PlannedMove(session_id="s1", source=paths[0],
                              destination=str(tmp_path / "elsewhere" / "a.jsonl")),)
    with pytest.raises(mt.MaintenanceRefused, match="destination"):
        mt.execute_maintenance(_forge(preview, plan=escaped), "ARCHIVE 1 FILE",
                               apply=True)
    assert os.path.isfile(paths[0])


# ------------------------------- executor: the ROOTS on the preview are not trusted

def _forbid_unc_makedirs(monkeypatch):
    """Contain the UNC-root probes below.

    Those tests hand the executor a UNC root, and on Windows `os.makedirs` against
    `\\\\host\\share` initiates an outbound SMB/NTLM authentication — an egress event
    this offline-only tool must never emit, from its own suite least of all. So the call
    is turned into a loud LOCAL failure before it can reach the network, while local
    makedirs passes through untouched. This is containment, not an oracle: every
    assertion is on the refusal itself, and if the guard is ever removed these tests
    fail with "reached a UNC path" instead of quietly authenticating to a stranger.
    """
    real = os.makedirs

    def _guarded(name, *args, **kwargs):
        if str(name).replace("/", "\\").startswith("\\\\"):
            raise AssertionError("os.makedirs reached a UNC path: %r" % (name,))
        return real(name, *args, **kwargs)

    monkeypatch.setattr(mt.os, "makedirs", _guarded)


@pytest.mark.parametrize("field,label", [("checkpoint_root", "checkpoint root"),
                                         ("destination_root", "destination root")])
def test_execute_refuses_a_unc_root_carried_on_the_preview(tmp_path, monkeypatch,
                                                           field, label):
    """The planner refuses both of these roots outright. The executor re-confines every
    source and every destination but then hands the two ROOTS straight to os.makedirs,
    so it must refuse the identical root rather than trust the object it was given —
    reachable the moment an RPC layer rebuilds a preview from JSON."""
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.ARCHIVE, [_copy(paths[0])], root, tmp_path))
    _forbid_unc_makedirs(monkeypatch)
    with pytest.raises(mt.MaintenancePathError, match="UNC / non-local " + label):
        mt.execute_maintenance(_forge(preview, **{field: r"\\evil.example\share\cp"}),
                               "ARCHIVE 1 FILE", apply=True)
    assert os.path.isfile(paths[0])


def test_execute_refuses_a_unc_checkpoint_root_even_with_an_empty_plan(tmp_path,
                                                                      monkeypatch):
    """An EMPTY plan is enough: the per-move loop never runs, and the checkpoint root is
    created before the destination loop anyway, so no plan entry has to be crafted."""
    root, _ = _store(tmp_path)
    empty = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [], root, tmp_path))
    _forbid_unc_makedirs(monkeypatch)
    with pytest.raises(mt.MaintenancePathError, match="UNC"):
        mt.execute_maintenance(_forge(empty, checkpoint_root=r"\\evil.example\share\cp"),
                               "DELETE 0 FILES", apply=True)


def test_execute_refuses_a_parent_traversal_root_on_the_preview(tmp_path):
    """The other half of what `_require_root` refuses in the planner: a `..` root."""
    root, _ = _store(tmp_path)
    empty = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [], root, tmp_path))
    escaping = os.path.join(str(tmp_path), "checkpoints", "..", "escape")
    with pytest.raises(mt.MaintenancePathError, match="parent-traversal"):
        mt.execute_maintenance(_forge(empty, checkpoint_root=escaping),
                               "DELETE 0 FILES", apply=True)
    assert not (tmp_path / "escape").exists()


def test_execute_refuses_a_poisoned_root_on_a_dry_run_too(tmp_path):
    """The roots are validated BEFORE the dry-run return. `_require_root` is purely
    lexical — it makes no filesystem call — so "a dry run touches nothing" still holds,
    and the operator learns the preview is unusable at preview time, not at apply time."""
    root, _ = _store(tmp_path)
    empty = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [], root, tmp_path))
    before = _tree(str(tmp_path))
    with pytest.raises(mt.MaintenancePathError, match="UNC"):
        mt.execute_maintenance(_forge(empty, checkpoint_root=r"\\evil.example\share\cp"),
                               "DELETE 0 FILES")
    assert _tree(str(tmp_path)) == before


# --------------------------- executor: a plan may not collide with itself

def test_execute_refuses_a_plan_that_moves_one_source_twice(tmp_path):
    """The planner de-duplicates now, but the executor does not trust the preview: a
    rebuilt or forged plan can still carry two moves from one source, and the second
    raises an uncaught FileNotFoundError mid-batch — after the first has already run."""
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.ARCHIVE, [_copy(paths[0])], root, tmp_path))
    doubled = preview.plan + (mt.PlannedMove(
        session_id="s1", source=paths[0],
        destination=str(tmp_path / "archive" / "a-1.jsonl")),)
    with pytest.raises(mt.MaintenanceRefused, match="cannot be moved twice"):
        mt.execute_maintenance(_forge(preview, plan=doubled), "ARCHIVE 1 FILE",
                               apply=True)
    assert _read_bytes(paths[0]) == b"synthetic-a.jsonl"


def test_execute_refuses_a_plan_that_writes_one_destination_twice(tmp_path):
    """Silent data loss if allowed: the occupancy check runs over the whole plan BEFORE
    the first move, so a shared destination is free at check time both times — and then
    the second shutil.move overwrites the first file's bytes."""
    root, paths = _store(tmp_path, "a/x.jsonl", "b/x.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.ARCHIVE,
                 [_copy(paths[0], "a"), _copy(paths[1], "b")], root, tmp_path))
    collided = tuple(mt.PlannedMove(session_id=m.session_id, source=m.source,
                                    destination=str(tmp_path / "archive" / "x.jsonl"))
                     for m in preview.plan)
    with pytest.raises(mt.MaintenanceRefused, match="would overwrite the first"):
        mt.execute_maintenance(_forge(preview, plan=collided), "ARCHIVE 2 FILES",
                               apply=True)
    assert _read_bytes(paths[0]) == b"synthetic-a/x.jsonl"
    assert _read_bytes(paths[1]) == b"synthetic-b/x.jsonl"


def test_execute_refuses_a_stale_plan_whose_source_vanished(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.ARCHIVE, [_copy(paths[0])], root, tmp_path))
    os.remove(paths[0])
    with pytest.raises(mt.MaintenanceRefused, match="not a regular file"):
        mt.execute_maintenance(preview, "ARCHIVE 1 FILE", apply=True)


def test_execute_refuses_rather_than_overwrite_an_occupied_destination(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.ARCHIVE, [_copy(paths[0])], root, tmp_path))
    occupied = tmp_path / "archive" / "a.jsonl"
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"precious")                    # appeared AFTER planning
    with pytest.raises(mt.MaintenanceRefused, match="already exists"):
        mt.execute_maintenance(preview, "ARCHIVE 1 FILE", apply=True)
    assert occupied.read_bytes() == b"precious"
    assert os.path.isfile(paths[0])


@pytest.mark.skipif(not _SYMLINKS, reason="file symlinks unavailable on this host")
def test_execute_refuses_a_dangling_symlink_at_the_destination(tmp_path):
    """ISOLATES the lexists guard. The link's target stays INSIDE the destination root,
    so confinement passes and only the occupancy check can refuse. os.path.exists() is
    False for a dangling link, so an `exists`-based check would move THROUGH it."""
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.ARCHIVE, [_copy(paths[0])], root, tmp_path))
    archive = tmp_path / "archive"
    archive.mkdir()
    os.symlink(str(archive / "sub" / "nowhere.jsonl"), str(archive / "a.jsonl"))
    assert not os.path.exists(archive / "a.jsonl")        # dangling
    assert os.path.lexists(archive / "a.jsonl")           # ...but the link is there
    with pytest.raises(mt.MaintenanceRefused, match="already exists"):
        mt.execute_maintenance(preview, "ARCHIVE 1 FILE", apply=True)
    assert os.path.isfile(paths[0])


@pytest.mark.skipif(not _SYMLINKS, reason="file symlinks unavailable on this host")
def test_execute_refuses_a_destination_symlink_that_escapes_its_root(tmp_path):
    """A link planted at the destination whose target is OUTSIDE the destination root:
    writing through it would drop the session file anywhere on disk. Confinement is
    re-resolved for destinations too, so this refuses before any move."""
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.ARCHIVE, [_copy(paths[0])], root, tmp_path))
    archive = tmp_path / "archive"
    archive.mkdir()
    os.symlink(str(tmp_path / "outside" / "planted.jsonl"), str(archive / "a.jsonl"))
    with pytest.raises(mt.MaintenanceRefused,
                       match="plan destination .outside-store-root"):
        mt.execute_maintenance(preview, "ARCHIVE 1 FILE", apply=True)
    assert os.path.isfile(paths[0])
    assert not (tmp_path / "outside").exists()


# -------------------------------- gate 3: checkpoint BEFORE the destructive act

def test_the_checkpoint_manifest_exists_before_the_first_move(tmp_path, monkeypatch):
    """Crash shutil.move on the FIRST call and prove the manifest is already on disk,
    listing every intended move — the recoverability precondition."""
    root, paths = _store(tmp_path, "a.jsonl", "b.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE,
                 [_copy(paths[0], "a"), _copy(paths[1], "b")], root, tmp_path))

    def _boom(src, dst):
        raise OSError("simulated crash")

    monkeypatch.setattr(mt.shutil, "move", _boom)
    with pytest.raises(OSError, match="simulated crash"):
        mt.execute_maintenance(preview, "DELETE 2 FILES", apply=True, now_ms=7)
    manifests = list((tmp_path / "checkpoints").glob("*.json"))
    assert len(manifests) == 1
    doc = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert doc["status"] == "pending"
    assert [m["source"] for m in doc["moves"]] == paths
    assert os.path.isfile(paths[0]) and os.path.isfile(paths[1])   # nothing moved yet


def test_restore_recovers_a_partially_applied_run_from_a_pending_manifest(tmp_path):
    """The real crash-recovery journey: DELETE two files, crash after the first move,
    then restore — the moved file comes back byte-identical and the untouched one is
    skipped rather than clobbered."""
    root, paths = _store(tmp_path, "a.jsonl", "b.jsonl")
    original = {p: _read_bytes(p) for p in paths}
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE,
                 [_copy(paths[0], "a"), _copy(paths[1], "b")], root, tmp_path))
    real_move = mt.shutil.move
    calls = []

    def _crash_after_first(src, dst):
        calls.append(src)
        if len(calls) > 1:
            raise OSError("simulated crash")
        return real_move(src, dst)

    mt.shutil.move = _crash_after_first
    try:
        with pytest.raises(OSError):
            mt.execute_maintenance(preview, "DELETE 2 FILES", apply=True, now_ms=7)
    finally:
        mt.shutil.move = real_move
    assert not os.path.exists(paths[0]) and os.path.isfile(paths[1])

    manifest = str(next((tmp_path / "checkpoints").glob("*.json")))
    result = mt.restore_checkpoint(manifest, apply=True)
    assert result.executed is True
    assert len(result.moves) == 1                        # only the one that had moved
    assert _read_bytes(paths[0]) == original[paths[0]]
    assert _read_bytes(paths[1]) == original[paths[1]]
    assert _read_json(manifest)["status"] == "restored"


def test_restore_puts_a_deleted_file_back_byte_identical(tmp_path):
    root, paths = _store(tmp_path, "important.jsonl")
    original = _read_bytes(paths[0])
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path))
    result = mt.execute_maintenance(preview, "DELETE 1 FILE", apply=True)
    assert not os.path.exists(paths[0])

    restored = mt.restore_checkpoint(result.manifest_path, apply=True)
    assert restored.executed is True
    assert _read_bytes(paths[0]) == original
    assert not os.path.exists(restored.moves[0].source)   # quarantine copy is gone


def test_restore_defaults_to_a_dry_run(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path))
    result = mt.execute_maintenance(preview, "DELETE 1 FILE", apply=True)
    before = _tree(str(tmp_path))
    dry = mt.restore_checkpoint(result.manifest_path)     # no apply=
    assert dry.executed is False and len(dry.moves) == 1
    assert _tree(str(tmp_path)) == before


def test_restore_refuses_a_manifest_already_restored(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path))
    result = mt.execute_maintenance(preview, "DELETE 1 FILE", apply=True)
    mt.restore_checkpoint(result.manifest_path, apply=True)
    with pytest.raises(mt.MaintenanceRefused, match="already restored"):
        mt.restore_checkpoint(result.manifest_path, apply=True)


def test_restore_refuses_when_the_original_path_is_occupied(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path))
    result = mt.execute_maintenance(preview, "DELETE 1 FILE", apply=True)
    with open(paths[0], "wb") as fh:                     # a new file took the name
        fh.write(b"newer content")
    with pytest.raises(mt.MaintenanceRefused, match="occupied"):
        mt.restore_checkpoint(result.manifest_path, apply=True)
    assert _read_bytes(paths[0]) == b"newer content"


def test_restore_refuses_when_both_ends_of_a_move_are_missing(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path))
    result = mt.execute_maintenance(preview, "DELETE 1 FILE", apply=True)
    os.remove(result.moves[0].destination)               # quarantine emptied by hand
    with pytest.raises(mt.MaintenanceRefused, match="unaccounted"):
        mt.restore_checkpoint(result.manifest_path, apply=True)


def test_restore_can_skip_unaccounted_moves_and_still_recover_the_rest(tmp_path):
    """The default stays a loud refusal: an unaccounted entry means the manifest no
    longer matches the disk, and guessing is how a recovery tool loses data. But an
    all-or-nothing refusal ALSO denies recovery of every other file in the batch, which
    is its own harm — so the operator can opt in explicitly, and what was skipped is
    both returned and recorded in the manifest rather than glossed over."""
    root, paths = _store(tmp_path, "a.jsonl", "b.jsonl")
    original = _read_bytes(paths[0])
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE,
                 [_copy(paths[0], "a"), _copy(paths[1], "b")], root, tmp_path))
    result = mt.execute_maintenance(preview, "DELETE 2 FILES", apply=True)
    os.remove(result.moves[1].destination)               # b vanished from quarantine
    with pytest.raises(mt.MaintenanceRefused, match="unaccounted"):
        mt.restore_checkpoint(result.manifest_path, apply=True)
    assert not os.path.exists(paths[0])                  # ...and nothing was restored
    recovered = mt.restore_checkpoint(result.manifest_path, apply=True,
                                      skip_unaccounted=True)
    assert recovered.executed is True
    assert [m.destination for m in recovered.moves] == [paths[0]]
    assert recovered.unaccounted == (paths[1],)
    assert _read_bytes(paths[0]) == original
    doc = _read_json(result.manifest_path)
    assert doc["status"] == "restored" and doc["unaccounted"] == [paths[1]]


def test_restore_names_every_unaccounted_move_in_a_single_refusal(tmp_path):
    """Collected, not first-fail: an operator repairing this by hand needs the whole
    list, and a per-entry raise would reveal them one slow round-trip at a time."""
    root, paths = _store(tmp_path, "a.jsonl", "b.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE,
                 [_copy(paths[0], "a"), _copy(paths[1], "b")], root, tmp_path))
    result = mt.execute_maintenance(preview, "DELETE 2 FILES", apply=True)
    for move in result.moves:
        os.remove(move.destination)
    with pytest.raises(mt.MaintenanceRefused) as excinfo:
        mt.restore_checkpoint(result.manifest_path, apply=True)
    assert "unaccounted" in str(excinfo.value)
    assert repr(paths[0]) in str(excinfo.value)          # %r, as everywhere else here
    assert repr(paths[1]) in str(excinfo.value)


def test_restore_reports_no_unaccounted_moves_on_a_clean_recovery(tmp_path):
    """The empty case is asserted too, so `unaccounted` cannot quietly become junk."""
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path))
    result = mt.execute_maintenance(preview, "DELETE 1 FILE", apply=True)
    dry = mt.restore_checkpoint(result.manifest_path, skip_unaccounted=True)
    assert dry.unaccounted == ()
    restored = mt.restore_checkpoint(result.manifest_path, apply=True)
    assert restored.unaccounted == ()
    assert "unaccounted" not in _read_json(result.manifest_path)


def test_restore_refuses_a_tampered_manifest_pointing_outside_the_store_root(tmp_path):
    """Manifest tampering must not turn restore into an arbitrary-write primitive."""
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path))
    result = mt.execute_maintenance(preview, "DELETE 1 FILE", apply=True)
    doc = _read_json(result.manifest_path)
    doc["moves"][0]["source"] = str(tmp_path / "outside" / "planted.jsonl")
    with open(result.manifest_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    with pytest.raises(mt.MaintenanceRefused, match="outside-store-root"):
        mt.restore_checkpoint(result.manifest_path, apply=True)
    assert not (tmp_path / "outside").exists()


def test_restore_refuses_a_tampered_destination_outside_the_checkpoint_root(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path))
    result = mt.execute_maintenance(preview, "DELETE 1 FILE", apply=True)
    doc = _read_json(result.manifest_path)
    doc["moves"][0]["destination"] = r"\\evil\share\a.jsonl"
    with open(result.manifest_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    with pytest.raises(mt.MaintenanceRefused, match="UNC"):
        mt.restore_checkpoint(result.manifest_path, apply=True)


def test_restore_refuses_a_manifest_that_recovers_two_copies_onto_one_original(tmp_path):
    """The executor refuses a plan whose DESTINATIONS collide, and states the reason: the
    occupancy check runs over the whole plan before the first move, so a shared
    destination looks free both times and the second move overwrites the first file's
    bytes. `restore_checkpoint` has the identical shape — it builds `pending` in a
    pre-flight loop and only then moves — and carried no such guard.

    Measured before the fix: a manifest naming one original twice restored one file and
    then silently overwrote it with the other, returning executed=True. That is the one
    outcome a recovery tool must never produce, and it is worse here than in the executor
    because the bytes it destroys are the bytes it was invoked to bring back.
    """
    root, paths = _store(tmp_path, "a.jsonl", "b.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE,
                 [_copy(paths[0], "a"), _copy(paths[1], "b")], root, tmp_path))
    result = mt.execute_maintenance(preview, "DELETE 2 FILES", apply=True)
    quarantined = {m.destination: _read_bytes(m.destination) for m in result.moves}
    doc = _read_json(result.manifest_path)
    doc["moves"][1]["source"] = doc["moves"][0]["source"]      # both aim at a.jsonl
    with open(result.manifest_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    with pytest.raises(mt.MaintenanceRefused, match="appears twice"):
        mt.restore_checkpoint(result.manifest_path, apply=True)
    assert not os.path.exists(paths[0])                        # nothing was restored...
    assert {p: _read_bytes(p) for p in quarantined} == quarantined   # ...nor consumed


def test_restore_refuses_a_manifest_that_recovers_one_copy_twice(tmp_path):
    """The other half of the same pre-flight gap, and the one that fails loudest if left
    alone: two entries naming ONE checkpoint copy both pass the pre-flight (the copy is
    still there when it is checked), the first move consumes it, and the second raises an
    uncaught FileNotFoundError. Measured before the fix: exactly that, after a partial
    restore, with the manifest left reading "executed" — so the operator is handed a
    half-recovered batch and a status that denies it happened.
    """
    root, paths = _store(tmp_path, "a.jsonl", "b.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE,
                 [_copy(paths[0], "a"), _copy(paths[1], "b")], root, tmp_path))
    result = mt.execute_maintenance(preview, "DELETE 2 FILES", apply=True)
    doc = _read_json(result.manifest_path)
    doc["moves"][1]["destination"] = doc["moves"][0]["destination"]
    with open(result.manifest_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    with pytest.raises(mt.MaintenanceRefused, match="appears twice"):
        mt.restore_checkpoint(result.manifest_path, apply=True)
    assert not os.path.exists(paths[0]) and not os.path.exists(paths[1])
    assert _read_json(result.manifest_path)["status"] == "executed"   # not half-flipped


def test_restore_refuses_a_manifest_with_an_unknown_status(tmp_path):
    """Only this module writes `status`, so an unrecognised value means tampering or a
    format the code cannot reason about: fail closed rather than act on a guess."""
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path))
    result = mt.execute_maintenance(preview, "DELETE 1 FILE", apply=True)
    doc = _read_json(result.manifest_path)
    doc["status"] = "half-done"
    with open(result.manifest_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    with pytest.raises(mt.MaintenanceRefused, match="unknown status"):
        mt.restore_checkpoint(result.manifest_path, apply=True)
    assert os.path.isfile(result.moves[0].destination)    # quarantine left untouched


def test_read_checkpoint_rejects_an_unknown_manifest_version(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"llm_anthology_maintenance_version": 99}),
                    encoding="utf-8")
    with pytest.raises(mt.MaintenanceRefused, match="version"):
        mt.read_checkpoint(str(path))


def test_read_checkpoint_round_trips_the_recorded_plan(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE, [_copy(paths[0])], root, tmp_path))
    result = mt.execute_maintenance(preview, "DELETE 1 FILE", apply=True, now_ms=11)
    doc = mt.read_checkpoint(result.manifest_path)
    assert doc["recorded_at_ms"] == 11
    assert doc["required_typed_confirmation"] == "DELETE 1 FILE"
    assert doc["store_root"] == root


# ----------------------------------------------------------- the audit ledger

def test_ensure_schema_is_idempotent_and_records_runs(tmp_path):
    root, paths = _store(tmp_path, "a.jsonl", "b.jsonl")
    conn = sqlite3.connect(":memory:")
    mt.ensure_schema(conn)
    mt.ensure_schema(conn)                                # idempotent
    first = mt.execute_maintenance(
        mt.plan_maintenance(_request(mt.MaintenanceAction.ARCHIVE, [_copy(paths[0])],
                                     root, tmp_path)),
        "ARCHIVE 1 FILE", apply=True, now_ms=100)
    second = mt.execute_maintenance(
        mt.plan_maintenance(_request(mt.MaintenanceAction.DELETE, [_copy(paths[1])],
                                     root, tmp_path)),
        "DELETE 1 FILE", apply=True, now_ms=200)
    mt.record_run(conn, first.manifest_path)
    mt.record_run(conn, second.manifest_path)
    rows = mt.list_runs(conn)
    assert [r["action"] for r in rows] == ["delete", "archive"]     # newest first
    assert rows[0]["status"] == "executed" and rows[0]["moved_count"] == 1
    assert rows[0]["store_root"] == root
    mt.restore_checkpoint(second.manifest_path, apply=True)
    mt.record_run(conn, second.manifest_path)                       # upsert, not dup
    rows = mt.list_runs(conn)
    assert len(rows) == 2 and rows[0]["status"] == "restored"
    conn.close()


def test_list_runs_honours_its_limit(tmp_path):
    conn = sqlite3.connect(":memory:")
    mt.ensure_schema(conn)
    root, paths = _store(tmp_path, "a.jsonl", "b.jsonl")
    for i, path in enumerate(paths):
        result = mt.execute_maintenance(
            mt.plan_maintenance(_request(mt.MaintenanceAction.ARCHIVE, [_copy(path)],
                                         root, tmp_path)),
            "ARCHIVE 1 FILE", apply=True, now_ms=1000 + i)
        mt.record_run(conn, result.manifest_path)
    assert len(mt.list_runs(conn, limit=1)) == 1
    conn.close()


def test_blocked_targets_are_recorded_in_the_manifest_for_audit(tmp_path):
    root, paths = _store(tmp_path, "keep.jsonl", ".codex/sessions/s.jsonl")
    preview = mt.plan_maintenance(
        _request(mt.MaintenanceAction.DELETE,
                 [_copy(paths[0]), _copy(paths[1], "protected")], root, tmp_path))
    result = mt.execute_maintenance(preview, "DELETE 1 FILE", apply=True)
    doc = mt.read_checkpoint(result.manifest_path)
    assert doc["blocked"] == [{"file_path": paths[1], "reason": "protected"}]


# ------------------------------------------------------------- model niceties

def test_warning_and_severity_serialize_for_the_rpc_layer(tmp_path):
    warning = mt.MaintenanceWarning(mt.MaintenanceWarningSeverity.REVIEW, "look here")
    assert warning.to_dict() == {"severity": "review", "message": "look here"}
    assert mt.MaintenanceWarningSeverity.DANGEROUS > mt.MaintenanceWarningSeverity.INFO
    assert mt.MaintenanceAction.RECONCILE.value == "reconcile"
    assert mt.SessionStoreKind.MIRROR.value == "mirror"
    assert mt.SessionStoreKind.OTHER.value == "other"
    assert mt.SessionStoreKind.UNKNOWN.value == "unknown"
