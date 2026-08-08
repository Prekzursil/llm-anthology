/**
 * The MAINTENANCE panel: the only destructive surface in the app.
 *
 * It archives, quarantines and restores the owner's real session files — irreplaceable
 * conversation history, some of it private. So this module is written as a safety mechanism
 * with a feature attached, not the other way round, and the shape of the UI is dictated by
 * the engine's PLANNER/EXECUTOR SPLIT rather than by convenience:
 *
 *   1. `maintenance.plan` is PURE. It mutates nothing and returns the whole act as data —
 *      every source -> destination pair, every target it REFUSED and why, and the exact
 *      phrase the operator must type. The operator therefore confirms the real thing.
 *   2. `maintenance.execute` runs only a plan the SERVER still holds, and only against an
 *      exactly-matching typed confirmation.
 *   3. A DELETE never unlinks. It relocates into `<checkpoint_root>/deleted`, so the
 *      checkpoint manifest is a real undo and {@link MaintenanceOutcomeView.canUndo} is a
 *      first-class part of the outcome rather than a footnote.
 *
 * THREE THINGS THIS MODULE REFUSES TO DO, each because the obvious alternative is a lie:
 *
 *   * There is NO route from "user pressed a button" to a destructive call. {@link
 *     MaintenancePanel.execute} takes no target list — it reads the plan the panel is
 *     already holding and fails if there is none. A one-click "clean up" cannot be built on
 *     top of this API without deleting code, which is the point.
 *   * Blocked targets are NEVER filtered out. A plan that quietly omits what it declined to
 *     touch is the same class of lie as a transcript that drops the lines it could not
 *     parse: the operator confirms a count that does not describe reality.
 *   * A -32003 is NOT one condition (see {@link classifyFailure}). Collapsing it into one
 *     "expired, re-plan" message would tell an operator who merely mistyped the
 *     confirmation to throw away a plan that is still perfectly live.
 *
 * DOM-FREE, like `ui/exportPanel` and `ui/readerPresent`: pure derivations plus a headless
 * controller that emits a render-state to an injected listener. vitest runs
 * `environment: "node"` here, so anything touching `document` is untestable and would sit at
 * 0% coverage — and this is the last module in the app that should be untested. The shell
 * binds these states to elements with `textContent`, never `innerHTML`, so a file path from
 * disk can never inject markup.
 */

import {
  RPC_CORPUS_NOT_INDEXED,
  RPC_INTERNAL_ERROR,
  RPC_INVALID_PARAMS,
  RPC_MAINTENANCE_REFUSED,
  rpcErrorCode,
} from "../ipc/types";
import type {
  MaintenanceActionName,
  MaintenanceBlocked,
  MaintenanceCopy,
  MaintenanceExecuteParams,
  MaintenancePlanParams,
  MaintenancePreview,
  MaintenanceResult,
  MaintenanceRestoreParams,
  MaintenanceRun,
  MaintenanceWarning,
  PlannedMove,
} from "../ipc/types";
import { formatBytes } from "./exportPanel";

// ---------------------------------------------------------------------------
// the IPC surface this panel needs
// ---------------------------------------------------------------------------

/**
 * The four `maintenance.*` methods, narrowed from the full client (interface segregation)
 * so a test injects a four-method fake. A real or mock `IpcClient` is assignable.
 */
export interface MaintenanceIpc {
  maintenancePlan(params: MaintenancePlanParams): Promise<MaintenancePreview>;
  maintenanceExecute(params: MaintenanceExecuteParams): Promise<MaintenanceResult>;
  maintenanceRestore(params: MaintenanceRestoreParams): Promise<MaintenanceResult>;
  maintenanceRuns(limit?: number): Promise<MaintenanceRun[]>;
}

// ---------------------------------------------------------------------------
// view-model types
// ---------------------------------------------------------------------------

/** One planned relocation, render-ready. */
export interface MoveRow {
  sessionId: string;
  source: string;
  destination: string;
  /** `source → destination`, for a single-line render. */
  label: string;
}

/** A target the safety model REFUSED. Always shown; never filtered. */
export interface BlockedRow {
  filePath: string;
  reason: string;
  detail: string;
  /** `path — reason: detail`, collapsing to `path — reason` when there is no detail. */
  label: string;
}

/** One row of the applied-run audit ledger, render-ready. */
export interface RunRow {
  manifestPath: string;
  action: string;
  status: string;
  recordedAtMs: number;
  /** `YYYY-MM-DD HH:MM` in UTC, matching `readerPresent.turnWhen`'s reasoning. */
  when: string;
  movedCount: number;
  blockedCount: number;
  storeRoot: string;
  label: string;
  /**
   * Whether offering "undo" for this row makes sense. A `restored` manifest refuses a second
   * restore (`llm_anthology/maintenance.py:658-659`), so presenting the affordance would
   * promise an action guaranteed to fail.
   */
  canRestore: boolean;
}

/**
 * A plan, ready to show BEFORE anything happens. This is the artifact the operator
 * confirms, so every number here has to describe what will really occur.
 */
export interface MaintenancePlanView {
  planId: string;
  action: MaintenanceActionName;
  /** e.g. `Delete 2 files (1 refused)`. */
  headline: string;
  storeRoot: string;
  /**
   * The EFFECTIVE destination, straight from the preview — a `delete` quarantines under
   * `<checkpoint_root>/deleted` and a `reconcile` under `<destination_root>/reconciled`
   * (`llm_anthology/maintenance.py:404-411`). This is where the files really go, which is
   * not necessarily the root the caller asked for.
   */
  destinationRoot: string;
  checkpointRoot: string;
  /** Why the files land where they land, in one sentence the operator can act on. */
  destinationExplanation: string;
  /** In ENGINE ORDER, not sorted — see the note in {@link derivePlanView}. */
  moves: MoveRow[];
  blocked: BlockedRow[];
  allowedCount: number;
  blockedCount: number;
  /**
   * Σ `size_bytes` over the ALLOWED targets — and that field is MEASURED by the engine, not
   * echoed from the request (`llm_anthology/maintenance.py`, `_measured_size`).
   *
   * That property is why this number may sit on the confirm screen at all. It used to be
   * whatever the client put in the plan request, which made the largest and most reassuring
   * figure on a delete dialog the only one not derived from disk. If a future refactor
   * reintroduces a caller-supplied size, this total stops being a fact and must either be
   * dropped or labelled reported-not-measured.
   */
  totalBytes: number;
  totalBytesLabel: string;
  /**
   * Allowed targets currently being written (`is_hot`) — each also raises a REVIEW warning.
   *
   * ALWAYS 0 over the wire today, and that is not this module's doing: the RPC edge never
   * reads `is_hot` from the request and nothing computes it, so it arrives as its dataclass
   * default `false` (`llm_anthology/maintenance.py`, the `SessionCopy` trust note). The
   * derivation and its render are correct for the day the engine populates it; until then
   * the hot-file warning cannot fire from a request that came over the wire, so do not read
   * a 0 here as "nothing is being written".
   */
  hotCount: number;
  reviewWarnings: string[];
  dangerousWarnings: string[];
  infoWarnings: string[];
  /** See {@link derivePlanView} — deliberately NOT `warnings.length > 0`. */
  needsAttention: boolean;
  /** Echoed verbatim from the engine; never reconstructed client-side. */
  requiredConfirmation: string;
}

/** What happened (or, on a dry run, what would have). */
export interface MaintenanceOutcomeView {
  executed: boolean;
  headline: string;
  moves: MoveRow[];
  manifestPath: string;
  /** `executed && manifestPath !== ""` — the engine writes `""`, not null, on a dry run. */
  canUndo: boolean;
  /** The undo instruction, or why there is nothing to undo. */
  undoHint: string;
  unaccounted: string[];
}

/** How the operator should respond to a failure. Drives which button the shell offers. */
export type MaintenanceRemedy =
  | "re-plan"
  | "correct-and-retry"
  | "fix-input"
  | "open-corpus"
  | "none";

/** A classified failure: what happened, and what the operator should do about it. */
export interface MaintenanceFailure {
  kind:
    | "plan-expired"
    | "refused"
    | "bad-params"
    | "no-corpus"
    | "manifest-unreadable"
    | "failed";
  /** What to show. Always carries the engine's own words so a new fault is never masked. */
  message: string;
  remedy: MaintenanceRemedy;
  /** The parsed JSON-RPC code, or null when the failure carried none. */
  code: number | null;
}

/** Which call failed. `manifest-unreadable` is only meaningful for a restore. */
export type MaintenanceStage = "plan" | "execute" | "restore" | "runs";

/** The panel's render-state. */
export type MaintenanceView =
  | { kind: "idle" }
  | { kind: "planning" }
  | { kind: "planned"; plan: MaintenancePlanView }
  | { kind: "executing"; plan: MaintenancePlanView }
  | { kind: "done"; plan: MaintenancePlanView | null; outcome: MaintenanceOutcomeView }
  | { kind: "restoring"; manifestPath: string }
  | { kind: "restored"; outcome: MaintenanceOutcomeView }
  | {
      kind: "error";
      stage: MaintenanceStage;
      failure: MaintenanceFailure;
      /** Kept on an error so a still-live plan stays on screen and can be retried. */
      plan: MaintenancePlanView | null;
    };

/** Called with the current {@link MaintenanceView} on every transition. */
export type MaintenanceViewListener = (view: MaintenanceView) => void;

/** Whether the typed confirmation may be submitted. */
export type ConfirmationState = "empty" | "mismatch" | "match";

// ---------------------------------------------------------------------------
// pure derivations
// ---------------------------------------------------------------------------

const ACTION_VERB: Record<MaintenanceActionName, string> = {
  delete: "Delete",
  archive: "Archive",
  move: "Move",
  reconcile: "Reconcile",
};

/**
 * A preview -> the model the operator confirms.
 *
 * ORDER IS PRESERVED, NOT SORTED. `exportPanel` sorts its deltas because they are a set;
 * these are a SEQUENCE — the engine performs the moves in plan order and the checkpoint
 * manifest records that order, so re-sorting for a tidier render would misrepresent what
 * happens and in which order a crash would leave things half-done. Engine order is already
 * deterministic (it follows the submitted targets), so nothing is lost.
 *
 * `needsAttention` is deliberately NOT `warnings.length > 0`. The engine emits a DANGEROUS
 * warning for EVERY allowed target plus a closing INFO summary, so a perfectly healthy
 * 2-file plan arrives carrying 3 warnings (`llm_anthology/maintenance.py:506-527`, and the
 * caveat on {@link MaintenanceWarning}). A UI keyed on non-emptiness would therefore flag
 * every plan it ever saw, which trains the operator to ignore the signal on the one plane
 * where that is most expensive. What actually deserves attention is a REFUSED target or a
 * REVIEW warning (a hot file, a duplicate, a stale confirmation) — and note that rule needs
 * no message-text matching, so a reworded engine warning cannot silently break it.
 */
export function derivePlanView(preview: MaintenancePreview): MaintenancePlanView {
  const moves = preview.plan.map(toMoveRow);
  const blocked = preview.blocked.map(toBlockedRow);
  const totalBytes = preview.allowed.reduce((sum, copy) => sum + copy.size_bytes, 0);
  const totalBytesLabel = formatBytes(totalBytes);
  const reviewWarnings = messagesAt(preview.warnings, "REVIEW");
  const dangerousWarnings = messagesAt(preview.warnings, "DANGEROUS");
  const infoWarnings = messagesAt(preview.warnings, "INFO");

  return {
    planId: preview.plan_id,
    action: preview.action,
    headline: planHeadline(preview.action, preview.allowed.length, preview.blocked.length),
    storeRoot: preview.store_root,
    destinationRoot: preview.destination_root,
    checkpointRoot: preview.checkpoint_root,
    destinationExplanation: destinationExplanation(preview),
    moves,
    blocked,
    allowedCount: preview.allowed.length,
    blockedCount: preview.blocked.length,
    totalBytes,
    totalBytesLabel,
    hotCount: preview.allowed.filter((copy: MaintenanceCopy) => copy.is_hot).length,
    reviewWarnings,
    dangerousWarnings,
    infoWarnings,
    needsAttention: blocked.length > 0 || reviewWarnings.length > 0,
    requiredConfirmation: preview.required_typed_confirmation,
  };
}

/**
 * Is `typed` submittable against this plan?
 *
 * The comparison is EXACT — no trim, no case fold — because the engine's is
 * (`llm_anthology/maintenance.py:554` compares the raw strings, having already refused a
 * whitespace-only one at `:552`). A UI that trimmed would enable its own button for
 * `"DELETE 1 FILE "` and then eat a refusal from the server, which reads to the operator as
 * the app being broken rather than as their trailing space. Mirroring the rule exactly is
 * what makes the disabled state honest.
 */
export function confirmationState(
  plan: MaintenancePlanView,
  typed: string,
): ConfirmationState {
  if (typed.trim() === "") return "empty";
  return typed === plan.requiredConfirmation ? "match" : "mismatch";
}

/**
 * A rejection -> what happened and what to do next.
 *
 * -32003 IS NOT ONE CONDITION, and the difference decides whether the operator's work is
 * lost. The engine raises it both for a spent/unknown plan handle
 * (`llm_anthology/sidecar.py:1581-1583`) and for every refusal that `execute_maintenance`
 * itself produces — a mismatched confirmation, a protected target, a colliding restore
 * (`:1518`). Only the FIRST means the plan is gone: the handle is deleted at `:1589`, AFTER
 * the call that raises, so a refused confirmation leaves it usable and the engine's own
 * comment at `:1587-1588` says that is deliberate ("a refused confirmation must be
 * correctable without forcing a re-plan").
 *
 * They are told apart by the `re-plan` token the engine puts in exactly one of those
 * messages (`:1583`). That is a TEXT match and therefore the brittle part of this module:
 * if the engine rewords it, a still-live plan would be reported as expired. The failure is
 * in the safe direction — re-planning is pure and costs the operator only a click, whereas
 * the inverse would loop them through retyping a confirmation against a handle that no
 * longer exists — and the engine's message is always shown verbatim either way, so the
 * operator can see what really happened. A distinct RPC code for a spent handle would
 * remove the guess entirely; that is an engine change, not a UI one.
 *
 * A missing manifest is called out separately because it does NOT arrive as a refusal:
 * `read_checkpoint` opens the file directly, so a bad path raises `FileNotFoundError` and
 * surfaces as -32603 (see the note on {@link rpcErrorCode}).
 */
export function classifyFailure(err: unknown, stage: MaintenanceStage): MaintenanceFailure {
  const code = rpcErrorCode(err);
  const message = errorText(err);

  if (code === RPC_MAINTENANCE_REFUSED) {
    if (message.includes("re-plan")) {
      return {
        kind: "plan-expired",
        message: `That plan is no longer valid — plans are single-use. Re-plan to continue. (${message})`,
        remedy: "re-plan",
        code,
      };
    }
    return {
      kind: "refused",
      message: `Refused, and your plan is still open — correct this and retry: ${message}`,
      remedy: "correct-and-retry",
      code,
    };
  }
  if (code === RPC_INVALID_PARAMS) {
    return { kind: "bad-params", message, remedy: "fix-input", code };
  }
  if (code === RPC_CORPUS_NOT_INDEXED) {
    return {
      kind: "no-corpus",
      message: "No corpus open — open one before running maintenance.",
      remedy: "open-corpus",
      code,
    };
  }
  if (code === RPC_INTERNAL_ERROR && stage === "restore") {
    return {
      kind: "manifest-unreadable",
      message: `That checkpoint manifest could not be read — it may have been moved or deleted: ${message}`,
      remedy: "fix-input",
      code,
    };
  }
  return { kind: "failed", message, remedy: "none", code };
}

/** A result -> the outcome model, with the undo route made explicit. */
export function deriveOutcome(
  result: MaintenanceResult,
  action: MaintenanceActionName | null,
): MaintenanceOutcomeView {
  const moves = result.moves.map(toMoveRow);
  const canUndo = result.executed && result.manifest_path !== "";
  return {
    executed: result.executed,
    headline: outcomeHeadline(result, action, moves.length),
    moves,
    manifestPath: result.manifest_path,
    canUndo,
    undoHint: undoHint(result, canUndo),
    unaccounted: [...result.unaccounted],
  };
}

/**
 * A courtesy pre-check on a `manifest_path`, mirroring the engine's edge rule: absolute and
 * local only, because merely resolving `\\host\share` on Windows initiates an outbound
 * SMB/NTLM authentication (`llm_anthology/sidecar.py:1599` -> `:500-514`).
 *
 * NOT A SECURITY BOUNDARY — the engine's check is, and it runs regardless. This exists only
 * so the operator learns immediately instead of after a round trip. Returns null when the
 * path looks acceptable.
 */
export function manifestPathProblem(path: string): string | null {
  if (path.trim() === "") return "Enter the path of a checkpoint manifest.";
  if (/^[\\/]{2}/.test(path)) {
    return "Network (UNC) paths are refused: opening one can leak your Windows credentials.";
  }
  if (!/^[A-Za-z]:[\\/]/.test(path) && !/^[\\/]/.test(path)) {
    return "Use a full path, not a relative one.";
  }
  return null;
}

/** The ledger -> render-ready rows, preserving the engine's newest-first order. */
export function deriveRuns(runs: MaintenanceRun[]): RunRow[] {
  return runs.map((run) => {
    const when = utcMinute(run.recorded_at_ms);
    return {
      manifestPath: run.manifest_path,
      action: run.action,
      status: run.status,
      recordedAtMs: run.recorded_at_ms,
      when,
      movedCount: run.moved_count,
      blockedCount: run.blocked_count,
      storeRoot: run.store_root,
      label: `${when} · ${run.action} · ${run.status} · ${run.moved_count} moved`,
      canRestore: run.status !== "restored",
    };
  });
}

/** A view -> the plain-text lines that describe it (never HTML, so nothing can inject). */
export function renderMaintenanceView(view: MaintenanceView): string[] {
  switch (view.kind) {
    case "idle":
      return ["No maintenance planned. Nothing will be touched until you plan and confirm."];
    case "planning":
      return ["Planning…"];
    case "planned":
      return planLines(view.plan);
    case "executing":
      return [`Running: ${view.plan.headline}`];
    case "done":
      return outcomeLines(view.outcome);
    case "restoring":
      return [`Restoring from ${view.manifestPath}…`];
    case "restored":
      return outcomeLines(view.outcome);
    case "error":
      return [`${stageLabel(view.stage)} failed: ${view.failure.message}`];
  }
}

// ---------------------------------------------------------------------------
// controller
// ---------------------------------------------------------------------------

/**
 * Headless maintenance controller.
 *
 * The plan is held here and is the ONLY thing {@link execute} can act on, so there is no
 * code path from a bare button press to a destructive call. `apply` is passed `true`
 * explicitly on execute and only after a local exact-match confirmation check, so the
 * engine's dry-run default is never the thing standing between the operator and their files.
 *
 * NOTE ON WHY THERE IS NO "dry-run execute" STEP: `maintenance.execute` consumes the plan
 * handle on ANY accepted run, dry or not (`llm_anthology/sidecar.py:1589` deletes it after
 * the call succeeds). A plan -> dry-execute -> apply-execute flow would therefore burn the
 * handle on the dry run and earn "already-used plan_id; re-plan" on the apply. The pure
 * `maintenance.plan` IS the dry run, and it is a better one: it reports what was REFUSED,
 * which a dry execute does not.
 */
export class MaintenancePanel {
  private view: MaintenanceView = { kind: "idle" };
  private plan: MaintenancePlanView | null = null;
  private busy = false;

  constructor(
    private readonly ipc: MaintenanceIpc,
    private readonly onChange: MaintenanceViewListener,
  ) {}

  get current(): MaintenanceView {
    return this.view;
  }

  /** The plan awaiting confirmation, or null. The shell gates its Execute button on this. */
  get pendingPlan(): MaintenancePlanView | null {
    return this.plan;
  }

  private emit(view: MaintenanceView): void {
    this.view = view;
    this.onChange(view);
  }

  private fail(stage: MaintenanceStage, err: unknown): void {
    const failure = classifyFailure(err, stage);
    // An expired plan is DROPPED — keeping it on screen would leave an Execute button
    // wired to a handle the engine has already thrown away.
    if (failure.kind === "plan-expired") this.plan = null;
    this.emit({ kind: "error", stage, failure, plan: this.plan });
  }

  /** Build a preview. Pure on the engine side: nothing is created, moved or deleted. */
  async planMaintenance(params: MaintenancePlanParams): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    this.plan = null;
    this.emit({ kind: "planning" });
    try {
      const preview = await this.ipc.maintenancePlan(params);
      this.plan = derivePlanView(preview);
      this.emit({ kind: "planned", plan: this.plan });
    } catch (err) {
      this.fail("plan", err);
    } finally {
      this.busy = false;
    }
  }

  /**
   * Apply the held plan, given an exactly-matching typed confirmation.
   *
   * Refuses locally on a missing plan or a non-matching phrase, so a doomed destructive
   * request is never sent and the shell can render the same rule it enforces.
   */
  async execute(typed: string): Promise<void> {
    if (this.busy) return;
    const plan = this.plan;
    if (plan === null) {
      this.emit({
        kind: "error",
        stage: "execute",
        failure: {
          kind: "refused",
          message: "There is no plan to run. Plan first, then confirm.",
          remedy: "re-plan",
          code: null,
        },
        plan: null,
      });
      return;
    }
    if (confirmationState(plan, typed) !== "match") {
      this.emit({
        kind: "error",
        stage: "execute",
        failure: {
          kind: "refused",
          message: `Type ${plan.requiredConfirmation} exactly to confirm.`,
          remedy: "correct-and-retry",
          code: null,
        },
        plan,
      });
      return;
    }

    this.busy = true;
    this.emit({ kind: "executing", plan });
    try {
      const result = await this.ipc.maintenanceExecute({
        plan_id: plan.planId,
        confirmation: typed,
        apply: true,
      });
      // Accepted, so the engine has consumed the handle — drop ours to match.
      this.plan = null;
      this.emit({ kind: "done", plan, outcome: deriveOutcome(result, plan.action) });
    } catch (err) {
      this.fail("execute", err);
    } finally {
      this.busy = false;
    }
  }

  /**
   * Roll a checkpoint back. `apply` defaults to false here as it does on the engine, so the
   * caller can show what a restore WOULD do before committing to it.
   */
  async restore(
    manifestPath: string,
    options: { apply?: boolean; skipUnaccounted?: boolean } = {},
  ): Promise<void> {
    if (this.busy) return;
    const problem = manifestPathProblem(manifestPath);
    if (problem !== null) {
      this.emit({
        kind: "error",
        stage: "restore",
        failure: { kind: "bad-params", message: problem, remedy: "fix-input", code: null },
        plan: this.plan,
      });
      return;
    }

    this.busy = true;
    this.emit({ kind: "restoring", manifestPath });
    try {
      const result = await this.ipc.maintenanceRestore({
        manifest_path: manifestPath,
        apply: options.apply ?? false,
        skip_unaccounted: options.skipUnaccounted ?? false,
      });
      this.emit({ kind: "restored", outcome: deriveOutcome(result, null) });
    } catch (err) {
      this.fail("restore", err);
    } finally {
      this.busy = false;
    }
  }

  /** The applied-run ledger. Read-only; never touches the filesystem. */
  async listRuns(limit?: number): Promise<RunRow[]> {
    try {
      return deriveRuns(await this.ipc.maintenanceRuns(limit));
    } catch (err) {
      this.fail("runs", err);
      return [];
    }
  }

  /** Drop any held plan and return to idle. */
  reset(): void {
    this.plan = null;
    this.emit({ kind: "idle" });
  }
}

// ---------------------------------------------------------------------------
// private helpers
// ---------------------------------------------------------------------------

function toMoveRow(move: PlannedMove): MoveRow {
  return {
    sessionId: move.session_id,
    source: move.source,
    destination: move.destination,
    label: `${move.source} → ${move.destination}`,
  };
}

function toBlockedRow(blocked: MaintenanceBlocked): BlockedRow {
  const detail = blocked.detail.trim();
  const head = `${blocked.target.file_path} — ${blocked.reason}`;
  return {
    filePath: blocked.target.file_path,
    reason: blocked.reason,
    detail: blocked.detail,
    label: detail === "" ? head : `${head}: ${detail}`,
  };
}

function messagesAt(
  warnings: MaintenanceWarning[],
  severity: MaintenanceWarning["severity_name"],
): string[] {
  return warnings.filter((w) => w.severity_name === severity).map((w) => w.message);
}

function fileWord(count: number): string {
  return count === 1 ? "file" : "files";
}

function planHeadline(
  action: MaintenanceActionName,
  allowed: number,
  blocked: number,
): string {
  const head = `${ACTION_VERB[action]} ${allowed} ${fileWord(allowed)}`;
  return blocked === 0 ? head : `${head} (${blocked} refused)`;
}

/**
 * Where the files go, and — for a delete — the fact that it is a QUARANTINE and not an
 * unlink. That is the single most load-bearing reassurance on this panel: the engine
 * relocates into `<checkpoint_root>/deleted` so the checkpoint can undo it
 * (`llm_anthology/maintenance.py:62-64`), and an operator who believes "delete" means
 * "gone forever" will either avoid a safe action or panic after taking it.
 */
function destinationExplanation(preview: MaintenancePreview): string {
  if (preview.action === "delete") {
    return `Nothing is erased. Files are moved to ${preview.destination_root}, and the checkpoint written to ${preview.checkpoint_root} can put them back.`;
  }
  return `Files are moved to ${preview.destination_root}. The checkpoint written to ${preview.checkpoint_root} can put them back.`;
}

function outcomeHeadline(
  result: MaintenanceResult,
  action: MaintenanceActionName | null,
  moved: number,
): string {
  const verb = action === null ? "Restore" : ACTION_VERB[action];
  if (!result.executed) {
    return `Dry run: ${verb.toLowerCase()} would move ${moved} ${fileWord(moved)}. Nothing changed.`;
  }
  return `${verb} complete: ${moved} ${fileWord(moved)} moved.`;
}

function undoHint(result: MaintenanceResult, canUndo: boolean): string {
  if (canUndo) {
    return `To undo, restore from ${result.manifest_path}`;
  }
  if (!result.executed) return "Nothing to undo — this was a dry run.";
  return "No checkpoint was written, so this cannot be undone here.";
}

/** `YYYY-MM-DD HH:MM` in UTC. Same reasoning as `readerPresent.turnWhen`. */
function utcMinute(ms: number): string {
  if (!Number.isFinite(ms)) return "";
  return new Date(ms).toISOString().replace("T", " ").slice(0, 16);
}

function stageLabel(stage: MaintenanceStage): string {
  switch (stage) {
    case "plan":
      return "Planning";
    case "execute":
      return "Maintenance";
    case "restore":
      return "Restore";
    case "runs":
      return "Loading past runs";
  }
}

function planLines(plan: MaintenancePlanView): string[] {
  const lines = [
    plan.headline,
    plan.destinationExplanation,
    `store root: ${plan.storeRoot}`,
    `${plan.allowedCount} ${fileWord(plan.allowedCount)} · ${plan.totalBytesLabel}`,
  ];
  for (const move of plan.moves) lines.push(`move: ${move.label}`);
  // Refusals come BEFORE the confirmation prompt: the operator must see what the engine
  // declined while deciding, not after committing.
  for (const blocked of plan.blocked) lines.push(`refused: ${blocked.label}`);
  if (plan.hotCount > 0) {
    lines.push(`${plan.hotCount} ${fileWord(plan.hotCount)} being written right now`);
  }
  for (const warning of plan.reviewWarnings) lines.push(`review: ${warning}`);
  lines.push(`Type ${plan.requiredConfirmation} to confirm.`);
  return lines;
}

function outcomeLines(outcome: MaintenanceOutcomeView): string[] {
  const lines = [outcome.headline, outcome.undoHint];
  for (const move of outcome.moves) lines.push(`moved: ${move.label}`);
  if (outcome.unaccounted.length > 0) {
    lines.push(
      `${outcome.unaccounted.length} could not be accounted for: ${outcome.unaccounted.join(", ")}`,
    );
  }
  return lines;
}

/** An unknown throwable -> display text (an Error's message, else its stringification). */
function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
