import { describe, expect, it } from "vitest";

import type { ElkNode } from "elkjs/lib/elk-api";

import type { ThreadNode } from "../ipc/types";
import {
  buildElkGraph,
  buildNodeIndex,
  DEFAULT_LAYOUT_CONFIG,
  extractLayout,
  isCrossProvider,
  nodeLabel,
  nodeSize,
  type LayoutInput,
} from "./layout";

function tn(id: string, provider: string, title = id): ThreadNode {
  return { id, title, provider, model_provider: "", created_at_ms: 1, child_count: 0,
           depth: 0 };
}

const INPUT: LayoutInput = {
  nodes: [tn("A", "claude", "Root A"), tn("B", "codex", "Child B")],
  edges: [
    { parent: "A", child: "B", status: "completed" }, // cross claude -> codex
    { parent: "X", child: "A" }, // X is a dangling parent (no node row)
    { parent: "A", child: "A" }, // self-loop, must be dropped
  ],
};

describe("buildNodeIndex", () => {
  it("unions edge endpoints and synthesizes dangling nodes", () => {
    const index = buildNodeIndex(INPUT);
    expect([...index.keys()].sort()).toEqual(["A", "B", "X"]);
    const x = index.get("X");
    expect(x?.provider).toBe("");
    expect(x?.created_at_ms).toBeNull();
  });
});

describe("buildElkGraph", () => {
  const graph = buildElkGraph(INPUT);

  it("pins the layered / BRANDES_KOEPF SOTA options", () => {
    expect(graph.id).toBe("root");
    expect(graph.layoutOptions?.["elk.algorithm"]).toBe("layered");
    expect(graph.layoutOptions?.["elk.layered.nodePlacement.strategy"]).toBe("BRANDES_KOEPF");
    // Guard against the banned strategy sneaking back in.
    expect(graph.layoutOptions?.["elk.layered.nodePlacement.strategy"]).not.toBe(
      "NETWORK_SIMPLEX",
    );
  });

  it("materializes every node (including the dangling parent) as a child", () => {
    const ids = (graph.children ?? []).map((c) => c.id).sort();
    expect(ids).toEqual(["A", "B", "X"]);
    for (const child of graph.children ?? []) {
      expect(child.width).toBeGreaterThan(0);
      expect(child.height).toBe(DEFAULT_LAYOUT_CONFIG.nodeHeight);
    }
  });

  it("drops self-loops and keeps real edges", () => {
    const edges = graph.edges ?? [];
    expect(edges).toHaveLength(2);
    expect(edges.map((e) => e.sources[0]).sort()).toEqual(["A", "X"]);
    for (const e of edges) {
      expect(e.sources).toHaveLength(1);
      expect(e.targets).toHaveLength(1);
    }
  });
});

describe("node sizing", () => {
  it("labels a titled node by title and a bare node by id", () => {
    expect(nodeLabel(tn("N", "claude", "A title"))).toBe("A title");
    expect(nodeLabel(tn("bare-id", "", ""))).toBe("bare-id");
  });

  it("clamps width between the configured bounds", () => {
    const tiny = nodeSize(tn("x", "claude", "x"));
    expect(tiny.width).toBe(DEFAULT_LAYOUT_CONFIG.minNodeWidth);
    const huge = nodeSize(tn("y", "claude", "y".repeat(500)));
    expect(huge.width).toBe(DEFAULT_LAYOUT_CONFIG.maxNodeWidth);
  });
});

describe("isCrossProvider", () => {
  it("is true only when both providers are known and differ", () => {
    expect(isCrossProvider(tn("a", "claude"), tn("b", "codex"))).toBe(true);
    expect(isCrossProvider(tn("a", "claude"), tn("b", "claude"))).toBe(false);
    expect(isCrossProvider(tn("a", "claude"), tn("b", ""))).toBe(false);
    expect(isCrossProvider(undefined, tn("b", "codex"))).toBe(false);
  });
});

describe("extractLayout", () => {
  const laid: ElkNode = {
    id: "root",
    width: 300,
    height: 200,
    children: [
      { id: "A", x: 10, y: 10, width: 120, height: 44 },
      { id: "B", x: 20, y: 100, width: 120, height: 44 },
      { id: "X", x: 200, y: 10, width: 120, height: 44 },
    ],
    edges: [
      {
        id: "A B",
        sources: ["A"],
        targets: ["B"],
        sections: [
          {
            id: "s1",
            startPoint: { x: 70, y: 54 },
            endPoint: { x: 80, y: 100 },
            bendPoints: [{ x: 75, y: 80 }],
          },
        ],
      },
      {
        id: "X A",
        sources: ["X"],
        targets: ["A"],
        sections: [{ id: "s2", startPoint: { x: 200, y: 54 }, endPoint: { x: 130, y: 20 } }],
      },
    ],
  };

  const positioned = extractLayout(laid, INPUT);

  it("maps children to positioned nodes with their ThreadNode attached", () => {
    expect(positioned.nodes).toHaveLength(3);
    const a = positioned.nodes.find((n) => n.id === "A");
    expect(a?.x).toBe(10);
    expect(a?.node.provider).toBe("claude");
    const x = positioned.nodes.find((n) => n.id === "X");
    expect(x?.node.provider).toBe(""); // synthesized dangling node
  });

  it("flattens edge sections into polylines, keeping cross + status", () => {
    const ab = positioned.edges.find((e) => e.parent === "A" && e.child === "B");
    expect(ab?.cross).toBe(true);
    expect(ab?.status).toBe("completed");
    expect(ab?.points).toEqual([
      { x: 70, y: 54 },
      { x: 75, y: 80 },
      { x: 80, y: 100 },
    ]);

    const xa = positioned.edges.find((e) => e.parent === "X" && e.child === "A");
    expect(xa?.cross).toBe(false);
    expect(xa?.status).toBeUndefined();
    expect(xa?.points).toHaveLength(2);
  });

  it("computes a bounding box over nodes and edge points", () => {
    // maxX: X node right edge 200+120=320; maxY: B node bottom 100+44=144, but the
    // ELK-reported height (200) is larger and wins.
    expect(positioned.width).toBe(320);
    expect(positioned.height).toBe(200);
  });
});

/**
 * The DEGENERATE ELK result. Every geometry field in `ElkNode`/`ElkExtendedEdge` is OPTIONAL
 * in elkjs's own declarations (`elkjs/lib/elk-api.d.ts`: `x?`, `y?`, `width?`, `height?`,
 * `children?`, `edges?`, `sections?`), and `extractLayout` carries a fallback for each one.
 *
 * Those fallbacks are the mapping's behaviour on a result that is legal but sparse — an empty
 * graph, a single node ELK places at the origin, or a partial result mapped after the worker's
 * layout guard fires. Untested, a wrong default here does not crash: it silently stacks every
 * node at (0,0) with zero size, or produces an edge whose endpoints name no node, and the
 * canvas draws that without complaint. So each fallback is asserted on its OBSERVABLE output.
 */
describe("extractLayout on a degenerate ELK result", () => {
  const EMPTY: LayoutInput = { nodes: [], edges: [] };

  it("maps a childless, edgeless graph to an empty positioned graph of zero extent", () => {
    const out = extractLayout({ id: "root" }, EMPTY);
    expect(out.nodes).toEqual([]);
    expect(out.edges).toEqual([]);
    // No node boxes, no edge points and no ELK-reported size: the extent is 0, not NaN, which
    // is what an unguarded `laidOut.width` would produce downstream in a viewport fit.
    expect(out.width).toBe(0);
    expect(out.height).toBe(0);
  });

  it("places a child with no geometry at the origin with zero size", () => {
    const out = extractLayout({ id: "root", children: [{ id: "A" }] }, INPUT);
    expect(out.nodes).toHaveLength(1);
    expect(out.nodes[0]).toMatchObject({ id: "A", x: 0, y: 0, width: 0, height: 0 });
    // The original DTO is still attached — the missing geometry must not lose the node.
    expect(out.nodes[0].node.title).toBe("Root A");
  });

  it("falls back to the computed extent when ELK reports no graph size", () => {
    // `Math.max(maxX, laidOut.width ?? 0)` — with no reported size the node boxes decide.
    const out = extractLayout(
      { id: "root", children: [{ id: "A", x: 10, y: 20, width: 30, height: 40 }] },
      INPUT,
    );
    expect(out.width).toBe(40);
    expect(out.height).toBe(60);
  });

  it("synthesizes a ThreadNode for an ELK id the input never mentioned", () => {
    // ELK echoes back the ids it was given, so in the shipped pipeline every child is in the
    // index. This is the defensive arm, and it is asserted EXACTLY because its shape differs
    // from `buildNodeIndex`'s synthesized node: `model_provider` is absent here rather than
    // "". Anything tinting by `model_provider` therefore reads `undefined` for this node.
    const out = extractLayout(
      { id: "root", children: [{ id: "ZZZ", x: 1, y: 2, width: 3, height: 4 }] },
      INPUT,
    );
    expect(out.nodes[0].node).toEqual({
      id: "ZZZ",
      title: "",
      provider: "",
      created_at_ms: null,
      child_count: 0,
      depth: 0,
    });
    expect(buildNodeIndex(INPUT).get("X")?.model_provider).toBe("");
  });

  it("reads an edge with no sections as an empty polyline", () => {
    // Both spellings ELK can use for "no route computed" must land on the same answer, so
    // this is checked in both states rather than only the one that happens to be produced.
    const missing = extractLayout(
      { id: "root", edges: [{ id: "e1", sources: ["A"], targets: ["B"] }] },
      INPUT,
    );
    expect(missing.edges[0].points).toEqual([]);
    // …and the rest of the edge is still mapped: an unrouted cross-provider spawn keeps its
    // cross flag and status, so it is drawn (as a straight line) rather than dropped.
    expect(missing.edges[0]).toMatchObject({ parent: "A", child: "B", cross: true,
                                             status: "completed" });

    const empty = extractLayout(
      { id: "root", edges: [{ id: "e1", sources: ["A"], targets: ["B"], sections: [] }] },
      INPUT,
    );
    expect(empty.edges).toEqual(missing.edges);
  });

  it("reads a sourceless / targetless edge as empty endpoints", () => {
    const out = extractLayout(
      { id: "root", edges: [{ id: "e1", sources: [], targets: [] }] },
      INPUT,
    );
    // "" names no node, so the pair cannot be cross-provider and carries no status — the
    // alternative (indexing `undefined`) would put `undefined` in the drawn edge list.
    expect(out.edges).toEqual([{ parent: "", child: "", cross: false, points: [] }]);
  });

  it("grows the bounding box to reach an edge point outside every node box", () => {
    // The edge-point half of the extent. A box computed from nodes alone clips a polyline
    // that ELK routed around them, so the far end of that edge is drawn off-canvas.
    const out = extractLayout(
      {
        id: "root",
        children: [{ id: "A", x: 0, y: 0, width: 10, height: 10 }],
        edges: [
          {
            id: "e1",
            sources: ["A"],
            targets: ["B"],
            sections: [
              { id: "s", startPoint: { x: 5, y: 5 }, endPoint: { x: 900, y: 700 } },
            ],
          },
        ],
      },
      INPUT,
    );
    expect(out.width).toBe(900);
    expect(out.height).toBe(700);
  });
});
