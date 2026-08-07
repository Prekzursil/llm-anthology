/**
 * The two decisions behind "draw the whole spawn forest", as PURE functions.
 *
 * They live here rather than inside `CockpitApp` for the same reason `emptyStateLabel` does:
 * this project's vitest runs `environment: "node"` with no DOM, so a decision embedded in a
 * method that also touches `document` cannot be tested at all. Splitting the decision from the
 * painting is the only reason either of these has a test rather than a promise.
 *
 *   * {@link loadForest} — HOW MANY round-trips the forest costs, and what it contains.
 *   * {@link buildView}  — what the canvas is actually handed, after folding and level-of-detail.
 */

import { collapseLinearChains } from "./aggregate";
import { capFanOut, isMoreId } from "./capFanOut";
import type { LayoutInput } from "./layout";
import type { GraphSnapshot, RootsParams, SpawnEdge, Subtree, ThreadNode } from "../ipc/types";

/** Field separator for an edge key: U+0000 cannot appear in a record id. */
const EDGE_KEY_SEP = "\u0000";

/** The slice of the IPC surface {@link loadForest} needs. */
export interface ForestIpc {
  graphAt?(asOfMs: number): Promise<GraphSnapshot>;
  graphRoots(params?: RootsParams): Promise<ThreadNode[]>;
  graphSubtree(threadId: string, depth?: number): Promise<Subtree>;
}

/**
 * Fetch the whole spawn forest.
 *
 * `graph.at(now)` returns it in ONE round-trip. The fallback below asks for up to 1000 roots
 * and then makes one `graph.subtree` call PER ROOT — over a thousand serial requests down a
 * pipe that is mutex-guarded to one in flight at a time, for a graph the engine can project in
 * a single query. That path was also silently incomplete twice over: roots past the 1000th
 * were dropped, and a node in no root's subtree (a dangling spawn target) was never fetched.
 *
 * The walk survives only because `graphAt` is OPTIONAL on the IPC surface (`types.ts` declares
 * it `graphAt?`), so an implementation without it must still work.
 *
 * A failure yields an EMPTY forest, never a partial one: in a tool whose whole subject is the
 * graph, a half-graph is a worse lie than a blank pane, because nothing distinguishes it from
 * a complete one.
 */
export async function loadForest(api: ForestIpc, nowMs: number): Promise<LayoutInput> {
  if (api.graphAt !== undefined) {
    try {
      // "now" means every node born by this instant — which is all of them.
      const snap = await api.graphAt(nowMs);
      return { nodes: snap.nodes, edges: snap.edges };
    } catch {
      // Fall through to the walk rather than blanking a pane we could still fill.
    }
  }
  const nodeMap = new Map<string, ThreadNode>();
  const edgeMap = new Map<string, SpawnEdge>();
  try {
    // "recent", not the engine's ascending "created" default: with a 1000 cap, `created`
    // returns the thousand OLDEST threads and hides everything since.
    for (const root of await api.graphRoots({ limit: 1000, order: "recent" })) {
      const sub = await api.graphSubtree(root.id);
      for (const n of sub.nodes) nodeMap.set(n.id, n);
      for (const e of sub.edges) edgeMap.set(`${e.parent}${EDGE_KEY_SEP}${e.child}`, e);
    }
  } catch {
    return { nodes: [], edges: [] };
  }
  return { nodes: [...nodeMap.values()], edges: [...edgeMap.values()] };
}

/** What {@link buildView} decided to draw, and what it left out. */
export interface ForestView {
  /** The graph handed to ELK. */
  view: LayoutInput;
  /** How many nodes level-of-detail removed, for the status line. */
  hiddenCount: number;
  /** Hidden tally per `more:<parent>` placeholder, for its detail panel. */
  moreCounts: Map<string, number>;
}

/**
 * Derive the drawn graph from the base graph.
 *
 * Order matters: fold first (the aggregated/expanded toggle), then cap. The cap must bound
 * whatever the toggle produced, because folding linear chains does not narrow a fan-out — the
 * two transforms address opposite shapes, and only the cap addresses the one this corpus has.
 */
export function buildView(input: LayoutInput, aggregated: boolean): ForestView {
  const folded = aggregated ? aggregateInput(input) : input;
  const view = capFanOut(folded);
  return {
    view,
    hiddenCount: folded.nodes.length - view.nodes.length,
    moreCounts: new Map(
      view.nodes.filter((n) => isMoreId(n.id)).map((n) => [n.id, n.child_count] as const),
    ),
  };
}

/**
 * Collapse linear chains and project the result back onto the node DTO the canvas draws:
 * each super-node renders with its fold id, its fold label and its aggregated token weight.
 */
export function aggregateInput(input: LayoutInput): LayoutInput {
  const agg = collapseLinearChains(input.nodes, input.edges);
  return {
    nodes: agg.nodes.map((n) => ({
      ...n.representative,
      id: n.id,
      title: n.label,
      tokens: n.tokens,
    })),
    edges: agg.edges,
  };
}
