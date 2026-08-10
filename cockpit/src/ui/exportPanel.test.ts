import { describe, expect, it } from "vitest";

import { CREDENTIAL_SHAPE_COVERAGE_LIMIT } from "../ipc/mock";
import type { CredentialScan, ExportMode, ExportPlan } from "../ipc/types";
import {
  asExportMode,
  derivePreview,
  exportIpcFrom,
  deriveVerdict,
  ExportPanel,
  formatBytes,
  modeLabel,
  renderView,
  type ExportIpc,
  type ExportRunResult,
  type ExportView,
} from "./exportPanel";

// ---------------------------------------------------------------------------
// fixtures
// ---------------------------------------------------------------------------

/**
 * The G-5 scan every export result now carries. `coverage_limit` is imported rather than
 * retyped: it is the field that stops "no findings" reading as "safe to share", and a
 * fixture that invented its own wording would be the one place this suite could quietly
 * disagree with the wire it is standing in for.
 */
function scanFixture(over: Partial<CredentialScan> = {}): CredentialScan {
  return {
    findings: [],
    coverage_limit: CREDENTIAL_SHAPE_COVERAGE_LIMIT,
    scrubbed: false,
    ...over,
  };
}

function planFixture(over: Partial<ExportPlan> = {}): ExportPlan {
  return {
    node_count: 16,
    edge_count: 20,
    conversation_count: 15,
    est_bytes: 3_400_000,
    mode: "full",
    credential_scan: scanFixture(),
    ...over,
  };
}

/**
 * A run result with the privacy fields filled in. They are REQUIRED on the wire but
 * orthogonal to what most cases below assert — a verdict is derived from the gates, not
 * from `mode`/`credential_scan` — so defaulting them here keeps each case about its own
 * subject instead of restating six fields to test two.
 */
function runResult(over: Partial<ExportRunResult> = {}): ExportRunResult {
  return {
    ok: true,
    graph_gate: true,
    transcript_gate: true,
    mode: "full",
    credential_scan: scanFixture(),
    ...over,
  };
}

/** A blocked run-result carrying the full round-trip delta + a missing-token shortfall. */
function blockedFull(): ExportRunResult {
  return runResult({
    ok: false,
    graph_gate: false,
    transcript_gate: false,
    // Deliberately UNSORTED so the derivation's own sort is exercised, not the wire's.
    diff: {
      added_nodes: ["n2", "n1"],
      removed_nodes: ["r1"],
      added_edges: [
        { parent: "b", child: "c" },
        { parent: "a", child: "z" },
        { parent: "a", child: "c" },
      ],
      removed_edges: [{ parent: "x", child: "y" }],
      changed_nodes: {
        z1: { title: ["old", "new"], tokens: [10, 20] },
        a1: { provider: ["claude", "codex"] },
      },
    },
    missing_tokens: ["b", "a"],
  });
}

/** A minimal deferred so a still-pending IPC call can be held open mid-test. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function fakeIpc(over: Partial<ExportIpc> = {}): ExportIpc {
  return {
    exportPlan: async () => planFixture(),
    exportRun: async (): Promise<ExportRunResult> =>
      runResult({
        ok: true,
        graph_gate: true,
        transcript_gate: true,
        written_path: "/out/export.json",
      }),
    ...over,
  };
}

// ---------------------------------------------------------------------------
// G-5 / G-6: the privacy plane, reachable from the panel
// ---------------------------------------------------------------------------

describe("the export privacy plane", () => {
  const FINDING = {
    shape: "api-key",
    offset: 12,
    preview: "sk-a… (34 chars)",
    scope: "thread" as const,
    id: "t-7",
    field: "preview",
  };

  it("carries the mode and the scan onto the preview", () => {
    const preview = derivePreview(planFixture({ mode: "shareable" }));
    expect(preview.mode).toBe("shareable");
    expect(preview.scan.coverageLimit).toBe(CREDENTIAL_SHAPE_COVERAGE_LIMIT);
  });

  it("RENDERS the coverage limit even when there are ZERO findings", () => {
    // The load-bearing case. An empty findings list is not a safety verdict, and the sentence
    // is the only thing that says so. A clean scan is exactly when a reader is most likely to
    // conclude "nothing to worry about", so this is where the limit matters most.
    const lines = renderView({ kind: "planned", preview: derivePreview(planFixture()) });
    expect(lines.join("\n")).toContain(CREDENTIAL_SHAPE_COVERAGE_LIMIT);
    expect(lines.some((l) => /no credential shapes/i.test(l))).toBe(true);
  });

  it("lists each finding LOCATED and MASKED, never echoing the run", () => {
    const preview = derivePreview(
      planFixture({ credential_scan: scanFixture({ findings: [FINDING] }) }),
    );
    expect(preview.scan.findings).toEqual(["api-key in thread t-7 field preview: sk-a… (34 chars)"]);
    // The whole point of the mask: a report is rendered, logged and pasted into bug threads,
    // so a finding that echoed the secret to prove it found one would be the worst possible bug.
    expect(preview.scan.findings.join()).not.toContain("sk-abcdefghijklmnop");
    const lines = renderView({ kind: "planned", preview });
    expect(lines.join("\n")).toContain("api-key in thread t-7");
    expect(lines.join("\n")).toContain(CREDENTIAL_SHAPE_COVERAGE_LIMIT);
  });

  it("describes what shareable ACTUALLY strips, without overpromising", () => {
    // The engine relativizes cwd/rollout_path and drops preview. It does NOT strip `title`,
    // `git_branch` or `agent_nickname` — and a Codex title is `_first_line(first_user, 80)`,
    // so for a coding corpus the title frequently IS a path. Labelling this "anonymised"
    // would be a lie the UI tells on the engine's behalf.
    const label = modeLabel("shareable");
    expect(label).toMatch(/preview/i);
    expect(label).toMatch(/~/);
    expect(label).not.toMatch(/anonymi[sz]ed|safe to share|removes all/i);
    expect(modeLabel("full")).toMatch(/every field|archive of record/i);

    // UPDATED FOR CF-23 (868a033). The first version of this label said title and git branch
    // are "NOT stripped". That was true when written and the engine has since closed it —
    // `shareable_thread` now runs both through `scrub_home_mentions` — so the warning became
    // inaccurate in the SAFE direction, which is still inaccurate. A label that overstates
    // the risk trains the reader to discount it.
    expect(label).toMatch(/home/i); // says WHAT is scrubbed out of the prose fields
    expect(label).not.toMatch(/title.{0,30}NOT stripped/i);
    // POSITIVE, not merely the negation above. The label must NAME the two prose fields it
    // now scrubs. Without these two lines a mutant that silently drops "out of the title and
    // git branch" keeps every other assertion in this case green — measured, it SURVIVED.
    // /home/i alone is satisfied by the unrelated "only HOME paths" clause further down.
    expect(label).toMatch(/title/i);
    expect(label).toMatch(/branch/i);
    // ...and it still names the two real residuals, because both survive: only the home ROOT
    // is substituted, and agent role/nickname are untouched by construction.
    expect(label).toMatch(/nickname/i);
    expect(label).toMatch(/D:|non-home|only home/i);
  });

  it("surfaces the scan on the verdict too, INCLUDING a blocked run", () => {
    // Forwarded on failure deliberately: a blocked export is exactly when the user is about
    // to retry, and dropping the warning there would make them retry blind.
    const blocked = deriveVerdict(
      runResult({ ok: false, graph_gate: false, credential_scan: scanFixture({ findings: [FINDING] }) }),
    );
    expect(blocked.scan.findings).toHaveLength(1);
    const lines = renderView({ kind: "done", preview: null, verdict: blocked });
    expect(lines.join("\n")).toContain(CREDENTIAL_SHAPE_COVERAGE_LIMIT);
  });

  it("distinguishes a scrub that REPLACED something from one that matched nothing", () => {
    // The first draft of this case asserted /replaced/ on a scrub with ZERO findings, which
    // was wrong: nothing was replaced, and saying so would be the opposite lie. Both facts
    // matter to someone about to hand the file over, so both are asserted.
    const nothingToDo = deriveVerdict(runResult({ credential_scan: scanFixture({ scrubbed: true }) }));
    expect(nothingToDo.scan.scrubbed).toBe(true);
    expect(nothingToDo.scan.headline).toMatch(/scrub was ON/i);
    expect(nothingToDo.scan.headline).not.toMatch(/REPLACED/);

    const replaced = deriveVerdict(
      runResult({ credential_scan: scanFixture({ scrubbed: true, findings: [FINDING] }) }),
    );
    expect(replaced.scan.headline).toMatch(/REPLACED in the written bytes/);
    expect(renderView({ kind: "done", preview: null, verdict: replaced }).join("\n"))
      .toMatch(/REPLACED/);
  });

  it("ADAPTS a client into ExportIpc while preserving mode and scrub", async () => {
    // This exists because a mutation proved it was needed. Reverting app.ts's wiring to
    // `exportRun: (destPath) => ipc.exportRun!(destPath)` — the exact CF-17 defect, one layer
    // up — left the whole suite GREEN, because app.ts has no tests and cannot get them here.
    // The adapter shaping is the only part of that wiring with behaviour, so it moves into a
    // function this suite can hold. What remains untested in app.ts is DOM plumbing.
    const seen: unknown[] = [];
    const client = {
      exportPlan: async (dest?: string, mode?: ExportMode) => {
        seen.push(["plan", dest, mode]);
        return planFixture();
      },
      exportRun: async (destPath: string, mode?: ExportMode, scrub?: boolean) => {
        seen.push(["run", destPath, mode, scrub]);
        return runResult();
      },
    };
    const adapted = exportIpcFrom(client);
    expect(adapted).not.toBeNull();
    await adapted!.exportPlan("/d", "shareable");
    await adapted!.exportRun("/d", "shareable", true);
    expect(seen).toEqual([
      ["plan", "/d", "shareable"],
      ["run", "/d", "shareable", true],
    ]);
  });

  it("returns null when the engine does not offer the export methods", () => {
    // app.ts paints "Export unavailable" on this; the decision is logic, so it is tested here
    // rather than left to an `if` nothing exercises.
    expect(exportIpcFrom({})).toBeNull();
    expect(exportIpcFrom({ exportPlan: async () => planFixture() })).toBeNull();
    expect(exportIpcFrom({ exportRun: async () => runResult() })).toBeNull();
  });

  it("narrows a raw select value to a mode, defaulting to the harmless one", () => {
    // Exists as a function purely so app.ts's one piece of wiring LOGIC has a test — the same
    // reason `searchPresent.searchParams` exists. `app.ts` itself cannot be unit-tested here
    // (constructing CockpitApp needs a canvas 2D context and a Worker, and vitest runs on
    // node), so the choice is between extracting this and leaving it unexercised.
    expect(asExportMode("shareable")).toBe("shareable");
    expect(asExportMode("full")).toBe("full");
    // ANYTHING ELSE IS `full`. A select that lost its options, a stale value, or a typo must
    // not silently downgrade the archive of record into a lossy projection — the safe default
    // is the one that changes nothing about the bytes.
    for (const junk of ["", "SHAREABLE", "share", "undefined", "null"]) {
      expect(asExportMode(junk)).toBe("full");
    }
  });

  it("FORWARDS the chosen mode to export.plan", async () => {
    let seen: [string | undefined, string | undefined] = ["UNSET", "UNSET"];
    const panel = new ExportPanel(
      fakeIpc({
        exportPlan: async (dest, mode) => {
          seen = [dest, mode];
          return planFixture({ mode: mode ?? "full" });
        },
      }),
      () => {},
    );
    await panel.plan("/out/x.json", "shareable");
    expect(seen).toEqual(["/out/x.json", "shareable"]);
  });

  it("FORWARDS mode and the scrub opt-in to export.run", async () => {
    let seen: unknown[] = [];
    const panel = new ExportPanel(
      fakeIpc({
        exportRun: async (destPath, mode, scrub) => {
          seen = [destPath, mode, scrub];
          return runResult({ written_path: destPath });
        },
      }),
      () => {},
    );
    await panel.run("/out/x.json", "shareable", true);
    expect(seen).toEqual(["/out/x.json", "shareable", true]);
  });

  it("defaults to WARN-ONLY: no scrub unless the caller opts in", async () => {
    // G-5's rule. The archive must not be altered behind the owner's back, so the absence of
    // a choice is "warn", never "quietly rewrite".
    let seenScrub: unknown = "UNSET";
    const panel = new ExportPanel(
      fakeIpc({
        exportRun: async (destPath, _mode, scrub) => {
          seenScrub = scrub;
          return runResult({ written_path: destPath });
        },
      }),
      () => {},
    );
    await panel.run("/out/x.json");
    expect(seenScrub).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// formatBytes
// ---------------------------------------------------------------------------

describe("formatBytes", () => {
  it("keeps sub-KB values in raw bytes", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1023)).toBe("1023 B");
  });

  it("scales into KB/MB with one decimal", () => {
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(812_345)).toBe("793.3 KB");
    expect(formatBytes(2_000_000)).toBe("1.9 MB");
  });

  it("caps at the largest unit for very large sizes", () => {
    // 1024**5 exceeds TB (1024**4); the loop stops at TB rather than inventing a unit.
    expect(formatBytes(1024 ** 5)).toBe("1024.0 TB");
  });
});

// ---------------------------------------------------------------------------
// derivePreview
// ---------------------------------------------------------------------------

describe("derivePreview", () => {
  it("maps an ExportPlan to a render-ready preview with a humanized size", () => {
    const preview = derivePreview(planFixture());
    expect(preview).toEqual({
      nodeCount: 16,
      edgeCount: 20,
      conversationCount: 15,
      estBytes: 3_400_000,
      mode: "full",
      scan: {
        findings: [],
        coverageLimit: CREDENTIAL_SHAPE_COVERAGE_LIMIT,
        scrubbed: false,
        headline: "No credential shapes found — read the coverage limit below before sharing.",
      },
      estBytesLabel: "3.2 MB",
      summary: "16 nodes · 20 edges · 15 conversations · ~3.2 MB",
    });
  });
});

// ---------------------------------------------------------------------------
// deriveVerdict
// ---------------------------------------------------------------------------

describe("deriveVerdict", () => {
  it("reports a written path on a successful export", () => {
    const verdict = deriveVerdict(runResult({
      ok: true,
      graph_gate: true,
      transcript_gate: true,
      written_path: "C:/out/export.json",
    }));
    expect(verdict).toEqual({
      status: "ok",
      graphGate: true,
      transcriptGate: true,
      writtenPath: "C:/out/export.json",
      headline: "Export written to C:/out/export.json",
      scan: {
        findings: [],
        coverageLimit: CREDENTIAL_SHAPE_COVERAGE_LIMIT,
        scrubbed: false,
        headline: "No credential shapes found — read the coverage limit below before sharing.",
      },
    });
  });

  it("falls back to a generic headline when ok but no path is echoed", () => {
    const verdict = deriveVerdict(runResult({ ok: true, graph_gate: true, transcript_gate: true }));
    expect(verdict.status).toBe("ok");
    if (verdict.status !== "ok") throw new Error("unreachable");
    expect(verdict.writtenPath).toBe("");
    expect(verdict.headline).toBe("Export written.");
  });

  it("blocks on the structural gate with no diff detail (lean wire)", () => {
    const verdict = deriveVerdict(runResult({ ok: false, graph_gate: false, transcript_gate: true }));
    expect(verdict).toEqual({
      status: "blocked",
      graphGate: false,
      transcriptGate: true,
      headline: "Blocked: structural fidelity gate failed",
      addedNodes: [],
      removedNodes: [],
      addedEdges: [],
      removedEdges: [],
      changedNodes: [],
      missingTokens: [],
      totalChanges: 0,
      scan: {
        findings: [],
        coverageLimit: CREDENTIAL_SHAPE_COVERAGE_LIMIT,
        scrubbed: false,
        headline: "No credential shapes found — read the coverage limit below before sharing.",
      },
    });
  });

  it("blocks on the transcript gate and surfaces the sorted missing tokens", () => {
    const verdict = deriveVerdict(runResult({
      ok: false,
      graph_gate: true,
      transcript_gate: false,
      missing_tokens: ["zeta", "alpha", "alpha"],
    }));
    expect(verdict.status).toBe("blocked");
    if (verdict.status !== "blocked") throw new Error("unreachable");
    expect(verdict.headline).toBe("Blocked: transcript fidelity gate failed");
    // multiset difference: a duplicate missing token is preserved, sorted.
    expect(verdict.missingTokens).toEqual(["alpha", "alpha", "zeta"]);
    expect(verdict.totalChanges).toBe(3);
  });

  it("blocks on both gates and normalizes the full delta, sorted deterministically", () => {
    const verdict = deriveVerdict(blockedFull());
    expect(verdict.status).toBe("blocked");
    if (verdict.status !== "blocked") throw new Error("unreachable");
    expect(verdict.headline).toBe("Blocked: structural and transcript fidelity gates failed");
    expect(verdict.addedNodes).toEqual(["n1", "n2"]);
    expect(verdict.removedNodes).toEqual(["r1"]);
    expect(verdict.addedEdges).toEqual([
      { parent: "a", child: "c", label: "a → c" },
      { parent: "a", child: "z", label: "a → z" },
      { parent: "b", child: "c", label: "b → c" },
    ]);
    expect(verdict.removedEdges).toEqual([{ parent: "x", child: "y", label: "x → y" }]);
    expect(verdict.changedNodes).toEqual([
      { id: "a1", fields: [{ field: "provider", old: "claude", new: "codex" }] },
      {
        id: "z1",
        fields: [
          { field: "title", old: "old", new: "new" },
          { field: "tokens", old: 10, new: 20 },
        ],
      },
    ]);
    expect(verdict.missingTokens).toEqual(["a", "b"]);
    expect(verdict.totalChanges).toBe(11);
  });

  it("blocks with an honest headline when neither gate names the failure", () => {
    const verdict = deriveVerdict(runResult({ ok: false, graph_gate: true, transcript_gate: true }));
    expect(verdict.status).toBe("blocked");
    if (verdict.status !== "blocked") throw new Error("unreachable");
    expect(verdict.headline).toBe("Blocked: export did not complete");
    expect(verdict.totalChanges).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// renderView
// ---------------------------------------------------------------------------

describe("renderView", () => {
  it("renders the transient states", () => {
    expect(renderView({ kind: "idle" })).toEqual([
      "Export idle. Choose a destination to preview.",
    ]);
    expect(renderView({ kind: "planning" })).toEqual(["Planning export…"]);
    expect(renderView({ kind: "running", preview: null })).toEqual(["Exporting…"]);
  });

  it("renders the planned preview line, the mode, and the scan", () => {
    // The scan lines are NOT optional trailing decoration — the coverage limit is the
    // load-bearing half of G-5, so it is asserted as part of the exact output rather than
    // with a `toContain` that would still pass if it silently disappeared.
    const preview = derivePreview(planFixture());
    expect(renderView({ kind: "planned", preview })).toEqual([
      `Ready to export: ${preview.summary}`,
      `Mode: ${modeLabel("full")}`,
      "No credential shapes found — read the coverage limit below before sharing.",
      CREDENTIAL_SHAPE_COVERAGE_LIMIT,
    ]);
  });

  it("renders an ok verdict as a single headline", () => {
    const verdict = deriveVerdict(runResult({
      ok: true,
      graph_gate: true,
      transcript_gate: true,
      written_path: "/out/export.json",
    }));
    expect(renderView({ kind: "done", preview: null, verdict })).toEqual([
      "Export written to /out/export.json",
      // Carried on SUCCESS too: a clean write is still an artifact about to be handed over.
      "No credential shapes found — read the coverage limit below before sharing.",
      CREDENTIAL_SHAPE_COVERAGE_LIMIT,
    ]);
  });

  it("renders a blocked verdict enumerating added/removed/changed + missing tokens", () => {
    const verdict = deriveVerdict(blockedFull());
    expect(renderView({ kind: "done", preview: null, verdict })).toEqual([
      "Blocked: structural and transcript fidelity gates failed",
      "gates: structural FAIL · transcript FAIL",
      "+2 nodes: n1, n2",
      "-1 nodes: r1",
      "+3 edges: a → c, a → z, b → c",
      "-1 edges: x → y",
      "~2 changed: a1, z1",
      "missing 2 tokens: a, b",
      // The warning travels with the REJECTION, which is the moment a retry is decided.
      "No credential shapes found — read the coverage limit below before sharing.",
      CREDENTIAL_SHAPE_COVERAGE_LIMIT,
    ]);
  });

  it("renders a clean-but-blocked verdict with a no-difference note and PASS gates", () => {
    const verdict = deriveVerdict(runResult({ ok: false, graph_gate: true, transcript_gate: true }));
    expect(renderView({ kind: "done", preview: null, verdict })).toEqual([
      "Blocked: export did not complete",
      "gates: structural PASS · transcript PASS",
      "no structural or token differences reported",
      // The warning travels with the REJECTION, which is the moment a retry is decided.
      "No credential shapes found — read the coverage limit below before sharing.",
      CREDENTIAL_SHAPE_COVERAGE_LIMIT,
    ]);
  });

  it("renders the failure of either stage", () => {
    expect(renderView({ kind: "error", stage: "plan", message: "boom" })).toEqual([
      "Export plan failed: boom",
    ]);
    expect(renderView({ kind: "error", stage: "run", message: "nope" })).toEqual([
      "Export run failed: nope",
    ]);
  });
});

// ---------------------------------------------------------------------------
// ExportPanel controller
// ---------------------------------------------------------------------------

describe("ExportPanel", () => {
  it("plans: emits planning then planned, forwarding the destination", async () => {
    let seenDest: string | undefined = "UNSET";
    const views: ExportView[] = [];
    const panel = new ExportPanel(
      fakeIpc({
        exportPlan: async (dest) => {
          seenDest = dest;
          return planFixture();
        },
      }),
      (v) => views.push(v),
    );

    expect(panel.current).toEqual({ kind: "idle" });
    await panel.plan("/dest/path.json");

    expect(views.map((v) => v.kind)).toEqual(["planning", "planned"]);
    expect(seenDest).toBe("/dest/path.json");
    const planned = views[1];
    if (planned.kind !== "planned") throw new Error("unreachable");
    expect(planned.preview.nodeCount).toBe(16);
    expect(panel.current).toBe(planned);
  });

  it("plans with no destination (dry-run to the default)", async () => {
    let seenDest: string | undefined = "UNSET";
    const panel = new ExportPanel(
      fakeIpc({
        exportPlan: async (dest) => {
          seenDest = dest;
          return planFixture();
        },
      }),
      () => {},
    );
    await panel.plan();
    expect(seenDest).toBeUndefined();
  });

  it("plans: reports an Error message on failure", async () => {
    const views: ExportView[] = [];
    const panel = new ExportPanel(
      fakeIpc({
        exportPlan: async () => {
          throw new Error("plan blew up");
        },
      }),
      (v) => views.push(v),
    );
    await panel.plan();
    expect(views.map((v) => v.kind)).toEqual(["planning", "error"]);
    const err = views[1];
    if (err.kind !== "error") throw new Error("unreachable");
    expect(err.stage).toBe("plan");
    expect(err.message).toBe("plan blew up");
  });

  it("plans: stringifies a non-Error rejection", async () => {
    const views: ExportView[] = [];
    const panel = new ExportPanel(
      fakeIpc({ exportPlan: () => Promise.reject("stringy failure") }),
      (v) => views.push(v),
    );
    await panel.plan();
    const err = views[1];
    if (err.kind !== "error") throw new Error("unreachable");
    expect(err.message).toBe("stringy failure");
  });

  it("runs after a plan: carries the preview and derives the verdict", async () => {
    let seenPath = "UNSET";
    const views: ExportView[] = [];
    const panel = new ExportPanel(
      fakeIpc({
        exportRun: async (dest) => {
          seenPath = dest;
          return runResult({ ok: true, graph_gate: true, transcript_gate: true, written_path: dest });
        },
      }),
      (v) => views.push(v),
    );
    await panel.plan("/dest.json");
    views.length = 0;
    await panel.run("/out/export.json");

    expect(seenPath).toBe("/out/export.json");
    expect(views.map((v) => v.kind)).toEqual(["running", "done"]);
    const running = views[0];
    if (running.kind !== "running") throw new Error("unreachable");
    expect(running.preview?.nodeCount).toBe(16);
    const done = views[1];
    if (done.kind !== "done") throw new Error("unreachable");
    expect(done.verdict.status).toBe("ok");
    expect(done.preview?.nodeCount).toBe(16);
  });

  it("runs without a prior plan: preview is null through the run", async () => {
    const views: ExportView[] = [];
    const panel = new ExportPanel(fakeIpc(), (v) => views.push(v));
    await panel.run("/out/export.json");
    const running = views[0];
    if (running.kind !== "running") throw new Error("unreachable");
    expect(running.preview).toBeNull();
    const done = views[1];
    if (done.kind !== "done") throw new Error("unreachable");
    expect(done.preview).toBeNull();
  });

  it("runs: reports an Error message on failure", async () => {
    const views: ExportView[] = [];
    const panel = new ExportPanel(
      fakeIpc({
        exportRun: async () => {
          throw new Error("write denied");
        },
      }),
      (v) => views.push(v),
    );
    await panel.run("/out/export.json");
    const err = views[1];
    if (err.kind !== "error") throw new Error("unreachable");
    expect(err.stage).toBe("run");
    expect(err.message).toBe("write denied");
  });

  it("ignores a re-entrant plan while one is in flight", async () => {
    const gate = deferred<ExportPlan>();
    let calls = 0;
    const views: ExportView[] = [];
    const panel = new ExportPanel(
      fakeIpc({
        exportPlan: () => {
          calls += 1;
          return gate.promise;
        },
      }),
      (v) => views.push(v),
    );

    const first = panel.plan();
    const second = panel.plan(); // busy -> no-op
    await second;
    expect(calls).toBe(1);
    expect(views.map((v) => v.kind)).toEqual(["planning"]);

    gate.resolve(planFixture());
    await first;
    expect(calls).toBe(1);
    expect(views.map((v) => v.kind)).toEqual(["planning", "planned"]);
  });

  it("ignores a re-entrant run while one is in flight", async () => {
    const gate = deferred<ExportRunResult>();
    let calls = 0;
    const views: ExportView[] = [];
    const panel = new ExportPanel(
      fakeIpc({
        exportRun: () => {
          calls += 1;
          return gate.promise;
        },
      }),
      (v) => views.push(v),
    );

    const first = panel.run("/out/export.json");
    const second = panel.run("/out/export.json"); // busy -> no-op
    await second;
    expect(calls).toBe(1);
    expect(views.map((v) => v.kind)).toEqual(["running"]);

    gate.resolve(
      runResult({ ok: true, graph_gate: true, transcript_gate: true, written_path: "/out/export.json" }),
    );
    await first;
    expect(views.map((v) => v.kind)).toEqual(["running", "done"]);
  });

  it("resets to idle and clears the carried preview", async () => {
    const views: ExportView[] = [];
    const panel = new ExportPanel(fakeIpc(), (v) => views.push(v));
    await panel.plan("/dest.json");
    views.length = 0;

    panel.reset();
    expect(views).toEqual([{ kind: "idle" }]);
    expect(panel.current).toEqual({ kind: "idle" });

    views.length = 0;
    await panel.run("/out/export.json");
    const running = views[0];
    if (running.kind !== "running") throw new Error("unreachable");
    expect(running.preview).toBeNull();
  });
});
