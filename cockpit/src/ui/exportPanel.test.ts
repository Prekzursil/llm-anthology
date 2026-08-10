import { describe, expect, it } from "vitest";

import { CREDENTIAL_SHAPE_COVERAGE_LIMIT } from "../ipc/mock";
import type { CredentialScan, ExportPlan } from "../ipc/types";
import {
  derivePreview,
  deriveVerdict,
  ExportPanel,
  formatBytes,
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

  it("renders the planned preview line", () => {
    const preview = derivePreview(planFixture());
    expect(renderView({ kind: "planned", preview })).toEqual([
      `Ready to export: ${preview.summary}`,
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
    ]);
  });

  it("renders a clean-but-blocked verdict with a no-difference note and PASS gates", () => {
    const verdict = deriveVerdict(runResult({ ok: false, graph_gate: true, transcript_gate: true }));
    expect(renderView({ kind: "done", preview: null, verdict })).toEqual([
      "Blocked: export did not complete",
      "gates: structural PASS · transcript PASS",
      "no structural or token differences reported",
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
