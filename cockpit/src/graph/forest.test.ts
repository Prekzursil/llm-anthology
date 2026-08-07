/**
 * The forest decisions: how many round-trips it costs, and what actually reaches the canvas.
 *
 * The round-trip count is the point of the first half. It was 1 + N SERIAL requests over a
 * pipe that carries one at a time, and "it still worked" is exactly why nobody noticed — a
 * performance defect has no failing assertion unless something counts the calls.
 */
import { describe, expect, it, vi } from "vitest";

import { buildView, loadForest, type ForestIpc } from "./forest";
import { DEFAULT_MAX_CHILDREN, MORE_ID_PREFIX } from "./capFanOut";
import type { SpawnEdge, ThreadNode } from "../ipc/types";

const NOW = 1_770_000_000_000;

function threadNode(id: string, createdMs: number | null = 1): ThreadNode {
  return { id, title: id, provider: "codex", created_at_ms: createdMs, child_count: 0, depth: 0 };
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
      'graphRoots({"limit":1000,"order":"recent"})',
      "graphSubtree(r1)",
      "graphSubtree(r2)",
    ]);
    expect(out.nodes.map((n) => n.id).sort()).toEqual(["r1", "r1-kid", "r2", "r2-kid"]);
  });

  it("asks the fallback for RECENT roots, not the ascending default", async () => {
    // `created` is ascending in the engine, so a 1000 cap returns the OLDEST thousand and
    // silently hides everything since — on a 2,000-session store, every recent month.
    const graphRoots = vi.fn(async () => [] as ThreadNode[]);
    await loadForest(fakeIpc({ graphAt: undefined, graphRoots }), NOW);
    expect(graphRoots).toHaveBeenCalledWith({ limit: 1000, order: "recent" });
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
