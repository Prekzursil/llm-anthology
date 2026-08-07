import { describe, expect, it } from "vitest";

import type { RollupMetrics, RollupTable, SpawnEdge, ThreadNode } from "../ipc/types";
import {
  type AggregateGraph,
  type AggregateNode,
  collapseLinearChains,
  foldSubtree,
} from "./aggregate";

/** Build a synthetic ThreadNode; `tokens` omitted (undefined) unless given. */
function tn(id: string, title: string, tokens?: number, provider = "claude"): ThreadNode {
  const n: ThreadNode = { id, title, provider, model_provider: "", created_at_ms: 1,
    child_count: 0, depth: 0 };
  if (tokens !== undefined) n.tokens = tokens;
  return n;
}

function roll(subtree_tokens: number, subtree_count: number): RollupMetrics {
  return {
    self_tokens: 1,
    subtree_tokens,
    self_count: 1,
    subtree_count,
    max_depth: 2,
    child_count: 2,
  };
}

// ---------------------------------------------------------------------------
// collapseLinearChains
// ---------------------------------------------------------------------------
describe("collapseLinearChains", () => {
  // A single graph exercising every structural case:
  //   * P -> a, P -> z          P branches (fan-out 2): a is a chain HEAD though it
  //                             has a single parent, because that parent branches.
  //   * a -> b -> c             a pure 3-node chain (c has an EMPTY title + no tokens).
  //   * m -> n                  a 2-node run (below the default min length).
  //   * g -> h, x -> h          h is a MERGE (fan-in 2): neither g nor x chains into it.
  //   * a -> a                  a self-loop (must be ignored everywhere).
  const a = tn("a", "Alpha", 10);
  const b = tn("b", "Beta", 20);
  const c = tn("c", ""); // empty title -> labelled by id; no tokens -> counts as 0
  const P = tn("P", "Papa", 5);
  const z = tn("z", ""); // empty title, no tokens -> single with tokens 0
  const m = tn("m", "Mike", 2);
  const n = tn("n", "November", 3);
  const g = tn("g", "Golf", 7);
  const x = tn("x", "Xray", 8);
  const h = tn("h", "Hotel", 9);

  const NODES: ThreadNode[] = [a, b, c, P, z, m, n, g, x, h];
  const EDGES: SpawnEdge[] = [
    { parent: "P", child: "a", status: "completed" },
    { parent: "P", child: "z" },
    { parent: "a", child: "b" },
    { parent: "b", child: "c" },
    { parent: "m", child: "n" },
    { parent: "g", child: "h" },
    { parent: "x", child: "h" },
    { parent: "a", child: "a" }, // self-loop
  ];

  it("folds a maximal 3-node chain and passes everything else through as singles", () => {
    const out = collapseLinearChains(NODES, EDGES);
    const expected: AggregateNode[] = [
      { id: "P", kind: "single", label: "Papa", members: ["P"], representative: P, tokens: 5, count: 1 },
      {
        id: "chain:a",
        kind: "chain",
        label: "Alpha…c (3)",
        members: ["a", "b", "c"],
        representative: a,
        tokens: 30, // 10 + 20 + (c has no tokens -> 0)
        count: 3,
      },
      { id: "g", kind: "single", label: "Golf", members: ["g"], representative: g, tokens: 7, count: 1 },
      { id: "h", kind: "single", label: "Hotel", members: ["h"], representative: h, tokens: 9, count: 1 },
      { id: "m", kind: "single", label: "Mike", members: ["m"], representative: m, tokens: 2, count: 1 },
      { id: "n", kind: "single", label: "November", members: ["n"], representative: n, tokens: 3, count: 1 },
      { id: "x", kind: "single", label: "Xray", members: ["x"], representative: x, tokens: 8, count: 1 },
      { id: "z", kind: "single", label: "z", members: ["z"], representative: z, tokens: 0, count: 1 },
    ];
    expect(out.nodes).toEqual(expected);
  });

  it("re-points boundary edges onto the super-node and drops internal + self edges", () => {
    const out = collapseLinearChains(NODES, EDGES);
    // status is dropped on aggregate edges (edge identity is the (parent, child) pair).
    const expected: SpawnEdge[] = [
      { parent: "P", child: "chain:a" },
      { parent: "P", child: "z" },
      { parent: "g", child: "h" },
      { parent: "m", child: "n" },
      { parent: "x", child: "h" },
    ];
    expect(out.edges).toEqual(expected);
  });

  it("honours a lower minLength, folding the 2-node run too", () => {
    const out = collapseLinearChains(NODES, EDGES, 2);
    const ids = out.nodes.map((node) => node.id);
    expect(ids).toContain("chain:a");
    expect(ids).toContain("chain:m");
    // the folded members no longer appear as their own singles
    expect(ids).not.toContain("m");
    expect(ids).not.toContain("n");
    const chainM = out.nodes.find((node) => node.id === "chain:m");
    expect(chainM).toEqual({
      id: "chain:m",
      kind: "chain",
      label: "Mike…November (2)",
      members: ["m", "n"],
      representative: m,
      tokens: 5,
      count: 2,
    });
    // g is a single-node run (length 1) -> never folds, even at minLength 2.
    expect(ids).toContain("g");
  });

  it("returns an empty graph for empty input", () => {
    const out: AggregateGraph = collapseLinearChains([], []);
    expect(out).toEqual({ nodes: [], edges: [] });
  });

  it("synthesizes bare nodes for ids that appear only on edges", () => {
    // 'root' has no node row; it is the head of root -> k1 -> k2.
    const k1 = tn("k1", "K1", 1);
    const k2 = tn("k2", "K2", 2);
    const out = collapseLinearChains([k1, k2], [
      { parent: "root", child: "k1" },
      { parent: "k1", child: "k2" },
    ]);
    const chain = out.nodes.find((node) => node.id === "chain:root");
    expect(chain?.members).toEqual(["k1", "k2", "root"]);
    expect(chain?.count).toBe(3);
    // the dangling 'root' (empty title) is labelled by its id; 'k2' by its title.
    expect(chain?.label).toBe("root…K2 (3)");
    expect(chain?.representative).toEqual({
      id: "root",
      title: "",
      provider: "",
      // an id known only from an edge has neither adapter nor model vendor
      model_provider: "",
      created_at_ms: null,
      child_count: 0,
      depth: 0,
    });
  });
});

// ---------------------------------------------------------------------------
// foldSubtree
// ---------------------------------------------------------------------------
describe("foldSubtree", () => {
  it("collapses a diamond subtree into one super-node carrying its rollup weight", () => {
    const r = tn("r", "Root", 1);
    const a = tn("a", "A", 2);
    const b = tn("b", "B", 3);
    const d = tn("d", "D", 4);
    const P = tn("P", "Papa", 100);
    const o1 = tn("o1", "O1", 200);
    const o2 = tn("o2", "O2", 300);
    const nodes: ThreadNode[] = [r, a, b, d, P, o1, o2];
    const edges: SpawnEdge[] = [
      { parent: "r", child: "a" },
      { parent: "r", child: "b" },
      { parent: "a", child: "d" }, // diamond: d reached via a...
      { parent: "b", child: "d" }, // ...and via b (BFS re-visit)
      { parent: "P", child: "r" }, // external parent -> subtree root
      { parent: "P", child: "o1" }, // external -> external
      { parent: "o1", child: "o2" }, // external -> external
      { parent: "o2", child: "a" }, // external -> internal (non-root)
      { parent: "o2", child: "d" }, // external -> internal (dup remap of o2 -> subtree:r)
      { parent: "r", child: "r" }, // self-loop
    ];
    const rollupById: RollupTable = { r: roll(999, 4) };

    const out = foldSubtree(nodes, edges, rollupById, "r");

    expect(out.nodes).toEqual([
      { id: "P", kind: "single", label: "Papa", members: ["P"], representative: P, tokens: 100, count: 1 },
      { id: "o1", kind: "single", label: "O1", members: ["o1"], representative: o1, tokens: 200, count: 1 },
      { id: "o2", kind: "single", label: "O2", members: ["o2"], representative: o2, tokens: 300, count: 1 },
      {
        id: "subtree:r",
        kind: "subtree",
        label: "Root (4)",
        members: ["a", "b", "d", "r"],
        representative: r,
        tokens: 999, // authoritative rollup weight, NOT the 1+2+3+4 self-token sum
        count: 4,
      },
    ]);
    expect(out.edges).toEqual([
      { parent: "P", child: "o1" },
      { parent: "P", child: "subtree:r" },
      { parent: "o1", child: "o2" },
      { parent: "o2", child: "subtree:r" },
    ]);
  });

  it("synthesizes an absent root and derives weight when no rollup is present", () => {
    const out = foldSubtree([], [], {}, "ghost");
    expect(out.edges).toEqual([]);
    expect(out.nodes).toEqual([
      {
        id: "subtree:ghost",
        kind: "subtree",
        label: "ghost (1)",
        members: ["ghost"],
        representative: {
          id: "ghost",
          title: "",
          provider: "",
          model_provider: "",
          created_at_ms: null,
          child_count: 0,
          depth: 0,
        },
        tokens: 0, // derived: bare node has no tokens -> 0
        count: 1,
      },
    ]);
  });

  it("derives weight by summing member self-tokens when the rollup lacks the root", () => {
    const p = tn("p", "Pee", 5);
    const q = tn("q", "Que", 7);
    const out = foldSubtree([p, q], [{ parent: "p", child: "q" }], {}, "p");
    expect(out.edges).toEqual([]);
    expect(out.nodes).toEqual([
      {
        id: "subtree:p",
        kind: "subtree",
        label: "Pee (2)",
        members: ["p", "q"],
        representative: p,
        tokens: 12, // derived 5 + 7
        count: 2,
      },
    ]);
  });
});
