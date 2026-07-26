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
        operator confirms the real thing rather than an intention.

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
        It also refuses to trust the preview it is handed: every plan source must be an
        ALLOWED target, confinement is re-asserted on both ends, the source must be a
        regular file, and the destination must not already exist (`lexists`, so a
        dangling symlink cannot be written through).

  restore_checkpoint(manifest_path, *, apply=False) -> MaintenanceResult
        The recovery half. Moves every relocated file back to its recorded original
        path, and is itself gated: dry-run by default, confinement re-validated against
        the roots recorded in the manifest (so tampering the manifest cannot turn
        restore into an arbitrary-write primitive), and it fails loud rather than
        overwrite a file that has since taken the original name.

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
from dataclasses import dataclass
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
    fields are flattened here, as the C# record itself does)."""
    session_id: str
    file_path: str
    store_kind: SessionStoreKind = SessionStoreKind.UNKNOWN
    last_write_ms: Optional[int] = None
    size_bytes: int = 0
    is_hot: bool = False


@dataclass(frozen=True)
class BlockedTarget:
    """A target the planner refuses to touch, with the machine-readable `reason`
    (protected / unsafe-path / traversal / outside-store-root) and a human `detail`.
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
    """
    executed: bool
    moves: Tuple[PlannedMove, ...] = ()
    manifest_path: str = ""


# ------------------------------------------------------------------ path safety

# Ported from MaintenancePlanner.ProtectedPathMarkers. These name the owner's LIVE
# Codex store; a maintenance op must never touch it even if the caller mislabelled the
# copy's store kind. Compared against a `\`-normalised, case-folded path.
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


def _is_protected(copy):
    """Ported from MaintenancePlanner.IsProtected: the LIVE store is untouchable, and
    so is any path spelling one of the protected markers."""
    if copy.store_kind is SessionStoreKind.LIVE:
        return True
    normalized = copy.file_path.replace("/", "\\").lower()
    return any(marker.lower() in normalized for marker in PROTECTED_PATH_MARKERS)


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

    allowed = []
    blocked = []
    warnings = []
    plan = []
    claimed = set()
    for target in request.targets:
        verdict = _classify(target.file_path, store_root)
        if verdict is None and _is_protected(target):
            verdict = ("protected", "protected store path: %r" % target.file_path)
        if verdict is not None:
            blocked.append(BlockedTarget(target=target, reason=verdict[0],
                                         detail=verdict[1]))
            warnings.append(MaintenanceWarning(
                MaintenanceWarningSeverity.DANGEROUS,
                "Protected path blocked: %s (%s)" % (target.file_path, verdict[0])))
            continue
        allowed.append(target)
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
    if not apply:
        return MaintenanceResult(executed=False, moves=preview.plan, manifest_path="")

    # The executor does not trust the preview object: a forged plan must not smuggle a
    # blocked target past the planner, so every source has to be an ALLOWED target.
    allowed_paths = {os.path.normcase(t.file_path) for t in preview.allowed}
    for move in preview.plan:
        if os.path.normcase(move.source) not in allowed_paths:
            raise MaintenanceRefused(
                "plan source %r is not an allowed target in this preview" % move.source)
        _require_confined(move.source, preview.store_root, "plan source")
        if not os.path.isfile(move.source):
            raise MaintenanceRefused("plan source %r is not a regular file (the plan is "
                                     "stale or the target is a directory)" % move.source)

    os.makedirs(preview.checkpoint_root, exist_ok=True)
    os.makedirs(preview.destination_root, exist_ok=True)
    for move in preview.plan:
        _require_confined(move.destination, preview.destination_root,
                          "plan destination")
        if os.path.lexists(move.destination):
            raise MaintenanceRefused("plan destination %r already exists; refusing to "
                                     "overwrite" % move.destination)

    recorded_at_ms = int(time.time() * 1000) if now_ms is None else now_ms
    manifest_path = os.path.join(preview.checkpoint_root, _unique_name(
        preview.checkpoint_root,
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

def restore_checkpoint(manifest_path, *, apply=False, now_ms=None):
    """Undo a checkpointed run: move every relocated file back to its original path.

    Works on a "pending" manifest too, which is what makes a crash mid-batch
    recoverable: each move is inspected independently and only the ones that actually
    landed are undone. Fails loud rather than lose data — if a file has since taken the
    original name, or if BOTH ends of a recorded move are missing, it refuses instead of
    guessing. Confinement is re-validated against the roots recorded in the manifest, so
    editing the manifest cannot turn restore into an arbitrary-write primitive.
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
    for entry in doc["moves"]:
        original = entry["source"]
        relocated = entry["destination"]
        _require_confined(original, store_root, "restore target")
        _require_confined(relocated, destination_root, "checkpoint copy")
        if not os.path.lexists(relocated):
            if os.path.lexists(original):
                continue                        # never moved (or already restored)
            raise MaintenanceRefused(
                "unaccounted move: neither %r nor %r exists; refusing to guess"
                % (original, relocated))
        if os.path.lexists(original):
            raise MaintenanceRefused("original path %r is occupied; refusing to "
                                     "overwrite it during restore" % original)
        pending.append(PlannedMove(session_id=entry["session_id"],
                                   source=relocated, destination=original))

    if not apply:
        return MaintenanceResult(executed=False, moves=tuple(pending),
                                 manifest_path=manifest_path)
    for move in pending:
        os.makedirs(os.path.dirname(move.destination), exist_ok=True)
        shutil.move(move.source, move.destination)
    doc["status"] = "restored"
    doc["restored_at_ms"] = int(time.time() * 1000) if now_ms is None else now_ms
    _write_json(manifest_path, doc)
    return MaintenanceResult(executed=True, moves=tuple(pending),
                            manifest_path=manifest_path)


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
