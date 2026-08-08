"""Gated, recoverable session-store maintenance: archive / move / reconcile / delete.

This is the ONLY module in LLM Anthology that moves or removes the owner's real files,
so it is built as a safety mechanism first and a feature second. Ported from the
retired codex-session-manager (C#/.NET WPF) — specifically
`Core/Maintenance/{MaintenanceRequest,MaintenanceAction,MaintenancePreview,
MaintenanceWarning,MaintenanceWarningSeverity}` and
`Storage/Maintenance/{MaintenancePlanner,MaintenanceExecutor,
MaintenanceExecutionResult}`. Zero code is shared between the stacks; the C# tests
(MaintenancePlannerTests / MaintenanceExecutorTests) were the behavioural spec.

THE SAFETY PROPERTY IS THE PLANNER/EXECUTOR SPLIT, not an implementation detail:

  plan_maintenance(request) -> MaintenancePreview
        PURE. Reads (stat / realpath) but MUTATES NOTHING — it creates no directory,
        writes no file, moves nothing. It answers "what would happen": which targets
        are ALLOWED, which are BLOCKED and why, which warnings at which severity, the
        exact source -> destination pair for every move, and the exact phrase the
        operator must type. Because the plan is fully materialised up front, the
        operator confirms the real thing rather than an intention. A target naming a
        file an earlier target already claimed is BLOCKED as `duplicate-target`, so one
        physical file is moved exactly once and the confirmation count is truthful.

  execute_maintenance(preview, confirmation, *, apply=False) -> MaintenanceResult
        Runs ONLY a plan. Three independent gates, in this order:
          1. TYPED CONFIRMATION — refuses unless `confirmation` equals
             `preview.required_typed_confirmation` exactly (case- and
             whitespace-sensitive); a blank confirmation, or a preview with
             `requires_typed_confirmation` switched off, also refuses (ported from
             MaintenanceExecutor.ExecuteAsync).
          2. DRY-RUN DEFAULT — `apply` defaults to False. A dry run touches the
             filesystem not at all: no checkpoint, no directory, no move.
          3. CHECKPOINT BEFORE THE ACT — the manifest naming every intended move is
             written and flushed BEFORE the first move, so a crash mid-batch leaves a
             complete recovery record (the C# wrote its manifest AFTER the moves; that
             is the one place this port deliberately diverges, in the safe direction).
        It also refuses to trust the preview it is handed. NOTHING on the preview is
        taken on faith, the two ROOTS included: both are re-validated with the planner's
        own `_require_root` before anything is created, because `os.makedirs` on a UNC
        root is an outbound SMB/NTLM authentication and a preview rebuilt from JSON by
        an RPC layer must not be able to name one. Then every plan source must be an
        ALLOWED target, confinement is re-asserted on both ends, no source and no
        destination may appear twice (the pre-flight runs over the whole plan BEFORE the
        first move, so a shared destination looks free both times and the second
        `shutil.move` would overwrite the first file's bytes), the source must be a
        regular file, and the destination must not already exist (`lexists`, so a
        dangling symlink cannot be written through).

  restore_checkpoint(manifest_path, *, apply=False) -> MaintenanceResult
        The recovery half. Moves every relocated file back to its recorded original
        path, and is itself gated: dry-run by default, and it fails loud rather than
        overwrite a file that has since taken the original name or guess at a move it
        cannot account for. It also refuses a manifest whose entries collide — two
        recorded moves sharing an original, or sharing a checkpoint copy — for exactly
        the reason the executor refuses that shape: this loop too decides the whole
        batch before the first move, so a collision looks free at check time and is
        discovered only by destroying something.
        CONFINEMENT HERE IS SELF-REFERENTIAL AND IS NOT AN ANTI-TAMPER GUARANTEE: each
        recorded path is re-validated against the roots recorded in the SAME manifest,
        so editing a path alone is caught, but editing a root along with it is not —
        whoever can write the manifest can aim a restore anywhere they could already
        write. The manifest lives in a directory the owner controls, so under this
        single-user local threat model that is accepted; a caller that obtains a
        manifest from anywhere less trusted must pin the roots out-of-band first.

A DELETE never unlinks. It relocates into `<checkpoint_root>/deleted`, so "delete" is
a quarantine that restore_checkpoint can undo (ported verbatim from
MaintenanceExecutor.GetEffectiveDestinationRoot).

PATH CONFINEMENT — two layers, because either alone is insufficient:
  * LEXICAL, via PureWindowsPath so the same rule runs identically on Linux/macOS/
    Windows: reject UNC / protocol-relative paths (`\\\\host\\share`, `//host/share`),
    NUL bytes, `..` components, an NTFS alternate-data-stream basename (`name:stream`),
    and anything not lexically inside the store root. This matches the guard in
    llm_anthology.export (`_norm_local` / `_confined_target`) — a crafted UNC path
    coerces an outbound SMB/NTLM authentication, which is an egress event this
    offline-only tool must never permit.
  * PHYSICAL, via os.path.realpath on BOTH sides: a symlink or junction sitting inside
    the store whose target is outside it is lexically innocent, so only resolution
    catches it. realpath also expands Windows 8.3 short names (`PREKZU~1`), so a root
    given in short form and a target in long form compare correctly instead of
    spuriously failing containment.
  A target that fails either layer is BLOCKED with a reason and a Dangerous warning —
  never silently skipped, never partially processed.

PRIVACY: local-only, no network, no telemetry. The checkpoint manifest records file
PATHS (restore needs them) and never conversation content. Tests use synthetic
fixtures exclusively and never read $CODEX_HOME, ~/.codex, or AppData.
"""
import enum
import json
import os
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import PureWindowsPath
from typing import Optional, Tuple

MAINTENANCE_CHECKPOINT_VERSION = 1

__all__ = [
    "MAINTENANCE_CHECKPOINT_VERSION",
    "MAINTENANCE_SCHEMA",
    "MaintenanceAction",
    "MaintenancePreview",
    "MaintenanceRefused",
    "MaintenancePathError",
    "MaintenanceRequest",
    "MaintenanceResult",
    "MaintenanceWarning",
    "MaintenanceWarningSeverity",
    "BlockedTarget",
    "PlannedMove",
    "SessionCopy",
    "SessionStoreKind",
    "ensure_schema",
    "execute_maintenance",
    "list_runs",
    "plan_maintenance",
    "read_checkpoint",
    "record_run",
    "restore_checkpoint",
]


# --------------------------------------------------------------------- errors

class MaintenanceRefused(Exception):
    """A gate refused: bad/blank confirmation, a stale or forged plan, an unsafe root,
    or a recovery that would destroy data. Every refusal is an exception rather than a
    falsy return, because a caller cannot accidentally ignore an exception."""


class MaintenancePathError(MaintenanceRefused, ValueError):
    """A path is unusable as a maintenance root: UNC / non-local, empty, `..`-bearing,
    or NUL-bearing. Mirrors llm_anthology.export.ExportPathError."""


# ----------------------------------------------------------------- enumerations

class MaintenanceAction(str, enum.Enum):
    """Ported from Core/Maintenance/MaintenanceAction.cs (Delete/Archive/Move/
    Reconcile). Values are lowercase strings so a manifest is human-readable."""
    DELETE = "delete"
    ARCHIVE = "archive"
    MOVE = "move"
    RECONCILE = "reconcile"


class MaintenanceWarningSeverity(enum.IntEnum):
    """Ported from MaintenanceWarningSeverity.cs. IntEnum keeps the C# ordering
    (Info < Review < Dangerous) so a UI can sort or threshold on it."""
    INFO = 0
    REVIEW = 1
    DANGEROUS = 2


class SessionStoreKind(str, enum.Enum):
    """Ported from Core/Sessions/SessionStoreKind.cs. LIVE is special: the live store
    is never a maintenance target."""
    UNKNOWN = "unknown"
    LIVE = "live"
    BACKUP = "backup"
    MIRROR = "mirror"
    OTHER = "other"


# ----------------------------------------------------------------- value models

@dataclass(frozen=True)
class MaintenanceWarning:
    """Ported from MaintenanceWarning.cs."""
    severity: MaintenanceWarningSeverity
    message: str

    def to_dict(self):
        """JSON-ready form for the RPC layer the orchestrator wires up."""
        return {"severity": self.severity.name.lower(), "message": self.message}


@dataclass(frozen=True)
class SessionCopy:
    """Ported from Core/Sessions/SessionPhysicalCopy.cs (its SessionPhysicalCopyState
    fields are flattened here, as the C# record itself does).

    WHICH FIELDS ARE TRUSTED, ON A COPY THAT CAME FROM A REQUEST:

    * `file_path` is the caller's, and is the ONLY field any safety decision reads — it goes
      through both confinement layers and the protected-marker test before anything happens.
    * `size_bytes` is MEASURED, not accepted: `plan_maintenance` overwrites it from disk for
      every ALLOWED target (see `_measured_size`). Do NOT reintroduce a caller-supplied value
      here — a UI puts this number on the confirm screen, and it was the one figure there a
      client could dictate.
    * `session_id` / `store_kind` are the caller's own labelling, carried through for display
      and audit. `store_kind` can only ever make a target MORE restricted (a `LIVE` copy is
      refused outright), never less, so accepting it is safe in the one direction it acts.
    * `last_write_ms` / `is_hot` are, over the RPC edge, ALWAYS `None` / `False` — that layer
      never reads them from the request and nothing computes them, so they are dataclass
      defaults rather than derived facts. An in-process caller may set them; a UI must not
      present them as measured. `is_hot` in particular gates a REVIEW warning that therefore
      cannot fire on a request that arrived over the wire.
    """
    session_id: str
    file_path: str
    store_kind: SessionStoreKind = SessionStoreKind.UNKNOWN
    last_write_ms: Optional[int] = None
    size_bytes: int = 0
    is_hot: bool = False


@dataclass(frozen=True)
class BlockedTarget:
    """A target the planner refuses to touch, with the machine-readable `reason`
    (protected / unsafe-path / traversal / outside-store-root / duplicate-target) and a
    human `detail`.
    The C# BlockedTargets carried only the copy; carrying the reason is a deliberate
    addition so the UI and the RPC layer can explain a refusal without re-deriving it."""
    target: SessionCopy
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class PlannedMove:
    """One materialised relocation. Both ends are absolute and already collision-free;
    the executor performs exactly these and computes nothing itself."""
    session_id: str
    source: str
    destination: str


@dataclass(frozen=True)
class MaintenanceRequest:
    """Ported from MaintenanceRequest.cs (Action / Targets / TypedConfirmation) plus the
    three ROOTS the C# scattered across the executor's arguments. Collecting them here
    is what makes `plan_maintenance` a pure function of ONE object — and therefore what
    makes the preview a complete, auditable description of the act.

    `typed_confirmation` is the phrase the caller OFFERS. It never becomes the required
    phrase (see plan_maintenance); a stale offer only earns a Review warning.
    """
    action: MaintenanceAction
    targets: Tuple[SessionCopy, ...]
    store_root: str
    checkpoint_root: str
    destination_root: str = ""
    typed_confirmation: str = ""


@dataclass(frozen=True)
class MaintenancePreview:
    """Ported from MaintenancePreview.cs, plus `plan` (the materialised moves) and the
    resolved roots. `destination_root` is the EFFECTIVE root after the per-action
    rewrite, i.e. what files will actually land in."""
    action: MaintenanceAction
    allowed: Tuple[SessionCopy, ...] = ()
    blocked: Tuple[BlockedTarget, ...] = ()
    warnings: Tuple[MaintenanceWarning, ...] = ()
    plan: Tuple[PlannedMove, ...] = ()
    requires_checkpoint: bool = True
    requires_typed_confirmation: bool = True
    required_typed_confirmation: str = ""
    store_root: str = ""
    destination_root: str = ""
    checkpoint_root: str = ""


@dataclass(frozen=True)
class MaintenanceResult:
    """Ported from MaintenanceExecutionResult.cs.

    `moves` is the EFFECTIVE move set — what happened, or on a dry run what would
    happen. `executed` is the single signal that the filesystem was touched, and
    `manifest_path` is empty on a dry run precisely because no checkpoint was written.
    Keeping `moves` populated on a dry run is what makes restore_checkpoint previewable
    at all (it has no separate preview object).

    `unaccounted` names the recorded originals a restore could not account for (neither
    end of the recorded move exists on disk). It is empty unless the caller passed
    `skip_unaccounted=True`, because otherwise restore refuses the batch outright.
    """
    executed: bool
    moves: Tuple[PlannedMove, ...] = ()
    manifest_path: str = ""
    unaccounted: Tuple[str, ...] = ()


# ------------------------------------------------------------------ path safety

# Ported from MaintenancePlanner.ProtectedPathMarkers. These name the owner's LIVE
# Codex store; a maintenance op must never touch it even if the caller mislabelled the
# copy's store kind. Compared against a `\`-normalised, case-folded REAL path as well as
# the literal one, and against write DESTINATIONS as well as targets — see
# `_spells_protected`, which explains why either half alone leaves the store reachable.
PROTECTED_PATH_MARKERS = (
    "\\.codex\\sessions\\",
    "\\.codex\\state_5.sqlite",
    "\\.codex\\codex-sqlite\\",
)


def _norm_local(raw, label):
    """os.PathLike | str -> PureWindowsPath, rejecting the path shapes that are unsafe
    before any filesystem call happens.

    UNC / protocol-relative paths are rejected FIRST and without touching the
    filesystem: merely resolving `\\\\host\\share` on Windows initiates an outbound SMB
    authentication (the NTLM-hash-leak class), which an offline-only tool must never
    do. PureWindowsPath models Windows semantics on every host, so this rule is
    identical on Linux/macOS/Windows.
    """
    text = os.fspath(raw)
    if not text.strip():
        raise MaintenancePathError("refusing empty %s" % label)
    if "\x00" in text:
        raise MaintenancePathError("refusing NUL byte in %s: %r" % (label, text))
    if text.replace("/", "\\").startswith("\\\\"):     # \\host\share, //host/share
        raise MaintenancePathError("refusing UNC / non-local %s: %r" % (label, text))
    return PureWindowsPath(text)


def _require_root(raw, label):
    """Validate a maintenance ROOT and return it verbatim. A bad root is a bad REQUEST,
    not a blockable target, so this raises instead of degrading to a warning: planning
    against an unsafe root could otherwise confine targets to a root that is itself an
    escape."""
    pure = _norm_local(raw, label)
    if ".." in pure.parts:
        raise MaintenancePathError("refusing parent-traversal in %s: %r"
                                  % (label, os.fspath(raw)))
    return os.fspath(raw)


def _within(child, parent):
    """True iff the already-resolved `child` is `parent` or lives beneath it.
    normcase makes the comparison case-insensitive on Windows and exact on POSIX;
    the separator suffix stops `.../store-evil` from passing as `.../store`."""
    c = os.path.normcase(child)
    p = os.path.normcase(parent)
    if c == p:
        return True
    return c.startswith(p.rstrip(os.sep) + os.sep)


def _classify(file_path, store_root):
    """None if `file_path` is safe to operate on, else (reason, detail).

    Layer 1 is lexical (no filesystem access, platform-independent); layer 2 resolves
    both sides with realpath so a symlink/junction inside the store that points OUTSIDE
    it is caught — the case a lexical guard structurally cannot see. realpath also
    expands 8.3 short names, so a short-form root and a long-form target still compare
    correctly.
    """
    try:
        candidate = _norm_local(file_path, "target")
    except MaintenancePathError as exc:
        return ("unsafe-path", str(exc))
    if ".." in candidate.parts:
        return ("traversal", "refusing parent-traversal in target: %r" % file_path)
    if ":" in candidate.name:
        return ("unsafe-path",
                "refusing NTFS alternate-data-stream basename: %r" % candidate.name)
    if not candidate.is_relative_to(PureWindowsPath(store_root)):
        return ("outside-store-root",
                "target %r is not within the store root %r" % (file_path, store_root))
    real_target = os.path.realpath(file_path)
    real_root = os.path.realpath(store_root)
    if not _within(real_target, real_root):
        return ("outside-store-root",
                "target %r resolved to %r, outside the store root %r"
                % (file_path, real_target, real_root))
    return None


def _require_confined(path, root, label):
    """Re-assert confinement at execution time and raise on failure. The executor and
    restore both use this: neither trusts the object it was handed."""
    verdict = _classify(path, root)
    if verdict is not None:
        raise MaintenanceRefused("refusing %s (%s): %s" % (label, verdict[0],
                                                           verdict[1]))


def _spells_protected(path):
    """True if `path` names a protected store location under ANY spelling of it.

    The literal string AND its realpath are both tested, because neither alone is
    sufficient. The literal catches a path that merely SPELLS a marker even when nothing
    is on disk. The realpath is what closes the hole: the marker test is a substring
    match, so `\\.codex\\\\sessions\\` (a doubled separator), `\\.codex\\.\\sessions\\` (a
    `.` component), an 8.3 short name, and a directory junction pointing into the live
    store each name the protected file while matching no marker — and every one of them
    satisfies `_classify`, whose own realpath resolves them straight back inside the
    store. The markers describe a PHYSICAL location, so they have to be compared against
    one; comparing only the caller's string made the guard a spelling contest.

    A trailing separator is appended before comparing, so a marker also matches the
    DIRECTORY it names rather than only files beneath it. `...\\sessionsfoo` still does
    not match `\\sessions\\`, because the separator has to land immediately after.
    """
    for text in (path, os.path.realpath(path)):
        normalized = text.replace("/", "\\").lower().rstrip("\\") + "\\"
        if any(marker.lower() in normalized for marker in PROTECTED_PATH_MARKERS):
            return True
    return False


def _is_protected(copy):
    """Ported from MaintenancePlanner.IsProtected: the LIVE store is untouchable, and so
    is any path naming one of the protected markers. The store-kind half cannot fire on
    an RPC request — that layer has no way to know a copy is LIVE and labels every target
    UNKNOWN — so the marker half is the live-store defence in practice, and it resolves
    before it compares."""
    if copy.store_kind is SessionStoreKind.LIVE:
        return True
    return _spells_protected(copy.file_path)


def _measured_size(file_path):
    """The size of `file_path` ON DISK, or 0 when it cannot be measured.

    The caller's `size_bytes` is not trusted. It was the ONE number on the preview a client
    could dictate — the RPC edge forwards it verbatim while never even reading
    `last_write_ms` or `is_hot` from the request, so those two arrive as their dataclass
    defaults — and it is not decorative: the cockpit's maintenance panel sums it and prints
    the total on the confirm screen beside the file count. An inflated claim therefore became
    the largest and most reassuring figure on the dialog that authorises a delete, while
    every other number beside it was derived here from what the planner actually found.

    The cost is one `stat` on a path this planner has ALREADY resolved with realpath, so
    "re-statting is wasted IO" does not hold: the file has been touched either way.

    ONLY EVER CALLED FOR AN ALLOWED TARGET. A refused target is left unmeasured on purpose —
    it was declined because it is protected or escapes the store, and stat-ing a path the
    confinement layer just rejected would be a small instance of the access that guard
    exists to prevent. Nothing reads a blocked target's size.

    0 MEANS UNMEASURABLE, NOT EMPTY: a name that passed both confinement layers but is not a
    readable regular file (never created, a directory, or gone since planning). Reporting 0
    is safe because such a target cannot execute anyway — the executor refuses a source that
    is not a regular file — so an honest 0 is followed by a hard refusal rather than by a
    silent move.
    """
    try:
        return os.path.getsize(file_path)
    except OSError:
        return 0


# ------------------------------------------------------------------- the planner

def _effective_destination_root(action, destination_root, checkpoint_root):
    """Ported from MaintenanceExecutor.GetEffectiveDestinationRoot. DELETE ignores the
    requested destination entirely and quarantines under the checkpoint root, which is
    what makes a delete recoverable."""
    if action is MaintenanceAction.DELETE:
        return os.path.join(checkpoint_root, "deleted")
    if action is MaintenanceAction.RECONCILE:
        return os.path.join(destination_root, "reconciled")
    return destination_root                              # ARCHIVE / MOVE, as in C#


def _confirmation_phrase(action, count):
    """The phrase the operator must type, in the C# tests' shape ("DELETE 2 FILES").
    Derived from the ALLOWED count, never from the caller's offer: the phrase has to be
    a function of what will ACTUALLY happen, so a plan that changed since the operator
    last looked changes the phrase too."""
    return "%s %d %s" % (action.value.upper(), count,
                         "FILE" if count == 1 else "FILES")


def _unique_name(directory, name, claimed):
    """A destination basename free both on disk and within this batch, with a
    deterministic `-N` suffix. The C# used a fresh Guid; a deterministic suffix means
    the destination can be shown in the PREVIEW and later verified, which a Guid
    minted inside the executor cannot be. lexists (not exists) so a dangling symlink
    still counts as occupied."""
    stem, ext = os.path.splitext(name)
    candidate = name
    attempt = 0
    while (os.path.normcase(candidate) in claimed
           or os.path.lexists(os.path.join(directory, candidate))):
        attempt += 1
        candidate = "%s-%d%s" % (stem, attempt, ext)
    claimed.add(os.path.normcase(candidate))
    return candidate


def plan_maintenance(request):
    """PURE: a request -> the preview of what would happen. Mutates nothing.

    Every target is classified exactly once, in this order: path safety first (so an
    unsafe path is never resolved or stat-ed further than realpath), then the C#
    protected-path/live-store rule. Blocked targets carry a reason and a Dangerous
    warning; allowed targets get a materialised PlannedMove with a collision-free
    destination.
    """
    store_root = _require_root(request.store_root, "store root")
    checkpoint_root = _require_root(request.checkpoint_root, "checkpoint root")
    if request.action is MaintenanceAction.DELETE:
        destination_root = ""                            # ignored by DELETE, as in C#
    else:
        if not request.destination_root.strip():
            raise MaintenanceRefused(
                "destination_root is required for the %s action" % request.action.value)
        destination_root = _require_root(request.destination_root, "destination root")
    effective_root = _effective_destination_root(request.action, destination_root,
                                                 checkpoint_root)
    # The marker rule guarded TARGETS only, which left the other direction open: nothing
    # stopped a run writing INTO the live store, and an ARCHIVE whose destination_root is
    # `<x>\.codex\sessions` put session files there. That is corruption rather than loss —
    # Codex then reads files it never wrote — and no checkpoint can undo it, because for a
    # DELETE the checkpoint root IS the misplaced destination. Refused once, here, for
    # whichever of the two roots produced the effective root.
    if _spells_protected(effective_root):
        raise MaintenanceRefused(
            "refusing to write into a protected store path: %r" % effective_root)

    allowed = []
    blocked = []
    warnings = []
    plan = []
    claimed = set()
    planned_sources = set()
    for target in request.targets:
        verdict = _classify(target.file_path, store_root)
        if verdict is None and _is_protected(target):
            verdict = ("protected", "protected store path: %r" % target.file_path)
        if verdict is None and os.path.normcase(target.file_path) in planned_sources:
            # Two targets CAN name one physical file — the DEDUP unit exists precisely
            # because one logical session has several physical copies — and both pass
            # the executor's is-a-regular-file pre-flight, because at check time the
            # file is still there. Planning both would hold two moves from one source:
            # the operator would be asked to confirm a count larger than the number of
            # files, and the second move would fail after the first had already run.
            # Checked last, so a duplicate that is ALSO protected is reported as
            # protected — the more dangerous reason is the one worth recording.
            verdict = ("duplicate-target",
                       "target %r is already planned by an earlier entry; one physical "
                       "file is moved once" % target.file_path)
        if verdict is not None:
            blocked.append(BlockedTarget(target=target, reason=verdict[0],
                                         detail=verdict[1]))
            if verdict[0] == "duplicate-target":
                warnings.append(MaintenanceWarning(
                    MaintenanceWarningSeverity.REVIEW,
                    "Duplicate target ignored: %s" % target.file_path))
            else:
                warnings.append(MaintenanceWarning(
                    MaintenanceWarningSeverity.DANGEROUS,
                    "Protected path blocked: %s (%s)" % (target.file_path, verdict[0])))
            continue
        planned_sources.add(os.path.normcase(target.file_path))
        # The size is MEASURED here, not taken from the request — see `_measured_size` for
        # why that one field mattered. Everything else on the copy is the caller's own
        # identifying data and is carried through untouched.
        allowed.append(replace(target, size_bytes=_measured_size(target.file_path)))
        warnings.append(MaintenanceWarning(
            MaintenanceWarningSeverity.DANGEROUS,
            "Dangerous maintenance target: %s" % target.file_path))
        if target.is_hot:
            warnings.append(MaintenanceWarning(
                MaintenanceWarningSeverity.REVIEW,
                "Target is hot (being written): %s" % target.file_path))
        name = _unique_name(effective_root, os.path.basename(target.file_path), claimed)
        plan.append(PlannedMove(session_id=target.session_id,
                                source=target.file_path,
                                destination=os.path.join(effective_root, name)))

    required = _confirmation_phrase(request.action, len(allowed))
    offered = request.typed_confirmation.strip()
    if offered and offered != required:
        warnings.append(MaintenanceWarning(
            MaintenanceWarningSeverity.REVIEW,
            "The offered confirmation %r is stale; %r is required" % (offered, required)))
    warnings.append(MaintenanceWarning(
        MaintenanceWarningSeverity.INFO,
        "%s preview: %d allowed, %d blocked; a checkpoint and a typed confirmation are "
        "required" % (request.action.value, len(allowed), len(blocked))))

    return MaintenancePreview(
        action=request.action,
        allowed=tuple(allowed),
        blocked=tuple(blocked),
        warnings=tuple(warnings),
        plan=tuple(plan),
        requires_checkpoint=True,                        # always, as in C#
        requires_typed_confirmation=True,                # always, as in C#
        required_typed_confirmation=required,
        store_root=store_root,
        destination_root=effective_root,
        checkpoint_root=checkpoint_root,
    )


# ------------------------------------------------------------------ checkpoints

def _manifest_doc(preview, recorded_at_ms, moves, status):
    return {
        "llm_anthology_maintenance_version": MAINTENANCE_CHECKPOINT_VERSION,
        "action": preview.action.value,
        "status": status,
        "recorded_at_ms": recorded_at_ms,
        "store_root": preview.store_root,
        "destination_root": preview.destination_root,
        "checkpoint_root": preview.checkpoint_root,
        "required_typed_confirmation": preview.required_typed_confirmation,
        "moves": [{"session_id": m.session_id, "source": m.source,
                   "destination": m.destination} for m in moves],
        "blocked": [{"file_path": b.target.file_path, "reason": b.reason}
                    for b in preview.blocked],
    }


def _write_json(path, doc):
    """Write deterministically (sorted keys, UTF-8 bytes, no newline translation) and
    flush all the way to the device: a checkpoint that is still in a buffer when the
    process dies is not a checkpoint."""
    payload = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2)
    with open(path, "wb") as handle:
        handle.write(payload.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def read_checkpoint(manifest_path):
    """Parse a checkpoint manifest, refusing an unknown format version rather than
    guessing at a layout whose meaning could have changed."""
    with open(manifest_path, "rb") as handle:
        doc = json.loads(handle.read().decode("utf-8"))
    found = doc.get("llm_anthology_maintenance_version")
    if found != MAINTENANCE_CHECKPOINT_VERSION:
        raise MaintenanceRefused(
            "unsupported checkpoint version %r (expected %d) in %r"
            % (found, MAINTENANCE_CHECKPOINT_VERSION, manifest_path))
    return doc


# ------------------------------------------------------------------- the executor

def _require_confirmation(preview, confirmation):
    """Ported from MaintenanceExecutor.ExecuteAsync's two guards. Note the first: a
    preview whose `requires_typed_confirmation` is False can never execute, so the flag
    cannot be flipped to bypass the gate — it only ever fails closed."""
    if not preview.requires_typed_confirmation or not confirmation.strip():
        raise MaintenanceRefused("Typed confirmation is required.")
    if preview.required_typed_confirmation != confirmation:
        raise MaintenanceRefused("Typed confirmation does not match the preview.")


def execute_maintenance(preview, confirmation, *, apply=False, now_ms=None):
    """Run a plan the planner produced — gated, checkpointed, and never overwriting.

    `apply` defaults to False: the dry-run posture is the default, and a dry run makes
    no filesystem call at all (it does not even create the checkpoint directory), so
    "nothing destructive happens without an explicit act" is structural.
    """
    _require_confirmation(preview, confirmation)
    # The ROOTS are part of the untrusted preview, so they get the planner's own rule
    # before anything is created: the planner refuses a UNC or `..` root, and so must
    # this, because os.makedirs on `\\host\share` initiates an outbound SMB/NTLM
    # authentication — the egress class this offline-only tool must never permit — and
    # an empty plan is enough to reach it. Validating here rather than after the dry-run
    # return costs nothing: _require_root is purely lexical, makes no filesystem call,
    # and so leaves "a dry run touches the filesystem not at all" intact while surfacing
    # a poisoned preview at preview time instead of at apply time.
    checkpoint_root = _require_root(preview.checkpoint_root, "checkpoint root")
    destination_root = _require_root(preview.destination_root, "destination root")
    if not apply:
        return MaintenanceResult(executed=False, moves=preview.plan, manifest_path="")

    # The executor does not trust the preview object: a forged plan must not smuggle a
    # blocked target past the planner, so every source has to be an ALLOWED target, and
    # the plan may not collide with itself. Both collision checks matter because the
    # occupancy check below runs over the WHOLE plan before the first move: a repeated
    # source would vanish after its first move (an uncaught FileNotFoundError mid-batch),
    # and a repeated destination would look free both times and then be overwritten.
    allowed_paths = {os.path.normcase(t.file_path) for t in preview.allowed}
    seen_sources = set()
    seen_destinations = set()
    for move in preview.plan:
        source_key = os.path.normcase(move.source)
        destination_key = os.path.normcase(move.destination)
        if source_key not in allowed_paths:
            raise MaintenanceRefused(
                "plan source %r is not an allowed target in this preview" % move.source)
        if source_key in seen_sources:
            raise MaintenanceRefused("plan source %r appears twice; one file cannot be "
                                     "moved twice" % move.source)
        if destination_key in seen_destinations:
            raise MaintenanceRefused("plan destination %r appears twice; the second move "
                                     "would overwrite the first" % move.destination)
        seen_sources.add(source_key)
        seen_destinations.add(destination_key)
        _require_confined(move.source, preview.store_root, "plan source")
        if not os.path.isfile(move.source):
            raise MaintenanceRefused("plan source %r is not a regular file (the plan is "
                                     "stale or the target is a directory)" % move.source)

    os.makedirs(checkpoint_root, exist_ok=True)
    os.makedirs(destination_root, exist_ok=True)
    for move in preview.plan:
        _require_confined(move.destination, destination_root, "plan destination")
        if os.path.lexists(move.destination):
            raise MaintenanceRefused("plan destination %r already exists; refusing to "
                                     "overwrite" % move.destination)

    recorded_at_ms = int(time.time() * 1000) if now_ms is None else now_ms
    manifest_path = os.path.join(checkpoint_root, _unique_name(
        checkpoint_root,
        "%d-%s.json" % (recorded_at_ms, preview.action.value), set()))
    # THE CHECKPOINT, BEFORE THE FIRST MOVE. Status "pending" means "these moves were
    # intended"; restore_checkpoint can undo whichever of them actually landed.
    _write_json(manifest_path, _manifest_doc(preview, recorded_at_ms, preview.plan,
                                            "pending"))
    for move in preview.plan:
        os.makedirs(os.path.dirname(move.destination), exist_ok=True)
        shutil.move(move.source, move.destination)
    _write_json(manifest_path, _manifest_doc(preview, recorded_at_ms, preview.plan,
                                            "executed"))
    return MaintenanceResult(executed=True, moves=preview.plan,
                             manifest_path=manifest_path)


# -------------------------------------------------------------------- recovery

def restore_checkpoint(manifest_path, *, apply=False, now_ms=None,
                       skip_unaccounted=False):
    """Undo a checkpointed run: move every relocated file back to its original path.

    Works on a "pending" manifest too, which is what makes a crash mid-batch
    recoverable: each move is inspected independently and only the ones that actually
    landed are undone. Fails loud rather than lose data — if a file has since taken the
    original name it refuses outright, and if BOTH ends of a recorded move are missing
    the manifest no longer describes the disk, so by default the whole batch is refused
    rather than guessed at. Every unaccounted entry is named in ONE refusal, because an
    operator repairing this by hand needs the whole list.

    `skip_unaccounted=True` restores the accounted moves anyway and reports the rest.
    The default stays fail-closed, but an all-or-nothing refusal is itself a hazard: one
    missing file would otherwise deny automated recovery to every other file in the
    batch, and automated recovery is the entire safety argument for a delete being a
    quarantine. What was skipped is returned on the result AND recorded in the manifest,
    so a partial restore is never silent.

    Confinement is re-validated against the roots recorded in the manifest; see the
    module docstring for why that is not an anti-tamper guarantee.
    """
    doc = read_checkpoint(manifest_path)
    status = doc.get("status", "")
    if status == "restored":
        raise MaintenanceRefused("checkpoint %r was already restored" % manifest_path)
    if status not in ("pending", "executed"):
        raise MaintenanceRefused("refusing checkpoint %r with unknown status %r"
                                 % (manifest_path, status))

    store_root = doc["store_root"]
    destination_root = doc["destination_root"]
    pending = []
    unaccounted = []
    seen_originals = set()
    seen_relocated = set()
    for entry in doc["moves"]:
        original = entry["source"]
        relocated = entry["destination"]
        _require_confined(original, store_root, "restore target")
        _require_confined(relocated, destination_root, "checkpoint copy")
        # The same pre-flight-then-batch shape the executor guards, for the same two
        # reasons: this loop decides the whole batch BEFORE the first move, so two entries
        # sharing an ORIGINAL both see it free and the second silently overwrites the file
        # the first just restored, and two sharing a CHECKPOINT COPY both see it present
        # and the second raises FileNotFoundError after a partial restore. Checked over
        # every entry including the ones skipped below, or a skipped duplicate slips past.
        original_key = os.path.normcase(original)
        relocated_key = os.path.normcase(relocated)
        if original_key in seen_originals:
            raise MaintenanceRefused("recorded original %r appears twice; the second "
                                     "restore would overwrite the first" % original)
        if relocated_key in seen_relocated:
            raise MaintenanceRefused("recorded checkpoint copy %r appears twice; one "
                                     "file cannot be restored twice" % relocated)
        seen_originals.add(original_key)
        seen_relocated.add(relocated_key)
        if not os.path.lexists(relocated):
            if os.path.lexists(original):
                continue                        # never moved (or already restored)
            unaccounted.append(original)        # collected, so one refusal names them all
            continue
        if os.path.lexists(original):
            raise MaintenanceRefused("original path %r is occupied; refusing to "
                                     "overwrite it during restore" % original)
        pending.append(PlannedMove(session_id=entry["session_id"],
                                   source=relocated, destination=original))
    if unaccounted and not skip_unaccounted:
        raise MaintenanceRefused(
            "unaccounted move(s): neither the original nor the checkpoint copy exists "
            "for %s; refusing to guess (skip_unaccounted=True restores the accounted "
            "moves and leaves these alone)"
            % ", ".join(repr(path) for path in unaccounted))

    if not apply:
        return MaintenanceResult(executed=False, moves=tuple(pending),
                                 manifest_path=manifest_path,
                                 unaccounted=tuple(unaccounted))
    for move in pending:
        os.makedirs(os.path.dirname(move.destination), exist_ok=True)
        shutil.move(move.source, move.destination)
    doc["status"] = "restored"
    doc["restored_at_ms"] = int(time.time() * 1000) if now_ms is None else now_ms
    if unaccounted:
        doc["unaccounted"] = list(unaccounted)  # a partial restore is never silent
    _write_json(manifest_path, doc)
    return MaintenanceResult(executed=True, moves=tuple(pending),
                            manifest_path=manifest_path,
                            unaccounted=tuple(unaccounted))


# ------------------------------------------------------------- the audit ledger

# This module owns these tables outright and creates them via ensure_schema; it never
# touches llm_anthology.corpus's schema. Keyed by manifest path so re-recording the same
# run after a restore UPDATES it rather than duplicating it.
MAINTENANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS maintenance_runs (
    manifest_path  TEXT PRIMARY KEY,
    action         TEXT NOT NULL,
    status         TEXT NOT NULL,
    recorded_at_ms INTEGER NOT NULL,
    moved_count    INTEGER NOT NULL DEFAULT 0,
    blocked_count  INTEGER NOT NULL DEFAULT 0,
    store_root     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_maintenance_runs_at
    ON maintenance_runs(recorded_at_ms DESC);
"""

_RUN_COLS = ("manifest_path", "action", "status", "recorded_at_ms", "moved_count",
             "blocked_count", "store_root")


def ensure_schema(conn):
    """Idempotently create this module's own tables; returns the connection."""
    conn.executescript(MAINTENANCE_SCHEMA)
    conn.commit()
    return conn


def record_run(conn, manifest_path):
    """Record (or update) one maintenance run from its manifest — an auditable ledger
    of every destructive act, including whether it was later restored."""
    doc = read_checkpoint(manifest_path)
    row = (manifest_path, doc["action"], doc["status"], doc["recorded_at_ms"],
           len(doc["moves"]), len(doc["blocked"]), doc["store_root"])
    conn.execute("INSERT OR REPLACE INTO maintenance_runs (%s) VALUES (%s)"
                 % (", ".join(_RUN_COLS), ", ".join("?" * len(_RUN_COLS))), row)
    conn.commit()
    return row


def list_runs(conn, limit=50):
    """Recorded runs, newest first (manifest path breaks ties for determinism).

    The only interpolated fragment is `_RUN_COLS`, a module-level constant of column
    names — every VALUE is bound as a parameter. This is the same single-source-of-truth
    column pattern llm_anthology.corpus uses, and it is why bandit's B608 fires here as
    it already does there.
    """
    rows = conn.execute(
        "SELECT %s FROM maintenance_runs ORDER BY recorded_at_ms DESC, manifest_path "
        "LIMIT ?" % ", ".join(_RUN_COLS), (limit,)).fetchall()
    return [dict(zip(_RUN_COLS, row)) for row in rows]
