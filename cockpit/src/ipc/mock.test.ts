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
