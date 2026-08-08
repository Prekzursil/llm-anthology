/**
 * The DOM half of {@link MaintenancePanel}.
 *
 * `MaintenancePanel` is a headless view-model: it owns the plan handle, the confirmation
 * gate and every engine call, and emits a {@link MaintenanceView} — it never touches the
 * DOM. That split is what let it reach full coverage under this project's DOM-less vitest,
 * and it is why a renderer has to exist somewhere. It lives HERE rather than inside
 * `app.ts` so the markup is a module that can be read, styled and (once this repo has a
 * DOM test environment) tested on its own.
 *
 * DELIBERATELY DECISION-FREE, in the same sense `metadataPanel`'s DOM half is. Every rule
 * it renders was decided and unit-tested in `maintenancePanel.ts`:
 *
 *   * the body text comes from the exported {@link renderMaintenanceView}, so the plan the
 *     operator reads is the tested one — this module does not compose a second description
 *     of a destructive action that could drift from it;
 *   * the Execute gate is {@link confirmationState}, which mirrors the ENGINE'S exact,
 *     un-trimmed comparison, so the disabled state is honest rather than approximately
 *     right;
 *   * the restore path is pre-checked by {@link manifestPathProblem} — a courtesy so the
 *     operator learns immediately, never a security boundary (the engine's UNC refusal is);
 *   * `needsAttention` and `canRestore` are read off the view, never recomputed. Both have
 *     non-obvious definitions (a healthy plan legitimately carries DANGEROUS warnings; a
 *     `restored` manifest refuses a second restore), and re-deriving either here is exactly
 *     how a UI ends up contradicting the engine it is describing.
 *
 * Everything is written with `textContent`. Every string on this plane is either a local
 * filesystem path or an engine message, and a path embeds the owner's username.
 *
 * The `<pre>` + `renderMaintenanceView` shape mirrors `mountExportPanel`'s output area, so
 * this app has one way of presenting a plan-then-apply flow rather than two.
 */

import {
  MaintenancePanel,
  confirmationState,
  manifestPathProblem,
  renderMaintenanceView,
  type MaintenanceIpc,
  type MaintenancePlanView,
  type MaintenanceRemedy,
  type MaintenanceView,
  type RunRow,
} from "./maintenancePanel";
import type { MaintenanceActionName, MaintenancePlanParams } from "../ipc/types";

/**
 * The four actions, LEAST destructive first.
 *
 * Order is a safety decision, not alphabetical drift: a `<select>` shows its first option to
 * an operator who never opens it, and that default must not be `delete`. `archive` moves
 * files to a destination the operator named; `delete` quarantines them under the checkpoint
 * root, which is recoverable but is still the one nobody should arrive at by accident.
 */
const ACTIONS: readonly MaintenanceActionName[] = ["archive", "move", "reconcile", "delete"];

/**
 * What to do about a failure, keyed on the view-model's own remedy enum.
 *
 * Keyed on the ENUM rather than matched against the message text, so a reworded engine
 * message cannot silently drop the guidance. The engine's own words are always shown too —
 * `renderMaintenanceView` carries them — so this adds the next step and never replaces the
 * explanation.
 */
const REMEDY_HINT: Record<MaintenanceRemedy, string> = {
  "re-plan": "That plan handle is gone. Plan again, then confirm.",
  "correct-and-retry": "The plan is still live — correct the confirmation and try again.",
  "fix-input": "Correct the path and try again.",
  "open-corpus": "Attach a corpus first, then plan.",
  none: "",
};

/**
 * Whether the operator has to name a destination.
 *
 * `delete` derives its own (`<checkpoint_root>/deleted`), so asking for one would be a field
 * that changes nothing. The other three land under a destination the engine will not invent.
 */
function needsDestination(action: MaintenanceActionName): boolean {
  return action !== "delete";
}

/** The textarea -> targets: one path per line, blanks dropped. */
export function parseTargets(text: string): string[] {
  return text
    .split(/[\r\n]+/)
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

/**
 * Why this plan cannot be REQUESTED yet, in the operator's terms, or null when it can.
 *
 * A courtesy check on required-ness ONLY, deliberately not a second copy of the engine's
 * path rules: `sidecar.py` refuses a relative or UNC root itself and its message is rendered
 * verbatim, so re-implementing that here would create a second rule to keep in sync for no
 * gain. This exists so an empty form does not spend a round trip earning a -32602.
 */
export function planFormProblem(
  action: MaintenanceActionName,
  storeRoot: string,
  checkpointRoot: string,
  destinationRoot: string,
  targets: readonly string[],
): string | null {
  if (storeRoot.trim() === "") return "Name the store root — the folder the files live under.";
  if (checkpointRoot.trim() === "") {
    return "Name a checkpoint root. Every run writes a checkpoint so it can be undone.";
  }
  if (needsDestination(action) && destinationRoot.trim() === "") {
    return `A ${action} needs a destination root. Only a delete derives its own.`;
  }
  if (targets.length === 0) return "List at least one file to act on, one path per line.";
  return null;
}

export class MaintenanceShell {
  private readonly panel: MaintenancePanel;

  private readonly actionEl: HTMLSelectElement;
  private readonly storeRootEl: HTMLInputElement;
  private readonly checkpointRootEl: HTMLInputElement;
  private readonly destinationEl: HTMLInputElement;
  private readonly destinationLabel: HTMLElement;
  private readonly targetsEl: HTMLTextAreaElement;
  private readonly planBtn: HTMLButtonElement;
  private readonly resetBtn: HTMLButtonElement;

  private readonly confirmRow: HTMLElement;
  private readonly confirmEl: HTMLInputElement;
  private readonly executeBtn: HTMLButtonElement;

  private readonly manifestEl: HTMLInputElement;
  private readonly restoreDryBtn: HTMLButtonElement;
  private readonly restoreApplyBtn: HTMLButtonElement;

  private readonly runsBtn: HTMLButtonElement;
  private readonly runsEl: HTMLElement;

  /**
   * The running narrative. Created ONCE and only its `textContent` replaced: it is a
   * `role="status"` live region, and replacing the element would land each message in a node
   * that was not yet in the accessibility tree, so none would be announced.
   */
  private readonly outputEl: HTMLElement;
  private readonly remedyEl: HTMLElement;

  constructor(ipc: MaintenanceIpc, container: HTMLElement) {
    this.panel = new MaintenancePanel(ipc, (view) => this.paint(view));
    container.classList.add("maintenance-panel");

    const form = document.createElement("div");
    form.className = "maintenance-form";

    const actionLabel = document.createElement("label");
    actionLabel.className = "maintenance-field";
    actionLabel.textContent = "Action";
    this.actionEl = document.createElement("select");
    this.actionEl.className = "maintenance-action";
    for (const action of ACTIONS) {
      const option = document.createElement("option");
      option.value = action;
      option.textContent = action;
      this.actionEl.append(option);
    }
    this.actionEl.addEventListener("change", () => this.syncDestinationField());
    actionLabel.append(this.actionEl);

    this.storeRootEl = field(form, "Store root", "maintenance-store-root",
      "C:\\Users\\you\\.codex");
    this.checkpointRootEl = field(form, "Checkpoint root", "maintenance-checkpoint-root",
      "Where the undo checkpoint is written");
    // Built through the same helper, then captured so the whole row can be hidden for a
    // delete — a field that is documented as ignored should not be on screen at all.
    this.destinationEl = field(form, "Destination root", "maintenance-destination",
      "Where the files are moved to");
    this.destinationLabel = this.destinationEl.parentElement as HTMLElement;

    const targetsLabel = document.createElement("label");
    targetsLabel.className = "maintenance-field maintenance-field-wide";
    targetsLabel.textContent = "Files to act on (one path per line)";
    this.targetsEl = document.createElement("textarea");
    this.targetsEl.className = "maintenance-targets";
    this.targetsEl.rows = 4;
    this.targetsEl.spellcheck = false;
    targetsLabel.append(this.targetsEl);

    form.prepend(actionLabel);
    form.append(targetsLabel);

    const actions = document.createElement("div");
    actions.className = "maintenance-actions";
    this.planBtn = button("Plan", "maintenance-plan", () => void this.plan());
    this.resetBtn = button("Reset", "maintenance-reset", () => this.panel.reset());
    actions.append(this.planBtn, this.resetBtn);

    this.outputEl = document.createElement("pre");
    this.outputEl.className = "maintenance-output";
    this.outputEl.setAttribute("role", "status");
    this.outputEl.setAttribute("aria-live", "polite");

    this.remedyEl = document.createElement("p");
    this.remedyEl.className = "maintenance-remedy";
    this.remedyEl.setAttribute("role", "alert");
    this.remedyEl.hidden = true;

    this.confirmRow = document.createElement("div");
    this.confirmRow.className = "maintenance-confirm";
    this.confirmEl = field(this.confirmRow, "Type the confirmation phrase",
      "maintenance-confirmation", "");
    this.confirmEl.addEventListener("input", () => this.syncConfirmGate());
    this.executeBtn = button("Execute", "maintenance-execute",
      () => void this.panel.execute(this.confirmEl.value));
    this.confirmRow.append(this.executeBtn);
    this.confirmRow.hidden = true;

    const restore = document.createElement("div");
    restore.className = "maintenance-restore";
    const restoreTitle = document.createElement("h3");
    restoreTitle.className = "maintenance-subtitle";
    restoreTitle.textContent = "Undo a past run";
    this.manifestEl = field(restore, "Checkpoint manifest path", "maintenance-manifest",
      "The manifest a run wrote");
    this.manifestEl.addEventListener("input", () => this.syncRestoreGate());
    this.restoreDryBtn = button("Preview restore", "maintenance-restore-dry",
      () => void this.panel.restore(this.manifestEl.value));
    this.restoreApplyBtn = button("Restore", "maintenance-restore-apply",
      () => void this.panel.restore(this.manifestEl.value, { apply: true }));
    restore.prepend(restoreTitle);
    restore.append(this.restoreDryBtn, this.restoreApplyBtn);

    const runs = document.createElement("div");
    runs.className = "maintenance-runs";
    this.runsBtn = button("Show past runs", "maintenance-runs-load", () => void this.loadRuns());
    this.runsEl = document.createElement("div");
    this.runsEl.className = "maintenance-runs-list";
    runs.append(this.runsBtn, this.runsEl);

    container.append(form, actions, this.outputEl, this.remedyEl, this.confirmRow, restore, runs);

    this.syncDestinationField();
    this.syncRestoreGate();
    this.paint(this.panel.current);
  }

  /** The action currently chosen, narrowed back to the closed union it came from. */
  private chosenAction(): MaintenanceActionName {
    // Found rather than cast: a `<select>` value is a string, and the engine answers -32602
    // for anything outside this set, so the narrowing has to be a real lookup.
    return ACTIONS.find((a) => a === this.actionEl.value) ?? ACTIONS[0];
  }

  private syncDestinationField(): void {
    this.destinationLabel.hidden = !needsDestination(this.chosenAction());
  }

  private async plan(): Promise<void> {
    const action = this.chosenAction();
    const targets = parseTargets(this.targetsEl.value);
    const problem = planFormProblem(
      action,
      this.storeRootEl.value,
      this.checkpointRootEl.value,
      this.destinationEl.value,
      targets,
    );
    if (problem !== null) {
      // Shown where a refusal is shown, so the operator does not have to learn two places
      // to look for "why did nothing happen".
      this.outputEl.textContent = problem;
      this.showRemedy("fix-input");
      return;
    }
    const params: MaintenancePlanParams = {
      store_root: this.storeRootEl.value.trim(),
      checkpoint_root: this.checkpointRootEl.value.trim(),
      action,
      targets: targets.map((file_path) => ({ file_path })),
    };
    if (needsDestination(action)) params.destination_root = this.destinationEl.value.trim();
    await this.panel.planMaintenance(params);
  }

  private async loadRuns(): Promise<void> {
    this.runsBtn.disabled = true;
    try {
      this.renderRuns(await this.panel.listRuns());
    } finally {
      this.runsBtn.disabled = false;
    }
  }

  private renderRuns(rows: RunRow[]): void {
    if (rows.length === 0) {
      const empty = document.createElement("p");
      empty.className = "maintenance-runs-empty muted";
      empty.textContent = "No maintenance has been applied to this corpus.";
      this.runsEl.replaceChildren(empty);
      return;
    }
    this.runsEl.replaceChildren(...rows.map((row) => {
      const el = document.createElement("div");
      el.className = "maintenance-run";
      const label = document.createElement("span");
      label.className = "maintenance-run-label";
      label.textContent = row.label;
      el.append(label);
      // `canRestore` off the row, never `status !== "restored"` recomputed here: the rule
      // that a restored manifest refuses a second restore belongs to the view-model.
      if (row.canRestore) {
        el.append(button("Use for undo", "maintenance-run-use", () => {
          this.manifestEl.value = row.manifestPath;
          this.syncRestoreGate();
          this.manifestEl.focus();
        }));
      }
      const path = document.createElement("span");
      path.className = "maintenance-run-path muted";
      path.textContent = row.manifestPath;
      path.title = row.manifestPath;
      el.append(path);
      return el;
    }));
  }

  private showRemedy(remedy: MaintenanceRemedy): void {
    const hint = REMEDY_HINT[remedy];
    this.remedyEl.textContent = hint;
    this.remedyEl.hidden = hint === "";
  }

  /**
   * Enable Execute only on an EXACT match, and only while a plan handle is actually held.
   *
   * `pendingPlan` rather than the last view's `kind`: an accepted run and an expired handle
   * both leave a plan on screen while the handle is gone, and the view-model is the only
   * thing that knows which. Gating on the render state would offer Execute for a plan the
   * engine has already thrown away.
   */
  private syncConfirmGate(): void {
    const plan = this.panel.pendingPlan;
    if (plan === null) {
      this.confirmRow.hidden = true;
      this.executeBtn.disabled = true;
      return;
    }
    this.confirmRow.hidden = false;
    const state = confirmationState(plan, this.confirmEl.value);
    this.confirmRow.dataset.confirm = state;
    this.executeBtn.disabled = state !== "match";
  }

  private syncRestoreGate(): void {
    const problem = manifestPathProblem(this.manifestEl.value);
    const ok = problem === null;
    this.restoreDryBtn.disabled = !ok;
    this.restoreApplyBtn.disabled = !ok;
    // The reason is shown only once the operator has typed something: the "enter a path"
    // case is what an empty field already says.
    this.manifestEl.title = this.manifestEl.value.trim() === "" ? "" : (problem ?? "");
  }

  private paint(view: MaintenanceView): void {
    this.outputEl.textContent = renderMaintenanceView(view).join("\n");
    this.outputEl.dataset.kind = view.kind;

    this.showRemedy(view.kind === "error" ? view.failure.remedy : "none");
    this.markAttention(planOf(view));

    // A finished run names its own manifest, so undoing it is one click rather than a path
    // the operator has to go and find.
    if ((view.kind === "done" || view.kind === "restored") && view.outcome.canUndo) {
      this.manifestEl.value = view.outcome.manifestPath;
      this.syncRestoreGate();
    }

    const busy = view.kind === "planning" || view.kind === "executing" || view.kind === "restoring";
    this.planBtn.disabled = busy;
    this.resetBtn.disabled = busy;
    this.restoreDryBtn.disabled = busy || manifestPathProblem(this.manifestEl.value) !== null;
    this.restoreApplyBtn.disabled = this.restoreDryBtn.disabled;

    // A fresh plan gets a fresh confirmation: carrying the previous phrase forward would
    // leave Execute enabled against a plan the operator has not read.
    if (view.kind === "planned") this.confirmEl.value = "";
    this.syncConfirmGate();
    if (busy) this.executeBtn.disabled = true;
  }

  /**
   * Flag a plan that deserves a second look, straight off `needsAttention`.
   *
   * NOT `warnings.length > 0` — the engine emits a DANGEROUS warning per allowed target plus
   * a closing INFO summary, so keying on non-emptiness would flag every plan ever made and
   * teach the operator to ignore the mark on the one plane where that costs most.
   */
  private markAttention(plan: MaintenancePlanView | null): void {
    if (plan !== null && plan.needsAttention) this.outputEl.dataset.attention = "true";
    else delete this.outputEl.dataset.attention;
  }
}

/** The plan a view carries, or null for the states that carry none. */
function planOf(view: MaintenanceView): MaintenancePlanView | null {
  switch (view.kind) {
    case "planned":
    case "executing":
      return view.plan;
    case "done":
    case "error":
      return view.plan;
    default:
      return null;
  }
}

function field(
  parent: HTMLElement,
  text: string,
  cls: string,
  placeholder: string,
): HTMLInputElement {
  const label = document.createElement("label");
  label.className = "maintenance-field";
  label.textContent = text;
  const input = document.createElement("input");
  input.type = "text";
  input.className = cls;
  input.placeholder = placeholder;
  input.autocomplete = "off";
  input.spellcheck = false;
  label.append(input);
  parent.append(label);
  return input;
}

function button(text: string, cls: string, onClick: () => void): HTMLButtonElement {
  const b = document.createElement("button");
  b.type = "button";
  b.className = cls;
  b.textContent = text;
  b.addEventListener("click", onClick);
  return b;
}
