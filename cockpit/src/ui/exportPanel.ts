/**
 * The EXPORT panel: a dry-run `export.plan` preview followed by a committing
 * `export.run`, rendering the fidelity-gate verdict — INCLUDING the blocked-with-diff
 * case, where the structural round-trip gate and/or the token-fidelity gate reject the
 * write and the panel must surface WHAT differs (added/removed/changed nodes & edges)
 * plus the missing prose tokens.
 *
 * This module is deliberately DOM-FREE. Everything here is a pure data transform or a
 * headless controller that emits a render-state ({@link ExportView}) to an injected
 * listener; a thin DOM adapter in the shell binds those states to elements (using
 * `textContent`, never `innerHTML`, so the untrusted diff/token strings can never inject
 * markup). Keeping it headless is what lets the whole module be unit-tested to 100% in
 * the cockpit's plain-node vitest environment, exactly like `graph/layout` and the IPC
 * mock — and unlike the DOM-bound `ui/search`, which is integration-wired only.
 *
 * The two derivations the task pins are {@link derivePreview} (plan -> preview model) and
 * {@link deriveVerdict} (run-result -> verdict model, incl. blocked). Every list they
 * emit is sorted by a stable id/label so a re-render over the same data is byte-identical.
 */

import type {
  ChangedFields,
  ChangedValue,
  CorpusDiffDto,
  CredentialScan,
  ExportMode,
  ExportPlan,
  ExportResult,
  SpawnEdge,
} from "../ipc/types";

// ---------------------------------------------------------------------------
// wire-adjacent input types
// ---------------------------------------------------------------------------

/**
 * The result the panel derives its verdict from. It is the wire {@link ExportResult}
 * (`ok`, `graph_gate`, `transcript_gate`, `written_path?`) OPTIONALLY enriched with the
 * blocked-case detail: the structural round-trip {@link CorpusDiffDto} and the
 * token-multiset shortfall. The sidecar already computes both in `export_with_gate`'s
 * report; a lean projection omits them today, so both are optional and the derivation
 * degrades to a detail-free "blocked" verdict when they are absent. A plain
 * `ExportResult` is assignable here, so the real/mock adapters drop straight in.
 */
export interface ExportRunResult extends ExportResult {
  /** Structural round-trip delta; present (and non-empty) only when the graph gate blocks. */
  diff?: CorpusDiffDto;
  /** Prose tokens the render dropped (sorted multiset); present only when transcript gate blocks. */
  missing_tokens?: string[];
}

/**
 * The minimal IPC surface the panel needs — the two `export.*` methods. Narrowed from
 * {@link import("../ipc/types").FullIpcClient} (interface segregation) so tests inject a
 * two-method fake and the mock/real client passes without the whole data surface.
 */
export interface ExportIpc {
  exportPlan(dest?: string, mode?: ExportMode): Promise<ExportPlan>;
  exportRun(destPath: string, mode?: ExportMode, scrub?: boolean): Promise<ExportRunResult>;
}

// ---------------------------------------------------------------------------
// G-5 / G-6: the privacy plane
// ---------------------------------------------------------------------------

/**
 * The credential-shape warning, render-ready.
 *
 * `coverageLimit` IS THE POINT and is unconditional. The scan matches credential SHAPES and
 * is blind to personal and medical content, so an empty `findings` list is not a safety
 * verdict — and a clean scan is precisely when a reader is most likely to read it as one.
 * A payload nobody renders does not discharge that; the sentence has to reach the screen.
 */
export interface CredentialScanView {
  /** One line per finding: shape, location, and the MASKED excerpt. Never the run itself. */
  findings: string[];
  /** The engine's coverage sentence, verbatim. Always present, findings or not. */
  coverageLimit: string;
  /** True when a scrub actually rewrote the bytes, as opposed to only warning. */
  scrubbed: boolean;
  /** A one-line summary of the findings count. */
  headline: string;
}

/*
 * THE PROSE THAT USED TO LIVE HERE IS DELETED, NOT AMENDED.
 *
 * It described the projection field by field — "drops `preview`, relativizes
 * `cwd`/`rollout_path`, and since CF-23 runs `title` and `git_branch` through
 * `scrub_home_mentions`; `agent_role`/`agent_nickname` are untouched by construction" — and
 * it carried a banner reading KEPT CURRENT. It was not current: `2b2492c` had dropped
 * `agent_nickname` and this paragraph still said it survived. Rewriting it a fourth time
 * would have restored exactly the arrangement that failed three times, banner included.
 *
 * A hand-maintained description of a machine-readable fact is the defect. The table below is
 * the description now, the label is generated from it, and `exportPanel.test.ts` re-derives
 * the same table straight from `redact.py` and refuses to pass if the two disagree.
 */

/** What SHAREABLE mode does to one `ThreadMeta` field. */
export type FieldTreatment = "dropped" | "scrubbed" | "relativized" | "kept";

/**
 * Every `ThreadMeta` field and what shareable mode does to it — a MIRROR of
 * `redact.shareable_thread`, not an independent opinion.
 *
 * This table exists because the prose it replaced rotted three times by the same mechanism:
 * the engine changed a field's treatment, this file kept describing the previous engine, and
 * every suite stayed green because a stale-but-plausible sentence produces no type error.
 * The last one shipped to `main` telling users `agent_nickname` was kept on the day it
 * started being dropped.
 *
 * `exportPanel.test.ts` re-derives this table from `redact.py` itself and demands an exact
 * match in both directions, so it cannot drift silently and cannot go stale by omission.
 * UPDATE IT ONLY TO FOLLOW THE ENGINE — the label below is generated from it, so a wrong
 * entry here is a wrong sentence shown to somebody deciding what to share.
 */
export const SHAREABLE_TREATMENT: Readonly<Record<string, FieldTreatment>> = {
  id: "kept",
  title: "scrubbed",
  model_provider: "kept",
  tokens_used: "kept",
  created_at_ms: "kept",
  updated_at_ms: "kept",
  git_branch: "scrubbed",
  cwd: "relativized",
  agent_role: "kept",
  agent_nickname: "dropped",
  preview: "dropped",
  rollout_path: "relativized",
  adapter: "kept",
};

/** Field names under one treatment, sorted so the sentence is stable across edits. */
function fieldsUnder(treatment: FieldTreatment): string[] {
  return Object.keys(SHAREABLE_TREATMENT)
    .filter((field) => SHAREABLE_TREATMENT[field] === treatment)
    .sort();
}

/** `a`, `a and b`, `a, b and c` — an empty list would be a bug, so it says so. */
function listed(fields: string[]): string {
  if (fields.length === 0) return "(none)";
  if (fields.length === 1) return fields[0];
  return `${fields.slice(0, -1).join(", ")} and ${fields[fields.length - 1]}`;
}

/**
 * What each mode ACTUALLY does today, in the words a user is shown.
 *
 * GENERATED from {@link SHAREABLE_TREATMENT} rather than written out, so the field lists
 * cannot disagree with the engine. Only the two sentences the table CANNOT express are
 * hand-written, and both are residuals rather than reassurances:
 *
 *   * home-ROOT-only substitution is a property of `scrub_home_mentions`, not of any field —
 *     `D:/work/client-x` is not a home leak and survives untouched;
 *   * "kept" means VERBATIM, which is what makes the kept list a warning rather than trivia.
 *
 * DELIBERATELY UNDERSOLD. Calling the mode "anonymised" would be a lie the UI tells on the
 * engine's behalf, and the person who believes it is the one about to hand the file to
 * somebody else.
 */
export function modeLabel(mode: ExportMode): string {
  if (mode !== "shareable") return "full — the archive of record: every field, unchanged.";
  return (
    `shareable — DROPS ${listed(fieldsUnder("dropped"))}; ` +
    `rewrites ${listed(fieldsUnder("relativized"))} to ~; ` +
    `scrubs home-directory mentions out of ${listed(fieldsUnder("scrubbed"))}. ` +
    `Everything else is kept VERBATIM: ${listed(fieldsUnder("kept"))}. ` +
    `Note: only HOME paths are scrubbed — a path like D:/work/client-x survives.`
  );
}

/** The subset of the full IPC client this panel can be built from. Both methods optional. */
export interface ExportCapableClient {
  exportPlan?: (dest?: string, mode?: ExportMode) => Promise<ExportPlan>;
  exportRun?: (destPath: string, mode?: ExportMode, scrub?: boolean) => Promise<ExportResult>;
}

/**
 * Adapt a client into {@link ExportIpc}, or null when the engine does not offer the methods.
 *
 * EXTRACTED BECAUSE A MUTATION PROVED IT WAS NEEDED. Reverting `app.ts`'s wiring to
 * `exportRun: (destPath) => ipc.exportRun!(destPath)` — the CF-17 defect exactly, one layer
 * up, and the one that made the whole privacy plane unreachable — left the entire suite
 * GREEN. `app.ts` has no tests and cannot get them in this environment, so any behaviour
 * left inline there is behaviour nothing can catch. Moving the forwarding here puts the part
 * that can silently drop a privacy parameter under test; what stays in `app.ts` is DOM
 * plumbing, which this suite could not have checked either way.
 */
export function exportIpcFrom(client: ExportCapableClient): ExportIpc | null {
  const { exportPlan, exportRun } = client;
  if (exportPlan === undefined || exportRun === undefined) return null;
  return {
    exportPlan: (dest, mode) => exportPlan(dest, mode),
    exportRun: (destPath, mode, scrub) => exportRun(destPath, mode, scrub),
  };
}

/**
 * A raw control value -> a mode, defaulting to the one that changes nothing.
 *
 * Exists as a function purely so `app.ts`'s one piece of wiring LOGIC has a test — the same
 * reason `searchPresent.searchParams` exists, and for the same reason: `app.ts` cannot be
 * unit-tested in this suite (constructing `CockpitApp` needs a canvas 2D context and a
 * Worker, neither of which vitest's node environment nor jsdom provides), so logic left
 * inline there is logic nothing exercises.
 *
 * ANYTHING UNRECOGNIZED IS `full`. A select that lost its options, a stale persisted value or
 * a typo must not silently downgrade the archive of record into a lossy projection. Note this
 * is the OPPOSITE of the engine's rule, deliberately: the engine answers -32602 on an
 * unrecognized mode because a wire value it cannot parse is a caller bug worth surfacing,
 * whereas this narrows a UI control that has no way to report one. Both choose the outcome
 * that cannot silently produce the wrong artifact.
 */
export function asExportMode(value: string): ExportMode {
  return value === "shareable" ? "shareable" : "full";
}

/** scan payload -> render-ready view. */
export function deriveScan(scan: CredentialScan): CredentialScanView {
  const findings = scan.findings.map(
    (f) => `${f.shape} in ${f.scope} ${f.id} field ${f.field}: ${f.preview}`,
  );
  // The scrub state is disclosed in EVERY branch, including the empty one. "Scrub was on and
  // matched nothing" and "scrub was off" are different facts about the artifact on disk, and
  // a reader deciding whether to hand the file over needs to know which happened. Claiming
  // "REPLACED" with zero findings would be the opposite error — nothing was replaced.
  const headline =
    findings.length === 0
      ? scan.scrubbed
        ? "No credential shapes found — scrub was ON, so nothing needed replacing."
        : "No credential shapes found — read the coverage limit below before sharing."
      : `${findings.length} credential shape${findings.length === 1 ? "" : "s"} found` +
        (scan.scrubbed ? " and REPLACED in the written bytes." : " — warning only, bytes untouched.");
  return { findings, coverageLimit: scan.coverage_limit, scrubbed: scan.scrubbed, headline };
}

/** The scan as display lines: headline, each finding, then the limit — always last, always there. */
function renderScan(scan: CredentialScanView): string[] {
  return [scan.headline, ...scan.findings, scan.coverageLimit];
}

// ---------------------------------------------------------------------------
// view-model types (the render-state)
// ---------------------------------------------------------------------------

/** `export.plan` -> a render-ready preview with a humanized size and one-line summary. */
export interface ExportPreview {
  nodeCount: number;
  edgeCount: number;
  conversationCount: number;
  estBytes: number;
  /** The projection this tally was measured against. */
  mode: ExportMode;
  /** The pre-write credential warning. Rendered whether or not it found anything. */
  scan: CredentialScanView;
  /** `estBytes` as a human-readable label, e.g. "3.2 MB". */
  estBytesLabel: string;
  /** A one-line "N nodes · N edges · N conversations · ~size" digest. */
  summary: string;
}

/** One spawn edge rendered as a stable "parent → child" label. */
export interface EdgeDelta {
  parent: string;
  child: string;
  label: string;
}

/** One changed field on a node: `old` -> `new`, values post-sanitize on the wire. */
export interface ChangedFieldDelta {
  field: string;
  old: ChangedValue;
  new: ChangedValue;
}

/** A changed node: its id and the per-field deltas, in the wire's declaration order. */
export interface ChangedNodeDelta {
  id: string;
  fields: ChangedFieldDelta[];
}

/**
 * `export.run` -> the verdict model. `ok` means the write happened and both gates
 * passed; otherwise it is `blocked`, carrying the full round-trip delta (added/removed
 * nodes & edges, changed nodes) and the missing prose tokens so the UI can explain the
 * rejection. Every list is sorted by a stable id/label.
 */
export type ExportVerdict =
  | {
      status: "ok";
      graphGate: boolean;
      transcriptGate: boolean;
      writtenPath: string;
      headline: string;
      /** Carried on success too — a clean write is still worth reading the limit against. */
      scan: CredentialScanView;
    }
  | {
      status: "blocked";
      graphGate: boolean;
      transcriptGate: boolean;
      headline: string;
      addedNodes: string[];
      removedNodes: string[];
      addedEdges: EdgeDelta[];
      removedEdges: EdgeDelta[];
      changedNodes: ChangedNodeDelta[];
      missingTokens: string[];
      /** Total delta items across every category — a compact "N differences" badge. */
      totalChanges: number;
      /** Carried on FAILURE too: a blocked export is when the user is about to retry. */
      scan: CredentialScanView;
    };

/** The panel's render-state: idle -> planning -> planned -> running -> done|error. */
export type ExportView =
  | { kind: "idle" }
  | { kind: "planning" }
  | { kind: "planned"; preview: ExportPreview }
  | { kind: "running"; preview: ExportPreview | null }
  | { kind: "done"; preview: ExportPreview | null; verdict: ExportVerdict }
  | { kind: "error"; stage: "plan" | "run"; message: string };

/** Called with the current {@link ExportView} on every state transition. */
export type ViewListener = (view: ExportView) => void;

// ---------------------------------------------------------------------------
// pure derivations
// ---------------------------------------------------------------------------

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

/**
 * A human-readable byte size. Sub-KB values stay raw ("512 B"); larger values scale by
 * 1024 with one decimal ("1.9 MB"), capping at the largest known unit rather than
 * inventing one. `bytes` is a non-negative content-byte estimate.
 */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${BYTE_UNITS[unit]}`;
}

/** plan -> preview model. */
export function derivePreview(plan: ExportPlan): ExportPreview {
  const estBytesLabel = formatBytes(plan.est_bytes);
  return {
    nodeCount: plan.node_count,
    edgeCount: plan.edge_count,
    conversationCount: plan.conversation_count,
    estBytes: plan.est_bytes,
    mode: plan.mode,
    scan: deriveScan(plan.credential_scan),
    estBytesLabel,
    summary: `${plan.node_count} nodes · ${plan.edge_count} edges · ${plan.conversation_count} conversations · ~${estBytesLabel}`,
  };
}

/** run-result -> verdict model, including the blocked-with-diff case. */
export function deriveVerdict(result: ExportRunResult): ExportVerdict {
  if (result.ok) {
    const writtenPath = result.written_path ?? "";
    return {
      status: "ok",
      graphGate: result.graph_gate,
      transcriptGate: result.transcript_gate,
      writtenPath,
      headline: writtenPath !== "" ? `Export written to ${writtenPath}` : "Export written.",
      scan: deriveScan(result.credential_scan),
    };
  }

  const diff = result.diff;
  const addedNodes = [...(diff?.added_nodes ?? [])].sort();
  const removedNodes = [...(diff?.removed_nodes ?? [])].sort();
  const addedEdges = toEdgeDeltas(diff?.added_edges ?? []);
  const removedEdges = toEdgeDeltas(diff?.removed_edges ?? []);
  const changedNodes = toChangedNodeDeltas(diff?.changed_nodes ?? {});
  const missingTokens = [...(result.missing_tokens ?? [])].sort();
  const totalChanges =
    addedNodes.length +
    removedNodes.length +
    addedEdges.length +
    removedEdges.length +
    changedNodes.length +
    missingTokens.length;

  return {
    status: "blocked",
    graphGate: result.graph_gate,
    transcriptGate: result.transcript_gate,
    headline: blockedHeadline(result.graph_gate, result.transcript_gate),
    addedNodes,
    removedNodes,
    addedEdges,
    removedEdges,
    changedNodes,
    missingTokens,
    totalChanges,
    scan: deriveScan(result.credential_scan),
  };
}

// ---------------------------------------------------------------------------
// pure renderer (plain-text lines; the DOM adapter paints each via textContent)
// ---------------------------------------------------------------------------

/** A view -> the plain-text lines that describe it (never HTML, so nothing can inject). */
export function renderView(view: ExportView): string[] {
  switch (view.kind) {
    case "idle":
      return ["Export idle. Choose a destination to preview."];
    case "planning":
      return ["Planning export…"];
    case "planned":
      return [
        `Ready to export: ${view.preview.summary}`,
        `Mode: ${modeLabel(view.preview.mode)}`,
        ...renderScan(view.preview.scan),
      ];
    case "running":
      return ["Exporting…"];
    case "done":
      return renderVerdict(view.verdict);
    case "error":
      return [`Export ${view.stage} failed: ${view.message}`];
  }
}

// ---------------------------------------------------------------------------
// controller
// ---------------------------------------------------------------------------

/**
 * Headless export-panel controller. Orchestrates `plan()` then `run(dest)` against the
 * injected {@link ExportIpc}, tracks a single-flight busy flag (a re-entrant call while
 * one is in flight is ignored), and emits the current {@link ExportView} to `onChange`
 * on every transition. `current` exposes the latest view for an initial paint.
 */
export class ExportPanel {
  private view: ExportView = { kind: "idle" };
  private preview: ExportPreview | null = null;
  private busy = false;

  constructor(
    private readonly ipc: ExportIpc,
    private readonly onChange: ViewListener,
  ) {}

  get current(): ExportView {
    return this.view;
  }

  private emit(view: ExportView): void {
    this.view = view;
    this.onChange(view);
  }

  /** Dry-run: fetch the plan and show the preview (no write). */
  async plan(dest?: string, mode?: ExportMode): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    this.emit({ kind: "planning" });
    try {
      const plan = await this.ipc.exportPlan(dest, mode);
      this.preview = derivePreview(plan);
      this.emit({ kind: "planned", preview: this.preview });
    } catch (err) {
      this.emit({ kind: "error", stage: "plan", message: errorMessage(err) });
    } finally {
      this.busy = false;
    }
  }

  /** Commit: write the export to `destPath` and show the gate verdict. */
  async run(destPath: string, mode?: ExportMode, scrub?: boolean): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    this.emit({ kind: "running", preview: this.preview });
    try {
      const result = await this.ipc.exportRun(destPath, mode, scrub);
      this.emit({ kind: "done", preview: this.preview, verdict: deriveVerdict(result) });
    } catch (err) {
      this.emit({ kind: "error", stage: "run", message: errorMessage(err) });
    } finally {
      this.busy = false;
    }
  }

  /** Clear back to idle, dropping any carried preview. */
  reset(): void {
    this.preview = null;
    this.emit({ kind: "idle" });
  }
}

// ---------------------------------------------------------------------------
// private helpers
// ---------------------------------------------------------------------------

/** Spawn edges -> "parent → child" deltas, sorted by label for a stable render. */
function toEdgeDeltas(edges: SpawnEdge[]): EdgeDelta[] {
  return edges
    .map((e) => ({ parent: e.parent, child: e.child, label: `${e.parent} → ${e.child}` }))
    .sort((a, b) => a.label.localeCompare(b.label));
}

/** The `{id: {field: [old, new]}}` wire map -> sorted-by-id changed-node deltas. */
function toChangedNodeDeltas(changed: Record<string, ChangedFields>): ChangedNodeDelta[] {
  return Object.keys(changed)
    .sort()
    .map((id) => ({
      id,
      fields: Object.entries(changed[id]).map(([field, [oldValue, newValue]]) => ({
        field,
        old: oldValue,
        new: newValue,
      })),
    }));
}

/** The headline for a blocked verdict, naming which fidelity gate(s) rejected the write. */
function blockedHeadline(graphGate: boolean, transcriptGate: boolean): string {
  if (!graphGate && !transcriptGate) {
    return "Blocked: structural and transcript fidelity gates failed";
  }
  if (!graphGate) return "Blocked: structural fidelity gate failed";
  if (!transcriptGate) return "Blocked: transcript fidelity gate failed";
  return "Blocked: export did not complete";
}

/** A verdict -> its plain-text lines. */
function renderVerdict(verdict: ExportVerdict): string[] {
  if (verdict.status === "ok") return [verdict.headline, ...renderScan(verdict.scan)];

  const lines = [
    verdict.headline,
    `gates: structural ${gateLabel(verdict.graphGate)} · transcript ${gateLabel(verdict.transcriptGate)}`,
  ];
  if (verdict.addedNodes.length > 0) {
    lines.push(`+${verdict.addedNodes.length} nodes: ${verdict.addedNodes.join(", ")}`);
  }
  if (verdict.removedNodes.length > 0) {
    lines.push(`-${verdict.removedNodes.length} nodes: ${verdict.removedNodes.join(", ")}`);
  }
  if (verdict.addedEdges.length > 0) {
    lines.push(`+${verdict.addedEdges.length} edges: ${labelsOf(verdict.addedEdges)}`);
  }
  if (verdict.removedEdges.length > 0) {
    lines.push(`-${verdict.removedEdges.length} edges: ${labelsOf(verdict.removedEdges)}`);
  }
  if (verdict.changedNodes.length > 0) {
    const ids = verdict.changedNodes.map((c) => c.id).join(", ");
    lines.push(`~${verdict.changedNodes.length} changed: ${ids}`);
  }
  if (verdict.missingTokens.length > 0) {
    lines.push(`missing ${verdict.missingTokens.length} tokens: ${verdict.missingTokens.join(", ")}`);
  }
  if (verdict.totalChanges === 0) {
    lines.push("no structural or token differences reported");
  }
  // LAST, and unconditional. A blocked run is the moment the user is about to retry, so the
  // warning has to travel with the rejection rather than only with a success.
  return [...lines, ...renderScan(verdict.scan)];
}

function labelsOf(edges: EdgeDelta[]): string {
  return edges.map((e) => e.label).join(", ");
}

function gateLabel(passed: boolean): string {
  return passed ? "PASS" : "FAIL";
}

/** An unknown throwable -> a display string (an Error's message, else its stringification). */
function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
