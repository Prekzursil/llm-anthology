/**
 * PURE mapping of a structural {@link CorpusDiffDto} onto per-node / per-edge render
 * state for the spawn-tree canvas.
 *
 * The sidecar's `graph.diff` answers "what changed between two ingests of the corpus?"
 * as id-set + field deltas (see `llm_anthology/diff.py` / the sidecar `_project_diff`). This
 * module turns that answer into the overlay the canvas paints on top of the base
 * graph — WITHOUT touching the layout, the DOM, or the graph data:
 *
 *   diffToOverlay(diff) -> { nodeClass, edgeClass, tooltips }
 *
 *   * nodeClass : node-id -> {@link DiffKind}   (added | removed | changed)
 *   * edgeClass : edge-key -> {@link DiffKind}  (added | removed) — edges never "change"
 *                 (a status-only change is not an edge add/remove, so the diff carries
 *                 no changed-edge set), keyed by {@link edgeKey} = `${parent} ${child}`
 *                 to match the layout module's edge-key convention.
 *   * tooltips  : node-id -> a human-readable field-delta string, present ONLY for
 *                 changed nodes (`field: old -> new`, one line per changed field).
 *
 * The visual treatment of each kind — added = green, removed = a red GHOST (a faded /
 * outlined mark, since a removed node no longer exists in the new graph), changed =
 * amber — lives in {@link DIFF_STYLES}, the diff analogue of `palette.ts`'s provider
 * tints, so the canvas maps kind -> colour without re-encoding the rule.
 *
 * Deterministic: the three output maps are rebuilt with their keys in ascending
 * (stable-id / edge-key) order, so `JSON.stringify(overlay)` is byte-identical across
 * runs regardless of the input arrays' order. Pure: it reads the diff and mutates
 * nothing. The three node categories are disjoint by the diff contract (added = new \\
 * old, removed = old \\ new, changed = present-in-both-with-differing-fields), so a
 * node lands in exactly one class.
 */

import type { ChangedFields, ChangedValue, CorpusDiffDto } from "../ipc/types";

/** How a node or edge differs between the two corpora. */
export type DiffKind = "added" | "removed" | "changed";

/** The canvas render treatment for a {@link DiffKind}. */
export interface DiffStyle {
  /** Overlay colour (chosen to read on both the light and dark app backgrounds). */
  color: string;
  /**
   * True for a "ghost" mark — a faded / outlined treatment for something that no longer
   * exists in the new graph (a removed node/edge). Added and changed items are solid.
   */
  ghost: boolean;
}

/**
 * The diff render palette: added = green (solid), removed = red (ghost), changed =
 * amber (solid). Exported so the canvas and a legend share one source of truth for the
 * kind -> colour rule, mirroring `palette.ts`'s provider tints.
 */
export const DIFF_STYLES: Record<DiffKind, DiffStyle> = {
  added: { color: "#3fb950", ghost: false },
  removed: { color: "#f85149", ghost: true },
  changed: { color: "#d29922", ghost: false },
};

/** Separator between an edge's parent and child in an {@link edgeKey}. */
const EDGE_KEY_SEP = " ";

/**
 * The stable string key for a directed edge, matching the layout module's convention
 * (`${parent} ${child}`). A consumer holding a laid-out edge computes the same key to
 * look up its overlay class.
 */
export function edgeKey(parent: string, child: string): string {
  return `${parent}${EDGE_KEY_SEP}${child}`;
}

/** The per-node / per-edge render state a {@link CorpusDiffDto} maps onto. */
export interface DiffOverlay {
  /** Node id -> its diff kind (added | removed | changed). */
  nodeClass: Record<string, DiffKind>;
  /** {@link edgeKey} -> its diff kind (added | removed). */
  edgeClass: Record<string, DiffKind>;
  /** Node id -> a field-delta tooltip, present only for changed nodes. */
  tooltips: Record<string, string>;
}

/** One changed value for display: `null` renders literally, everything else stringifies. */
function formatValue(value: ChangedValue): string {
  return value === null ? "null" : String(value);
}

/**
 * A changed node's `{field: [old, new]}` map -> a `field: old -> new` line per field,
 * joined by newlines, in the DTO's field order (the sidecar emits fields in a stable
 * declaration order).
 */
function formatTooltip(fields: ChangedFields): string {
  return Object.entries(fields)
    .map(([name, [oldValue, newValue]]) =>
      `${name}: ${formatValue(oldValue)} -> ${formatValue(newValue)}`,
    )
    .join("\n");
}

/** Rebuild `record` with its keys in ascending order, for byte-stable output. */
function sortedByKey<V>(record: Record<string, V>): Record<string, V> {
  const out: Record<string, V> = {};
  for (const key of Object.keys(record).sort()) out[key] = record[key];
  return out;
}

/**
 * Map a structural {@link CorpusDiffDto} onto the canvas overlay: per-node and per-edge
 * diff kinds plus a field-delta tooltip for each changed node. Pure and deterministic
 * (see the module header).
 */
export function diffToOverlay(diff: CorpusDiffDto): DiffOverlay {
  const nodeClass: Record<string, DiffKind> = {};
  for (const id of diff.added_nodes) nodeClass[id] = "added";
  for (const id of diff.removed_nodes) nodeClass[id] = "removed";

  const tooltips: Record<string, string> = {};
  for (const id of Object.keys(diff.changed_nodes)) {
    nodeClass[id] = "changed";
    tooltips[id] = formatTooltip(diff.changed_nodes[id]);
  }

  const edgeClass: Record<string, DiffKind> = {};
  for (const edge of diff.added_edges) edgeClass[edgeKey(edge.parent, edge.child)] = "added";
  for (const edge of diff.removed_edges) {
    edgeClass[edgeKey(edge.parent, edge.child)] = "removed";
  }

  return {
    nodeClass: sortedByKey(nodeClass),
    edgeClass: sortedByKey(edgeClass),
    tooltips: sortedByKey(tooltips),
  };
}
