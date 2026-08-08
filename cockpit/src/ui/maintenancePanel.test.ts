/**
 * The maintenance panel's decisions — the destructive plane, so the tests that matter most
 * here are the NEGATIVE ones.
 *
 * Four properties carry the safety argument and each is asserted directly:
 *
 *   1. THERE IS NO ROUTE FROM A BUTTON TO A DELETION. `execute` with no held plan, or with a
 *      confirmation that does not match exactly, must make NO IPC CALL AT ALL. Asserting the
 *      resulting error message is not enough — a panel that reported an error and sent the
 *      request anyway would pass that weaker check.
 *   2. A CLEAN PLAN IS NOT A QUIET PLAN. The engine emits one DANGEROUS warning per allowed
 *      target plus a closing INFO summary, so a healthy plan arrives carrying warnings.
 *      `needsAttention` must be false there, or the operator is trained to ignore the signal.
 *   3. -32003 IS TWO CONDITIONS. A spent plan and a mistyped confirmation both arrive as
 *      MAINTENANCE_REFUSED, and only the first has thrown the plan away. Both directions are
 *      asserted, including that a still-live plan SURVIVES the refusal.
 *   4. AN UNDO IS ONLY REAL IF A CHECKPOINT WAS WRITTEN. `manifest_path` is `""` — not null —
 *      on a dry run, so the empty-manifest case is asserted separately from the dry run.
 *
 * Every fixture is invented. Nothing here reads a real session store, and no assertion
 * depends on the host machine.
 */

import { describe, expect, it, vi } from "vitest";

import {
  MaintenancePanel,
  classifyFailure,
  confirmationState,
  derivePlanView,
  deriveOutcome,
  deriveRuns,
  manifestPathProblem,
  renderMaintenanceView,
} from "./maintenancePanel";
import type {
  MaintenanceIpc,
  MaintenanceStage,
  MaintenanceView,
  MoveRow,
} from "./maintenancePanel";
import type {
  MaintenanceBlocked,
  MaintenanceCopy,
  MaintenancePreview,
  MaintenanceResult,
  MaintenanceRun,
  MaintenanceWarning,
} from "../ipc/types";

// --------------------------------------------------------------------- fixtures

const copy = (over: Partial<MaintenanceCopy> = {}): MaintenanceCopy => ({
  session_id: "s1",
  file_path: "C:\\store\\a.jsonl",
  store_kind: "unknown",
  last_write_ms: 1_700_000_000_000,
  size_bytes: 2048,
  is_hot: false,
  ...over,
});

const warn = (
  severity_name: MaintenanceWarning["severity_name"],
  message: string,
): MaintenanceWarning => ({
  severity: severity_name === "INFO" ? 0 : severity_name === "REVIEW" ? 1 : 2,
  severity_name,
  message,
});

const blockedTarget = (over: Partial<MaintenanceBlocked> = {}): MaintenanceBlocked => ({
  target: copy({ file_path: "C:\\store\\.codex\\sessions\\live.jsonl" }),
  reason: "protected",
  detail: "protected store path",
  ...over,
});

/**
 * A DELETE preview shaped exactly as the engine sends one, INCLUDING the per-target
 * DANGEROUS warnings and the closing INFO summary that a healthy plan always carries.
 * Building the noisy-but-healthy case as the default is deliberate: it is the shape that
 * breaks a naive `warnings.length > 0` check.
 */
const preview = (over: Partial<MaintenancePreview> = {}): MaintenancePreview => ({
  plan_id: "plan-1",
  action: "delete",
  store_root: "C:\\store",
  destination_root: "C:\\cp\\deleted",
  checkpoint_root: "C:\\cp",
  allowed: [
    copy({ session_id: "a", file_path: "C:\\store\\a.jsonl" }),
    copy({ session_id: "b", file_path: "C:\\store\\b.jsonl", size_bytes: 1024 }),
  ],
  blocked: [],
  warnings: [
    warn("DANGEROUS", "Dangerous maintenance target: C:\\store\\a.jsonl"),
    warn("DANGEROUS", "Dangerous maintenance target: C:\\store\\b.jsonl"),
    warn("INFO", "delete preview: 2 allowed, 0 blocked; a checkpoint and a typed confirmation are required"),
  ],
  plan: [
    { session_id: "a", source: "C:\\store\\a.jsonl", destination: "C:\\cp\\deleted\\a.jsonl" },
    { session_id: "b", source: "C:\\store\\b.jsonl", destination: "C:\\cp\\deleted\\b.jsonl" },
  ],
  requires_checkpoint: true,
  requires_typed_confirmation: true,
  required_typed_confirmation: "DELETE 2 FILES",
  ...over,
});

const result = (over: Partial<MaintenanceResult> = {}): MaintenanceResult => ({
  executed: true,
  manifest_path: "C:\\cp\\1700000000000-delete.json",
  moves: [
    { session_id: "a", source: "C:\\store\\a.jsonl", destination: "C:\\cp\\deleted\\a.jsonl" },
  ],
  unaccounted: [],
  ...over,
});

/**
 * A rejection shaped as the app really receives one: the Rust bridge flattens the JSON-RPC
 * envelope into a STRING (`sidecar.rs`'s `rpc error (id N): {...}`), which is why
 * `rpcErrorCode` has to parse it back out. Constructing anything tidier would test a
 * transport that does not exist.
 */
const rpcError = (code: number, message: string): Error =>
  new Error(`rpc error (id 3): {"code":${code},"message":"${message}"}`);

const fakeIpc = (over: Partial<MaintenanceIpc> = {}): MaintenanceIpc => ({
  maintenancePlan: vi.fn(async () => preview()),
  maintenanceExecute: vi.fn(async () => result()),
  maintenanceRestore: vi.fn(async () => result()),
  maintenanceRuns: vi.fn(async () => [] as MaintenanceRun[]),
  ...over,
});

/** A panel plus the list of every view it emitted, in order. */
function harness(ipc: MaintenanceIpc = fakeIpc()) {
  const views: MaintenanceView[] = [];
  const panel = new MaintenancePanel(ipc, (v) => views.push(v));
  return { panel, views, ipc };
}

const sources = (rows: MoveRow[]): string[] => rows.map((r) => r.source);

/**
 * The last view a panel emitted.
 *
 * Indexed rather than `views.at(-1)`: `Array.prototype.at` needs `lib: es2022` and this
 * project targets earlier, so `.at` type-checks nowhere here. vitest transforms without
 * type-checking, so it ran green and only `tsc --noEmit` caught it.
 */
const last = (views: MaintenanceView[]): MaintenanceView | undefined =>
  views[views.length - 1];

// ------------------------------------------------------------ derivePlanView

describe("derivePlanView", () => {
  it("PRESERVES engine order instead of sorting it", () => {
    // The moves are a SEQUENCE, not a set: the engine performs them in this order and the
    // checkpoint records that order, so a tidier sorted render would misstate both what
    // happens and how a crash would leave things half-done.
    const view = derivePlanView(
      preview({
        plan: [
          { session_id: "z", source: "C:\\store\\z.jsonl", destination: "C:\\cp\\deleted\\z.jsonl" },
          { session_id: "a", source: "C:\\store\\a.jsonl", destination: "C:\\cp\\deleted\\a.jsonl" },
        ],
      }),
    );
    expect(sources(view.moves)).toEqual(["C:\\store\\z.jsonl", "C:\\store\\a.jsonl"]);
  });

  it("does NOT treat a healthy plan's own warnings as something wrong", () => {
    // The default fixture is a clean 2-file plan and it still carries 3 warnings. A UI keyed
    // on `warnings.length > 0` would flag every plan it ever saw.
    const view = derivePlanView(preview());
    expect(view.dangerousWarnings).toHaveLength(2);
    expect(view.infoWarnings).toHaveLength(1);
    expect(view.needsAttention).toBe(false);
  });

  it("DOES need attention when a target was refused", () => {
    const view = derivePlanView(preview({ blocked: [blockedTarget()] }));
    expect(view.needsAttention).toBe(true);
  });

  it("DOES need attention on a REVIEW warning, with no message matching involved", () => {
    const view = derivePlanView(
      preview({ warnings: [warn("REVIEW", "Target is hot (being written): C:\\store\\a.jsonl")] }),
    );
    expect(view.reviewWarnings).toHaveLength(1);
    expect(view.needsAttention).toBe(true);
  });

  it("SURFACES every refused target with its reason and detail", () => {
    // Filtering these out would let the operator confirm a count that does not describe
    // reality — the same lie class as a transcript that hides the lines it could not parse.
    const view = derivePlanView(preview({ blocked: [blockedTarget()] }));
    expect(view.blockedCount).toBe(1);
    expect(view.blocked[0].reason).toBe("protected");
    expect(view.blocked[0].label).toContain("live.jsonl");
    expect(view.blocked[0].label).toContain("protected store path");
  });

  it("collapses a refusal label when the engine gave no detail", () => {
    const view = derivePlanView(preview({ blocked: [blockedTarget({ detail: "  " })] }));
    expect(view.blocked[0].label).toBe("C:\\store\\.codex\\sessions\\live.jsonl — protected");
  });

  it("reports the EFFECTIVE destination and says a delete is not an erase", () => {
    // `destination_root` on a delete is `<checkpoint_root>/deleted`, not whatever the caller
    // asked for. An operator who believes delete means "gone forever" will either avoid a
    // safe action or panic after taking it.
    const view = derivePlanView(preview());
    expect(view.destinationRoot).toBe("C:\\cp\\deleted");
    expect(view.destinationExplanation).toContain("Nothing is erased");
    expect(view.destinationExplanation).toContain("C:\\cp\\deleted");
    expect(view.destinationExplanation).toContain("C:\\cp");
  });

  it("explains a non-delete action without claiming nothing is erased", () => {
    const view = derivePlanView(
      preview({ action: "archive", destination_root: "C:\\arch", required_typed_confirmation: "ARCHIVE 2 FILES" }),
    );
    expect(view.destinationExplanation).toContain("C:\\arch");
    expect(view.destinationExplanation).not.toContain("Nothing is erased");
    expect(view.headline).toBe("Archive 2 files");
  });

  it("echoes the required phrase verbatim rather than rebuilding it", () => {
    // The engine derives it from the ALLOWED count, so a plan that changed since the operator
    // last looked changes the phrase. Reconstructing it client-side would defeat that.
    const view = derivePlanView(preview({ required_typed_confirmation: "DELETE 2 FILES" }));
    expect(view.requiredConfirmation).toBe("DELETE 2 FILES");
  });

  it("totals the bytes and counts the files being written right now", () => {
    const view = derivePlanView(
      preview({ allowed: [copy({ size_bytes: 2048, is_hot: true }), copy({ size_bytes: 1024 })] }),
    );
    expect(view.totalBytes).toBe(3072);
    expect(view.totalBytesLabel).toBe("3.0 KB");
    expect(view.hotCount).toBe(1);
  });

  it("says 'file' not 'files' for one, and names the refused count", () => {
    const view = derivePlanView(
      preview({ allowed: [copy()], plan: [], blocked: [blockedTarget()] }),
    );
    expect(view.headline).toBe("Delete 1 file (1 refused)");
  });
});

// --------------------------------------------------------- confirmationState

describe("confirmationState", () => {
  const plan = derivePlanView(preview());

  it("accepts only the exact phrase", () => {
    expect(confirmationState(plan, "DELETE 2 FILES")).toBe("match");
  });

  it("does NOT trim, because the engine does not either", () => {
    // The engine compares the raw strings. A UI that trimmed would enable its own button for
    // a trailing space and then eat a server refusal, which reads as the app being broken
    // rather than as the operator's stray keystroke.
    expect(confirmationState(plan, "DELETE 2 FILES ")).toBe("mismatch");
    expect(confirmationState(plan, " DELETE 2 FILES")).toBe("mismatch");
  });

  it("is case-sensitive", () => {
    expect(confirmationState(plan, "delete 2 files")).toBe("mismatch");
  });

  it("distinguishes empty from wrong, because they need different prompts", () => {
    expect(confirmationState(plan, "")).toBe("empty");
    expect(confirmationState(plan, "   ")).toBe("empty");
    expect(confirmationState(plan, "DELETE 3 FILES")).toBe("mismatch");
  });
});

// ------------------------------------------------------------ classifyFailure

describe("classifyFailure", () => {
  it("reads a SPENT plan as expired and sends the operator to re-plan", () => {
    const f = classifyFailure(
      rpcError(-32003, "unknown or already-used plan_id 'plan-1'; re-plan"),
      "execute",
    );
    expect(f.kind).toBe("plan-expired");
    expect(f.remedy).toBe("re-plan");
    expect(f.code).toBe(-32003);
    expect(f.message).toContain("single-use");
  });

  it("reads any OTHER refusal as correctable, because the plan survives it", () => {
    // The engine deletes the handle AFTER the call that raises, so a refused confirmation
    // leaves the plan usable — its own comment calls that deliberate. Collapsing this into
    // "expired, re-plan" would throw away a live plan over a typo.
    const f = classifyFailure(
      rpcError(-32003, "Typed confirmation does not match the preview."),
      "execute",
    );
    expect(f.kind).toBe("refused");
    expect(f.remedy).toBe("correct-and-retry");
    expect(f.message).toContain("still open");
    expect(f.message).toContain("does not match the preview");
  });

  it("keeps the engine's own words in every branch", () => {
    // A future engine refusal nobody anticipated must still reach the operator intact.
    const f = classifyFailure(rpcError(-32003, "refusing to write into a protected store path"), "execute");
    expect(f.message).toContain("protected store path");
  });

  it("maps a params fault to fix-input", () => {
    const f = classifyFailure(rpcError(-32602, "store_root must be a string"), "plan");
    expect(f.kind).toBe("bad-params");
    expect(f.remedy).toBe("fix-input");
  });

  it("maps a detached corpus to open-corpus without echoing the RPC name", () => {
    const f = classifyFailure(rpcError(-32000, "corpus not indexed"), "plan");
    expect(f.kind).toBe("no-corpus");
    expect(f.remedy).toBe("open-corpus");
    expect(f.message).not.toContain("open_corpus");
  });

  it("names an unreadable manifest ON RESTORE, where -32603 means a missing file", () => {
    // `read_checkpoint` opens the path directly, so a bad one raises FileNotFoundError and
    // escapes the refusal mapping entirely — it lands as INTERNAL_ERROR, not REFUSED.
    const f = classifyFailure(
      rpcError(-32603, "Internal error"),
      "restore",
    );
    expect(f.kind).toBe("manifest-unreadable");
    expect(f.remedy).toBe("fix-input");
  });

  it("does NOT claim an unreadable manifest for the same code on another stage", () => {
    const f = classifyFailure(rpcError(-32603, "Internal error"), "execute");
    expect(f.kind).toBe("failed");
  });

  it("treats an unclassifiable rejection as a failure, never as success", () => {
    const f = classifyFailure(new Error("engine mutex poisoned"), "execute");
    expect(f.kind).toBe("failed");
    expect(f.code).toBeNull();
    expect(f.message).toBe("engine mutex poisoned");
  });

  it("handles a thrown non-Error", () => {
    expect(classifyFailure("plain string blew up", "runs").message).toBe("plain string blew up");
  });
});

// -------------------------------------------------------------- deriveOutcome

describe("deriveOutcome", () => {
  it("offers a real undo when a checkpoint was written", () => {
    const out = deriveOutcome(result(), "delete");
    expect(out.executed).toBe(true);
    expect(out.canUndo).toBe(true);
    expect(out.undoHint).toContain("C:\\cp\\1700000000000-delete.json");
    expect(out.headline).toBe("Delete complete: 1 file moved.");
  });

  it("offers NO undo on a dry run, and says nothing changed", () => {
    const out = deriveOutcome(result({ executed: false }), "delete");
    expect(out.canUndo).toBe(false);
    expect(out.headline).toContain("Nothing changed");
    expect(out.undoHint).toContain("dry run");
  });

  it("offers NO undo when an applied run wrote no manifest", () => {
    // `manifest_path` is `""` and not null, so truthiness is the correct test — and this is a
    // DIFFERENT case from the dry run: it really did execute, it just cannot be rolled back.
    const out = deriveOutcome(result({ manifest_path: "" }), "delete");
    expect(out.executed).toBe(true);
    expect(out.canUndo).toBe(false);
    expect(out.undoHint).toContain("cannot be undone");
  });

  it("surfaces what a restore could not account for", () => {
    const out = deriveOutcome(
      result({ unaccounted: ["C:\\store\\gone.jsonl"] }),
      null,
    );
    expect(out.unaccounted).toEqual(["C:\\store\\gone.jsonl"]);
    expect(out.headline).toContain("Restore complete");
  });
});

// -------------------------------------------------------- manifestPathProblem

describe("manifestPathProblem", () => {
  it("accepts a drive-absolute and a POSIX-absolute path", () => {
    expect(manifestPathProblem("C:\\cp\\run.json")).toBeNull();
    expect(manifestPathProblem("/var/cp/run.json")).toBeNull();
  });

  it("refuses a UNC path and says WHY, in terms of the real risk", () => {
    // Merely resolving \\host\share on Windows initiates an outbound SMB/NTLM auth. "Invalid
    // path" would be a lie: the path is well-formed, it is the network hop that is refused.
    for (const bad of ["\\\\evil\\share\\run.json", "//evil/share/run.json"]) {
      expect(manifestPathProblem(bad)).toContain("credentials");
    }
  });

  it("refuses a relative path and an empty one, differently", () => {
    expect(manifestPathProblem("cp\\run.json")).toContain("full path");
    expect(manifestPathProblem("   ")).toContain("Enter the path");
  });
});

// ------------------------------------------------------------------ deriveRuns

describe("deriveRuns", () => {
  const run = (over: Partial<MaintenanceRun> = {}): MaintenanceRun => ({
    manifest_path: "C:\\cp\\200-delete.json",
    action: "delete",
    status: "executed",
    recorded_at_ms: 1_700_000_000_000,
    moved_count: 2,
    blocked_count: 1,
    store_root: "C:\\store",
    ...over,
  });

  it("keeps the engine's newest-first order and formats the time in UTC", () => {
    const rows = deriveRuns([run({ manifest_path: "b" }), run({ manifest_path: "a" })]);
    expect(rows.map((r) => r.manifestPath)).toEqual(["b", "a"]);
    expect(rows[0].when).toBe("2023-11-14 22:13");
    expect(rows[0].label).toContain("2 moved");
  });

  it("does not offer to restore a manifest already restored", () => {
    // A second restore refuses outright, so the affordance would promise a guaranteed failure.
    expect(deriveRuns([run({ status: "restored" })])[0].canRestore).toBe(false);
    expect(deriveRuns([run({ status: "executed" })])[0].canRestore).toBe(true);
    expect(deriveRuns([run({ status: "pending" })])[0].canRestore).toBe(true);
  });

  it("tolerates a non-finite timestamp rather than rendering 'Invalid Date'", () => {
    expect(deriveRuns([run({ recorded_at_ms: Number.NaN })])[0].when).toBe("");
  });
});

// ----------------------------------------------------- renderMaintenanceView

describe("renderMaintenanceView", () => {
  it("renders SOMETHING for every view kind", () => {
    const plan = derivePlanView(preview());
    const outcome = deriveOutcome(result(), "delete");
    const views: MaintenanceView[] = [
      { kind: "idle" },
      { kind: "planning" },
      { kind: "planned", plan },
      { kind: "executing", plan },
      { kind: "done", plan, outcome },
      { kind: "restoring", manifestPath: "C:\\cp\\x.json" },
      { kind: "restored", outcome },
      {
        kind: "error",
        stage: "plan",
        failure: { kind: "failed", message: "boom", remedy: "none", code: null },
        plan: null,
      },
    ];
    for (const view of views) {
      const lines = renderMaintenanceView(view);
      expect(lines.length, `${view.kind} rendered nothing`).toBeGreaterThan(0);
      expect(lines.every((l) => l !== ""), `${view.kind} rendered a blank line`).toBe(true);
    }
  });

  it("shows the refusals BEFORE the confirmation prompt", () => {
    // Order is the point: the operator has to see what the engine declined while deciding,
    // not after committing.
    const lines = renderMaintenanceView({
      kind: "planned",
      plan: derivePlanView(preview({ blocked: [blockedTarget()] })),
    });
    const refused = lines.findIndex((l) => l.startsWith("refused:"));
    const prompt = lines.findIndex((l) => l.startsWith("Type "));
    expect(refused).toBeGreaterThanOrEqual(0);
    expect(prompt).toBeGreaterThan(refused);
  });

  it("names EVERY stage on an error, so a restore fault is not read as a delete fault", () => {
    // Walked exhaustively rather than spot-checked: a new stage added to the union without a
    // label here would otherwise render "undefined failed: …" only on the path nobody tried.
    const stages: MaintenanceStage[] = ["plan", "execute", "restore", "runs"];
    const seen = new Set<string>();
    for (const stage of stages) {
      const [line] = renderMaintenanceView({
        kind: "error",
        stage,
        failure: { kind: "failed", message: "boom", remedy: "none", code: null },
        plan: null,
      });
      expect(line, `${stage} rendered no label`).toMatch(/^\S.* failed: boom$/);
      expect(line.toLowerCase()).not.toContain("undefined");
      seen.add(line);
    }
    // Distinct labels, or two different faults read as the same one.
    expect(seen.size).toBe(stages.length);
    expect(
      renderMaintenanceView({
        kind: "error",
        stage: "restore",
        failure: { kind: "failed", message: "boom", remedy: "none", code: null },
        plan: null,
      })[0],
    ).toContain("Restore failed");
  });

  it("mentions hot files and review warnings in a planned render", () => {
    const lines = renderMaintenanceView({
      kind: "planned",
      plan: derivePlanView(
        preview({
          allowed: [copy({ is_hot: true })],
          warnings: [warn("REVIEW", "Target is hot (being written): C:\\store\\a.jsonl")],
        }),
      ),
    });
    expect(lines.some((l) => l.includes("being written right now"))).toBe(true);
    expect(lines.some((l) => l.startsWith("review:"))).toBe(true);
  });

  it("lists what an outcome moved and what it could not account for", () => {
    const lines = renderMaintenanceView({
      kind: "restored",
      outcome: deriveOutcome(result({ unaccounted: ["C:\\store\\gone.jsonl"] }), null),
    });
    expect(lines.some((l) => l.startsWith("moved:"))).toBe(true);
    expect(lines.some((l) => l.includes("could not be accounted for"))).toBe(true);
  });
});

// -------------------------------------------------------------- the controller

describe("MaintenancePanel", () => {
  const planParams = {
    store_root: "C:\\store",
    checkpoint_root: "C:\\cp",
    action: "delete" as const,
    targets: [{ file_path: "C:\\store\\a.jsonl" }],
  };

  it("plans, then holds the plan for confirmation", async () => {
    const { panel, views } = harness();
    await panel.planMaintenance(planParams);
    expect(views.map((v) => v.kind)).toEqual(["planning", "planned"]);
    expect(panel.pendingPlan?.planId).toBe("plan-1");
  });

  it("makes NO CALL when asked to execute with no plan", async () => {
    // The load-bearing assertion is the call count, not the message: a panel that errored
    // AND sent the request would satisfy a message-only check.
    const { panel, views, ipc } = harness();
    await panel.execute("DELETE 2 FILES");
    expect(ipc.maintenanceExecute).not.toHaveBeenCalled();
    const latest = last(views);
    expect(latest?.kind).toBe("error");
    expect(renderMaintenanceView(latest as MaintenanceView)[0]).toContain("no plan to run");
  });

  it("makes NO CALL when the confirmation does not match exactly", async () => {
    const { panel, views, ipc } = harness();
    await panel.planMaintenance(planParams);
    await panel.execute("DELETE 2 FILES ");
    expect(ipc.maintenanceExecute).not.toHaveBeenCalled();
    expect(last(views)?.kind).toBe("error");
    // ...and the plan is still there, so the operator just retypes.
    expect(panel.pendingPlan?.planId).toBe("plan-1");
  });

  it("applies EXPLICITLY, passing the server's own plan id and apply:true", async () => {
    const { panel, ipc } = harness();
    await panel.planMaintenance(planParams);
    await panel.execute("DELETE 2 FILES");
    expect(ipc.maintenanceExecute).toHaveBeenCalledWith({
      plan_id: "plan-1",
      confirmation: "DELETE 2 FILES",
      apply: true,
    });
    // The engine consumed the handle, so the panel must not keep offering it.
    expect(panel.pendingPlan).toBeNull();
  });

  it("DROPS an expired plan so no button stays wired to a dead handle", async () => {
    const ipc = fakeIpc({
      maintenanceExecute: vi.fn(async () => {
        throw rpcError(-32003, "unknown or already-used plan_id 'plan-1'; re-plan");
      }),
    });
    const { panel, views } = harness(ipc);
    await panel.planMaintenance(planParams);
    await panel.execute("DELETE 2 FILES");
    const latest = last(views);
    expect(latest?.kind).toBe("error");
    expect(panel.pendingPlan).toBeNull();
    if (latest?.kind === "error") {
      expect(latest.failure.remedy).toBe("re-plan");
      expect(latest.plan).toBeNull();
    }
  });

  it("KEEPS a plan the engine merely refused, so a typo costs no re-plan", async () => {
    const ipc = fakeIpc({
      maintenanceExecute: vi.fn(async () => {
        throw rpcError(-32003, "Typed confirmation does not match the preview.");
      }),
    });
    const { panel, views } = harness(ipc);
    await panel.planMaintenance(planParams);
    await panel.execute("DELETE 2 FILES");
    const latest = last(views);
    expect(panel.pendingPlan?.planId).toBe("plan-1");
    if (latest?.kind === "error") {
      expect(latest.failure.remedy).toBe("correct-and-retry");
      expect(latest.plan?.planId).toBe("plan-1");
    }
  });

  it("reports a plan failure without inventing a plan", async () => {
    const ipc = fakeIpc({
      maintenancePlan: vi.fn(async () => {
        throw rpcError(-32602, "store_root must be a string");
      }),
    });
    const { panel, views } = harness(ipc);
    await panel.planMaintenance(planParams);
    expect(last(views)?.kind).toBe("error");
    expect(panel.pendingPlan).toBeNull();
  });

  it("restores as a DRY RUN unless told otherwise", async () => {
    const { panel, ipc } = harness();
    await panel.restore("C:\\cp\\run.json");
    expect(ipc.maintenanceRestore).toHaveBeenCalledWith({
      manifest_path: "C:\\cp\\run.json",
      apply: false,
      skip_unaccounted: false,
    });
  });

  it("passes apply and skip_unaccounted through when asked", async () => {
    const { panel, ipc } = harness();
    await panel.restore("C:\\cp\\run.json", { apply: true, skipUnaccounted: true });
    expect(ipc.maintenanceRestore).toHaveBeenCalledWith({
      manifest_path: "C:\\cp\\run.json",
      apply: true,
      skip_unaccounted: true,
    });
    expect(panel.current.kind).toBe("restored");
  });

  it("makes NO CALL for a UNC or relative manifest path", async () => {
    const { panel, ipc } = harness();
    await panel.restore("\\\\evil\\share\\run.json", { apply: true });
    await panel.restore("run.json", { apply: true });
    expect(ipc.maintenanceRestore).not.toHaveBeenCalled();
    expect(panel.current.kind).toBe("error");
  });

  it("classifies a restore failure with the restore stage", async () => {
    const ipc = fakeIpc({
      maintenanceRestore: vi.fn(async () => {
        throw rpcError(-32603, "Internal error");
      }),
    });
    const { panel } = harness(ipc);
    await panel.restore("C:\\cp\\gone.json", { apply: true });
    const view = panel.current;
    expect(view.kind).toBe("error");
    if (view.kind === "error") expect(view.failure.kind).toBe("manifest-unreadable");
  });

  it("lists runs, and returns an empty list rather than throwing on failure", async () => {
    const rows = await harness().panel.listRuns(5);
    expect(rows).toEqual([]);

    const ipc = fakeIpc({
      maintenanceRuns: vi.fn(async () => {
        throw rpcError(-32000, "corpus not indexed");
      }),
    });
    const { panel, views } = harness(ipc);
    expect(await panel.listRuns()).toEqual([]);
    expect(last(views)?.kind).toBe("error");
  });

  it("is single-flight: a second call while one is in flight is ignored", async () => {
    let release: (() => void) | null = null;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const ipc = fakeIpc({
      maintenancePlan: vi.fn(async () => {
        await gate;
        return preview();
      }),
    });
    const { panel } = harness(ipc);
    const first = panel.planMaintenance(planParams);
    await panel.planMaintenance(planParams); // ignored: still busy
    (release as unknown as () => void)();
    await first;
    expect(ipc.maintenancePlan).toHaveBeenCalledTimes(1);
  });

  it("refuses to execute or restore while busy", async () => {
    let release: (() => void) | null = null;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const ipc = fakeIpc({
      maintenanceExecute: vi.fn(async () => {
        await gate;
        return result();
      }),
    });
    const { panel } = harness(ipc);
    await panel.planMaintenance(planParams);
    const first = panel.execute("DELETE 2 FILES");
    await panel.execute("DELETE 2 FILES"); // ignored
    await panel.restore("C:\\cp\\run.json"); // ignored
    (release as unknown as () => void)();
    await first;
    expect(ipc.maintenanceExecute).toHaveBeenCalledTimes(1);
    expect(ipc.maintenanceRestore).not.toHaveBeenCalled();
  });

  it("resets to idle, dropping the held plan", async () => {
    const { panel } = harness();
    await panel.planMaintenance(planParams);
    panel.reset();
    expect(panel.current.kind).toBe("idle");
    expect(panel.pendingPlan).toBeNull();
  });
});
