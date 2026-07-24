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
  exportPlan(dest?: string): Promise<ExportPlan>;
  exportRun(destPath: string): Promise<ExportRunResult>;
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
      return [`Ready to export: ${view.preview.summary}`];
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
  async plan(dest?: string): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    this.emit({ kind: "planning" });
    try {
      const plan = await this.ipc.exportPlan(dest);
      this.preview = derivePreview(plan);
      this.emit({ kind: "planned", preview: this.preview });
    } catch (err) {
      this.emit({ kind: "error", stage: "plan", message: errorMessage(err) });
    } finally {
      this.busy = false;
    }
  }

  /** Commit: write the export to `destPath` and show the gate verdict. */
  async run(destPath: string): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    this.emit({ kind: "running", preview: this.preview });
    try {
      const result = await this.ipc.exportRun(destPath);
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
  if (verdict.status === "ok") return [verdict.headline];

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
  return lines;
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
