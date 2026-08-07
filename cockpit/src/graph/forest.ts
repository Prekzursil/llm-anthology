/**
 * The decisions behind "draw the whole spawn forest" and "list every thread", as PURE
 * functions.
 *
 * They live here rather than inside `CockpitApp` for the same reason `emptyStateLabel` does:
 * this project's vitest runs `environment: "node"` with no DOM, so a decision embedded in a
 * method that also touches `document` cannot be tested at all. Splitting the decision from the
 * painting is the only reason any of these has a test rather than a promise.
 *
 *   * {@link loadAllRoots} — how much of the root list a caller actually gets.
 *   * {@link rootsStatus}  — what the sidebar SAYS about that, when it is not everything.
 *   * {@link loadForest} — HOW MANY round-trips the forest costs, and what it contains.
 *   * {@link buildView}  — what the canvas is actually handed, after folding and level-of-detail.
 */

import { collapseLinearChains } from "./aggregate";
import { capFanOut, isMoreId } from "./capFanOut";
import type { LayoutInput } from "./layout";
import type {
  GraphSnapshot,
  RootOrder,
  RootsParams,
  SpawnEdge,
  Subtree,
  ThreadNode,
} from "../ipc/types";

/** Field separator for an edge key: U+0000 cannot appear in a record id. */
const EDGE_KEY_SEP = "\u0000";

/** The slice of the IPC surface {@link loadForest} needs. */
export interface ForestIpc {
  graphAt?(asOfMs: number): Promise<GraphSnapshot>;
  graphRoots(params?: RootsParams): Promise<ThreadNode[]>;
  graphSubtree(threadId: string, depth?: number): Promise<Subtree>;
}

/** The (smaller) slice {@link loadAllRoots} needs. */
export type RootsIpc = Pick<ForestIpc, "graphRoots">;

/**
 * Roots per `graph.roots` page.
 *
 * Deliberately larger than the measured corpus's root count (~1,140 — 2,112 threads minus
 * 972 distinct children), so the ordinary case still costs exactly ONE round-trip. This
 * bounds the size of a single PAYLOAD; it is not a bound on how much of the corpus the user
 * is allowed to see, because {@link loadAllRoots} keeps asking until the corpus runs out.
 */
export const ROOTS_PAGE_SIZE = 2000;

/**
 * Hard ceiling on a roots walk.
 *
 * The stdio pipe is mutex-guarded to one request in flight, so a truly unbounded walk could
 * pin it. Unlike the `limit: 1000` this replaces, reaching this ceiling is DISCLOSED — see
 * {@link rootsStatus}. A cap is not the defect; a cap nobody is told about is.
 */
export const MAX_ROOTS = 20_000;

/**
 * Newest first. `created` is ASCENDING in the engine (`sidecar.py` `_graph_roots`), so a
 * capped `created` walk returns the OLDEST page and hides everything since.
 */
export const ROOTS_ORDER: RootOrder = "recent";

/** What a roots walk found, and whether that is all of them. */
export interface RootsWalk {
  roots: ThreadNode[];
  /** True when the walk reached the end of the corpus; false when {@link MAX_ROOTS} cut it. */
  complete: boolean;
}

/**
 * Every root in the corpus, paged, up to {@link MAX_ROOTS}.
 *
 * Replaces a bare `graphRoots({ limit: 1000 })`, which on the measured store returned 1,000
 * of ~1,140 roots and left the caller no way to know. Three options were on the table — raise
 * the cap, page it, or merely disclose the truncation — and paging is the one that makes the
 * common case COMPLETE rather than just better-labelled: raising a cap only moves the cliff,
 * and disclosure alone leaves 140 threads permanently unreachable. The sidebar list is
 * virtualized, so the extra rows cost nothing to draw; the cap was only ever a transport
 * bound, and paging is how you lift a transport bound without unbounding the transport.
 *
 * Termination is EXACT rather than heuristic: the engine answers `nodes[offset:offset + limit]`
 * with no server-side clamp on `limit`, so a page shorter than requested can only mean there
 * were no more rows to give. When the ceiling stops the walk instead, one extra one-row probe
 * separates "there are more" from "the corpus happened to be exactly this size" — so
 * `complete` is never a guess, in either direction.
 *
 * Rejects rather than returning a partial walk: the sidebar and the forest have different
 * failure policies (empty the list vs. blank the pane), and inventing a third one here would
 * hide the error from both.
 */
export async function loadAllRoots(
  api: RootsIpc,
  pageSize: number = ROOTS_PAGE_SIZE,
  maxRoots: number = MAX_ROOTS,
): Promise<RootsWalk> {
  // A page size of 0 would ask for nothing, receive nothing, and never advance the offset:
  // a hang rather than an error, which is the worst way for this loop to fail.
  const size = Math.max(1, pageSize);
  const roots: ThreadNode[] = [];
  const seen = new Set<string>();
  // `received` drives the OFFSET and the ceiling; `roots.length` is the DISTINCT count. They
  // are deliberately different numbers, and conflating them is a hang rather than a bug you
  // notice: this app ingests LIVE sessions, so a row genuinely can shift between two
  // offset-based requests and arrive twice. If the offset advanced by distinct rows, a page
  // of pure duplicates would never move it and the same page would be fetched forever.
  let received = 0;
  while (received < maxRoots) {
    // Never ask for rows the ceiling will not let us keep.
    const limit = Math.min(size, maxRoots - received);
    const page = await api.graphRoots({ limit, offset: received, order: ROOTS_ORDER });
    received += page.length;
    // Not `push(...page)`: a page can be thousands of rows, and spreading them as arguments
    // is a stack limit waiting to be found by the biggest corpus rather than the smallest.
    // First occurrence wins, so the order the engine chose survives de-duplication.
    for (const root of page) {
      if (seen.has(root.id)) continue;
      seen.add(root.id);
      roots.push(root);
    }
    if (page.length < limit) return { roots, complete: true };
  }
  const beyond = await api.graphRoots({ limit: 1, offset: received, order: ROOTS_ORDER });
  return { roots, complete: beyond.length === 0 };
}

/** Thousands-grouped, so a four-digit count is readable at a glance. */
function grouped(n: number): string {
  return n.toLocaleString("en-US");
}

/**
 * The line above the sidebar's thread list.
 *
 * Enforces the rule the old `limit: 1000` broke: the user is never shown a truncated list
 * that looks complete. A complete walk is a plain count; a walk a ceiling stopped is
 * qualified in the same breath, not in a tooltip nobody opens. Same shape as
 * `searchPresent`'s `resultStatus`, which had to learn this after printing "1,432 hits" over
 * a list holding 200.
 *
 * Silent for an empty corpus: `VirtualList` already paints its own empty state there, and two
 * stacked "nothing here" messages read as a fault rather than as an empty corpus.
 */
export function rootsStatus(shown: number, complete: boolean): string {
  if (!complete) return `showing the first ${grouped(shown)} threads · more exist`;
  if (shown === 0) return "";
  return `${grouped(shown)} thread${shown === 1 ? "" : "s"}`;
}

/**
 * Fetch the whole spawn forest.
 *
 * `graph.at(now)` returns it in ONE round-trip. The fallback below walks the roots and then
 * makes one `graph.subtree` call PER ROOT — a thousand-plus serial requests down a pipe that
 * is mutex-guarded to one in flight at a time, for a graph the engine can project in a single
 * query. It remains silently incomplete in one respect: a node in no root's subtree (a
 * dangling spawn target) is never fetched.
 *
 * It is no longer incomplete in the OTHER respect. The walk used to ask for `limit: 1000` and
 * stop, dropping every root past the thousandth; it now pages to the end of the corpus via
 * {@link loadAllRoots}.
 *
 * The walk survives only because `graphAt` is OPTIONAL on the IPC surface (`types.ts` declares
 * it `graphAt?`), so an implementation without it must still work.
 *
 * A failure yields an EMPTY forest, never a partial one: in a tool whose whole subject is the
 * graph, a half-graph is a worse lie than a blank pane, because nothing distinguishes it from
 * a complete one. A root list the ceiling truncated is exactly that case — this pane has no
 * status line to qualify it with, so it gets the same treatment.
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
    const { roots, complete } = await loadAllRoots(api);
    // Blank rather than partial, per the contract above: a forest grown from a truncated
    // root list is a partial graph wearing a complete graph's clothes.
    if (!complete) return { nodes: [], edges: [] };
    for (const root of roots) {
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
