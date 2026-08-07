/**
 * Level-of-detail for the spawn graph: no parent renders more than N children at once.
 *
 * WHY THIS EXISTS, measured rather than assumed
 * (`.scratch/maturation/MEASURED-layout-scale.md`, harness `tools/measure_layout_collapse.mjs`,
 * 3 repeats per threshold, medians):
 *
 * | children under one parent | visible nodes | median layout |
 * |--------------------------:|--------------:|--------------:|
 * | none (the real corpus)    |        12,791 | >20,000ms fail |
 * |                       500 |           502 | >20,000ms fail |
 * |                       250 |           252 |     14,069ms   |
 * |                       200 |           202 |      5,484ms   |
 * |                       100 |           102 |        868ms   |
 *
 * The counter-intuitive row is 500: **502 nodes with 501 edges takes over twenty seconds**
 * while 202 nodes takes 2.1s. The only structural difference is how many children hang off
 * ONE parent, so ELK `layered`'s cost here is driven by the width of a single layer, not by
 * the size of the graph. It was never struggling with 12,791 nodes — it was struggling with
 * one node that had thousands of siblings beneath it.
 *
 * That is why the rule is local ("no parent renders more than N children") rather than a
 * global budget on graph size, and why N is bounded by measurement rather than taste.
 *
 * Tuning the algorithm is not an alternative: every `layered` variant (thoroughness=1,
 * crossingMinimization=NONE, nodePlacement=SIMPLE, all three) exceeded 20s on the real
 * corpus, `mrtree` overflowed the stack, and the two that completed — `box` and
 * `rectpacking` — are PACKING algorithms that would render the spawn tree as a grid, i.e.
 * fix the budget by deleting the feature.
 *
 * THE COST, stated plainly. At threshold 100 the real corpus draws ~102 of 12,791 nodes; the
 * rest sit behind "+N more". The graph becomes navigable by ceasing to be complete, and any
 * UI built on this MUST make that obvious or a user will believe their corpus is tiny.
 *
 * AND THE HARD LIMIT ON EXPANSION. Showing N children of one parent costs the same whether
 * you reached N by collapsing down or expanding up, so the table above IS the expansion
 * curve: a wide parent can never be expanded past ~200 children, and the 4,844-child hub can
 * never be fully expanded in the graph at all — not with a longer timeout, not with paging
 * that accumulates. "+N more" must therefore open a LIST, never expand in place.
 */

import type { SpawnEdge, ThreadNode } from "../ipc/types";

/** The `{ nodes, edges }` pair the canvas lays out (structurally `LayoutInput`). */
export interface FanOutGraph {
  nodes: ThreadNode[];
  edges: SpawnEdge[];
}

/**
 * Maximum children drawn under one parent.
 *
 * 100 measured at 868ms median against the app's own 8,000ms layout guard — 9x headroom on a
 * machine that was at 100% CPU at the time. 200 is the hard ceiling (5.5s, ~2.5s of margin)
 * and 250 already blows the guard at 14s. Lowering this is cheap; raising it needs a
 * re-measurement, not an opinion.
 */
export const DEFAULT_MAX_CHILDREN = 100;

/** Id prefix marking a synthetic "+N more" placeholder, like `chain:` / `subtree:`. */
export const MORE_ID_PREFIX = "more:";

/** Is `id` a placeholder this module synthesized (rather than a real record)? */
export function isMoreId(id: string): boolean {
  return id.startsWith(MORE_ID_PREFIX);
}

/** The parent whose hidden children a placeholder stands for. */
export function moreParentId(id: string): string {
  return id.slice(MORE_ID_PREFIX.length);
}

/** A bare synthesized node for an id that appears only on an edge — as `aggregate.ts` does. */
function bareNode(id: string): ThreadNode {
  return { id, title: "", provider: "", model_provider: "", created_at_ms: null,
           child_count: 0, depth: 0 };
}

/**
 * Which children survive the cap, best first.
 *
 * Newest first, because that matches what the sidebar shows and because on a hub with
 * thousands of children the recent ones are the ones being worked on. An undated node
 * (`created_at_ms === null`) sorts AFTER every dated one rather than as epoch (which would
 * silently always drop it) or as now (silently always keep it), and ties break on id so the
 * transform is deterministic — the canvas must not reshuffle between two identical renders.
 */
function byRecencyThenId(index: Map<string, ThreadNode>) {
  return (a: string, b: string): number => {
    const ta = index.get(a)?.created_at_ms ?? null;
    const tb = index.get(b)?.created_at_ms ?? null;
    if (ta !== tb) {
      if (ta === null) return 1;
      if (tb === null) return -1;
      return tb - ta;
    }
    return a < b ? -1 : a > b ? 1 : 0;
  };
}

/**
 * Drop children past `maxChildren` from every over-wide parent, replacing them with one
 * "+N more" placeholder, and remove whatever that hides.
 *
 * A hidden child's subtree goes with it — leaving descendants behind would strand them as
 * floating orphans and would not reduce the node count, which is the entire point. But a node
 * is only removed if NOTHING still reaches it: the spawn graph is a DAG, so a child hidden by
 * one parent may still be drawn under another, and deleting it would erase a node that is
 * legitimately on screen.
 *
 * The placeholder's count is "children of THIS parent not drawn beneath it", which is what a
 * user reading a fan-out is asking and what agrees with the parent's own `child_count`. A
 * child that survives via a second parent is still not under this one, so it still counts.
 *
 * Pure: the input graph is not mutated, and the output is fully determined by the input.
 */
export function capFanOut(
  input: FanOutGraph,
  maxChildren: number = DEFAULT_MAX_CHILDREN,
): FanOutGraph {
  const index = new Map<string, ThreadNode>();
  for (const node of input.nodes) index.set(node.id, node);
  for (const edge of input.edges) {
    for (const id of [edge.parent, edge.child]) {
      if (!index.has(id)) index.set(id, bareNode(id));
    }
  }

  // Children per parent, self-loops ignored and duplicates collapsed — a self-loop is not a
  // fan-out and double-counting one child would cap a parent that is not actually wide.
  // A placeholder from a previous pass is held aside rather than counted: it is this
  // function's own output, and re-counting it would make a second pass cap again and again.
  const children = new Map<string, Set<string>>();
  const carried = new Map<string, Set<string>>();
  for (const edge of input.edges) {
    if (edge.parent === edge.child) continue;
    const into = isMoreId(edge.child) ? carried : children;
    let set = into.get(edge.parent);
    if (set === undefined) into.set(edge.parent, (set = new Set()));
    set.add(edge.child);
  }

  // Decide, per parent, which child edges survive. This is purely local, so a parent that is
  // itself only reachable below another cap is still capped — a deep graph must not be
  // narrowed at the top layer and left thousands wide underneath.
  //
  // The placeholder counts against the budget, because it is a node ELK has to place: the
  // measurement is about how many children one parent hands the layout, and a "cap" that
  // emitted maxChildren + 1 would miss its own limit by exactly one every time.
  const rank = byRecencyThenId(index);
  const keptEdges: SpawnEdge[] = [];
  const hiddenCount = new Map<string, number>();
  for (const [parent, kids] of children) {
    const ordered = [...kids].sort(rank);
    // A parent that already carries a placeholder needs no SECOND slot for one.
    const room = maxChildren;
    const keep = ordered.length > room ? ordered.slice(0, Math.max(0, room - 1)) : ordered;
    for (const child of keep) keptEdges.push({ parent, child });
    if (ordered.length > keep.length) hiddenCount.set(parent, ordered.length - keep.length);
  }
  // A placeholder carried in from a previous pass is not re-emitted as a second node with
  // the same id — its tally is FOLDED INTO this pass's, so re-capping the same graph at a
  // tighter threshold deepens one placeholder instead of duplicating it. The count travels
  // in `child_count` rather than being re-parsed out of the "+N more" label.
  for (const [parent, kids] of carried) {
    for (const child of kids) {
      const prior = index.get(child)?.child_count ?? 0;
      if (hiddenCount.has(parent)) hiddenCount.set(parent, hiddenCount.get(parent)! + prior);
      else if (prior > 0) hiddenCount.set(parent, prior);
    }
  }

  // What survives. A node is kept when the capped graph still reaches it, so hiding a child
  // takes its subtree with it — leaving descendants behind would strand them as floating
  // orphans and would not reduce the node count, which is the entire point.
  //
  // Reachability is computed TWICE, over the original edges and over the kept ones, because
  // "unreachable" has two very different causes that must not be conflated:
  //   * unreachable in BOTH  -> a component with no entry point (e.g. a pure cycle). It was
  //     never reached by the seed rule, capping did not remove it, and deleting it would be
  //     the same silent-data-loss bug as the 1000-root truncation. Kept.
  //   * reachable originally, not after -> genuinely hidden behind a cap. Dropped.
  // The visited set also makes both walks terminate on cyclic data instead of hanging the UI.
  const allIds = [...index.keys()].sort();
  const hasParent = new Set(input.edges.filter((e) => e.parent !== e.child).map((e) => e.child));
  const seeds = allIds.filter((id) => !hasParent.has(id));
  const originally = reach(seeds, adjacencyOf(input.edges));
  const orphanComponents = allIds.filter((id) => !originally.has(id));
  const visible = reach([...seeds, ...orphanComponents], adjacencyOf(keptEdges));

  // A carried placeholder is re-emitted below with a merged count, so it must not also
  // survive as an input node — that is what produced two nodes sharing one id.
  const nodes = allIds
    .filter((id) => visible.has(id) && !isMoreId(id))
    .map((id) => index.get(id)!);
  const edges = keptEdges.filter(
    (e) => visible.has(e.parent) && visible.has(e.child) && !isMoreId(e.child),
  );

  // One placeholder per capped parent that is itself still on screen.
  for (const [parent, count] of [...hiddenCount].sort()) {
    if (!visible.has(parent)) continue;
    const id = `${MORE_ID_PREFIX}${parent}`;
    nodes.push({
      ...bareNode(id),
      title: `+${count} more`,
      // The tally lives here so a later pass can merge it exactly, instead of parsing it
      // back out of the display label.
      child_count: count,
      created_at_ms: index.get(parent)?.created_at_ms ?? null,
    });
    edges.push({ parent, child: id });
  }

  // Canonical order, as `aggregate.ts` does. Without it the output merely HAPPENS to be
  // sorted: placeholders are appended after the id-sorted pass, so feeding the result back in
  // (where the placeholder now sorts among the rest) returned the same graph in a different
  // order — the transform was not idempotent, and the canvas would reshuffle on a re-render.
  nodes.sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  edges.sort((a, b) =>
    a.parent === b.parent
      ? a.child < b.child
        ? -1
        : a.child > b.child
          ? 1
          : 0
      : a.parent < b.parent
        ? -1
        : 1,
  );
  return { nodes, edges };
}

/** Child lists keyed by parent, self-loops excluded. */
function adjacencyOf(edges: SpawnEdge[]): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const edge of edges) {
    if (edge.parent === edge.child) continue;
    const list = out.get(edge.parent);
    if (list === undefined) out.set(edge.parent, [edge.child]);
    else list.push(edge.child);
  }
  return out;
}

/** Every id reachable from `seeds`; the visited set bounds the walk on cyclic data. */
function reach(seeds: string[], children: Map<string, string[]>): Set<string> {
  const seen = new Set<string>();
  const stack = [...seeds];
  while (stack.length > 0) {
    const id = stack.pop()!;
    if (seen.has(id)) continue;
    seen.add(id);
    for (const child of children.get(id) ?? []) stack.push(child);
  }
  return seen;
}
