import { describe, expect, it } from "vitest";

import {
  createMockIpc,
  CREDENTIAL_SHAPE_COVERAGE_LIMIT,
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
import REDACT_PY from "../../../llm_anthology/redact.py?raw";

import {
  RPC_BUILD_IN_PROGRESS,
  RPC_BUILD_UNAVAILABLE,
  RPC_CORPUS_EXISTS,
  RPC_CORPUS_NOT_INDEXED,
  RPC_INDEX_REBUILD_REQUIRED,
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
  INDEX_REBUILD_REQUIRED: RPC_INDEX_REBUILD_REQUIRED,
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

  // DECISION G-17: the mock no longer answers `graph.children` / `graph.ancestors`,
  // because the IPC contract no longer declares them — nothing in the app ever called
  // either one. Asserted rather than just deleted: the mock is the surface a dev sees in
  // a browser, so a method reappearing HERE with no contract entry would make a preview
  // pane look wired while `real.ts` had no route at all. That mock/real divergence is the
  // exact failure `ipc/index.ts` documents.
  it("does not answer the two graph walks G-17 removed", () => {
    expect("graphChildren" in mockIpc).toBe(false);
    expect("graphAncestors" in mockIpc).toBe(false);
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
      // child_count is the direct out-degree, cross-checked against the raw edge fixture.
      // This used to go through `graph.children` (removed by DECISION G-17) — counting
      // MOCK_EDGES is the STRONGER check anyway: `graphChildren` and `graphRollup` both
      // derived from the same `MockGraph`, so agreeing proved only that one class was
      // self-consistent. The fixture is an independent signal.
      const outDegree = MOCK_EDGES.filter((e) => e.parent === id).length;
      expect(table[id].child_count).toBe(outDegree);
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
    // ZERO, not MOCK_THREADS.length. This bite bundles no transcripts, so a run writes no
    // conversations; the engine returns 0 and the mock must not claim otherwise. The old
    // assertion counted rows that nothing exports.
    expect(plan.conversation_count).toBe(0);
    // est_bytes is the SERIALIZED GRAPH, not a content sum. It used to assert equality with
    // corpus.stats bytes — the Σ(char_count) quantity the engine explicitly rejected because
    // it overstates a run by counting transcripts that never leave the index. Asserting the
    // two are DIFFERENT is the point: if they ever coincide again, the mock has drifted back.
    const stats = await mockIpc.corpusStats();
    expect(plan.est_bytes).toBeGreaterThan(0);
    expect(plan.est_bytes).not.toBe(stats.bytes);
  });

  it("carries the ENGINE's coverage sentence byte-for-byte, not a copy that drifted", () => {
    // WHY THIS IS NOT `toBe(CREDENTIAL_SHAPE_COVERAGE_LIMIT)`. Every other assertion in this
    // block compares the mock against the mock's own exported constant, which is a
    // tautology for the TEXT: reword the constant and both sides move together. Measured —
    // a mutation that gutted the sentence left this suite GREEN. The only non-circular
    // check is against the engine's source, so that is what this reads.
    const m = /CREDENTIAL_SHAPE_COVERAGE_LIMIT = \(([\s\S]*?)\n\)/.exec(REDACT_PY as string);
    expect(m).not.toBeNull(); // control: a parse failure must fail, not silently pass
    const engineText = [...(m as RegExpExecArray)[1].matchAll(/"((?:[^"\\]|\\.)*)"/g)]
      .map((s) => s[1])
      .join("");
    expect(engineText.length).toBeGreaterThan(200); // control: the parse found real prose
    expect(CREDENTIAL_SHAPE_COVERAGE_LIMIT).toBe(engineText);
  });

  it("defaults to full mode and carries the unconditional coverage limit", async () => {
    const plan = await mockIpc.exportPlan();
    expect(plan.mode).toBe("full"); // absent mode means full, the projection that changes nothing
    // The load-bearing assertion: coverage_limit is present WITH ZERO FINDINGS. An empty
    // findings list is not a safety verdict, and the sentence is what says so.
    expect(plan.credential_scan.findings).toEqual([]);
    expect(plan.credential_scan.coverage_limit).toBe(CREDENTIAL_SHAPE_COVERAGE_LIMIT);
    expect(plan.credential_scan.coverage_limit).not.toBe("");
    expect(plan.credential_scan.scrubbed).toBe(false); // a dry run changes nothing, by definition
  });

  it("measures est_bytes on the projection the MODE would write, not on the full graph", async () => {
    const full = await mockIpc.exportPlan(undefined, "full");
    const shareable = await mockIpc.exportPlan(undefined, "shareable");
    expect(full.mode).toBe("full");
    expect(shareable.mode).toBe("shareable");
    // Dropping `preview` is a real reduction, so a shareable preview must not be quoted at
    // the archive-of-record's size. This is the whole reason the estimate takes a mode.
    expect(shareable.est_bytes).toBeLessThan(full.est_bytes);
    expect(shareable.node_count).toBe(full.node_count); // a projection, not a filter
  });

  it("scans the PROJECTED graph, so a shareable export loses a preview-borne finding", async () => {
    const leaky = [
      {
        id: "t-leak",
        title: "harmless title",
        provider: "claude",
        created_at_ms: 1,
        cwd: "C:/Users/someone/proj",
        preview: "here is the key sk-abcdefghijklmnop0123456789 do not share",
      },
    ];
    const ipc = createMockIpc(leaky, []);
    const full = await ipc.exportPlan(undefined, "full");
    expect(full.credential_scan.findings).toHaveLength(1);
    const [hit] = full.credential_scan.findings;
    expect(hit.scope).toBe("thread");
    expect(hit.id).toBe("t-leak");
    expect(hit.field).toBe("preview");
    expect(hit.shape).toBe("api-key");
    // MASKED: first four characters and a length, never the run itself.
    expect(hit.preview).toMatch(/^sk-a… \(\d+ chars\)$/);
    expect(hit.preview).not.toContain("abcdefghijklmnop");

    // SHAREABLE drops `preview` outright, so the finding is genuinely gone from the artifact
    // — not filtered from the report. That is the projection doing privacy work.
    const shareable = await ipc.exportPlan(undefined, "shareable");
    expect(shareable.credential_scan.findings).toEqual([]);
    // ...and the coverage sentence still travels, which is exactly when it matters most.
    expect(shareable.credential_scan.coverage_limit).toBe(CREDENTIAL_SHAPE_COVERAGE_LIMIT);
  });

  it("relativizes a home-anchored cwd in shareable mode", async () => {
    // NO `preview` on this row, deliberately. That isolates the cwd: preview-dropping is
    // already covered above, so with it absent the ONLY difference between the two
    // projections is the path, and a size delta can only have come from relativizing it.
    const rows = [
      {
        id: "t1",
        title: "t",
        provider: "claude",
        created_at_ms: 1,
        cwd: "C:/Users/a-long-operating-system-username/projects/anthology",
      },
    ];
    const ipc = createMockIpc(rows, []);
    const full = await ipc.exportPlan(undefined, "full");
    const shareable = await ipc.exportPlan(undefined, "shareable");
    expect(shareable.est_bytes).toBeLessThan(full.est_bytes);
    // WHAT THIS CANNOT SEE, stated rather than implied: ExportPlan carries counts and a
    // size, not the projected nodes, so the relativized STRING is not on this wire and no
    // assertion here can read it. The byte delta is the only observable the plan offers.
    // A direct check belongs where the projected node is visible — which, for the engine,
    // is `redact.shareable_thread`'s own tests.
    const unprojected = JSON.stringify(await ipc.exportPlan(undefined, "full"));
    expect(unprojected).not.toContain("a-long-operating-system-username");
  });

  it("runs the export and passes both fidelity gates for a valid dest", async () => {
    const run = await mockIpc.exportRun("/tmp/llm-anthology-export.json");
    expect(run.ok).toBe(true);
    expect(run.written_path).toBe("/tmp/llm-anthology-export.json");
    expect(run.graph_gate).toBe(true);
    expect(run.transcript_gate).toBe(true);
    expect(run.mode).toBe("full");
    expect(run.credential_scan.coverage_limit).toBe(CREDENTIAL_SHAPE_COVERAGE_LIMIT);
    expect(run.credential_scan.scrubbed).toBe(false);
  });

  it("echoes the requested mode and the scrub opt-in", async () => {
    const run = await mockIpc.exportRun("/tmp/x.json", "shareable", true);
    expect(run.mode).toBe("shareable");
    // `scrubbed` reports what actually happened to the bytes, so the reader can tell a
    // warning-only run from one that rewrote the artifact.
    expect(run.credential_scan.scrubbed).toBe(true);
  });

  it("refuses a blank dest: not ok, no written_path, gates still pass", async () => {
    const run = await mockIpc.exportRun("   ");
    expect(run.ok).toBe(false);
    expect(run.written_path).toBeUndefined();
    expect(run.graph_gate).toBe(true);
    expect(run.transcript_gate).toBe(true);
    // FORWARDED ON FAILURE. A blocked export is exactly when the user is about to retry,
    // and dropping the warning there would make them retry blind.
    expect(run.credential_scan.coverage_limit).toBe(CREDENTIAL_SHAPE_COVERAGE_LIMIT);
    expect(run.mode).toBe("full");
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

  it("refuses an empty store_root / checkpoint_root as a PARAM error", async () => {
    // The engine requires both as non-empty strings at the RPC edge (`sidecar.py:1524-1534`).
    // The code matters as much as the refusal: -32602 says "your form is incomplete" while
    // -32003 says "the engine declined a valid request", and a panel routes them differently.
    const ipc = createMockIpc();
    for (const over of [{ store_root: "" }, { checkpoint_root: "" }] as const) {
      const key = Object.keys(over)[0];
      const err = await ipc
        .maintenancePlan(planParams([`${STORE}\\a.jsonl`], over))
        .catch((e: unknown) => e);
      expect(String((err as Error).message)).toContain(`${key} must be a non-empty string`);
      expect(rpcErrorCode(err)).toBe(RPC_INVALID_PARAMS);
    }
  });

  it("never quarantines two different files onto one destination path", async () => {
    // A store is date-nested, so the SAME basename recurs in every day directory. Flattening
    // them into one checkpoint root without disambiguating would have the second move
    // overwrite the first — a silent data loss inside the feature whose whole promise is that
    // a delete is recoverable. The suffix is a deterministic `-N`, not a GUID, so the
    // destination can be SHOWN in the preview and checked later (`maintenance.py:472-486`).
    const ipc = createMockIpc();
    const preview = await ipc.maintenancePlan({
      store_root: STORE,
      checkpoint_root: CHECKPOINT,
      action: "delete",
      targets: [
        { session_id: "s1", file_path: `${STORE}\\jan\\rollout.jsonl` },
        { session_id: "s2", file_path: `${STORE}\\feb\\rollout.jsonl` },
        { session_id: "s3", file_path: `${STORE}\\mar\\rollout.jsonl` },
        // No extension AND no session_id — the stem/ext split has nothing to cut on, and the
        // omitted id must default to "" rather than land `undefined` in the manifest.
        { file_path: `${STORE}\\extensionless` },
      ],
    });
    expect(preview.plan.map((m) => m.destination)).toEqual([
      `${CHECKPOINT}\\deleted\\rollout.jsonl`,
      `${CHECKPOINT}\\deleted\\rollout-2.jsonl`,
      `${CHECKPOINT}\\deleted\\rollout-3.jsonl`,
      `${CHECKPOINT}\\deleted\\extensionless`,
    ]);
    expect(new Set(preview.plan.map((m) => m.destination)).size).toBe(4);
    expect(preview.plan[3].session_id).toBe("");
    expect(preview.allowed[3].session_id).toBe("");
    expect(preview.required_typed_confirmation).toBe("DELETE 4 FILES");
  });

  it("refuses an empty plan_id as a PARAM error, not a maintenance refusal", async () => {
    // Distinct from an unknown-but-present id, which is -32003 (tested above). A panel that
    // conflated them would tell the user to re-plan when the real fault is a blank field.
    const ipc = createMockIpc();
    const err = await ipc.maintenanceExecute({ plan_id: "" }).catch((e: unknown) => e);
    expect(String((err as Error).message)).toMatch(/plan_id must be a non-empty string/);
    expect(rpcErrorCode(err)).toBe(RPC_INVALID_PARAMS);
    const unknown = await ipc
      .maintenanceExecute({ plan_id: "plan-999", confirmation: "DELETE 1 FILE" })
      .catch((e: unknown) => e);
    expect(rpcErrorCode(unknown)).toBe(RPC_MAINTENANCE_REFUSED);
  });

  it("refuses an empty manifest_path before looking for a checkpoint", async () => {
    // -32602, where a well-formed path with no checkpoint behind it is -32603 (an engine-side
    // failure). Same split as plan_id above: a blank field is the caller's, not the engine's.
    const ipc = createMockIpc();
    const err = await ipc.maintenanceRestore({ manifest_path: "" }).catch((e: unknown) => e);
    expect(String((err as Error).message)).toMatch(/manifest_path must be a non-empty string/);
    expect(rpcErrorCode(err)).toBe(RPC_INVALID_PARAMS);
    const missing = await ipc
      .maintenanceRestore({ manifest_path: `${CHECKPOINT}\\manifest-404.json` })
      .catch((e: unknown) => e);
    expect(rpcErrorCode(missing)).toBe(RPC_INTERNAL_ERROR);
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

// ---------------------------------------------------------------------------
// The FIRST-RUN plane: discover -> create -> build -> poll.
//
// None of these four had a single call against the mock. They are the ONLY path a user with
// no corpus can take, and outside Tauri the mock IS the engine — so an untested first-run
// surface is untested in exactly the environment (vite dev, `vite preview`, the screenshot
// harness, a design review) where it is the only implementation running.
// ---------------------------------------------------------------------------

describe("sources.discover", () => {
  it("serves the measured scan shape: 30 findings, an engine-truncated group, scan errors",
    async () => {
      const scan = await mockIpc.discoverSources();
      expect(scan.findings).toHaveLength(30);
      // The shapes the panel has to survive, all present at once.
      expect(new Set(scan.findings.map((f) => f.kind))).toEqual(
        new Set(["built_index", "session_store", "export_file"]),
      );
      // `truncated_groups` is the ENGINE's own cap (`discover.py:108`), a different fact from
      // any collapsing the UI does, and the only signal that older items exist on disk that
      // the scan never listed.
      expect(scan.stats.truncated_groups).toEqual(["chatgpt/export_file"]);
      expect(scan.stats.errors).toHaveLength(2);
      expect(scan.stats.budget_exhausted).toBe(false);
      // 0 means "nothing datable was seen", NOT 1970 (`discover.py:645`).
      expect(scan.findings.find((f) => f.provider === "gemini")?.newest_mtime).toBe(0);
    });

  it("returns a fresh findings array and fresh detail objects on every scan", async () => {
    // The panel sorts the findings in place. A shared fixture is how a "why did the order
    // change on rescan?" bug is born, and a shared `detail` is how one render's annotation
    // leaks into the next.
    const first = await mockIpc.discoverSources();
    const beforePaths = first.findings.map((f) => f.path);
    first.findings.reverse();
    first.findings[0].detail.injected = true;

    const second = await mockIpc.discoverSources();
    expect(second.findings.map((f) => f.path)).toEqual(beforePaths);
    expect(second.findings.some((f) => "injected" in f.detail)).toBe(false);
  });

  it("returns fresh stats ARRAYS too, not just a fresh stats object", async () => {
    // The sibling above covered `findings` and left `stats` half-copied: the spread
    // `{ ...DISCOVERY.stats }` is shallow, so `errors` and `truncated_groups` — both
    // arrays, both rendered by the panel — stayed shared by reference across every scan.
    // The method's own docstring promised "a fresh deep-ish copy so a caller that sorts or
    // mutates the array in place cannot corrupt the fixture", which was true of findings
    // and false of exactly the two fields most likely to be sorted for display.
    //
    // The damage is cross-test contamination, which is the worst shape for a fixture: one
    // spec mutating the error list silently changes what a later spec observes, so the
    // failure surfaces somewhere unrelated and depends on execution order.
    const first = await mockIpc.discoverSources();
    const beforeErrors = [...first.stats.errors];
    const beforeTruncated = [...first.stats.truncated_groups];
    first.stats.errors.push("injected by a previous caller");
    first.stats.errors.reverse();
    first.stats.truncated_groups.push("codex/injected");

    const second = await mockIpc.discoverSources();
    expect(second.stats.errors).toEqual(beforeErrors);
    expect(second.stats.truncated_groups).toEqual(beforeTruncated);
  });
});

describe("corpus.create / corpus.build / corpus.build_status", () => {
  it("corpus.create reports the requested path created, and applies the engine's path guard",
    async () => {
      expect(await mockIpc.createCorpus("C:\\Users\\me\\anthology.db")).toEqual({
        index_path: "C:\\Users\\me\\anthology.db",
        created: true,
      });
      // The engine rejects a UNC or relative `index_path` before touching the disk
      // (`_reject_nonlocal_path`, `llm_anthology/sidecar.py:781`), and the mock now does the
      // same. It has to carry a JSON-RPC code, because a UI that lets the user name a network
      // destination must fail the SAME WAY in a dev run as against the engine — resolving
      // `\\host\share` on Windows initiates an outbound SMB/NTLM authentication.
      const unc = await mockIpc.createCorpus("\\\\evil.example\\share\\x.db")
        .catch((e: unknown) => e);
      expect(rpcErrorCode(unc)).toBe(RPC_INVALID_PARAMS);
      expect(String((unc as Error).message)).toMatch(/index_path must not be a UNC path/);
      const relative = await mockIpc.createCorpus("anthology.db").catch((e: unknown) => e);
      expect(rpcErrorCode(relative)).toBe(RPC_INVALID_PARAMS);
      // The CLOBBER check (`CORPUS_EXISTS`) and the parent-directory check stay absent — both
      // genuinely need a filesystem this adapter does not have.
    });

  it("corpus.build accepts the job and echoes whichever source root was named", async () => {
    const codex = await createMockIpc().corpusBuild({ sessions_root: "C:\\me\\.codex\\sessions" });
    // "running" reports ACCEPTED, not finished — build_status is the source of truth
    // (`sidecar.py:834-836`).
    expect(codex).toEqual({
      job_id: "mock-build-1",
      state: "running",
      sessions_root: "C:\\me\\.codex\\sessions",
      grok_root: "",
      claude_root: "",
      started_ms: 1_700_000_000_000,
    });
    // A GROK-ONLY build reports the Grok root AS grok_root, and `sessions_root` EMPTY.
    //
    // This asserted the opposite — that the echo "falls through to the Grok one rather
    // than reporting an empty source" — and that was a divergence dressed as a
    // convenience. Measured against the engine: a Grok-only `corpus.build` returns
    // `sessions_root: ''` alongside `grok_root: <path>`, all three keys present. So the
    // mock was putting a Grok path in the field that means "the Codex tree", and any UI
    // reading `sessions_root` to report what it imported was right against the engine and
    // wrong in every dev run — the direction that is hardest to notice.
    const grokOnly = await createMockIpc().corpusBuild({ grok_root: "C:\\me\\grok" });
    expect(grokOnly.sessions_root).toBe("");
    expect(grokOnly.grok_root).toBe("C:\\me\\grok");
    // With NEITHER root named the engine raises -32602 "name at least one source"
    // (`sidecar.py:858-861`) — a pure params check needing no filesystem — and the mock now
    // does too. Otherwise an empty build form succeeds in every dev run and fails only
    // against the engine.
    const none = await createMockIpc().corpusBuild({}).catch((e: unknown) => e);
    expect(rpcErrorCode(none)).toBe(RPC_INVALID_PARAMS);
    expect(String((none as Error).message)).toMatch(/name at least one source/);
    // A named root is refused if UNC or relative, per root (`sidecar.py:865-871`). The
    // engine's `os.path.isdir` check is NOT reproduced — that one needs a real disk.
    const unc = await createMockIpc()
      .corpusBuild({ sessions_root: "\\\\evil.example\\share" }).catch((e: unknown) => e);
    expect(rpcErrorCode(unc)).toBe(RPC_INVALID_PARAMS);
    expect(String((unc as Error).message)).toMatch(/sessions_root must not be a UNC path/);
    const grokRelative = await createMockIpc()
      .corpusBuild({ grok_root: "relative/grok" }).catch((e: unknown) => e);
    expect(rpcErrorCode(grokRelative)).toBe(RPC_INVALID_PARAMS);
    expect(String((grokRelative as Error).message))
      .toMatch(/grok_root must be an absolute local path/);
  });

  it("corpus.build_status reads back idle before any build", async () => {
    // Poll-safe at any time — the engine answers idle rather than erroring so the UI can
    // render unconditionally (`sidecar.py:973-975`, `:995`).
    expect(await createMockIpc().corpusBuildStatus()).toEqual({
      state: "idle",
      indexed_conversations: 0,
      errors: [],
    });
  });

  it("refuses a job_id supplied before any build has started", async () => {
    // `sidecar.py:991-994` raises -32602 "unknown job_id ...: no build has been started" for
    // exactly this call. Ignoring the argument and answering "idle" would tell a client that
    // polls with a stale handle after a restart that all is well, where the engine returns a
    // param error — so the poll-safe `idle` answer is for an UNNAMED poll only.
    const err = await createMockIpc().corpusBuildStatus("stale-job").catch((e: unknown) => e);
    expect(rpcErrorCode(err)).toBe(RPC_INVALID_PARAMS);
    expect(String((err as Error).message))
      .toMatch(/unknown job_id 'stale-job': no build has been started/);
  });

  it("climbs through running polls and REACHES a terminal done that then stays put",
    async () => {
      // The point of a fixed poll count: a mock that stayed `running` forever would make a
      // poll loop that never terminates look correct in every dev run.
      const ipc = createMockIpc();
      const handle = await ipc.corpusBuild({ sessions_root: "C:\\me\\.codex\\sessions" });

      // Polling WITH the handle proves the client is reading the job it started.
      const first = await ipc.corpusBuildStatus(handle.job_id);
      expect(first.state).toBe("running");
      expect(first.indexed_conversations).toBe(400);
      expect(first.finished_ms).toBeUndefined();
      expect(first.job_id).toBe("mock-build-1");
      // `sessions_root` is the root the build NAMED, matching the handle above. The engine
      // reads it off the job snapshot (`sidecar.py:1000`); a hardcoded stand-in would show a
      // panel that reads the ingest source off the STATUS a path that exists nowhere.
      expect(first.sessions_root).toBe("C:\\me\\.codex\\sessions");

      const second = await ipc.corpusBuildStatus();
      expect(second.state).toBe("running");
      expect(second.indexed_conversations).toBe(800);

      const third = await ipc.corpusBuildStatus();
      expect(third.state).toBe("done");
      expect(third.indexed_conversations).toBe(1200);
      expect(third.finished_ms).toBe(1_700_000_030_000);

      // Terminal means terminal: a fourth poll neither reverts to running nor keeps climbing.
      const fourth = await ipc.corpusBuildStatus();
      expect(fourth.state).toBe("done");
      expect(fourth.indexed_conversations).toBe(1200);
    });

  it("refuses a poll naming a DIFFERENT job than the one in flight", async () => {
    const ipc = createMockIpc();
    await ipc.corpusBuild({ sessions_root: "C:\\me\\.codex\\sessions" });
    const err = await ipc.corpusBuildStatus("other-job").catch((e: unknown) => e);
    expect(String((err as Error).message)).toMatch(/unknown job_id 'other-job'/);
    // Thrown through `rpcError`, so it carries a JSON-RPC envelope and `rpcErrorCode` finds
    // the engine's code (`sidecar.py:996-998`). A bare `Error` here would read `null` and
    // leave a panel's RPC_INVALID_PARAMS branch dead in every dev run — the exact
    // invisible-dead-path class `rpcError` was introduced (mock.ts:645-649) to prevent.
    expect(rpcErrorCode(err)).toBe(RPC_INVALID_PARAMS);
    // The refused poll must not consume a poll slot either.
    expect((await ipc.corpusBuildStatus()).indexed_conversations).toBe(400);
  });
});

describe("MockGraph walks not otherwise exercised", () => {
  it("allEdges hands back the constructor's array BY REFERENCE", () => {
    // Characterizing a hazard, not blessing it: unlike `discoverSources` and `dedupSessions`,
    // which both copy defensively, this returns the live array — a caller that sorts it in
    // place reorders the module-level fixture for every later reader. Measured: nothing in
    // `cockpit/src` calls it, so the hazard is latent rather than live.
    const g = new MockGraph(MOCK_THREADS, MOCK_EDGES);
    expect(g.allEdges()).toEqual(MOCK_EDGES);
    expect(g.allEdges()).toBe(MOCK_EDGES);
  });

  it("graph.subtree honours a depth cap on the WALK", async () => {
    // Mirrors `_collect_subtree` (`sidecar.py:1677-1687`): the cap bounds how far the frontier
    // expands, and depth 0 is a real value meaning "this node only" — not "no cap".
    expect((await mockIpc.graphSubtree("orch", 0)).nodes.map((n) => n.id)).toEqual(["orch"]);
    const d1 = await mockIpc.graphSubtree("orch", 1);
    expect(d1.nodes.map((n) => n.id).sort()).toEqual(["ipc", "orch", "plan", "tests"]);
    expect(d1.nodes.map((n) => n.id)).not.toContain("deepfix"); // depth 3, out of reach
    // Edges are every edge WITHIN the collected set, so a diamond closed inside the cap is
    // kept even though the walk reached its child by another route.
    expect(d1.edges).toContainEqual({ parent: "plan", child: "tests", status: "completed" });
    for (const e of d1.edges) {
      expect(d1.nodes.map((n) => n.id)).toContain(e.parent);
      expect(d1.nodes.map((n) => n.id)).toContain(e.child);
    }
    // Uncapped still reaches the depth-3 leaf, so the cap is what bounded it above.
    expect((await mockIpc.graphSubtree("orch")).nodes.map((n) => n.id)).toContain("deepfix");
  });

  it("the ancestor walk reports a shared ancestor ONCE, nearest first", () => {
    // `review` is reached from three parents and `repro` sits above two of them, so an
    // un-deduped BFS would list it twice and a panel would draw the same thread as two
    // ancestors (`_collect_ancestors`, `sidecar.py:1689-1698`).
    //
    // Driven through `MockGraph.collectAncestors` rather than an IPC method: DECISION G-17
    // removed `graphAncestors` from the contract (no caller), but the BFS-dedup property is
    // engine-parity behaviour worth keeping under test, and this is its only implementation.
    // Same shape as the `childrenOf`/`depth`/`fanOut` fixture test above.
    const anc = new MockGraph(MOCK_THREADS, MOCK_EDGES).collectAncestors("review");
    expect(anc).toEqual([
      "crosscheck", "bisect", "orphan", // the direct parents, in edge order
      "repro", "real", "pruned-parent", // their parents
      "ipc", "orch",
    ]);
    expect(new Set(anc).size).toBe(anc.length);
    expect(anc).not.toContain("review"); // never itself
  });
});

describe("graph.roots ordering", () => {
  it("orders by RECENCY, falling back created -> 0 for a node with neither", async () => {
    // `_order_nodes` "recent" (`sidecar.py:1670-1672`) keys on updated_at_ms OR created_at_ms
    // OR 0. All three arms are live in the built-in forest: `orch` has an update, `repro` and
    // `research` have only a birth date, and the dangling `pruned-parent` has neither — so it
    // sinks to the bottom instead of being dropped or thrown at.
    const recent = await mockIpc.graphRoots({ order: "recent" });
    expect(recent.map((n) => n.id)).toEqual(["orch", "repro", "research", "pruned-parent"]);
  });

  it("orders by creation with undated LAST and a total id tiebreak", async () => {
    const created = await mockIpc.graphRoots();
    expect(created.map((n) => n.id)).toEqual(["orch", "research", "repro", "pruned-parent"]);

    // Two undated roots plus a dated one: both undated collapse to the same sort key, so the
    // id tiebreak is the only thing making the order TOTAL — and a total order is what lets
    // limit/offset paging partition the set instead of depending on sort stability.
    const ipc = createMockIpc(
      [{ id: "dated", title: "Dated", provider: "claude", created_at_ms: 900 }],
      [
        { parent: "dated", child: "kid" },
        { parent: "zz-undated", child: "kid" },
        { parent: "aa-undated", child: "kid" },
      ],
    );
    expect((await ipc.graphRoots()).map((n) => n.id))
      .toEqual(["dated", "aa-undated", "zz-undated"]);
  });

  it("breaks a same-millisecond tie on id, whatever order the rows arrive in", async () => {
    // Roots sharing one timestamp: the id comparison is the only thing left deciding.
    //
    // THREE input arrangements, not one. A comparator is asked one question per pair it is
    // handed, and which DIRECTION it is asked depends on the incoming order — measured here:
    // the shuffled arrangement alone exercised only the "after" answer, so a mutation to the
    // "before" arm survived it. The contract is that all three arrangements agree.
    const roots = (ids: string[]) =>
      createMockIpc(
        ids.map((id) => ({ id, title: id, provider: "claude", created_at_ms: 500 })),
        [],
      ).graphRoots();
    const expected = ["alpha", "beta", "delta", "gamma"];
    expect((await roots(["beta", "alpha", "delta", "gamma"])).map((n) => n.id))
      .toEqual(expected);
    expect((await roots([...expected])).map((n) => n.id)).toEqual(expected);
    expect((await roots([...expected].reverse())).map((n) => n.id)).toEqual(expected);
  });
});

describe("search.query edges", () => {
  it("treats an EMPTY query as match-all, still newest-first and still provider-filtered",
    async () => {
      const all = await mockIpc.searchQuery({ q: "" });
      expect(all.total).toBe(15);
      expect(all.hits).toHaveLength(15);
      expect(all.hits[0].thread_id).toBe("orphan"); // the newest row
      const filtered = await mockIpc.searchQuery({ q: "", provider: "codex" });
      expect(filtered.total).toBe(7);
      expect(filtered.hits.every((h) => h.provider === "codex")).toBe(true);
      // Whitespace is trimmed to the same empty query, not searched for literally.
      expect((await mockIpc.searchQuery({ q: "   " })).total).toBe(15);
    });

  it("breaks a same-timestamp tie on id so paging PARTITIONS the result set", async () => {
    // The engine's stated order is `created_at DESC, conversation_id`. Without the tiebreak
    // two rows born in the same millisecond have no defined order, and a LIMIT/OFFSET page
    // boundary can then show one row twice and skip another.
    const ipc = createMockIpc(
      ["zeta", "alpha", "mu"].map((id) => ({
        id, title: "Same clock", provider: "claude", created_at_ms: 1000,
      })),
      [],
    );
    const whole = await ipc.searchQuery({ q: "clock" });
    expect(whole.hits.map((h) => h.thread_id)).toEqual(["alpha", "mu", "zeta"]);
    const pageOne = await ipc.searchQuery({ q: "clock", limit: 2 });
    const pageTwo = await ipc.searchQuery({ q: "clock", limit: 2, offset: 2 });
    expect([...pageOne.hits, ...pageTwo.hits].map((h) => h.thread_id))
      .toEqual(whole.hits.map((h) => h.thread_id));
  });
});

describe("search.query D-3 facets (since / until / histogram)", () => {
  /** Three rows a month apart, so a month-granularity bound has something to include AND
   *  exclude on both sides. `created_at_ms` is UTC-explicit to keep the ISO prefix stable. */
  const DATED = [
    { id: "feb", title: "winter note", provider: "claude", created_at_ms: Date.UTC(2026, 1, 10) },
    { id: "mar-early", title: "spring note", provider: "claude", created_at_ms: Date.UTC(2026, 2, 1) },
    // 23:59:59 on the LAST day of March — the row a naive `created_at <= '2026-03-31'`
    // silently drops, because the full timestamp sorts after the bare date.
    { id: "mar-late", title: "spring note", provider: "codex", created_at_ms: Date.UTC(2026, 2, 31, 23, 59, 59) },
    { id: "apr", title: "summer note", provider: "claude", created_at_ms: Date.UTC(2026, 3, 5) },
  ];
  const dated = () => createMockIpc(DATED, []);

  it("bounds INCLUSIVELY at the granularity the caller expressed", async () => {
    // `2026-03` is a 7-character prefix compare, so it covers the whole month at both ends.
    const march = await dated().searchQuery({ q: "note", since: "2026-03", until: "2026-03" });
    expect(march.hits.map((h) => h.thread_id).sort()).toEqual(["mar-early", "mar-late"]);
    expect(march.total).toBe(2);
    // The 23:59:59 row is the one this is really about: it is IN March and must not be lost.
    const toEndOfMarch = await dated().searchQuery({ q: "note", until: "2026-03" });
    expect(toEndOfMarch.hits.map((h) => h.thread_id)).toContain("mar-late");
    // A year bound is a 4-character compare, so it covers everything in 2026.
    expect((await dated().searchQuery({ q: "note", since: "2026", until: "2026" })).total).toBe(4);
  });

  it("composes the bounds with the provider facet rather than replacing it", async () => {
    const r = await dated().searchQuery({ q: "note", since: "2026-03", provider: "codex" });
    expect(r.hits.map((h) => h.thread_id)).toEqual(["mar-late"]);
  });

  it("REFUSES a malformed bound instead of returning a cheerful zero-hit page", async () => {
    // The engine's stated reason: the compare is a PREFIX compare, so `2026-3` would test six
    // characters and match nothing. An empty result is the worst answer to a typo — it is
    // indistinguishable from a true negative.
    for (const bad of ["2026-3", "2026/03/15", "2026-03-15T00:00:00Z", "march"]) {
      await expect(dated().searchQuery({ q: "note", since: bad })).rejects.toSatisfy(
        (e: unknown) => rpcErrorCode(e) === RPC_INVALID_PARAMS,
      );
    }
    // ...and the same rule on the other bound, not just the one that happened to be tested.
    await expect(dated().searchQuery({ q: "note", until: "2026-3" })).rejects.toSatisfy(
      (e: unknown) => rpcErrorCode(e) === RPC_INVALID_PARAMS,
    );
  });

  it("EXCLUDES an undated row from either bound rather than admitting it", async () => {
    // Without this, `until` admits exactly the rows `since` rejects: the same facet giving
    // opposite answers with no error anywhere. "In this range" must not mean "or undated".
    const undated = [...DATED, { id: "nodate", title: "note", provider: "claude", created_at_ms: NaN }];
    const ipc = createMockIpc(undated, []);
    expect((await ipc.searchQuery({ q: "note" })).total).toBe(5); // present when unbounded
    expect((await ipc.searchQuery({ q: "note", since: "2026" })).hits.map((h) => h.thread_id))
      .not.toContain("nodate");
    expect((await ipc.searchQuery({ q: "note", until: "2026" })).hits.map((h) => h.thread_id))
      .not.toContain("nodate");
  });

  it("OMITS the histogram key entirely when no granularity was asked for", async () => {
    // Absent means absent, not an empty array: an old caller must pay neither a new response
    // key nor a second pass. This is the contract that keeps the pre-D-3 response three keys.
    const r = await dated().searchQuery({ q: "note" });
    expect("histogram" in r).toBe(false);
    expect(Object.keys(r).sort()).toEqual(["hits", "took_ms", "total"]);
  });

  it("rolls the histogram over the WHOLE match set, so its counts sum to total not to the page",
    async () => {
      // The invariant the engine pins: the histogram exists to say where the OTHER pages are,
      // so a roll-up scoped to `limit` rows would draw a March missing conversations the list
      // beside it can still open.
      const r = await dated().searchQuery({ q: "note", histogram: "month", limit: 1 });
      expect(r.hits).toHaveLength(1);
      expect(r.total).toBe(4);
      const summed = (r.histogram ?? []).reduce((n, b) => n + b.count, 0);
      expect(summed).toBe(r.total);
      expect(summed).not.toBe(r.hits.length);
      expect(r.histogram).toEqual([
        { bucket: "2026-02", count: 1 },
        { bucket: "2026-03", count: 2 },
        { bucket: "2026-04", count: 1 },
      ]);
    });

  it("buckets at year and day granularity too, keyed by the ISO prefix", async () => {
    const byYear = await dated().searchQuery({ q: "note", histogram: "year" });
    expect(byYear.histogram).toEqual([{ bucket: "2026", count: 4 }]);
    // BOUNDED at both ends on purpose. A lone `since: "2026-03"` also admits April, so the
    // first draft of this case expected two columns and got three — the histogram was right
    // and the expectation was not.
    const byDay = await dated().searchQuery({
      q: "note", histogram: "day", since: "2026-03", until: "2026-03",
    });
    expect(byDay.histogram).toEqual([
      { bucket: "2026-03-01", count: 1 },
      { bucket: "2026-03-31", count: 1 },
    ]);
  });

  it("REFUSES a bucket outside the vocabulary, including a flag-shaped one", async () => {
    // `true` is refused explicitly rather than coerced: a caller that guessed the API has to
    // fail loudly instead of silently selecting a granularity nobody chose.
    for (const bad of ["week", "", "MONTH"]) {
      await expect(dated().searchQuery({ q: "note", histogram: bad as never })).rejects.toSatisfy(
        (e: unknown) => rpcErrorCode(e) === RPC_INVALID_PARAMS,
      );
    }
    await expect(dated().searchQuery({ q: "note", histogram: true as never })).rejects.toSatisfy(
      (e: unknown) => rpcErrorCode(e) === RPC_INVALID_PARAMS,
    );
  });

  it("returns an EMPTY histogram, not an absent one, when the filter matches nothing", async () => {
    // Asked-for-and-empty is a different answer from not-asked-for, and a UI drawing an axis
    // needs to tell them apart.
    const r = await dated().searchQuery({ q: "note", histogram: "month", since: "2030" });
    expect(r.total).toBe(0);
    expect(r.histogram).toEqual([]);
  });
});

describe("projections over a sparse thread row", () => {
  /** One thread carrying ONLY the required fields — no tokens, sizes, dates or preview. */
  const SPARSE = [{ id: "bare", title: "Bare row", provider: "claude", created_at_ms: 5 }];

  it("reports an UNKNOWN adapter's model vendor as '', never the adapter name", async () => {
    // The pair `provider` (adapter) / `model_provider` (model vendor) is derived, and the
    // realistic mapping is the whole point: a Codex rollout records "openai". An adapter the
    // table does not know must degrade to "" like the engine, rather than echoing itself —
    // otherwise anything tinting by vendor invents a colour for a provider it cannot know.
    const ipc = createMockIpc(
      [
        { id: "g", title: "Grok", provider: "grok", created_at_ms: 1 },
        { id: "w", title: "Unheard of", provider: "wat", created_at_ms: 2 },
      ],
      [],
    );
    expect((await ipc.threadGet("g")).model_provider).toBe("xai");
    expect((await ipc.threadGet("w")).model_provider).toBe("");
    expect((await ipc.threadGet("w")).provider).toBe("wat");
  });

  it("counts a row with no turn_count / char_count as zero, not NaN", async () => {
    // `records` and `bytes` are summed, so a single undefined would poison the whole tally
    // into NaN and render as "NaN conversations indexed".
    expect(await createMockIpc(SPARSE, []).corpusStats()).toEqual({
      conversations: 1,
      records: 0,
      threads: 1,
      edges: 0,
      bytes: 0,
      providers: { claude: 1 },
    });
    // NOT zero any more, and that is the correction rather than a regression. est_bytes is
    // the serialized GRAPH, so a corpus with one node and no turns still has a node to
    // serialize. What this case actually guards is the original intent — a row with no
    // char_count/turn_count must not poison the arithmetic — so assert a finite positive
    // number rather than the 0 the old content-sum model produced.
    const sparsePlan = await createMockIpc(SPARSE, []).exportPlan();
    expect(sparsePlan.est_bytes).toBeGreaterThan(0);
    expect(Number.isFinite(sparsePlan.est_bytes)).toBe(true);
    expect(sparsePlan.conversation_count).toBe(0);
  });

  it("conversation.get falls back to the birth date and the title as body text", async () => {
    // `plan` carries neither `updated_at_ms` nor `preview`, which is the common shape.
    const conv = await mockIpc.conversationGet("plan");
    expect(conv.available).toBe(true);
    if (!conv.available) throw new Error("unreachable: plan is a real row");
    // An absent update is reported as the creation time, NOT as null/absent — a reader that
    // formats `updated_at` unconditionally would otherwise print "Invalid Date".
    expect(conv.updated_at).toBe(conv.created_at);
    expect(conv.created_at).toBe(new Date(1_700_000_060_000).toISOString());
    expect(conv.turns[0].blocks).toEqual([{ type: "text", text: "Plan the spawn-tree UI" }]);
    // …and a row that HAS both uses them, so the fallback is not simply always taken.
    const withBoth = await mockIpc.conversationGet("orch");
    if (!withBoth.available) throw new Error("unreachable: orch is a real row");
    expect(withBoth.updated_at).not.toBe(withBoth.created_at);
    expect(withBoth.turns[0].blocks).toEqual([
      { type: "text", text: "Ship the cockpit spawn-tree UI end to end." },
    ]);
  });
});

describe("metadata projections that need a row the fixture does not have", () => {
  it("metadata.search drops an annotation whose conversation is not indexed (INNER JOIN)",
    async () => {
      // The annotation store is keyed by conversation id and does not police it, so an
      // annotation can outlive its conversation. `metadata.get` still reads it back; the
      // SEARCH view must not, or the panel lists a row nothing can open
      // (`llm_anthology/metadata.py`'s join, mirrored at mock.ts:1562-1563).
      //
      // RE-ANCHORED, and it was drift rather than a wrong claim: this said `mock.ts:1468-1469`,
      // which is `exportPlan` and has nothing to do with a metadata join — the real
      // `// INNER JOIN: no indexed conversation, no row` line sits ~94 lines further down.
      // Found by auditing what the G-17 deletion above shifted, which is the only reason it
      // surfaced: `test_citation_anchors.py` scrapes `.py` citations only, so a rotten
      // `mock.ts:<line>` anchor like this one is invisible to every gate in the repo.
      const ipc = createMockIpc();
      await ipc.metadataSet({ conversation_id: "ghost-conversation", tags: ["triage"] });
      expect((await ipc.metadataGet("ghost-conversation")).tags).toEqual(["triage"]);

      const rows = await ipc.metadataSearch({ tag: "triage" });
      expect(rows.map((r) => r.conversation_id)).toEqual(["repro"]);
      // `repro` has no `updated_at_ms`, so the row reports its birth date rather than null.
      expect(rows[0].updated_at).toBe(rows[0].created_at);
      expect(rows[0].turn_count).toBe(27);
    });

  it("metadata.search reports 0 turns and a blank account for a sparse row", async () => {
    // `conversations.account` is NOT NULL DEFAULT '' (`corpus.py:183`), so unknown is "" on
    // THIS surface while `conversation.get` reports the same fact as null. Two shapes, one
    // blank — a panel that expects null here renders "null".
    const ipc = createMockIpc(
      [{ id: "bare", title: "Bare row", provider: "claude", created_at_ms: 5 }],
      [],
    );
    await ipc.metadataSet({ conversation_id: "bare", tags: ["needle"] });
    const rows = await ipc.metadataSearch({ tag: "needle" });
    expect(rows).toHaveLength(1);
    expect(rows[0].turn_count).toBe(0);
    expect(rows[0].account).toBe("");
    expect(rows[0].thread_id).toBe("bare");
  });

  it("metadata.tags keeps the lexicographically-first spelling as the facet label",
    async () => {
      // Seeded: `orch` -> "Rust", `ipc` -> "rust". Adding a THIRD conversation spelled "rust"
      // makes the scan meet a spelling that is NOT better than the one already chosen, which
      // is the only way to prove the label is a MINIMUM rather than a last-write-wins.
      const ipc = createMockIpc();
      await ipc.metadataSet({ conversation_id: "plan", tags: ["rust"] });
      expect(await ipc.metadataTags()).toEqual([
        { tag: "Rust", count: 3 },
        { tag: "shipped", count: 1 },
        { tag: "triage", count: 1 },
      ]);
    });
});

describe("graph.at over a duplicated edge", () => {
  it("keeps both rows of a repeated (parent, child) pair in input order", async () => {
    // The built-in forest has no repeated pair, but `createMockIpc` takes any edge list and
    // the engine's edge table does not enforce uniqueness either. The stable-sort tie is what
    // decides here, and the two rows differ only in `status` — collapsing them would silently
    // drop a spawn outcome, and reordering them would flip which status a renderer shows.
    const ipc = createMockIpc(
      [
        { id: "p", title: "P", provider: "claude", created_at_ms: 1 },
        { id: "c", title: "C", provider: "codex", created_at_ms: 2 },
      ],
      [
        { parent: "p", child: "c", status: "completed" },
        { parent: "p", child: "c", status: "failed" },
      ],
    );
    const snap = await ipc.graphAt(10);
    expect(snap.nodes.map((n) => n.id)).toEqual(["c", "p"]);
    expect(snap.edges).toEqual([
      { parent: "p", child: "c", status: "completed" },
      { parent: "p", child: "c", status: "failed" },
    ]);
    // graph.diff keys an edge by its (parent, child) pair alone, so the SAME input collapses
    // to one there. Both are right for their own contract; a caller must not assume the two
    // surfaces agree on edge cardinality.
    const diff = await ipc.graphDiff(0, 10);
    expect(diff.added_edges).toEqual([{ parent: "p", child: "c" }]);
  });
});
