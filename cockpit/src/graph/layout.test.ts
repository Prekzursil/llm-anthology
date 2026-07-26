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
  return { id, title, provider, created_at_ms: 1, child_count: 0, depth: 0 };
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
