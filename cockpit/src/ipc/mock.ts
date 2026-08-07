/**
 * In-memory MOCK implementation of {@link IpcClient}.
 *
 * It stands up a deterministic ~15-node / 20-edge spawn FOREST across two providers
 * (`claude` + `codex`) so the cockpit builds and runs with no engine attached. The
 * dataset deliberately contains the shapes the renderer must handle:
 *   * a DANGLING PARENT (`pruned-parent`) — an id that appears only on an edge and has
 *     no threads-table row, synthesized into a bare node exactly as the sidecar does;
 *   * a DEPTH-3 CHAIN (`orch -> plan -> layout -> deepfix`);
 *   * CROSS-PROVIDER spawns (claude spawning codex and vice-versa);
 *   * an ISOLATED root (`research`) with no edges;
 *   * DIAMONDS (a child with several parents) that exercise the shortest-path depth.
 *
 * The `MockGraph` helper re-implements `llm_anthology/corpus.py`'s graph semantics (union of
 * nodes, no-incoming-edge roots, sorted children, out-degree fan-out, cycle-safe
 * shortest-path depth) so the mock is a faithful stand-in, not a caricature.
 *
 * All timestamps are literals — nothing here reads the clock, so tests are stable.
 */

import type {
  BuildHandle,
  BuildParams,
  BuildStatus,
  Conversation,
  CorpusDiffDto,
  CorpusStats,
  CreateCorpusResult,
  DiscoveryFinding,
  DiscoveryResult,
  ExportPlan,
  ExportResult,
  FullIpcClient,
  GraphSnapshot,
  HealthInfo,
  OpenCorpusResult,
  RollupTable,
  RootsParams,
  SearchHit,
  SearchParams,
  SearchResult,
  SpawnEdge,
  Subtree,
  ThreadMeta,
  ThreadNode,
  Timeline,
} from "./types";

/** Raw thread row: a superset of the wire node, before graph fields are computed. */
interface RawThread {
  id: string;
  title: string;
  provider: string;
  model?: string;
  tokens?: number;
  created_at_ms: number;
  updated_at_ms?: number;
  git_branch?: string;
  cwd?: string;
  agent_role?: string;
  agent_nickname?: string;
  preview?: string;
  /** Synthetic body size, summed into corpus.stats `bytes`. */
  char_count?: number;
  /** Synthetic turn count, summed into corpus.stats `records`. */
  turn_count?: number;
}

const T0 = 1_700_000_000_000; // fixed epoch base (2023-11-14T22:13:20Z)
const CLAUDE_MODEL = "claude-opus-4-8";
const CODEX_MODEL = "gpt-5-codex";

/** The 15 real threads. `pruned-parent` is intentionally absent (dangling). */
const RAW_THREADS: RawThread[] = [
  {
    id: "orch",
    title: "Orchestrate cockpit build",
    provider: "claude",
    model: CLAUDE_MODEL,
    tokens: 125000,
    created_at_ms: T0,
    updated_at_ms: T0 + 300_000,
    git_branch: "cockpit-p2-ui",
    cwd: "/repo/cockpit",
    agent_role: "orchestrator",
    preview: "Ship the cockpit spawn-tree UI end to end.",
    char_count: 812345,
    turn_count: 48,
  },
  {
    id: "research",
    title: "Standalone research: ELK vs dagre",
    provider: "claude",
    model: CLAUDE_MODEL,
    tokens: 8000,
    created_at_ms: T0 + 30_000,
    preview: "Compare ELK and dagre for 2k sparse forests.",
    char_count: 41200,
    turn_count: 6,
  },
  {
    id: "repro",
    title: "Reproduce the build failure",
    provider: "codex",
    model: CODEX_MODEL,
    tokens: 61000,
    created_at_ms: T0 + 50_000,
    git_branch: "fix/build",
    agent_role: "reproducer",
    agent_nickname: "keen-lynx",
    char_count: 388100,
    turn_count: 27,
  },
  {
    id: "plan",
    title: "Plan the spawn-tree UI",
    provider: "claude",
    model: CLAUDE_MODEL,
    tokens: 42000,
    created_at_ms: T0 + 60_000,
    agent_role: "planner",
    char_count: 210500,
    turn_count: 15,
  },
  {
    id: "ipc",
    title: "Build the IPC mock layer",
    provider: "codex",
    model: CODEX_MODEL,
    tokens: 51000,
    created_at_ms: T0 + 90_000,
    updated_at_ms: T0 + 250_000,
    agent_role: "implementer",
    agent_nickname: "brisk-heron",
    char_count: 305900,
    turn_count: 22,
  },
  {
    id: "bisect",
    title: "Bisect the regression",
    provider: "codex",
    model: CODEX_MODEL,
    tokens: 44000,
    created_at_ms: T0 + 110_000,
    char_count: 260400,
    turn_count: 19,
  },
  {
    id: "layout",
    title: "Design the ELK layered layout",
    provider: "claude",
    model: CLAUDE_MODEL,
    tokens: 38000,
    created_at_ms: T0 + 120_000,
    char_count: 198700,
    turn_count: 14,
  },
  {
    id: "tests",
    title: "Write the vitest suite",
    provider: "claude",
    model: CLAUDE_MODEL,
    tokens: 33000,
    created_at_ms: T0 + 130_000,
    agent_role: "tester",
    char_count: 176300,
    turn_count: 12,
  },
  {
    id: "crosscheck",
    title: "Cross-check with Claude",
    provider: "claude",
    model: CLAUDE_MODEL,
    tokens: 30000,
    created_at_ms: T0 + 140_000,
    agent_role: "reviewer",
    char_count: 151200,
    turn_count: 11,
  },
  {
    id: "canvas",
    title: "Prototype the canvas renderer",
    provider: "codex",
    model: CODEX_MODEL,
    tokens: 47000,
    created_at_ms: T0 + 150_000,
    agent_nickname: "swift-otter",
    char_count: 289400,
    turn_count: 20,
  },
  {
    id: "real",
    title: "Wire the real invoke path",
    provider: "codex",
    model: CODEX_MODEL,
    tokens: 22000,
    created_at_ms: T0 + 160_000,
    char_count: 133800,
    turn_count: 9,
  },
  {
    id: "deepfix",
    title: "Deep hotfix at the leaf (depth 3)",
    provider: "codex",
    model: CODEX_MODEL,
    tokens: 15000,
    created_at_ms: T0 + 180_000,
    agent_role: "fixer",
    char_count: 92600,
    turn_count: 7,
  },
  {
    id: "search",
    title: "Virtualized list + search box",
    provider: "claude",
    model: CLAUDE_MODEL,
    tokens: 28000,
    created_at_ms: T0 + 200_000,
    char_count: 164900,
    turn_count: 13,
  },
  {
    id: "review",
    title: "Adversarial final review",
    provider: "claude",
    model: CLAUDE_MODEL,
    tokens: 19000,
    created_at_ms: T0 + 220_000,
    updated_at_ms: T0 + 400_000,
    agent_role: "adversary",
    agent_nickname: "stern-heron",
    char_count: 118500,
    turn_count: 10,
  },
  {
    id: "orphan",
    title: "Child of a pruned parent",
    provider: "codex",
    model: CODEX_MODEL,
    tokens: 5000,
    created_at_ms: T0 + 240_000,
    preview: "Resumed after its parent thread was pruned from the corpus.",
    char_count: 47700,
    turn_count: 5,
  },
];

/**
 * The 20 spawn edges. `pruned-parent` (edge 16) has no threads-table row -> it is a
 * DANGLING PARENT. Nine edges cross provider boundaries.
 */
const RAW_EDGES: SpawnEdge[] = [
  { parent: "orch", child: "plan", status: "completed" },
  { parent: "plan", child: "layout", status: "completed" },
  { parent: "layout", child: "deepfix", status: "completed" }, // cross claude->codex
  { parent: "orch", child: "ipc", status: "completed" }, // cross claude->codex
  { parent: "ipc", child: "canvas", status: "completed" },
  { parent: "ipc", child: "real", status: "running" },
  { parent: "orch", child: "tests" },
  { parent: "plan", child: "tests", status: "completed" },
  { parent: "canvas", child: "search", status: "completed" }, // cross codex->claude
  { parent: "real", child: "search" }, // cross codex->claude
  { parent: "repro", child: "bisect", status: "completed" },
  { parent: "repro", child: "crosscheck", status: "completed" }, // cross codex->claude
  { parent: "crosscheck", child: "review", status: "completed" },
  { parent: "bisect", child: "review", status: "failed" }, // cross codex->claude
  { parent: "real", child: "bisect" },
  { parent: "pruned-parent", child: "orphan", status: "completed" }, // dangling parent
  { parent: "orphan", child: "review" }, // cross codex->claude
  { parent: "ipc", child: "tests", status: "completed" }, // cross codex->claude
  { parent: "plan", child: "canvas" }, // cross claude->codex
  { parent: "layout", child: "search", status: "completed" },
];

// ---------------------------------------------------------------------------
// auto-discovery fixture
// ---------------------------------------------------------------------------

/** `T0` as UNIX SECONDS — discovery reports seconds, not the ms everything else uses. */
const T0_SEC = T0 / 1000;

/**
 * WINDOWS-shaped paths, deliberately, while the rest of this mock uses POSIX ones.
 *
 * Discovery is the one surface whose payload is a scan of the LOCAL filesystem, and the
 * only platform this app ships on is Windows — so a POSIX fixture here would leave the
 * separator handling in `ui/discoveryPanel`'s path split unexercised in every preview and
 * screenshot, which is exactly where a basename bug would otherwise hide.
 */
const HOME = "C:\\Users\\preview";

/**
 * The 25 near-identical ChatGPT exports.
 *
 * A REAL census on the author's machine returned 25 of them (against 7 Claude, 2 Codex and
 * 1 Gemini export), which is also the engine's per-group cap — `DEFAULT_MAX_PER_GROUP`
 * (`llm_anthology/discover.py:106`). So this group is simultaneously the worst case for
 * the UI's own collapsing AND a group the ENGINE truncated, and it exists here so a
 * preview shows both at once rather than either in isolation.
 */
function chatgptExports(): DiscoveryFinding[] {
  return Array.from({ length: 25 }, (_, i) => ({
    provider: "chatgpt",
    kind: "export_file",
    path: `${HOME}\\Downloads\\chatgpt-export-${String(i + 1).padStart(2, "0")}\\conversations.json`,
    count: 1,
    // Descending age, one day apart, so the newest-first ordering has something to sort.
    newest_mtime: T0_SEC - i * 86400,
    confidence: "high",
    detail: { size_bytes: 4_100_000 + i * 9_137 },
  }));
}

/**
 * A whole scan, shaped like the measured one: a built index, both STORE shapes, and the
 * long export tail. The two store shapes differ in the way the ingest derivation depends
 * on (`llm_anthology/discover.py:550-551`) and that difference is the whole reason both
 * are here:
 *
 *   * codex reports its BASE (`report="base"`), so `path` is the Codex home and
 *     `detail.items_root` is the distinct sessions tree -> both `corpus.build` parameters
 *     are derivable;
 *   * claude-code reports its SUBDIR (`report="subdir"`), so `path` and
 *     `detail.items_root` are the SAME directory and no Codex home is named -> the ingest
 *     parameters are NOT derivable, which the panel has to say rather than guess.
 */
const DISCOVERY: DiscoveryResult = {
  findings: [
    {
      provider: "anthology",
      kind: "built_index",
      path: `${HOME}\\Documents\\anthology.db`,
      count: 1_284,
      newest_mtime: T0_SEC - 3_600,
      confidence: "high",
      detail: { conversations: 1_284, tables: ["conversations", "conversations_fts"] },
    },
    {
      provider: "codex",
      kind: "session_store",
      path: `${HOME}\\.codex`,
      count: 2_043,
      newest_mtime: T0_SEC - 900,
      confidence: "high",
      detail: {
        // The measured live store: thousands of COMPRESSED rollouts and zero plain ones
        // (`llm_anthology/adapters/codex_rollout.py:357-359`). `ingestable` counts only
        // the plain form, so it reads 0 here even though `ingest_sessions` globs both
        // (`codex_rollout.py:416-419`) — see `ui/discoveryPanel`'s note on why the panel
        // does not gate the ingest on that field.
        rollouts_jsonl: 0,
        rollouts_zst: 2_043,
        ingestable: 0,
        state_db: `${HOME}\\.codex\\state_5.sqlite`,
        items_root: `${HOME}\\.codex\\sessions`,
      },
    },
    {
      provider: "claude-code",
      kind: "session_store",
      path: `${HOME}\\.claude\\projects`,
      count: 162,
      newest_mtime: T0_SEC - 120,
      confidence: "high",
      detail: {
        "*.jsonl": 162,
        ingestable: 162,
        project_dirs: 9,
        items_root: `${HOME}\\.claude\\projects`,
      },
    },
    {
      provider: "claude",
      kind: "export_file",
      path: `${HOME}\\Downloads\\claude-data\\conversations.json`,
      count: 1,
      newest_mtime: T0_SEC - 172_800,
      confidence: "high",
      detail: { size_bytes: 18_400_000, ambiguous_with: ["chatgpt"] },
    },
    {
      provider: "gemini",
      kind: "export_file",
      path: `${HOME}\\Downloads\\Takeout\\Gemini Apps\\_converted\\transcript.json`,
      count: 1,
      // Nothing datable was seen. The engine reports 0.0, NOT null (`discover.py:552`),
      // so a renderer that treated this as a timestamp would print 1970.
      newest_mtime: 0,
      confidence: "low",
      detail: { size_bytes: 2_100 },
    },
    ...chatgptExports(),
  ],
  stats: {
    elapsed_seconds: 1.78,
    roots_scanned: 5,
    dirs_visited: 2_188,
    files_examined: 39_512,
    budget_exhausted: false,
    // The ENGINE capped this group. Distinct from any collapsing the UI does, and the
    // only signal that older items exist on disk that the scan never listed.
    truncated_groups: ["chatgpt/export_file"],
    errors: [
      `${HOME}\\Downloads\\locked: [WinError 5] Access is denied`,
      `${HOME}\\Desktop\\gone: [WinError 3] The system cannot find the path specified`,
    ],
  },
};

/**
 * How many `corpus.build_status` polls the mock reports as `running` before it reports
 * `done`. Fixed rather than clock-based so a preview always shows a real progress
 * transition and a test always terminates — the mock reads no clock anywhere.
 */
const MOCK_BUILD_POLLS = 3;

/**
 * A faithful port of `llm_anthology/corpus.py`'s graph helpers. Answers purely from the edge
 * list; any id an edge names is a node even if it has no thread row.
 */
export class MockGraph {
  private readonly byId = new Map<string, RawThread>();
  private readonly childIds = new Set<string>();

  constructor(
    threads: RawThread[],
    private readonly edges: SpawnEdge[],
  ) {
    for (const t of threads) this.byId.set(t.id, t);
    for (const e of edges) this.childIds.add(e.child);
  }

  /** Every id in the graph: the thread rows UNION every id an edge names. */
  nodeIds(): string[] {
    const ids = new Set<string>(this.byId.keys());
    for (const e of this.edges) {
      ids.add(e.parent);
      ids.add(e.child);
    }
    return [...ids];
  }

  private parentsOf(tid: string): string[] {
    return this.edges.filter((e) => e.child === tid).map((e) => e.parent);
  }

  /** Nodes with no incoming spawn, sorted for deterministic rendering. */
  roots(): string[] {
    return this.nodeIds()
      .filter((n) => !this.childIds.has(n))
      .sort();
  }

  /** The distinct children `tid` spawned, sorted. */
  childrenOf(tid: string): string[] {
    const kids = new Set<string>();
    for (const e of this.edges) if (e.parent === tid) kids.add(e.child);
    return [...kids].sort();
  }

  /** Out-degree: how many distinct threads `tid` spawned. */
  fanOut(tid: string): number {
    return this.childrenOf(tid).length;
  }

  /** Distance to the nearest root (a root is 0), shortest path, cycle-safe. */
  depth(tid: string): number {
    return this.depthInner(tid, new Set<string>());
  }

  private depthInner(tid: string, seen: Set<string>): number {
    const parents = this.parentsOf(tid);
    if (parents.length === 0) return 0;
    if (seen.has(tid)) return 0; // back-edge: stop, contribute no further depth
    const next = new Set(seen);
    next.add(tid);
    return 1 + Math.min(...parents.map((p) => this.depthInner(p, next)));
  }

  /** The lean ThreadNode DTO for `tid`; synthesizes a bare node for a dangling id. */
  node(tid: string): ThreadNode {
    const raw = this.byId.get(tid);
    const dto: ThreadNode = {
      id: tid,
      title: raw?.title ?? "",
      provider: raw?.provider ?? "",
      created_at_ms: raw?.created_at_ms ?? null,
      child_count: this.fanOut(tid),
      depth: this.depth(tid),
    };
    if (raw?.model) dto.model = raw.model;
    if (raw?.tokens) dto.tokens = raw.tokens;
    if (raw?.updated_at_ms !== undefined) dto.updated_at_ms = raw.updated_at_ms;
    if (raw?.git_branch) dto.git_branch = raw.git_branch;
    if (raw?.cwd) dto.cwd = raw.cwd;
    if (raw?.agent_role) dto.agent_role = raw.agent_role;
    if (raw?.agent_nickname) dto.agent_nickname = raw.agent_nickname;
    if (raw?.preview) dto.preview = raw.preview;
    return dto;
  }

  /** The full ThreadMeta projection for `thread.get`. */
  meta(tid: string): ThreadMeta | null {
    const raw = this.byId.get(tid);
    if (!raw) return null; // thread.get only answers for real rows
    return {
      id: raw.id,
      title: raw.title,
      provider: raw.provider,
      tokens: raw.tokens ?? null,
      created_at_ms: raw.created_at_ms,
      updated_at_ms: raw.updated_at_ms ?? null,
      git_branch: raw.git_branch ?? null,
      cwd: raw.cwd ?? null,
      agent_role: raw.agent_role ?? null,
      agent_nickname: raw.agent_nickname ?? null,
      preview: raw.preview ?? null,
      child_count: this.fanOut(tid),
      depth: this.depth(tid),
      has_rollout: false,
    };
  }

  /** BFS collect the subtree ids under `tid`, honouring an optional depth cap. */
  collectSubtree(tid: string, depthCap?: number): string[] {
    const order: string[] = [];
    const seen = new Set<string>();
    const frontier: Array<[string, number]> = [[tid, 0]];
    while (frontier.length > 0) {
      const [node, level] = frontier.shift() as [string, number];
      if (seen.has(node)) continue;
      seen.add(node);
      order.push(node);
      if (depthCap === undefined || level < depthCap) {
        for (const c of this.childrenOf(node)) frontier.push([c, level + 1]);
      }
    }
    return order;
  }

  /** BFS collect spawn-ancestors of `tid`, nearest first. */
  collectAncestors(tid: string): string[] {
    const order: string[] = [];
    const seen = new Set<string>([tid]);
    const frontier: string[] = [tid];
    while (frontier.length > 0) {
      const node = frontier.shift() as string;
      for (const p of this.parentsOf(node)) {
        if (!seen.has(p)) {
          seen.add(p);
          order.push(p);
          frontier.push(p);
        }
      }
    }
    return order;
  }

  edgesWithin(ids: Set<string>): SpawnEdge[] {
    return this.edges.filter((e) => ids.has(e.parent) && ids.has(e.child));
  }

  rawThread(tid: string): RawThread | undefined {
    return this.byId.get(tid);
  }

  allEdges(): SpawnEdge[] {
    return this.edges;
  }

  /** The node's own token count, or 0 for a dangling id with no thread row. */
  private tokensOf(tid: string): number {
    return this.byId.get(tid)?.tokens ?? 0;
  }

  /**
   * BFS descendant walk mirroring `llm_anthology/rollup.py`'s `_walk`: the count of DISTINCT
   * subtree nodes (root included), the sum of their self_tokens, and the greatest
   * SHORTEST-path depth reached. The visited-set makes it cycle-safe and dedupes a
   * diamond's shared node; FIFO order dequeues each node at its shortest depth.
   */
  private walkSubtree(root: string): { count: number; tokens: number; maxDepth: number } {
    const seen = new Set<string>();
    let tokens = 0;
    let maxDepth = 0;
    const frontier: Array<[string, number]> = [[root, 0]];
    while (frontier.length > 0) {
      const [tid, level] = frontier.shift() as [string, number];
      if (seen.has(tid)) continue; // back-edge / already-reached node
      seen.add(tid);
      tokens += this.tokensOf(tid);
      if (level > maxDepth) maxDepth = level;
      for (const c of this.childrenOf(tid)) frontier.push([c, level + 1]);
    }
    return { count: seen.size, tokens, maxDepth };
  }

  /**
   * Per-node {@link RollupMetrics} over EVERY graph node, keyed in sorted-id order — a
   * faithful port of `llm_anthology/rollup.py`'s `rollup` (self vs whole-subtree token/count,
   * diamond-deduped, cycle-safe). A dangling id contributes 0 self_tokens.
   */
  rollup(): RollupTable {
    const table: RollupTable = {};
    for (const tid of this.nodeIds().sort()) {
      const { count, tokens, maxDepth } = this.walkSubtree(tid);
      table[tid] = {
        self_tokens: this.tokensOf(tid),
        subtree_tokens: tokens,
        self_count: 1,
        subtree_count: count,
        max_depth: maxDepth,
        child_count: this.fanOut(tid),
      };
    }
    return table;
  }

  /**
   * The node-creation event axis: the sorted DISTINCT dated timestamps, their range, and
   * the count of undated (dangling, row-less) nodes that float outside the axis.
   */
  timeline(): Timeline {
    const dated = new Set<number>();
    let undated = 0;
    for (const id of this.nodeIds()) {
      const ms = this.byId.get(id)?.created_at_ms;
      if (ms === undefined) undated += 1; // a dangling edge endpoint has no row/date
      else dated.add(ms);
    }
    const events = [...dated].sort((a, b) => a - b);
    return {
      events,
      min_ms: events.length > 0 ? events[0] : null,
      max_ms: events.length > 0 ? events[events.length - 1] : null,
      undated_count: undated,
    };
  }

  /** Whether a node exists as-of `t`: dated on/before `t`, or undated (always present). */
  private presentAsOf(tid: string, t: number): boolean {
    const ms = this.byId.get(tid)?.created_at_ms;
    return ms === undefined || ms <= t;
  }

  /**
   * The graph AS-OF `t`: the node ids (sorted) present as-of `t`, and the edges (sorted
   * by parent,child) whose CHILD is present as-of `t` (an edge's time is its child's
   * spawn time). Edges keep their status; an undated node/child is always present.
   */
  snapshotAt(t: number): { nodeIds: string[]; edges: SpawnEdge[] } {
    const nodeIds = this.nodeIds()
      .filter((id) => this.presentAsOf(id, t))
      .sort();
    const edges = this.edges
      .filter((e) => this.presentAsOf(e.child, t))
      .slice()
      .sort(compareEdges);
    return { nodeIds, edges };
  }

  /**
   * The structural {@link CorpusDiffDto} between the as-of-`a` and as-of-`b` snapshots.
   * Edge deltas are status-free (edge identity is the (parent, child) pair); every list
   * is sorted. `changed_nodes` is always empty — both snapshots view this one immutable
   * corpus, so a node present in both is byte-identical — carried for shape parity.
   */
  diffAsOf(a: number, b: number): CorpusDiffDto {
    const snapA = this.snapshotAt(a);
    const snapB = this.snapshotAt(b);
    const nodesA = new Set(snapA.nodeIds);
    const nodesB = new Set(snapB.nodeIds);
    const key = (e: SpawnEdge): string => JSON.stringify([e.parent, e.child]);
    const edgesA = new Map(snapA.edges.map((e) => [key(e), e]));
    const edgesB = new Map(snapB.edges.map((e) => [key(e), e]));
    const bare = (e: SpawnEdge): SpawnEdge => ({ parent: e.parent, child: e.child });
    return {
      added_nodes: snapB.nodeIds.filter((n) => !nodesA.has(n)),
      removed_nodes: snapA.nodeIds.filter((n) => !nodesB.has(n)),
      added_edges: [...edgesB.values()]
        .filter((e) => !edgesA.has(key(e)))
        .map(bare)
        .sort(compareEdges),
      removed_edges: [...edgesA.values()]
        .filter((e) => !edgesB.has(key(e)))
        .map(bare)
        .sort(compareEdges),
      changed_nodes: {},
    };
  }
}

function orderNodes(nodes: ThreadNode[], order: RootsParams["order"]): ThreadNode[] {
  const copy = [...nodes];
  if (order === undefined || order === "created") {
    return copy.sort((a, b) => {
      const an = a.created_at_ms === null ? 1 : 0;
      const bn = b.created_at_ms === null ? 1 : 0;
      if (an !== bn) return an - bn;
      const av = a.created_at_ms ?? 0;
      const bv = b.created_at_ms ?? 0;
      if (av !== bv) return av - bv;
      return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
    });
  }
  if (order === "recent") {
    return copy.sort(
      (a, b) =>
        (b.updated_at_ms ?? b.created_at_ms ?? 0) -
        (a.updated_at_ms ?? a.created_at_ms ?? 0),
    );
  }
  // "title"
  return copy.sort((a, b) => a.title.toLowerCase().localeCompare(b.title.toLowerCase()));
}

/** Stable edge order by (parent, child) — the order graph.at / graph.diff emit. */
function compareEdges(a: SpawnEdge, b: SpawnEdge): number {
  if (a.parent !== b.parent) return a.parent < b.parent ? -1 : 1;
  if (a.child !== b.child) return a.child < b.child ? -1 : 1;
  return 0;
}

/**
 * The mock's surface: the whole {@link FullIpcClient} data contract PLUS a live
 * `openedIndex` readback of the last path handed to `openCorpus`. The readback exists
 * because a reference implementation that silently discarded its one lifecycle argument
 * would be indistinguishable from one that never received it — and `openCorpus` returns
 * the same `{ok, index}` either way. It is additive, so a `MockIpcClient` still drops
 * straight into any `IpcClient`/`FullIpcClient` slot.
 */
export interface MockIpcClient extends FullIpcClient {
  /** Path of the last successful `openCorpus`, or null before any open. */
  readonly openedIndex: string | null;
}

/**
 * Build a mock IPC client over the given data. The default export uses the built-in
 * synthetic forest; the factory is exported so tests can drive tiny graphs. The return
 * type is {@link MockIpcClient} — the mock implements the full Phase-3 surface, so the
 * six time-travel/export methods are callable without an optional-chaining dance.
 */
export function createMockIpc(
  threads: RawThread[] = RAW_THREADS,
  edges: SpawnEdge[] = RAW_EDGES,
): MockIpcClient {
  const graph = new MockGraph(threads, edges);
  /** The last path `openCorpus` was given. Read back via the `openedIndex` getter. */
  let lastOpenedIndex: string | null = null;
  /** The in-flight/last mock ingest, or null before any `corpusBuild`. */
  let buildJob: { id: string; polls: number } | null = null;

  function runSearch(params: SearchParams): SearchResult {
    const q = params.q.trim().toLowerCase();
    const limit = params.limit ?? 50;
    const offset = params.offset ?? 0;
    const matches = threads.filter((t) => {
      if (params.provider !== undefined && t.provider !== params.provider) return false;
      if (q === "") return true;
      return (
        t.title.toLowerCase().includes(q) ||
        (t.preview ?? "").toLowerCase().includes(q) ||
        t.id.toLowerCase().includes(q)
      );
    });
    // Newest first, mirroring an FTS `ORDER BY rank` proxy.
    matches.sort((a, b) => b.created_at_ms - a.created_at_ms);
    const page = matches.slice(offset, offset + limit);
    const hits: SearchHit[] = page.map((t, i) => {
      const hit: SearchHit = {
        conversation_id: t.id,
        thread_id: t.id,
        snippet: t.title,
        score: 1.0 / (offset + i + 1),
        provider: t.provider,
        ts_ms: t.created_at_ms,
      };
      return hit;
    });
    return { hits, total: matches.length, took_ms: 1 };
  }

  return {
    get openedIndex(): string | null {
      return lastOpenedIndex;
    },

    /**
     * Record the requested index and report success.
     *
     * DELIBERATELY NOT A GATE — the fixture forest is served from construction, before
     * and after any `openCorpus`. The real engine does refuse every read until a corpus
     * is attached, but copying that here would break the mock's whole reason to exist:
     * `./index.ts` selects this adapter for every NON-Tauri environment (vite dev,
     * `vite preview`, the screenshot harness, a design review), and the native file
     * picker only exists inside the Tauri webview. A gated mock would leave those
     * environments unable to attach anything and unable to leave the empty state —
     * reintroducing, in the preview, the exact dead boot this method was added to cure.
     */
    async openCorpus(indexPath: string): Promise<OpenCorpusResult> {
      lastOpenedIndex = indexPath;
      return { ok: true, index: indexPath };
    },

    /**
     * The fixture scan. Returns a fresh deep-ish copy so a caller that sorts or mutates
     * the array in place cannot corrupt the fixture for the next scan — the panel does
     * sort, and a shared mutable fixture is how a "why did the order change on rescan?"
     * bug gets born.
     */
    async discoverSources(): Promise<DiscoveryResult> {
      return {
        findings: DISCOVERY.findings.map((f) => ({ ...f, detail: { ...f.detail } })),
        stats: { ...DISCOVERY.stats },
      };
    },

    /**
     * Record the requested path and report it created. NO filesystem write, and — unlike
     * the engine — no clobber check, for the same reason `openCorpus` is not a gate here:
     * this adapter serves every non-Tauri environment, where there is no real index to
     * collide with.
     */
    async createCorpus(indexPath: string): Promise<CreateCorpusResult> {
      return { index_path: indexPath, created: true };
    },

    async corpusBuild(params: BuildParams): Promise<BuildHandle> {
      buildJob = { id: "mock-build-1", polls: 0 };
      return {
        job_id: buildJob.id,
        state: "running",
        // Echo whichever source was named. Every root is opt-in now, so a Grok-only build
        // carries no `sessions_root` at all — defaulting to "" mirrors the engine, which
        // reports `_clean(sessions_root)` on an unnamed root rather than omitting the key.
        sessions_root: params.sessions_root ?? params.grok_root ?? "",
        started_ms: T0,
      };
    },

    /**
     * Poll the mock ingest. Reports `idle` before any build (exactly as the engine does
     * rather than erroring — `llm_anthology/sidecar.py:824`), then `running` with a
     * climbing `indexed_conversations` for {@link MOCK_BUILD_POLLS} polls, then `done`.
     * Reaching a terminal state is the point: a mock that stayed `running` forever would
     * make a poll loop that never stops look correct.
     */
    async corpusBuildStatus(jobId?: string): Promise<BuildStatus> {
      if (buildJob === null) {
        return { state: "idle", indexed_conversations: 0, errors: [] };
      }
      if (jobId !== undefined && jobId !== buildJob.id) {
        throw new Error(`unknown job_id '${jobId}'; the current job is '${buildJob.id}'`);
      }
      buildJob.polls += 1;
      const running = buildJob.polls < MOCK_BUILD_POLLS;
      return {
        job_id: buildJob.id,
        state: running ? "running" : "done",
        sessions_root: "/mock/sessions",
        started_ms: T0,
        indexed_conversations: Math.min(buildJob.polls, MOCK_BUILD_POLLS) * 400,
        errors: [],
        ...(running ? {} : { finished_ms: T0 + 30_000 }),
      };
    },

    async healthPing(): Promise<HealthInfo> {
      return {
        ok: true,
        engine_version: "mock-0.1.0",
        ir_version: "1",
        corpus_ready: true,
      };
    },

    async corpusStats(): Promise<CorpusStats> {
      const providers: Record<string, number> = {};
      let records = 0;
      let bytes = 0;
      for (const t of threads) {
        providers[t.provider] = (providers[t.provider] ?? 0) + 1;
        records += t.turn_count ?? 0;
        bytes += t.char_count ?? 0;
      }
      return {
        conversations: threads.length,
        records,
        threads: threads.length,
        edges: edges.length,
        bytes,
        providers,
      };
    },

    async graphRoots(params: RootsParams = {}): Promise<ThreadNode[]> {
      const limit = params.limit ?? 100;
      const offset = params.offset ?? 0;
      const nodes = orderNodes(
        graph.roots().map((id) => graph.node(id)),
        params.order,
      );
      return nodes.slice(offset, offset + limit);
    },

    async graphChildren(threadId: string): Promise<ThreadNode[]> {
      return graph.childrenOf(threadId).map((id) => graph.node(id));
    },

    async graphSubtree(threadId: string, depth?: number): Promise<Subtree> {
      const ids = graph.collectSubtree(threadId, depth);
      const idset = new Set(ids);
      return {
        nodes: ids.map((id) => graph.node(id)),
        edges: graph.edgesWithin(idset),
      };
    },

    async graphAncestors(threadId: string): Promise<ThreadNode[]> {
      return graph.collectAncestors(threadId).map((id) => graph.node(id));
    },

    async searchQuery(params: SearchParams): Promise<SearchResult> {
      return runSearch(params);
    },

    async threadGet(threadId: string): Promise<ThreadMeta> {
      const meta = graph.meta(threadId);
      if (meta === null) throw new Error(`thread not found: ${threadId}`);
      return meta;
    },

    async conversationGet(id: string): Promise<Conversation> {
      const raw = graph.rawThread(id);
      if (raw === undefined) {
        return { id, turns: [], available: false, reason: "conversation not found" };
      }
      const iso = new Date(raw.created_at_ms).toISOString();
      return {
        id: raw.id,
        title: raw.title,
        provider: raw.provider,
        created_at: iso,
        updated_at: raw.updated_at_ms ? new Date(raw.updated_at_ms).toISOString() : iso,
        account: null,
        available: true,
        ir_version: "1",
        parse_errors: 0,
        turns: [
          {
            role: "user",
            blocks: [{ type: "text", text: raw.preview ?? raw.title }],
          },
          {
            role: "assistant",
            blocks: [
              {
                type: "text",
                text: `(mock body for ${raw.id} — the real transcript arrives when the sidecar is wired.)`,
              },
            ],
          },
        ],
      };
    },

    async graphRollup(): Promise<RollupTable> {
      return graph.rollup();
    },

    async graphTimeline(): Promise<Timeline> {
      return graph.timeline();
    },

    async graphAt(asOfMs: number): Promise<GraphSnapshot> {
      const snap = graph.snapshotAt(asOfMs);
      return { nodes: snap.nodeIds.map((id) => graph.node(id)), edges: snap.edges };
    },

    async graphDiff(asOfA?: number, asOfB?: number): Promise<CorpusDiffDto> {
      // An omitted operand is "now" (the full corpus), mirroring the sidecar's
      // graph.diff, so graphDiff() is the empty self-diff.
      return graph.diffAsOf(
        asOfA ?? Number.POSITIVE_INFINITY,
        asOfB ?? Number.POSITIVE_INFINITY,
      );
    },

    async exportPlan(): Promise<ExportPlan> {
      // A dry run: tally the graph the sidecar would serialize; est_bytes is the summed
      // synthetic content size (== corpus.stats bytes). No filesystem access.
      let estBytes = 0;
      for (const t of threads) estBytes += t.char_count ?? 0;
      return {
        node_count: graph.nodeIds().length,
        edge_count: edges.length,
        conversation_count: threads.length,
        est_bytes: estBytes,
      };
    },

    async exportRun(destPath: string): Promise<ExportResult> {
      // The mock forest is self-consistent, so both fidelity gates trivially pass, and
      // the mock performs NO filesystem write — it returns the verdict a faithful export
      // of a self-consistent corpus would yield, echoing a valid dest as written_path.
      const graph_gate = true;
      const transcript_gate = true;
      const ok = destPath.trim() !== "" && graph_gate && transcript_gate;
      const result: ExportResult = { ok, graph_gate, transcript_gate };
      if (ok) result.written_path = destPath;
      return result;
    },
  };
}

/** The default mock client over the built-in synthetic forest. */
export const mockIpc: MockIpcClient = createMockIpc();

/** Exposed so tests can assert against the same data the default client serves. */
export const MOCK_THREADS = RAW_THREADS;
export const MOCK_EDGES = RAW_EDGES;
