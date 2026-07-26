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
  * the executor never trusts a hand-built preview: it re-asserts confinement, that
    every plan source is an ALLOWED target, that the source exists, and that the
    destination does not (lexists, so a dangling symlink cannot be written through).

Path semantics are asserted with pure PureWindowsPath rules where possible so the
suite behaves identically on Linux/macOS/Windows CI; the real moves use the
host-native tmp_path, so the mutating path is exercised on every platform.
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
