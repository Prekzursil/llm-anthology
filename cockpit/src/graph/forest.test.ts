/**
 * The forest decisions: how many round-trips it costs, and what actually reaches the canvas.
 *
 * The round-trip count is the point of the first half. It was 1 + N SERIAL requests over a
 * pipe that carries one at a time, and "it still worked" is exactly why nobody noticed — a
 * performance defect has no failing assertion unless something counts the calls.
 *
 * The `loadAllRoots` half is the same shape of defect one level down: a `limit` that was
 * never reached in testing has no failing assertion either, and a list that stops early
 * while looking complete is indistinguishable from a corpus that simply ends there.
 */
import { describe, expect, it, vi } from "vitest";

import {
  buildView,
  loadAllRoots,
  loadForest,
  MAX_ROOTS,
  ROOTS_PAGE_SIZE,
  rootsStatus,
  type ForestIpc,
  type RootsIpc,
} from "./forest";
import { DEFAULT_MAX_CHILDREN, MORE_ID_PREFIX } from "./capFanOut";
import type { RootsParams, SpawnEdge, ThreadNode } from "../ipc/types";

const NOW = 1_770_000_000_000;

function threadNode(id: string, createdMs: number | null = 1): ThreadNode {
  return { id, title: id, provider: "codex", created_at_ms: createdMs, child_count: 0, depth: 0 };
}

/**
 * A `graph.roots` over a corpus of exactly `total` roots, sliced the way the ENGINE slices:
 * `sidecar.py`'s `_graph_roots` answers `nodes[offset:offset + limit]` with no server-side
 * clamp on `limit`. Getting that detail right is what makes "a short page means the end"
 * a fact about the wire rather than an assumption about it.
 */
function rootsApi(total: number): RootsIpc & { calls: RootsParams[] } {
  const all = Array.from({ length: total }, (_, i) => threadNode(`r${i}`));
  const calls: RootsParams[] = [];
  return {
    calls,
    async graphRoots(params: RootsParams = {}) {
      calls.push(params);
      const offset = params.offset ?? 0;
      return all.slice(offset, offset + (params.limit ?? 100));
    },
  };
}

/** An ipc whose every method records that it was called. */
function fakeIpc(over: Partial<ForestIpc> = {}): ForestIpc & { calls: string[] } {
  const calls: string[] = [];
  const base: ForestIpc = {
    async graphAt(asOfMs) {
      calls.push(`graphAt(${asOfMs})`);
      return { nodes: [threadNode("a"), threadNode("b")], edges: [{ parent: "a", child: "b" }] };
    },
    async graphRoots(params) {
      calls.push(`graphRoots(${JSON.stringify(params)})`);
      return [threadNode("r1"), threadNode("r2")];
    },
    async graphSubtree(id) {
      calls.push(`graphSubtree(${id})`);
      return { nodes: [threadNode(id), threadNode(`${id}-kid`)],
               edges: [{ parent: id, child: `${id}-kid` }] };
    },
  };
  return Object.assign({ calls }, base, over);
}

describe("loadForest", () => {
  it("costs exactly ONE round-trip when the engine can snapshot", async () => {
    // The whole reason this function exists. Anything above 1 here is the old 1+N fan-out.
    const api = fakeIpc();
    await loadForest(api, NOW);
    expect(api.calls).toEqual([`graphAt(${NOW})`]);
  });

  it("returns the snapshot as the graph", async () => {
    const out = await loadForest(fakeIpc(), NOW);
    expect(out.nodes.map((n) => n.id)).toEqual(["a", "b"]);
    expect(out.edges).toEqual([{ parent: "a", child: "b" }]);
  });

  it("walks the roots when the engine has no snapshot method", async () => {
    // `graphAt` is optional on the IPC surface, so this path must keep working.
    const api = fakeIpc({ graphAt: undefined });
    const out = await loadForest(api, NOW);
    expect(api.calls).toEqual([
      `graphRoots({"limit":${ROOTS_PAGE_SIZE},"offset":0,"order":"recent"})`,
      "graphSubtree(r1)",
      "graphSubtree(r2)",
    ]);
    expect(out.nodes.map((n) => n.id).sort()).toEqual(["r1", "r1-kid", "r2", "r2-kid"]);
  });

  it("asks the fallback for RECENT roots, not the ascending default", async () => {
    // `created` is ascending in the engine, so a capped `created` walk returns the OLDEST
    // page and silently hides everything since — on a 2,000-session store, every recent month.
    const graphRoots = vi.fn(async () => [] as ThreadNode[]);
    await loadForest(fakeIpc({ graphAt: undefined, graphRoots }), NOW);
    expect(graphRoots).toHaveBeenCalledWith({
      limit: ROOTS_PAGE_SIZE,
      offset: 0,
      order: "recent",
    });
  });

  it("falls back to the walk when the snapshot rejects", async () => {
    const api = fakeIpc({
      graphAt: async () => {
        throw new Error("engine says no");
      },
    });
    const out = await loadForest(api, NOW);
    expect(api.calls).toContain("graphSubtree(r1)");
    expect(out.nodes).not.toHaveLength(0);
  });

  it("de-duplicates nodes and edges shared between two root subtrees", async () => {
    const api = fakeIpc({
      graphAt: undefined,
      async graphSubtree() {
        return { nodes: [threadNode("shared"), threadNode("kid")],
                 edges: [{ parent: "shared", child: "kid" }] };
      },
    });
    const out = await loadForest(api, NOW);
    expect(out.nodes.map((n) => n.id)).toEqual(["shared", "kid"]);
    expect(out.edges).toHaveLength(1);
  });

  it("returns an EMPTY forest rather than a partial one when the walk breaks midway", async () => {
    // A half-graph is indistinguishable from a complete one, in a tool whose entire subject
    // is the graph. Blank is honest; partial is a lie.
    let n = 0;
    const api = fakeIpc({
      graphAt: undefined,
      async graphSubtree(id) {
        if (++n > 1) throw new Error("pipe died");
        return { nodes: [threadNode(id)], edges: [] };
      },
    });
    expect(await loadForest(api, NOW)).toEqual({ nodes: [], edges: [] });
  });

  it("returns an empty forest when even the roots call fails", async () => {
    const api = fakeIpc({
      graphAt: undefined,
      graphRoots: async () => {
        throw new Error("not attached");
      },
    });
    expect(await loadForest(api, NOW)).toEqual({ nodes: [], edges: [] });
  });

  it("walks EVERY root, not just the first page", async () => {
    // The walk used to ask for one page and stop, so a corpus with more roots than the
    // page size lost the remainder — and the canvas drew the survivors as if that were
    // the whole forest.
    const roots = rootsApi(ROOTS_PAGE_SIZE + 500);
    const api = fakeIpc({ graphAt: undefined, graphRoots: roots.graphRoots });
    const out = await loadForest(api, NOW);
    expect(api.calls.filter((c) => c.startsWith("graphSubtree("))).toHaveLength(
      ROOTS_PAGE_SIZE + 500,
    );
    expect(out.nodes).toHaveLength(2 * (ROOTS_PAGE_SIZE + 500));
  });

  it("returns an EMPTY forest when a ceiling truncated the root list", async () => {
    // Same rule the mid-walk failure above follows. This pane has no status line, so a
    // forest grown from a truncated root list would be a partial graph wearing a complete
    // graph's clothes — and nothing on screen could tell the two apart.
    const api = fakeIpc({
      graphAt: undefined,
      // A corpus that never runs out: every page comes back full, so only the ceiling
      // can stop the walk.
      async graphRoots(params: RootsParams = {}) {
        const offset = params.offset ?? 0;
        return Array.from({ length: params.limit ?? 100 }, (_, i) => threadNode(`r${offset + i}`));
      },
    });
    expect(await loadForest(api, NOW)).toEqual({ nodes: [], edges: [] });
    // And it gave up BEFORE spending a subtree round-trip per root.
    expect(api.calls.filter((c) => c.startsWith("graphSubtree("))).toHaveLength(0);
  });
});

describe("loadAllRoots", () => {
  it("returns all 1,140 roots of the measured corpus, not the first 1,000", async () => {
    // THE DEFECT. The sidebar asked for `limit: 1000` against a store holding ~1,140 roots
    // (2,112 threads minus 972 distinct children), so 140 threads were unreachable and the
    // list said nothing about a remainder. A cap with no disclosure reads as "this is
    // everything" — the same class as the 1000-OLDEST ordering and the "1432 hits" printed
    // over a list of 200.
    const api = rootsApi(1140);
    const { roots, complete } = await loadAllRoots(api, 1000);
    expect(roots).toHaveLength(1140);
    expect(complete).toBe(true);
    expect(api.calls).toEqual([
      { limit: 1000, offset: 0, order: "recent" },
      { limit: 1000, offset: 1000, order: "recent" },
    ]);
  });

  it("costs exactly ONE round-trip when the corpus fits in a page", async () => {
    // The page size is chosen to be larger than the measured corpus, so the ordinary case
    // must not pay for the paging that protects the pathological one.
    const api = rootsApi(1140);
    const { roots, complete } = await loadAllRoots(api);
    expect(roots).toHaveLength(1140);
    expect(complete).toBe(true);
    expect(api.calls).toHaveLength(1);
  });

  it("reports an empty corpus as complete", async () => {
    const { roots, complete } = await loadAllRoots(rootsApi(0));
    expect(roots).toEqual([]);
    expect(complete).toBe(true);
  });

  it("stops at the ceiling and reports the walk as INCOMPLETE", async () => {
    const api = rootsApi(250);
    const { roots, complete } = await loadAllRoots(api, 100, 200);
    expect(roots).toHaveLength(200);
    expect(complete).toBe(false);
  });

  it("does not claim truncation for a corpus that is exactly the ceiling", async () => {
    // "We got a full last page" does NOT mean more exist. One extra one-row probe settles
    // it exactly, so the disclosure is never a lie in the other direction either.
    const api = rootsApi(200);
    const { roots, complete } = await loadAllRoots(api, 100, 200);
    expect(roots).toHaveLength(200);
    expect(complete).toBe(true);
    // Not `.at(-1)`: this tsconfig targets ES2020, where `Array.prototype.at` does not exist.
    expect(api.calls[api.calls.length - 1]).toEqual({ limit: 1, offset: 200, order: "recent" });
  });

  it("never asks for more rows than the ceiling allows it to keep", async () => {
    const api = rootsApi(1000);
    await loadAllRoots(api, 400, 500);
    expect(api.calls.map((c) => c.limit)).toEqual([400, 100, 1]);
  });

  it("asks for RECENT order on every page, not just the first", async () => {
    // A walk that forgot the order on page 2 would page through two DIFFERENT sortings and
    // silently duplicate some roots while dropping others.
    const api = rootsApi(1140);
    await loadAllRoots(api, 1000);
    expect(api.calls.every((c) => c.order === "recent")).toBe(true);
  });

  it("terminates on a non-positive page size instead of asking for zero rows forever", async () => {
    // `Math.min(0, …)` would ask for nothing, receive nothing, and never advance the
    // offset — a hang, not an error, which is the worst way for this to fail.
    const api = rootsApi(3);
    const { roots, complete } = await loadAllRoots(api, 0);
    expect(roots).toHaveLength(3);
    expect(complete).toBe(true);
  });

  it("propagates a failure rather than passing off a partial walk as the corpus", async () => {
    // Each caller has its own failure policy (the sidebar empties the list, the forest
    // blanks the pane); inventing a third one here would hide the error from both.
    const api: RootsIpc = {
      async graphRoots() {
        throw new Error("not attached");
      },
    };
    await expect(loadAllRoots(api)).rejects.toThrow("not attached");
  });
});

describe("rootsStatus", () => {
  it("counts the threads when the list is everything", () => {
    expect(rootsStatus(1140, true)).toBe("1,140 threads");
  });

  it("does not pluralise a single thread", () => {
    expect(rootsStatus(1, true)).toBe("1 thread");
  });

  it("says nothing for an empty corpus — the list shows its own empty state", () => {
    // Two messages saying "nothing here" stacked on top of each other reads as a fault.
    expect(rootsStatus(0, true)).toBe("");
  });

  it("SAYS SO when a ceiling truncated the walk", () => {
    // The entire point of the pair: whatever the cap is, the user is never shown a
    // truncated list that looks complete.
    expect(rootsStatus(MAX_ROOTS, false)).toBe("showing the first 20,000 threads · more exist");
  });
});

describe("buildView", () => {
  /** One parent with `n` children — the shape that kills the layout. */
  function wide(n: number) {
    const nodes = [threadNode("p", 0)];
    const edges: SpawnEdge[] = [];
    for (let i = 0; i < n; i++) {
      nodes.push(threadNode(`c${String(i).padStart(4, "0")}`, 100 + i));
      edges.push({ parent: "p", child: `c${String(i).padStart(4, "0")}` });
    }
    return { nodes, edges };
  }

  it("bounds the widest layer even with aggregation OFF", () => {
    // The cap is not part of the aggregated toggle. A user who never touches that button
    // still must not be handed a 4,844-wide layer.
    const { view } = buildView(wide(1000), false);
    const perParent = new Map<string, number>();
    for (const e of view.edges) perParent.set(e.parent, (perParent.get(e.parent) ?? 0) + 1);
    expect(Math.max(...perParent.values())).toBeLessThanOrEqual(DEFAULT_MAX_CHILDREN);
  });

  it("bounds the widest layer with aggregation ON as well", () => {
    // Folding linear CHAINS does not narrow a fan-out; the two transforms address opposite
    // shapes, so capping only in one branch would leave the other unrenderable.
    const { view } = buildView(wide(1000), true);
    const perParent = new Map<string, number>();
    for (const e of view.edges) perParent.set(e.parent, (perParent.get(e.parent) ?? 0) + 1);
    expect(Math.max(...perParent.values())).toBeLessThanOrEqual(DEFAULT_MAX_CHILDREN);
  });

  it("reports how many nodes it removed", () => {
    const { hiddenCount, view } = buildView(wide(1000), false);
    // 1001 in; 1 parent + 99 kept + 1 placeholder out.
    expect(view.nodes).toHaveLength(101);
    expect(hiddenCount).toBe(1001 - 101);
  });

  it("reports the per-placeholder tally the detail panel needs", () => {
    const { moreCounts } = buildView(wide(1000), false);
    expect([...moreCounts]).toEqual([[`${MORE_ID_PREFIX}p`, 901]]);
  });

  it("reports nothing hidden for a graph that fits", () => {
    const { hiddenCount, moreCounts } = buildView(wide(5), false);
    expect(hiddenCount).toBe(0);
    expect(moreCounts.size).toBe(0);
  });

  it("still folds linear chains when aggregation is on", () => {
    // The cap must not have quietly disabled the feature it runs after.
    const nodes = ["a", "b", "c", "d"].map((id) => threadNode(id));
    const edges: SpawnEdge[] = [
      { parent: "a", child: "b" },
      { parent: "b", child: "c" },
      { parent: "c", child: "d" },
    ];
    const folded = buildView({ nodes, edges }, true).view;
    expect(folded.nodes.some((n) => n.id.startsWith("chain:"))).toBe(true);
    expect(folded.nodes.length).toBeLessThan(4);
  });

  it("leaves a small graph's content alone, in canonical order", () => {
    // Content-identical, not byte-identical: the cap emits nodes and edges sorted by id so
    // two renders of the same graph are the same graph, which means the output order need
    // not match whatever order the engine happened to return.
    const input = wide(3);
    const { view } = buildView(input, false);
    expect(view.nodes.map((n) => n.id).sort()).toEqual(input.nodes.map((n) => n.id).sort());
    expect(view.edges).toEqual([...input.edges].sort((a, b) => (a.child < b.child ? -1 : 1)));
  });
});
