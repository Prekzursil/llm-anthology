import { describe, expect, it } from "vitest";

import { createMockIpc, MockGraph, mockIpc, MOCK_EDGES, MOCK_THREADS } from "./mock";

describe("mock dataset shape", () => {
  it("is a ~15-node / 20-edge, 2-provider forest", () => {
    expect(MOCK_THREADS).toHaveLength(15);
    expect(MOCK_EDGES).toHaveLength(20);
    const providers = new Set(MOCK_THREADS.map((t) => t.provider));
    expect([...providers].sort()).toEqual(["claude", "codex"]);
  });

  it("has exactly one dangling parent (an edge id with no thread row)", () => {
    const ids = new Set(MOCK_THREADS.map((t) => t.id));
    const dangling = new Set<string>();
    for (const e of MOCK_EDGES) {
      for (const end of [e.parent, e.child]) if (!ids.has(end)) dangling.add(end);
    }
    expect([...dangling]).toEqual(["pruned-parent"]);
  });

  it("carries 9 cross-provider edges (both providers known and different)", () => {
    const provider = new Map(MOCK_THREADS.map((t) => [t.id, t.provider]));
    const cross = MOCK_EDGES.filter((e) => {
      const p = provider.get(e.parent);
      const c = provider.get(e.child);
      return p !== undefined && c !== undefined && p !== "" && c !== "" && p !== c;
    });
    expect(cross).toHaveLength(9);
  });
});

describe("MockGraph semantics (mirrors corpus.py)", () => {
  it("computes shortest-path depth, roots, fan-out over the full forest", () => {
    const g = new MockGraph(MOCK_THREADS, MOCK_EDGES);
    expect(g.roots()).toEqual(["orch", "pruned-parent", "repro", "research"]);
    // The required depth-3 chain: orch -> plan -> layout -> deepfix.
    expect(g.depth("orch")).toBe(0);
    expect(g.depth("plan")).toBe(1);
    expect(g.depth("layout")).toBe(2);
    expect(g.depth("deepfix")).toBe(3);
    // Diamond nodes report their shallowest position.
    expect(g.depth("tests")).toBe(1);
    expect(g.depth("review")).toBe(2);
    expect(g.fanOut("orch")).toBe(3);
    expect(g.childrenOf("pruned-parent")).toEqual(["orphan"]);
  });

  it("synthesizes a bare node for a dangling id and survives cycles", () => {
    // a <-> b cycle plus a dangling 'ghost' parent of 'a'.
    const g = new MockGraph(
      [{ id: "a", title: "A", provider: "claude", created_at_ms: 1 }],
      [
        { parent: "ghost", child: "a" },
        { parent: "a", child: "b" },
        { parent: "b", child: "a" }, // back-edge
      ],
    );
    const ghost = g.node("ghost");
    expect(ghost.provider).toBe("");
    expect(ghost.created_at_ms).toBeNull();
    expect(g.roots()).toEqual(["ghost"]);
    // depth terminates despite the a<->b cycle.
    expect(g.depth("a")).toBe(1);
  });
});

describe("mockIpc IpcClient contract", () => {
  it("health.ping reports a ready corpus", async () => {
    const h = await mockIpc.healthPing();
    expect(h.ok).toBe(true);
    expect(h.corpus_ready).toBe(true);
    expect(typeof h.engine_version).toBe("string");
  });

  it("corpus.stats aggregates conversations, threads, edges and providers", async () => {
    const s = await mockIpc.corpusStats();
    expect(s.conversations).toBe(15);
    expect(s.threads).toBe(15);
    expect(s.edges).toBe(20);
    expect(s.providers).toEqual({ claude: 8, codex: 7 });
    expect(s.records).toBeGreaterThan(0);
    expect(s.bytes).toBeGreaterThan(0);
  });

  it("graph.roots projects the dangling root as a bare node", async () => {
    const roots = await mockIpc.graphRoots();
    const pruned = roots.find((n) => n.id === "pruned-parent");
    expect(pruned).toBeDefined();
    expect(pruned?.provider).toBe("");
    expect(pruned?.created_at_ms).toBeNull();
    expect(pruned?.child_count).toBe(1);
    expect(pruned?.depth).toBe(0);
  });

  it("graph.roots honours the title ordering (by title, as the sidecar does)", async () => {
    const roots = await mockIpc.graphRoots({ order: "title" });
    // Sidecar `_order_nodes` sorts by `title.lower()`, so a dangling node (title "")
    // sorts first — the mock must match that, not fall back to id.
    const titles = roots.map((n) => n.title.toLowerCase());
    const sorted = [...titles].sort((a, b) => a.localeCompare(b));
    expect(titles).toEqual(sorted);
    expect(titles[0]).toBe(""); // the dangling 'pruned-parent' leads
  });

  it("graph.subtree(orch) reaches the depth-3 leaf and returns its edges", async () => {
    const sub = await mockIpc.graphSubtree("orch");
    const ids = sub.nodes.map((n) => n.id);
    expect(ids).toContain("deepfix");
    expect(ids).not.toContain("repro"); // a different tree
    expect(sub.edges.length).toBeGreaterThan(0);
    for (const e of sub.edges) {
      expect(ids).toContain(e.parent);
      expect(ids).toContain(e.child);
    }
  });

  it("graph.children and graph.ancestors walk the edges", async () => {
    expect((await mockIpc.graphChildren("pruned-parent")).map((n) => n.id)).toEqual([
      "orphan",
    ]);
    expect((await mockIpc.graphAncestors("deepfix")).map((n) => n.id)).toEqual([
      "layout",
      "plan",
      "orch",
    ]);
  });

  it("search.query matches titles/previews and filters by provider", async () => {
    const elk = await mockIpc.searchQuery({ q: "ELK" });
    expect(elk.total).toBe(2);
    const hitIds = elk.hits.map((h) => h.thread_id);
    expect(hitIds).toContain("layout");
    expect(hitIds).toContain("research");

    const codexOnly = await mockIpc.searchQuery({ q: "the", provider: "codex" });
    expect(codexOnly.hits.every((h) => h.provider === "codex")).toBe(true);
    expect(codexOnly.hits.length).toBeGreaterThan(0);
  });

  it("thread.get returns full meta for a real row and rejects a dangling id", async () => {
    const meta = await mockIpc.threadGet("orch");
    expect(meta.child_count).toBe(3);
    expect(meta.depth).toBe(0);
    expect(meta.tokens).toBe(125000);
    expect(meta.has_rollout).toBe(false);
    await expect(mockIpc.threadGet("pruned-parent")).rejects.toThrow();
  });

  it("conversation.get returns a body for a known id and a stub otherwise", async () => {
    const conv = await mockIpc.conversationGet("orch");
    expect(conv.available).toBe(true);
    if (conv.available) expect(conv.turns).toHaveLength(2);
    const missing = await mockIpc.conversationGet("nope");
    expect(missing.available).toBe(false);
  });
});

describe("createMockIpc over custom data", () => {
  it("serves a caller-supplied graph", async () => {
    const ipc = createMockIpc(
      [
        { id: "a", title: "A", provider: "claude", created_at_ms: 1 },
        { id: "b", title: "B", provider: "codex", created_at_ms: 2 },
        { id: "c", title: "C", provider: "claude", created_at_ms: 3 },
      ],
      [
        { parent: "a", child: "b", status: "completed" },
        { parent: "b", child: "c" },
      ],
    );
    expect((await ipc.graphRoots()).map((n) => n.id)).toEqual(["a"]);
    expect((await ipc.threadGet("c")).depth).toBe(2);
    const sub = await ipc.graphSubtree("a");
    expect(sub.nodes.map((n) => n.id).sort()).toEqual(["a", "b", "c"]);
    expect(sub.edges).toHaveLength(2);
  });
});

// ---------------------------------------------------------------------------
// Phase-3 bite-2: time-travel (rollup / timeline / at / diff) + export surface.
// ---------------------------------------------------------------------------

/** Every distinct node id in the built-in forest (threads UNION edge endpoints). */
const FOREST_NODE_IDS = [
  ...new Set([
    ...MOCK_THREADS.map((t) => t.id),
    ...MOCK_EDGES.flatMap((e) => [e.parent, e.child]),
  ]),
];

/** Compare two edges by (parent, child) — the stable order graph.at / graph.diff use. */
function edgeOrder(
  a: { parent: string; child: string },
  b: { parent: string; child: string },
): number {
  if (a.parent !== b.parent) return a.parent < b.parent ? -1 : 1;
  if (a.child !== b.child) return a.child < b.child ? -1 : 1;
  return 0;
}

describe("graph.rollup (mirrors aisr/rollup.py over the forest)", () => {
  it("keys every graph node (incl. the dangling parent) with self_count 1", async () => {
    const table = await mockIpc.graphRollup();
    expect(Object.keys(table).sort()).toEqual([...FOREST_NODE_IDS].sort());
    for (const id of FOREST_NODE_IDS) {
      expect(table[id].self_count).toBe(1);
      // child_count is the direct out-degree — cross-checked against graph.children.
      const kids = await mockIpc.graphChildren(id);
      expect(table[id].child_count).toBe(kids.length);
      expect(table[id].subtree_tokens).toBeGreaterThanOrEqual(table[id].self_tokens);
      expect(table[id].subtree_count).toBeGreaterThanOrEqual(1);
    }
  });

  it("gives a leaf its own metrics and a dangling parent zero self tokens", async () => {
    const table = await mockIpc.graphRollup();
    // deepfix is the depth-3 leaf: no children, subtree is itself alone.
    expect(table.deepfix.child_count).toBe(0);
    expect(table.deepfix.subtree_count).toBe(1);
    expect(table.deepfix.max_depth).toBe(0);
    expect(table.deepfix.self_tokens).toBe(15000);
    expect(table.deepfix.subtree_tokens).toBe(15000);
    // pruned-parent has no thread row -> 0 self tokens, one child (orphan).
    expect(table["pruned-parent"].self_tokens).toBe(0);
    expect(table["pruned-parent"].child_count).toBe(1);
  });
});

describe("MockGraph.rollup (dedup + cycle-safety, mirrors rollup.py)", () => {
  it("counts a diamond's shared node ONCE (no double counting)", () => {
    const g = new MockGraph(
      [
        { id: "a", title: "A", provider: "claude", created_at_ms: 1, tokens: 10 },
        { id: "b", title: "B", provider: "claude", created_at_ms: 2, tokens: 20 },
        { id: "c", title: "C", provider: "claude", created_at_ms: 3, tokens: 30 },
        { id: "d", title: "D", provider: "claude", created_at_ms: 4, tokens: 40 },
      ],
      [
        { parent: "a", child: "b" },
        { parent: "a", child: "c" },
        { parent: "b", child: "d" },
        { parent: "c", child: "d" },
      ],
    );
    const table = g.rollup();
    // d is reachable two ways but counted once: 4 nodes, 100 tokens (not 5 / 140).
    expect(table.a).toEqual({
      self_tokens: 10,
      subtree_tokens: 100,
      self_count: 1,
      subtree_count: 4,
      max_depth: 2,
      child_count: 2,
    });
    expect(table.d).toEqual({
      self_tokens: 40,
      subtree_tokens: 40,
      self_count: 1,
      subtree_count: 1,
      max_depth: 0,
      child_count: 0,
    });
  });

  it("terminates on a cycle and dedups the shared nodes", () => {
    const g = new MockGraph(
      [
        { id: "x", title: "X", provider: "claude", created_at_ms: 1, tokens: 1 },
        { id: "y", title: "Y", provider: "codex", created_at_ms: 2, tokens: 2 },
      ],
      [
        { parent: "x", child: "y" },
        { parent: "y", child: "x" },
      ],
    );
    expect(g.rollup().x).toEqual({
      self_tokens: 1,
      subtree_tokens: 3,
      self_count: 1,
      subtree_count: 2,
      max_depth: 1,
      child_count: 1,
    });
  });
});

describe("graph.timeline", () => {
  it("reports the distinct dated events, range, and undated count", async () => {
    const tl = await mockIpc.graphTimeline();
    const distinct = [...new Set(MOCK_THREADS.map((t) => t.created_at_ms))].sort(
      (a, b) => a - b,
    );
    expect(tl.events).toEqual(distinct);
    expect(tl.min_ms).toBe(distinct[0]);
    expect(tl.max_ms).toBe(distinct[distinct.length - 1]);
    // pruned-parent has no thread row -> it is the one undated node.
    expect(tl.undated_count).toBe(1);
  });

  it("counts a dangling edge endpoint as undated", async () => {
    const g = createMockIpc(
      [
        { id: "a", title: "A", provider: "claude", created_at_ms: 100 },
        { id: "b", title: "B", provider: "codex", created_at_ms: 200 },
      ],
      [
        { parent: "ghost", child: "a" },
        { parent: "a", child: "b" },
      ],
    );
    const tl = await g.graphTimeline();
    expect(tl.events).toEqual([100, 200]);
    expect(tl.min_ms).toBe(100);
    expect(tl.max_ms).toBe(200);
    expect(tl.undated_count).toBe(1); // ghost
  });

  it("nulls the range for an empty corpus", async () => {
    const tl = await createMockIpc([], []).graphTimeline();
    expect(tl.events).toEqual([]);
    expect(tl.min_ms).toBeNull();
    expect(tl.max_ms).toBeNull();
    expect(tl.undated_count).toBe(0);
  });
});

describe("graph.at (structure as-of T)", () => {
  const created = new Map(MOCK_THREADS.map((t) => [t.id, t.created_at_ms]));
  const presentAt = (id: string, t: number): boolean => {
    const c = created.get(id);
    return c === undefined || c <= t; // undated (dangling) always present
  };

  it("includes exactly the nodes/edges present at the cutoff, undated always in", async () => {
    const byTime = [...MOCK_THREADS].sort((a, b) => a.created_at_ms - b.created_at_ms);
    const cutoff = byTime[4].created_at_ms; // the 5th-earliest creation
    const at = await mockIpc.graphAt(cutoff);

    const expectedNodes = FOREST_NODE_IDS.filter((id) => presentAt(id, cutoff)).sort();
    expect(at.nodes.map((n) => n.id)).toEqual(expectedNodes); // sorted by stable id
    expect(at.nodes.map((n) => n.id)).toContain("pruned-parent");

    // An edge is present iff its CHILD is present as-of the cutoff (spawn time).
    const expectedEdges = MOCK_EDGES.filter((e) => presentAt(e.child, cutoff))
      .slice()
      .sort(edgeOrder);
    expect(at.edges).toEqual(expectedEdges); // full SpawnEdge incl. status
  });

  it("returns the whole graph at +inf and only undated nodes before time begins", async () => {
    const all = await mockIpc.graphAt(Number.MAX_SAFE_INTEGER);
    expect(all.nodes).toHaveLength(FOREST_NODE_IDS.length);
    expect(all.edges).toHaveLength(MOCK_EDGES.length);

    const minTs = Math.min(...MOCK_THREADS.map((t) => t.created_at_ms));
    const none = await mockIpc.graphAt(minTs - 1);
    expect(none.nodes.map((n) => n.id)).toEqual(["pruned-parent"]);
    expect(none.edges).toEqual([]);
  });

  it("gates edges by child creation and preserves status", async () => {
    const g = createMockIpc(
      [
        { id: "a", title: "A", provider: "claude", created_at_ms: 10 },
        { id: "b", title: "B", provider: "codex", created_at_ms: 20 },
        { id: "c", title: "C", provider: "claude", created_at_ms: 30 },
      ],
      [
        { parent: "a", child: "b", status: "completed" },
        { parent: "b", child: "c" },
      ],
    );
    const at15 = await g.graphAt(15);
    expect(at15.nodes.map((n) => n.id)).toEqual(["a"]);
    expect(at15.edges).toEqual([]); // b@20 and c@30 not yet created

    const at25 = await g.graphAt(25);
    expect(at25.nodes.map((n) => n.id)).toEqual(["a", "b"]);
    expect(at25.edges).toEqual([{ parent: "a", child: "b", status: "completed" }]);
  });
});

describe("graph.diff (time-travel as-of delta)", () => {
  it("is empty for the default self-diff (both operands = now)", async () => {
    const d = await mockIpc.graphDiff();
    expect(d.added_nodes).toEqual([]);
    expect(d.removed_nodes).toEqual([]);
    expect(d.added_edges).toEqual([]);
    expect(d.removed_edges).toEqual([]);
    expect(d.changed_nodes).toEqual({});
  });

  it("diffs two cutoffs, strips edge status, and never reports a field change", async () => {
    const g = createMockIpc(
      [
        { id: "a", title: "A", provider: "claude", created_at_ms: 10 },
        { id: "b", title: "B", provider: "codex", created_at_ms: 20 },
        { id: "c", title: "C", provider: "claude", created_at_ms: 30 },
      ],
      [
        { parent: "a", child: "b", status: "completed" },
        { parent: "b", child: "c" },
      ],
    );
    const d = await g.graphDiff(15, 25);
    expect(d.added_nodes).toEqual(["b"]);
    expect(d.removed_nodes).toEqual([]);
    expect(d.added_edges).toEqual([{ parent: "a", child: "b" }]); // status stripped
    expect(d.removed_edges).toEqual([]);
    // Time-travel views one immutable corpus, so a node in BOTH snapshots is identical.
    expect(d.changed_nodes).toEqual({});

    // Swapping the operands mirrors added <-> removed.
    const rev = await g.graphDiff(25, 15);
    expect(rev.removed_nodes).toEqual(["b"]);
    expect(rev.added_nodes).toEqual([]);
    expect(rev.removed_edges).toEqual([{ parent: "a", child: "b" }]);
    expect(rev.added_edges).toEqual([]);
  });

  it("treats an omitted operand as 'now' (the full corpus)", async () => {
    const g = createMockIpc(
      [
        { id: "a", title: "A", provider: "claude", created_at_ms: 10 },
        { id: "b", title: "B", provider: "codex", created_at_ms: 20 },
        { id: "c", title: "C", provider: "claude", created_at_ms: 30 },
      ],
      [
        { parent: "a", child: "b", status: "completed" },
        { parent: "b", child: "c" },
      ],
    );
    const d = await g.graphDiff(15); // old = as-of 15, new = now
    expect(d.added_nodes).toEqual(["b", "c"]);
    expect(d.added_edges).toEqual([
      { parent: "a", child: "b" },
      { parent: "b", child: "c" },
    ]);
    expect(d.removed_nodes).toEqual([]);
  });

  it("only ever ADDS going forward across two forest cutoffs", async () => {
    const byTime = [...MOCK_THREADS].sort((a, b) => a.created_at_ms - b.created_at_ms);
    const d = await mockIpc.graphDiff(byTime[2].created_at_ms, byTime[8].created_at_ms);
    expect(d.removed_nodes).toEqual([]);
    expect(d.added_nodes.length).toBeGreaterThan(0);
    expect(d.changed_nodes).toEqual({});
  });
});

describe("export.plan / export.run", () => {
  it("plans the whole graph as a dry-run with a byte estimate", async () => {
    const plan = await mockIpc.exportPlan();
    expect(plan.node_count).toBe(FOREST_NODE_IDS.length); // incl. dangling parent
    expect(plan.edge_count).toBe(MOCK_EDGES.length);
    expect(plan.conversation_count).toBe(MOCK_THREADS.length);
    const stats = await mockIpc.corpusStats();
    expect(plan.est_bytes).toBe(stats.bytes); // content-byte estimate
    expect(plan.est_bytes).toBeGreaterThan(0);
  });

  it("runs the export and passes both fidelity gates for a valid dest", async () => {
    const run = await mockIpc.exportRun("/tmp/aisr-export.json");
    expect(run.ok).toBe(true);
    expect(run.written_path).toBe("/tmp/aisr-export.json");
    expect(run.graph_gate).toBe(true);
    expect(run.transcript_gate).toBe(true);
  });

  it("refuses a blank dest: not ok, no written_path, gates still pass", async () => {
    const run = await mockIpc.exportRun("   ");
    expect(run.ok).toBe(false);
    expect(run.written_path).toBeUndefined();
    expect(run.graph_gate).toBe(true);
    expect(run.transcript_gate).toBe(true);
  });
});
