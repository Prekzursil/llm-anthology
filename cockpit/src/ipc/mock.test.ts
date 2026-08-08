import { describe, expect, it } from "vitest";

import {
  createMockIpc,
  MockGraph,
  mockIpc,
  MOCK_DEDUP_SESSIONS,
  MOCK_EDGES,
  MOCK_THREADS,
} from "./mock";
// Read the engine's own source as a Vite `?raw` asset, exactly as `graph/palette.test.ts`
// reads `discover.py`. NOT via `node:fs`: the cockpit tsconfig is browser-only and adding
// `@types/node` to satisfy one test would let APP code typecheck references to
// `process`/`Buffer` that do not exist in the Tauri webview.
import SIDECAR_PY from "../../../llm_anthology/sidecar.py?raw";

import {
  RPC_BUILD_IN_PROGRESS,
  RPC_BUILD_UNAVAILABLE,
  RPC_CORPUS_EXISTS,
  RPC_CORPUS_NOT_INDEXED,
  RPC_DB_BUSY,
  RPC_INTERNAL_ERROR,
  RPC_INVALID_PARAMS,
  RPC_MAINTENANCE_REFUSED,
  RPC_THREAD_NOT_FOUND,
  rpcErrorCode,
} from "./types";

/**
 * The error-code vocabulary must match the ENGINE's, in both directions.
 *
 * Why this needs a test: the constants are plain numbers written down by hand, and the
 * regression happens on the PYTHON side — the engine renumbers a code, or adds an eighth one,
 * and nothing in TypeScript changes. A panel then branches on a stale number and silently
 * takes the wrong action forever, because a wrong-but-valid code produces no type error and no
 * runtime error. Same failure shape as the provider palette: a test that only pinned the TS
 * list would not catch it. So this reads the engine's own definitions and diffs both ways.
 *
 * The TS name is DERIVED (`RPC_` + the engine name) rather than transcribed, so a rename
 * cannot quietly re-baseline the test to whatever the code happens to say.
 */
// A trailing `# comment` is tolerated so a harmless engine-side comment cannot raise a false
// alarm. Note the failure DIRECTION if this regex ever under-matches: the missed constant
// drops out of `engineCodes()` while staying in `TS_CODES`, so the bidirectional comparison
// FAILS loudly rather than passing vacuously.
const ENGINE_CODE_RE = /^([A-Z][A-Z0-9_]*)\s*=\s*(-32\d{3})\s*(?:#.*)?$/gm;

/** Parse an engine source's app-specific code table. Text-in, so it is testable on synthetic
 *  drift without touching `llm_anthology/sidecar.py`. */
function parseCodes(text: string): Record<string, number> {
  const found: Record<string, number> = {};
  for (const m of text.matchAll(ENGINE_CODE_RE)) found[m[1]] = Number(m[2]);
  return found;
}

/** The app-specific codes the engine defines (`llm_anthology/sidecar.py:250-267`). */
function engineCodes(): Record<string, number> {
  return parseCodes(SIDECAR_PY);
}

/** `RPC_` + engine name -> the value this module exports. */
const TS_CODES: Record<string, number> = {
  CORPUS_NOT_INDEXED: RPC_CORPUS_NOT_INDEXED,
  THREAD_NOT_FOUND: RPC_THREAD_NOT_FOUND,
  DB_BUSY: RPC_DB_BUSY,
  MAINTENANCE_REFUSED: RPC_MAINTENANCE_REFUSED,
  BUILD_IN_PROGRESS: RPC_BUILD_IN_PROGRESS,
  BUILD_UNAVAILABLE: RPC_BUILD_UNAVAILABLE,
  CORPUS_EXISTS: RPC_CORPUS_EXISTS,
};

describe("RPC error-code vocabulary vs the engine", () => {
  it("can actually read the engine's code table", () => {
    // Guard the guard. If the relative path or the regex ever stops matching, every
    // assertion below would pass vacuously against an empty object.
    const codes = engineCodes();
    expect(Object.keys(codes).length).toBeGreaterThan(3);
    expect(codes.MAINTENANCE_REFUSED).toBe(-32003);
  });

  it("defines EVERY app-specific code the engine defines, with the same value", () => {
    // The direction that matters most: the engine gaining an eighth code, or renumbering an
    // existing one, must fail HERE rather than in a panel's stale branch.
    expect(TS_CODES).toEqual(engineCodes());
  });

  it("exports no code the engine does not define", () => {
    const engine = engineCodes();
    expect(Object.keys(TS_CODES).filter((name) => !(name in engine))).toEqual([]);
  });

  it("covers the two STANDARD codes the engine uses inline, and they are live", () => {
    // -32602/-32603 are JSON-RPC standard, so they are not in the app-specific block above
    // (`sidecar.py:250-251` says so) — but both are really raised, and a panel needs them.
    expect(RPC_INVALID_PARAMS).toBe(-32602);
    expect(RPC_INTERNAL_ERROR).toBe(-32603);
    expect(SIDECAR_PY).toContain("RpcError(-32602");
    expect(SIDECAR_PY).toContain("-32603");
  });

  it("has no duplicate values — every code is distinguishable", () => {
    // Two names sharing a number would make `rpcErrorCode` branching ambiguous, which is the
    // one thing this whole vocabulary exists to prevent.
    const values = [...Object.values(TS_CODES), RPC_INVALID_PARAMS, RPC_INTERNAL_ERROR];
    expect(new Set(values).size).toBe(values.length);
  });

  // -- proof the drift detector actually FIRES -----------------------------------
  //
  // The both-states test. A comparison against a parse that silently matched nothing would be
  // green in the drifted state too, and would therefore measure nothing. These drive
  // `parseCodes` with SYNTHETIC engine sources so the sensitivity is proven WITHOUT editing
  // `llm_anthology/sidecar.py` — which is another agent's file and would be a live hazard to
  // mutate even briefly.

  it("FIRES when the engine renumbers a code", () => {
    const renumbered = parseCodes(
      SIDECAR_PY.replace("CORPUS_EXISTS = -32006", "CORPUS_EXISTS = -32009"),
    );
    expect(renumbered.CORPUS_EXISTS).toBe(-32009);
    expect(renumbered).not.toEqual(TS_CODES);
  });

  it("FIRES when the engine ADDS an eighth code", () => {
    const extended = parseCodes(`${SIDECAR_PY}\nEXPORT_REFUSED = -32007\n`);
    expect(extended.EXPORT_REFUSED).toBe(-32007);
    expect(extended).not.toEqual(TS_CODES);
    // And names the newcomer, so the failure says WHICH code to go add.
    expect(Object.keys(extended).filter((n) => !(n in TS_CODES))).toEqual(["EXPORT_REFUSED"]);
  });

  it("FIRES when the engine REMOVES a code", () => {
    const shrunk = parseCodes(SIDECAR_PY.replace("BUILD_UNAVAILABLE = -32005", ""));
    expect("BUILD_UNAVAILABLE" in shrunk).toBe(false);
    expect(shrunk).not.toEqual(TS_CODES);
  });
});

describe("rpcErrorCode", () => {
  it("recovers the code the Rust bridge flattened into the message", () => {
    // The literal wire text: `format!("rpc error (id {id}): {err}")`
    // (`cockpit/src-tauri/src/sidecar.rs:161`) over `{code, message}` (`sidecar.py:299-303`).
    const wire =
      'rpc error (id 7): {"code":-32003,"message":"unknown or already-used plan_id \'plan-1\'; re-plan"}';
    expect(rpcErrorCode(new Error(wire))).toBe(RPC_MAINTENANCE_REFUSED);
    // Tauri rejects with the bare string, so both forms have to work.
    expect(rpcErrorCode(wire)).toBe(-32003);
  });

  it("returns null for a failure that carries no envelope — NOT a success signal", () => {
    // `no corpus attached` is raised by the Rust bridge itself (`lib.rs:45`) and never went
    // through the sidecar, so it has no code. Reading null as "fine" would swallow it.
    expect(rpcErrorCode(new Error("no corpus attached: call open_corpus first"))).toBeNull();
    expect(rpcErrorCode(undefined)).toBeNull();
    expect(rpcErrorCode({ code: -32003 })).toBeNull(); // a bare object is not the wire form
  });
});

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

describe("graph.rollup (mirrors llm_anthology/rollup.py over the forest)", () => {
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
    const run = await mockIpc.exportRun("/tmp/llm-anthology-export.json");
    expect(run.ok).toBe(true);
    expect(run.written_path).toBe("/tmp/llm-anthology-export.json");
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

// ---------------------------------------------------------------------------
// annotations / dedup / maintenance
// ---------------------------------------------------------------------------
//
// These tests are SHAPE tests first. A panel written against this mock has to work against
// the engine, so each block pins the exact key set the sidecar emits (cited inline) rather
// than only spot-checking a value — an assertion on `Object.keys()` catches a MISSING field
// and a SPURIOUS one, which a per-field `expect` does not.
//
// Every stateful block builds its own client with `createMockIpc()`: the module-level
// `mockIpc` singleton carries annotation, dedup and maintenance state, so sharing it would
// make these tests order-dependent.
//
// `research.*` is absent on purpose and is pinned NEGATIVELY below — see the NOT BOUND note
// in `./types.ts`.

/** The engine's annotation key set (`llm_anthology/sidecar.py:1319-1325`). */
const ANNOTATION_KEYS = ["alias", "conversation_id", "is_empty", "notes", "tags"];

describe("the research plane is NOT bound", () => {
  it("exposes no research method on the mock", () => {
    // A negative pin, not a formality. The research plane has no backend: both
    // `research_backend` and `local_backend` fall back to `MockBackend()`
    // (`llm_anthology/sidecar.py:587-590`) whose `synthesize` returns `""`
    // (`research.py:88-97`), so the shipped app can never produce output. If someone
    // re-adds a mock that returns canned prose, a research panel will look finished in
    // every dev run and screenshot and be permanently blank for the user — this test is
    // what makes that regression loud instead of invisible.
    // Control first: prove the detector can see a method that IS there, so the two
    // `false` assertions below mean absence rather than a broken probe.
    expect("metadataGet" in mockIpc).toBe(true);
    for (const name of ["researchSynthesize", "researchExtractEntities"]) {
      expect(name in mockIpc).toBe(false);
    }
  });
});

describe("metadata.* annotations", () => {
  it("reads an UNANNOTATED conversation back as an empty annotation, not an error", async () => {
    const ipc = createMockIpc();
    // Byte-for-byte the engine's answer (tests/test_sidecar_metadata.py:70-73).
    expect(await ipc.metadataGet("search")).toEqual({
      conversation_id: "search",
      alias: "",
      tags: [],
      notes: "",
      is_empty: true,
    });
  });

  it("round-trips a set and orders tags deterministically", async () => {
    const ipc = createMockIpc();
    const out = await ipc.metadataSet({
      conversation_id: "search",
      alias: "Refactor run",
      tags: ["work", "rust"],
      notes: "picked up Tuesday",
    });
    expect(Object.keys(out).sort()).toEqual(ANNOTATION_KEYS);
    expect(out.alias).toBe("Refactor run");
    // Ordered by (casefold, exact) — NOT insertion order (metadata.py:226-228).
    expect(out.tags).toEqual(["rust", "work"]);
    expect(out.is_empty).toBe(false);
    expect(await ipc.metadataGet("search")).toEqual(out);
  });

  it("is a PARTIAL update: an omitted field is untouched", async () => {
    // The whole point of the contract — the cockpit edits one field at a time and a
    // per-field call must not blank the other two (tests/test_sidecar_metadata.py:89-97).
    const ipc = createMockIpc();
    await ipc.metadataSet({
      conversation_id: "search",
      alias: "first",
      tags: ["keep"],
      notes: "keep me",
    });
    const out = await ipc.metadataSet({ conversation_id: "search", alias: "second" });
    expect(out.alias).toBe("second");
    expect(out.tags).toEqual(["keep"]);
    expect(out.notes).toBe("keep me");
  });

  it("treats an explicit blank as CLEAR, unlike an omission", async () => {
    const ipc = createMockIpc();
    await ipc.metadataSet({ conversation_id: "search", alias: "x", tags: ["t"], notes: "n" });
    const out = await ipc.metadataSet({ conversation_id: "search", alias: "", tags: [] });
    expect(out.alias).toBe("");
    expect(out.tags).toEqual([]);
    expect(out.notes).toBe("n"); // omitted -> untouched
    expect(out.is_empty).toBe(false);
  });

  it("dedups tags case-insensitively, keeping the first-seen casing", async () => {
    const ipc = createMockIpc();
    const out = await ipc.metadataSet({
      conversation_id: "search",
      tags: ["Renderer", "renderer", "  spaced  out  ", ""],
    });
    // metadata.py:223-225: one tag, first casing kept; whitespace runs collapse; blanks drop.
    expect(out.tags).toEqual(["Renderer", "spaced out"]);
  });

  it("rejects an empty or whitespace-only conversation_id", async () => {
    const ipc = createMockIpc();
    await expect(ipc.metadataGet("   ")).rejects.toThrow(/conversation_id/);
    await expect(ipc.metadataSet({ conversation_id: "" })).rejects.toThrow(/conversation_id/);
    await expect(ipc.metadataClear("  ")).rejects.toThrow(/conversation_id/);
  });

  it("clears the whole annotation, and an absent row is a silent no-op", async () => {
    const ipc = createMockIpc();
    expect((await ipc.metadataClear("orch")).is_empty).toBe(true);
    expect((await ipc.metadataGet("orch")).is_empty).toBe(true);
    // Clearing something that was never annotated is not an error.
    expect((await ipc.metadataClear("orch")).is_empty).toBe(true);
  });

  it("returns NOTHING for a blank search — a blank query must not dump the catalogue", async () => {
    const ipc = createMockIpc();
    expect(await ipc.metadataSearch()).toEqual([]);
    expect(await ipc.metadataSearch({})).toEqual([]);
    expect(await ipc.metadataSearch({ text: "  ", tag: "" })).toEqual([]);
  });

  it("joins the display columns onto a tag match", async () => {
    const ipc = createMockIpc();
    const rows = await ipc.metadataSearch({ tag: "rust" });
    // The seed puts 'Rust' on orch and 'rust' on ipc: a whole-tag match is case-insensitive.
    expect(rows.map((r) => r.conversation_id)).toEqual(["ipc", "orch"]);
    // sidecar.py:1378-1389 — the exact row shape a listing renders.
    expect(Object.keys(rows[0]).sort()).toEqual([
      "account",
      "annotation",
      "conversation_id",
      "created_at",
      "provider",
      "thread_id",
      "title",
      "turn_count",
      "updated_at",
    ]);
    const orch = rows.find((r) => r.conversation_id === "orch") as (typeof rows)[number];
    expect(orch.provider).toBe("claude");
    expect(orch.title).toBe("Orchestrate cockpit build");
    expect(orch.turn_count).toBeGreaterThan(0);
    // conversations.account is NOT NULL DEFAULT '' (corpus.py:183) — "" here, NOT null.
    expect(orch.account).toBe("");
    expect(orch.account).not.toBeNull();
    // created_at/updated_at are STRINGS on this surface, not the _ms numbers the graph uses.
    expect(typeof orch.created_at).toBe("string");
    expect(typeof orch.updated_at).toBe("string");
    expect(Object.keys(orch.annotation).sort()).toEqual(ANNOTATION_KEYS);
  });

  it("matches a tag WHOLE, but text as a SUBSTRING", async () => {
    const ipc = createMockIpc();
    // metadata.py:243-269 probes a sentinel-delimited needle, never a LIKE — so a prefix
    // of a real tag finds nothing. A substring implementation would return two rows here.
    expect(await ipc.metadataSearch({ tag: "rus" })).toEqual([]);
    // ...while `text` really is a substring over alias + tags + notes (metadata.py:470-480).
    expect((await ipc.metadataSearch({ text: "needle" })).map((r) => r.conversation_id))
      .toEqual(["ipc"]);
  });

  it("ANDs the two filters rather than ORing them", async () => {
    const ipc = createMockIpc();
    expect(await ipc.metadataSearch({ tag: "triage", text: "needle" })).toEqual([]);
    expect((await ipc.metadataSearch({ tag: "rust", text: "needle" }))
      .map((r) => r.conversation_id)).toEqual(["ipc"]);
  });

  it("collapses the tag facet case-insensitively with the first display form", async () => {
    const ipc = createMockIpc();
    const facet = await ipc.metadataTags();
    expect(Object.keys(facet[0]).sort()).toEqual(["count", "tag"]);
    // 'Rust' (orch) and 'rust' (ipc) are ONE entry of count 2, labelled with the
    // lexicographically-first form, ordered by the casefolded tag (metadata.py:529-544).
    expect(facet).toEqual([
      { tag: "Rust", count: 2 },
      { tag: "shipped", count: 1 },
      { tag: "triage", count: 1 },
    ]);
  });

  it("has an empty facet once every annotation is cleared", async () => {
    const ipc = createMockIpc();
    for (const cid of ["orch", "ipc", "repro"]) await ipc.metadataClear(cid);
    expect(await ipc.metadataTags()).toEqual([]);
    expect(await ipc.metadataSearch({ tag: "rust" })).toEqual([]);
  });
});

describe("dedup.scan / dedup.sessions", () => {
  it("is EMPTY before any scan", async () => {
    // tests/test_sidecar_dedup.py:170-171 — the persisted view does not exist yet.
    expect(await createMockIpc().dedupSessions()).toEqual([]);
  });

  it("tallies the scan consistently with the sessions it then serves", async () => {
    const ipc = createMockIpc();
    const scan = await ipc.dedupScan("C:\\Users\\preview\\.codex");
    // sidecar.py:1442-1449.
    expect(Object.keys(scan).sort()).toEqual([
      "copy_count",
      "duplicate_count",
      "errors",
      "flagged_truncated",
      "session_count",
      "unidentified",
    ]);
    const rows = await ipc.dedupSessions();
    expect(scan.session_count).toBe(rows.length);
    expect(scan.copy_count).toBe(rows.reduce((n, r) => n + r.copy_count, 0));
    expect(scan.duplicate_count).toBe(scan.copy_count - scan.session_count);
  });

  it("REPORTS a partial-parse error rather than a silently clean scan", async () => {
    // The engine can produce one (`scan_store` passes `ingest_sessions`' errors through and
    // skips a torn last line — `llm_anthology/dedup.py:244-252`), so a mock that always
    // returned `[]` left the panel's error list dead in every dev run while the shipped app
    // could populate it. Measured against the engine before this: engine n=1, mock n=0.
    const scan = await createMockIpc().dedupScan("C:\\Users\\preview\\.codex");
    expect(scan.errors).toHaveLength(1);
    expect(typeof scan.errors[0]).toBe("string");
    // A torn line costs the COPY, not the scan: sessions still come back.
    expect(scan.session_count).toBeGreaterThan(0);
  });

  it("hands out a fresh errors array so a caller cannot drain the fixture", async () => {
    const ipc = createMockIpc();
    (await ipc.dedupScan("C:\\x")).errors.length = 0;
    expect((await ipc.dedupScan("C:\\x")).errors).toHaveLength(1);
  });

  it("serves every row shape a dedup panel has to survive", async () => {
    const ipc = createMockIpc();
    await ipc.dedupScan("C:\\Users\\preview\\.codex");
    const rows = await ipc.dedupSessions();
    expect(rows).toHaveLength(MOCK_DEDUP_SESSIONS.length);
    // sidecar.py:1416-1426.
    expect(Object.keys(rows[0]).sort()).toEqual([
      "canonical_path",
      "copy_count",
      "duplicate_paths",
      "has_larger_copy",
      "is_identified",
      "last_write_ms",
      "session_id",
      "size_bytes",
      "store_kind",
    ]);
    // The truncated-canonical case: the flag exists because the view would otherwise show
    // the SHORTER conversation (dedup.py:149-164).
    const truncated = rows.filter((r) => r.has_larger_copy);
    expect(truncated).toHaveLength(1);
    expect(truncated[0].duplicate_paths.length).toBeGreaterThan(0);
    expect(truncated[0].store_kind).toBe("live"); // canonical rule NOT reversed
    // An unidentifiable file is kept as a path-keyed singleton, never merged.
    const unnamed = rows.filter((r) => !r.is_identified);
    expect(unnamed).toHaveLength(1);
    expect(unnamed[0].session_id).toBe("");
    // PhysicalCopy.last_write_ms is Optional[int] (dedup.py:118) — a renderer that formats
    // it unconditionally throws on this row.
    expect(rows.some((r) => r.last_write_ms === null)).toBe(true);
    // store_kind is a PLAIN string here, from dedup.STORE_* (dedup.py:87-91).
    for (const r of rows) {
      expect(["live", "backup", "mirror", "other", "unknown"]).toContain(r.store_kind);
    }
  });

  it("requires a codex_home and refuses a UNC or relative one", async () => {
    // sidecar.py:1429-1437: never defaulted, because a defaulted home once made a probe
    // read the owner's live Codex store.
    const ipc = createMockIpc();
    await expect(ipc.dedupScan("")).rejects.toThrow(/codex_home/);
    await expect(ipc.dedupScan("\\\\evil.example\\share\\codex")).rejects.toThrow(/UNC/);
    await expect(ipc.dedupScan("relative/codex")).rejects.toThrow(/absolute/);
    // A refused scan must not mark the view as scanned.
    expect(await ipc.dedupSessions()).toEqual([]);
  });

  it("hands out fresh rows so a caller that sorts in place cannot corrupt the fixture", async () => {
    const ipc = createMockIpc();
    await ipc.dedupScan("C:\\Users\\preview\\.codex");
    const first = await ipc.dedupSessions();
    first.reverse();
    first[0].duplicate_paths.push("mutated");
    const second = await ipc.dedupSessions();
    expect(second.map((r) => r.session_id)).toEqual(
      MOCK_DEDUP_SESSIONS.map((r) => r.session_id),
    );
    expect(second.some((r) => r.duplicate_paths.includes("mutated"))).toBe(false);
  });
});

describe("maintenance.* (the only destructive surface)", () => {
  const STORE = "C:\\store";
  const CHECKPOINT = "C:\\cp";

  function planParams(
    files: string[],
    over: Partial<Parameters<ReturnType<typeof createMockIpc>["maintenancePlan"]>[0]> = {},
  ): Parameters<ReturnType<typeof createMockIpc>["maintenancePlan"]>[0] {
    return {
      store_root: STORE,
      checkpoint_root: CHECKPOINT,
      action: "delete",
      targets: files.map((f, i) => ({ session_id: `s${i}`, file_path: f })),
      ...over,
    };
  }

  it("previews without mutating anything, under a single-use handle", async () => {
    const ipc = createMockIpc();
    const preview = await ipc.maintenancePlan(
      planParams([`${STORE}\\a.jsonl`, `${STORE}\\b.jsonl`]),
    );
    // sidecar.py:1477-1494.
    expect(Object.keys(preview).sort()).toEqual([
      "action",
      "allowed",
      "blocked",
      "checkpoint_root",
      "destination_root",
      "plan",
      "plan_id",
      "required_typed_confirmation",
      "requires_checkpoint",
      "requires_typed_confirmation",
      "store_root",
      "warnings",
    ]);
    expect(preview.plan_id).toBe("plan-1");
    expect(preview.action).toBe("delete");
    expect(preview.allowed).toHaveLength(2);
    expect(preview.blocked).toEqual([]);
    expect(preview.requires_checkpoint).toBe(true);
    expect(preview.requires_typed_confirmation).toBe(true);
    // maintenance.py:414-419 — derived from the ALLOWED count, plural-aware.
    expect(preview.required_typed_confirmation).toBe("DELETE 2 FILES");
    // sidecar.py:1473-1475 — every RPC-built target is forced to UNKNOWN (sidecar.py:1556).
    expect(Object.keys(preview.allowed[0]).sort()).toEqual([
      "file_path",
      "is_hot",
      "last_write_ms",
      "session_id",
      "size_bytes",
      "store_kind",
    ]);
    expect(preview.allowed[0].store_kind).toBe("unknown");
  });

  it("uses the singular FILE for a one-target plan and a distinct id per plan", async () => {
    const ipc = createMockIpc();
    const first = await ipc.maintenancePlan(planParams([`${STORE}\\a.jsonl`]));
    expect(first.required_typed_confirmation).toBe("DELETE 1 FILE");
    const second = await ipc.maintenancePlan(planParams([`${STORE}\\a.jsonl`]));
    expect(second.plan_id).not.toBe(first.plan_id);
  });

  it("reports the EFFECTIVE destination root, not the requested one", async () => {
    const ipc = createMockIpc();
    // A delete quarantines under <checkpoint_root>\deleted — that is what makes it
    // recoverable (maintenance.py:404-411, :538).
    const del = await ipc.maintenancePlan(planParams([`${STORE}\\a.jsonl`]));
    expect(del.destination_root).toBe(`${CHECKPOINT}\\deleted`);
    expect(del.plan[0].destination).toBe(`${CHECKPOINT}\\deleted\\a.jsonl`);
    // A reconcile lands under <destination_root>\reconciled.
    const rec = await ipc.maintenancePlan(
      planParams([`${STORE}\\a.jsonl`], {
        action: "reconcile",
        destination_root: "C:\\dst",
      }),
    );
    expect(rec.destination_root).toBe("C:\\dst\\reconciled");
    // An archive/move goes to the requested destination as-is.
    const arc = await ipc.maintenancePlan(
      planParams([`${STORE}\\a.jsonl`], { action: "archive", destination_root: "C:\\dst" }),
    );
    expect(arc.destination_root).toBe("C:\\dst");
  });

  it("WARNS on a perfectly healthy plan — a clean plan is not a quiet plan", async () => {
    const ipc = createMockIpc();
    const preview = await ipc.maintenancePlan(
      planParams([`${STORE}\\a.jsonl`, `${STORE}\\b.jsonl`]),
    );
    // maintenance.py:506-527: a DANGEROUS warning per allowed target plus a closing INFO
    // summary. A panel that reads non-empty `warnings` as "something is broken" flags every
    // healthy plan.
    expect(preview.warnings).toHaveLength(3);
    expect(Object.keys(preview.warnings[0]).sort()).toEqual([
      "message",
      "severity",
      "severity_name",
    ]);
    expect(preview.warnings.filter((w) => w.severity_name === "DANGEROUS")).toHaveLength(2);
    const info = preview.warnings.filter((w) => w.severity_name === "INFO");
    expect(info).toHaveLength(1);
    expect(info[0].severity).toBe(0); // 0 INFO | 1 REVIEW | 2 DANGEROUS
  });

  it("blocks the SECOND entry naming one physical file", async () => {
    // The likeliest real bug on this surface: feeding a dedup session's canonical AND
    // duplicate path into one plan. The engine blocks the repeat rather than moving a file
    // twice (maintenance.py:480-491).
    const ipc = createMockIpc();
    const preview = await ipc.maintenancePlan(
      planParams([`${STORE}\\a.jsonl`, `${STORE}\\A.JSONL`]),
    );
    expect(preview.allowed).toHaveLength(1);
    expect(preview.blocked).toHaveLength(1);
    expect(Object.keys(preview.blocked[0]).sort()).toEqual(["detail", "reason", "target"]);
    expect(preview.blocked[0].reason).toBe("duplicate-target");
    expect(preview.required_typed_confirmation).toBe("DELETE 1 FILE"); // allowed, not offered
    expect(preview.warnings.some((w) => w.severity_name === "REVIEW")).toBe(true);
  });

  it("BLOCKS a target spelling a protected marker, and keeps it out of allowed", async () => {
    // `protected` is the LIVE-STORE guard — the most important reason a target is ever
    // refused. Measured against the engine before the mock implemented it: engine blocked
    // [duplicate-target, protected] where the mock blocked only [duplicate-target], so the
    // mock showed an UNDELETABLE file as deletable and demanded a different confirmation
    // phrase. All three markers are ported from `llm_anthology/maintenance.py:277-281`.
    const ipc = createMockIpc();
    for (const spelling of [
      `${STORE}\\.codex\\sessions\\live.jsonl`,
      `${STORE}\\.codex\\state_5.sqlite`,
      `${STORE}\\.codex\\codex-sqlite\\db.sqlite`,
      // Separator- and case-insensitive, like the engine's normalisation.
      `${STORE}/.CODEX/SESSIONS/live.jsonl`,
    ]) {
      const preview = await ipc.maintenancePlan(planParams([spelling]));
      expect(preview.allowed).toEqual([]);
      expect(preview.blocked).toHaveLength(1);
      expect(preview.blocked[0].reason).toBe("protected");
      expect(preview.blocked[0].target.file_path).toBe(spelling);
      // The phrase follows the ALLOWED count, so a fully-blocked plan asks for 0 files.
      expect(preview.required_typed_confirmation).toBe("DELETE 0 FILES");
      expect(preview.warnings.some((w) => w.severity_name === "DANGEROUS")).toBe(true);
    }
  });

  it("does NOT block a path that merely resembles a marker", async () => {
    // The engine appends one separator before matching, so `\sessionsfoo` must not match
    // `\sessions\` (`maintenance.py:380-382`). Over-blocking would be its own bug.
    const ipc = createMockIpc();
    for (const safe of [`${STORE}\\.codexsessions\\a.jsonl`, `${STORE}\\sessions\\a.jsonl`]) {
      const preview = await ipc.maintenancePlan(planParams([safe]));
      expect(preview.blocked).toEqual([]);
      expect(preview.allowed).toHaveLength(1);
    }
  });

  it("reports a duplicate that is ALSO protected as protected", async () => {
    // Ordering, not cosmetics: the engine checks duplicate LAST so the more dangerous reason
    // wins (`maintenance.py:487-489`). Reversing the two mislabels the worst case.
    const ipc = createMockIpc();
    const live = `${STORE}\\.codex\\sessions\\live.jsonl`;
    const preview = await ipc.maintenancePlan(planParams([live, live]));
    expect(preview.blocked.map((b) => b.reason)).toEqual(["protected", "protected"]);
  });

  it("blocks a target outside the store root", async () => {
    const ipc = createMockIpc();
    const preview = await ipc.maintenancePlan(
      planParams([`${STORE}\\a.jsonl`, "C:\\elsewhere\\b.jsonl"]),
    );
    expect(preview.allowed.map((a) => a.file_path)).toEqual([`${STORE}\\a.jsonl`]);
    expect(preview.blocked[0].reason).toBe("outside-store-root");
  });

  it("refuses a UNC root, a relative root, a bad action and empty targets", async () => {
    const ipc = createMockIpc();
    await expect(
      ipc.maintenancePlan(planParams([`${STORE}\\a.jsonl`], {
        store_root: "\\\\evil.example\\share",
      })),
    ).rejects.toThrow(/UNC/);
    await expect(
      ipc.maintenancePlan(planParams([`${STORE}\\a.jsonl`], {
        checkpoint_root: "relative/cp",
      })),
    ).rejects.toThrow(/absolute/);
    await expect(
      ipc.maintenancePlan(planParams([`${STORE}\\a.jsonl`], {
        action: "obliterate" as "delete",
      })),
    ).rejects.toThrow(/action/);
    await expect(ipc.maintenancePlan(planParams([]))).rejects.toThrow(/non-empty/);
    await expect(
      ipc.maintenancePlan(planParams([], { targets: [{ file_path: "" }] })),
    ).rejects.toThrow(/file_path/);
  });

  it("DRY-RUNS by default, returning the planned moves and no manifest", async () => {
    const ipc = createMockIpc();
    const preview = await ipc.maintenancePlan(planParams([`${STORE}\\a.jsonl`]));
    const out = await ipc.maintenanceExecute({
      plan_id: preview.plan_id,
      confirmation: preview.required_typed_confirmation,
    });
    // sidecar.py:1498-1504.
    expect(Object.keys(out).sort()).toEqual([
      "executed",
      "manifest_path",
      "moves",
      "unaccounted",
    ]);
    expect(out.executed).toBe(false);
    // "" and NOT null (maintenance.py:266) — truthiness is the right "did it write" test.
    expect(out.manifest_path).toBe("");
    // maintenance.py:618 returns preview.plan on a dry run, NOT an empty list.
    expect(out.moves).toEqual(preview.plan);
    expect(out.unaccounted).toEqual([]);
  });

  it("requires the typed confirmation even for a DRY RUN", async () => {
    // maintenance.py:606 runs the confirmation guard AHEAD of the apply branch at :617, so
    // a preview button that skips collecting the phrase is refused, not answered.
    const ipc = createMockIpc();
    const preview = await ipc.maintenancePlan(planParams([`${STORE}\\a.jsonl`]));
    await expect(
      ipc.maintenanceExecute({ plan_id: preview.plan_id }),
    ).rejects.toThrow(/Typed confirmation is required/);
  });

  it("leaves the handle usable after a REFUSED confirmation, and consumes it on accept", async () => {
    // A typo must be correctable without forcing a re-plan; a completed run cannot be
    // replayed (sidecar.py:1587-1589, tests/test_sidecar_maintenance.py:194-206).
    const ipc = createMockIpc();
    const preview = await ipc.maintenancePlan(planParams([`${STORE}\\a.jsonl`]));
    await expect(
      ipc.maintenanceExecute({ plan_id: preview.plan_id, confirmation: "nope", apply: true }),
    ).rejects.toThrow(/does not match/);
    const out = await ipc.maintenanceExecute({
      plan_id: preview.plan_id,
      confirmation: preview.required_typed_confirmation,
      apply: true,
    });
    expect(out.executed).toBe(true);
    expect(out.manifest_path).not.toBe("");
    await expect(
      ipc.maintenanceExecute({
        plan_id: preview.plan_id,
        confirmation: preview.required_typed_confirmation,
        apply: true,
      }),
    ).rejects.toThrow(/unknown or already-used/);
  });

  it("refuses an unknown plan_id", async () => {
    const ipc = createMockIpc();
    await expect(
      ipc.maintenanceExecute({ plan_id: "plan-999", confirmation: "DELETE 1 FILE", apply: true }),
    ).rejects.toThrow(/unknown or already-used/);
  });

  it("records only APPLIED runs in the ledger, newest first, honouring a limit", async () => {
    const ipc = createMockIpc();
    expect(await ipc.maintenanceRuns()).toEqual([]);

    // A dry run does not enter the ledger (tests/test_sidecar_maintenance.py:305-312).
    const dry = await ipc.maintenancePlan(planParams([`${STORE}\\a.jsonl`]));
    await ipc.maintenanceExecute({
      plan_id: dry.plan_id,
      confirmation: dry.required_typed_confirmation,
    });
    expect(await ipc.maintenanceRuns()).toEqual([]);

    const applied: string[] = [];
    for (const name of ["a.jsonl", "b.jsonl"]) {
      const p = await ipc.maintenancePlan(planParams([`${STORE}\\${name}`]));
      const r = await ipc.maintenanceExecute({
        plan_id: p.plan_id,
        confirmation: p.required_typed_confirmation,
        apply: true,
      });
      applied.push(r.manifest_path);
    }
    const runs = await ipc.maintenanceRuns();
    expect(runs).toHaveLength(2);
    // maintenance.py:786-787 + the schema at :772-780.
    expect(Object.keys(runs[0]).sort()).toEqual([
      "action",
      "blocked_count",
      "manifest_path",
      "moved_count",
      "recorded_at_ms",
      "status",
      "store_root",
    ]);
    // Newest FIRST (maintenance.py:817-820).
    expect(runs.map((r) => r.manifest_path)).toEqual([...applied].reverse());
    expect(runs[0].recorded_at_ms).toBeGreaterThan(runs[1].recorded_at_ms);
    expect(runs[0].action).toBe("delete");
    expect(runs[0].status).toBe("executed");
    expect(runs[0].store_root).toBe(STORE);
    expect(await ipc.maintenanceRuns(1)).toHaveLength(1);
    await expect(ipc.maintenanceRuns(-1)).rejects.toThrow(/non-negative/);
  });

  it("restores a manifest, dry by default", async () => {
    const ipc = createMockIpc();
    const preview = await ipc.maintenancePlan(planParams([`${STORE}\\a.jsonl`]));
    const done = await ipc.maintenanceExecute({
      plan_id: preview.plan_id,
      confirmation: preview.required_typed_confirmation,
      apply: true,
    });
    const dry = await ipc.maintenanceRestore({ manifest_path: done.manifest_path });
    expect(dry.executed).toBe(false);
    // A restore move points the OTHER WAY than the plan that created it: the checkpoint copy
    // is the source and the original path is the destination
    // (`llm_anthology/maintenance.py:792-793`). The mock used to echo the plan verbatim, which
    // renders as an arrow pointing backwards in any panel that shows "moving X -> Y".
    expect(dry.moves).toEqual(
      preview.plan.map((m) => ({
        session_id: m.session_id,
        source: m.destination,
        destination: m.source,
      })),
    );
    const back = await ipc.maintenanceRestore({
      manifest_path: done.manifest_path,
      apply: true,
    });
    expect(back.executed).toBe(true);
    expect(Object.keys(back).sort()).toEqual([
      "executed",
      "manifest_path",
      "moves",
      "unaccounted",
    ]);
    // An APPLIED restore cannot be replayed — the engine's first check
    // (`maintenance.py:750-752`) refuses a checkpoint already restored.
    const replay = await ipc
      .maintenanceRestore({ manifest_path: done.manifest_path, apply: true })
      .catch((e: unknown) => e);
    expect(rpcErrorCode(replay)).toBe(RPC_MAINTENANCE_REFUSED);
    expect(String((replay as Error).message)).toMatch(/already restored/);
  });

  it("classifies an unaccountable entry as UNACCOUNTED, not a completed move", async () => {
    // Measured against the engine before this: given input the engine called "0 moves, 2
    // unaccounted", the mock said "2 moves, 0 unaccounted" — the OPPOSITE verdict, so a panel
    // would report a restore that did not happen. A multi-move manifest now models one entry
    // removed out of band (`llm_anthology/maintenance.py:731-735`).
    const ipc = createMockIpc();
    const preview = await ipc.maintenancePlan(
      planParams([`${STORE}\\a.jsonl`, `${STORE}\\b.jsonl`]),
    );
    const done = await ipc.maintenanceExecute({
      plan_id: preview.plan_id,
      confirmation: preview.required_typed_confirmation,
      apply: true,
    });

    // WITHOUT the opt-in the restore is CLEAN, matching the engine: right after an applied
    // delete every checkpoint copy exists, so the engine reports 0 unaccounted. An earlier
    // move-count rule refused here instead and was a false positive on the normal path — the
    // parity probe caught it as `MOCKERR` against a clean engine result.
    const clean = await ipc.maintenanceRestore({ manifest_path: done.manifest_path });
    expect(clean.unaccounted).toEqual([]);
    expect(clean.moves).toHaveLength(preview.plan.length);

    // With the opt-in, the checkpoint copies are gone AND the originals were already moved
    // away by the execute, so the engine's per-entry rule (`maintenance.py:784-788`) makes
    // EVERY entry unaccounted and leaves NOTHING pending. Measured against the engine on this
    // exact input: 0 moves, 2 unaccounted. An earlier "exactly one" rule reported 1 and 1.
    const partial = await ipc.maintenanceRestore({
      manifest_path: done.manifest_path,
      skip_unaccounted: true,
    });
    expect(partial.unaccounted).toHaveLength(preview.plan.length);
    expect(partial.moves).toEqual([]);
    // An unaccounted entry names the ORIGINAL path, and must never also appear as a move.
    expect(partial.unaccounted).toEqual(preview.plan.map((m) => m.source));
  });

  it("still restores a single-move manifest cleanly", async () => {
    // The common case has to stay reachable: only a MULTI-move manifest models an
    // out-of-band deletion, so a one-file restore needs no `skip_unaccounted`.
    const ipc = createMockIpc();
    const preview = await ipc.maintenancePlan(planParams([`${STORE}\\a.jsonl`]));
    const done = await ipc.maintenanceExecute({
      plan_id: preview.plan_id,
      confirmation: preview.required_typed_confirmation,
      apply: true,
    });
    const back = await ipc.maintenanceRestore({
      manifest_path: done.manifest_path,
      apply: true,
    });
    expect(back.unaccounted).toEqual([]);
    expect(back.moves).toHaveLength(1);
    expect(back.executed).toBe(true);
  });

  it("refuses a non-boolean skip_unaccounted", async () => {
    const ipc = createMockIpc();
    const preview = await ipc.maintenancePlan(planParams([`${STORE}\\a.jsonl`]));
    const done = await ipc.maintenanceExecute({
      plan_id: preview.plan_id,
      confirmation: preview.required_typed_confirmation,
      apply: true,
    });
    const bad = await ipc
      .maintenanceRestore({
        manifest_path: done.manifest_path,
        skip_unaccounted: 1 as unknown as boolean,
      })
      .catch((e: unknown) => e);
    expect(rpcErrorCode(bad)).toBe(RPC_INVALID_PARAMS);
  });

  it("refuses a UNC, relative or unknown manifest_path", async () => {
    const ipc = createMockIpc();
    await expect(
      ipc.maintenanceRestore({ manifest_path: "\\\\evil.example\\share\\m.json" }),
    ).rejects.toThrow(/UNC/);
    await expect(
      ipc.maintenanceRestore({ manifest_path: "relative/m.json" }),
    ).rejects.toThrow(/absolute/);
    await expect(
      ipc.maintenanceRestore({ manifest_path: "C:\\cp\\nope.json" }),
    ).rejects.toThrow(/no checkpoint manifest/);
  });

  it("makes an EXPIRED plan distinguishable from a bad param by CODE", async () => {
    // The requirement a Maintenance panel actually has: "that plan expired, re-plan" and
    // "you gave me a bad path" need different next actions from the operator, so a single
    // generic failure toast is not enough. The engine separates them as -32003 vs -32602
    // (`llm_anthology/sidecar.py:255-257`); the mock must too, or the panel's branch is dead
    // in every dev run.
    const ipc = createMockIpc();
    const preview = await ipc.maintenancePlan(planParams([`${STORE}\\a.jsonl`]));
    const spend = {
      plan_id: preview.plan_id,
      confirmation: preview.required_typed_confirmation,
      apply: true,
    };
    await ipc.maintenanceExecute(spend);

    const refused = await ipc.maintenanceExecute(spend).catch((e: unknown) => e);
    expect(rpcErrorCode(refused)).toBe(RPC_MAINTENANCE_REFUSED);

    const badParam = await ipc
      .maintenancePlan(planParams([`${STORE}\\a.jsonl`], { store_root: "relative/store" }))
      .catch((e: unknown) => e);
    expect(rpcErrorCode(badParam)).toBe(RPC_INVALID_PARAMS);
    expect(rpcErrorCode(badParam)).not.toBe(rpcErrorCode(refused));
  });

  it("codes a blank and a mismatched confirmation as REFUSED, not as a param error", async () => {
    // Both are well-formed requests the safety model declined — the -32602/-32003 split.
    const ipc = createMockIpc();
    const preview = await ipc.maintenancePlan(planParams([`${STORE}\\a.jsonl`]));
    const blank = await ipc
      .maintenanceExecute({ plan_id: preview.plan_id })
      .catch((e: unknown) => e);
    expect(rpcErrorCode(blank)).toBe(RPC_MAINTENANCE_REFUSED);
    const wrong = await ipc
      .maintenanceExecute({ plan_id: preview.plan_id, confirmation: "nope" })
      .catch((e: unknown) => e);
    expect(rpcErrorCode(wrong)).toBe(RPC_MAINTENANCE_REFUSED);
  });

  it("reports a MISSING manifest as an internal fault, NOT as a refusal", async () => {
    // Verified engine roughness, reproduced deliberately: `read_checkpoint` opens the file
    // directly (`llm_anthology/maintenance.py:577`), so a bad path raises FileNotFoundError,
    // which is neither MaintenanceRefused nor ValueError and therefore escapes
    // `_maintenance_call`'s mapping into the -32603 catch-all (`sidecar.py:676-677`).
    // A panel that treats "missing manifest" as a refusal will mislabel it.
    const ipc = createMockIpc();
    const missing = await ipc
      .maintenanceRestore({ manifest_path: "C:\\cp\\nope.json" })
      .catch((e: unknown) => e);
    expect(rpcErrorCode(missing)).toBe(RPC_INTERNAL_ERROR);
    expect(rpcErrorCode(missing)).not.toBe(RPC_MAINTENANCE_REFUSED);
  });

  it("performs NO filesystem work: two clients never see each other's state", async () => {
    // The safety property that lets a dev run drive this panel. Nothing is persisted, so a
    // second client starts with an empty ledger even after the first applied a run.
    const first = createMockIpc();
    const p = await first.maintenancePlan(planParams([`${STORE}\\a.jsonl`]));
    await first.maintenanceExecute({
      plan_id: p.plan_id,
      confirmation: p.required_typed_confirmation,
      apply: true,
    });
    expect(await first.maintenanceRuns()).toHaveLength(1);
    expect(await createMockIpc().maintenanceRuns()).toEqual([]);
  });
});
