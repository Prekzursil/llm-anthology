/**
 * IPC wire contract (engine <-> UI).
 *
 * These interfaces mirror, field-for-field, the DTOs the committed Python sidecar
 * (`llm_anthology/sidecar.py`) emits over stdio NDJSON JSON-RPC 2.0. Optional fields marked
 * `?` are the ones the sidecar OMITS when empty/falsy (see `_thread_node` /
 * `_search_query`); fields typed `| null` are always present but may be null
 * (see `_thread_meta`). Keeping the shapes exact is what lets the integrate stage
 * swap the mock for the real sidecar with a one-line flag flip (see `./index.ts`).
 */

/** One spawn-graph node. `id` maps 1:1 to a record id (used as the canvas node id). */
export interface ThreadNode {
  id: string;
  title: string;
  provider: string;
  /** Model slug; omitted by the sidecar's lean node projection. */
  model?: string;
  /** Tokens used; omitted when 0. */
  tokens?: number;
  /** Epoch ms; null for a synthesized dangling node with no threads-table row. */
  created_at_ms: number | null;
  updated_at_ms?: number;
  git_branch?: string;
  cwd?: string;
  agent_role?: string;
  agent_nickname?: string;
  preview?: string;
  /** Out-degree: distinct threads this node spawned. */
  child_count: number;
  /** Distance to nearest root (a root is 0), shortest parent path. */
  depth: number;
}

/** One directed spawn edge: `parent` spawned `child`. */
export interface SpawnEdge {
  parent: string;
  child: string;
  /** Spawn/child outcome (e.g. completed / failed); omitted when empty. */
  status?: string;
}

/** One full-text search hit. */
export interface SearchHit {
  conversation_id: string;
  thread_id?: string;
  snippet: string;
  score: number;
  provider: string;
  ts_ms?: number;
}

/** `health.ping` result. Works even with no corpus attached. */
export interface HealthInfo {
  ok: boolean;
  engine_version: string;
  ir_version: string;
  corpus_ready: boolean;
}

/** `corpus.stats` result. `providers` is a provider -> conversation-count map. */
export interface CorpusStats {
  conversations: number;
  records: number;
  threads: number;
  edges: number;
  bytes: number;
  providers: Record<string, number>;
}

/** `graph.subtree` result. */
export interface Subtree {
  nodes: ThreadNode[];
  edges: SpawnEdge[];
}

/** `search.query` result. */
export interface SearchResult {
  hits: SearchHit[];
  total: number;
  took_ms: number;
}

/** `thread.get` result: the full node projection (every field present, may be null). */
export interface ThreadMeta {
  id: string;
  title: string;
  provider: string;
  tokens: number | null;
  created_at_ms: number | null;
  updated_at_ms: number | null;
  git_branch: string | null;
  cwd: string | null;
  agent_role: string | null;
  agent_nickname: string | null;
  preview: string | null;
  child_count: number;
  depth: number;
  has_rollout: boolean;
}

export interface ConversationBlock {
  type: string;
  text: string;
  data?: unknown;
  citations?: unknown;
}

export interface ConversationTurn {
  role: string;
  uuid?: string;
  timestamp?: string | null;
  blocks: ConversationBlock[];
  branch?: unknown;
}

/** A fully-parsed conversation body. */
export interface ConversationAvailable {
  id: string;
  title: string;
  provider: string;
  created_at?: string | null;
  updated_at?: string | null;
  account?: string | null;
  ir_version?: string;
  available: true;
  turns: ConversationTurn[];
  meta?: unknown;
  parse_errors?: number;
}

/** An honest partial for a conversation whose body is not retrievable. */
export interface ConversationStub {
  id: string;
  title?: string;
  provider?: string;
  created_at?: string | null;
  updated_at?: string | null;
  account?: string | null;
  turns: [];
  available: false;
  reason: string;
}

export type Conversation = ConversationAvailable | ConversationStub;

/** Ordering for `graph.roots`. */
export type RootOrder = "created" | "recent" | "title";

export interface RootsParams {
  limit?: number;
  offset?: number;
  order?: RootOrder;
}

export interface SearchParams {
  q: string;
  limit?: number;
  offset?: number;
  provider?: string;
}

/**
 * `graph.rollup` value: per-node subtree aggregates, mirroring `llm_anthology/rollup.py`'s
 * `RollupMetrics` (every value a non-negative int; `self_count` is always 1). The wire
 * form is the flat dataclass `asdict`, so the field names are snake_case.
 */
export interface RollupMetrics {
  /** Tokens this node alone used (0 for a dangling id with no thread row). */
  self_tokens: number;
  /** Tokens this node PLUS every distinct descendant used (diamond-deduped). */
  subtree_tokens: number;
  /** Always 1 — present so `subtree_count == sum of descendants' self_count` holds. */
  self_count: number;
  /** Count of this node PLUS all distinct descendants (a leaf is 1). */
  subtree_count: number;
  /** Greatest shortest-path spawn distance to any subtree node (a leaf is 0). */
  max_depth: number;
  /** Direct out-degree (== the node's fan-out). */
  child_count: number;
}

/** `graph.rollup` result: `{thread_id: RollupMetrics}` over EVERY graph node. */
export type RollupTable = Record<string, RollupMetrics>;

/**
 * `graph.timeline` result: the spawn tree's creation-event axis for a time scrubber.
 * `events` is the sorted, DISTINCT set of node creation timestamps (epoch ms);
 * `min_ms`/`max_ms` bound that range (both null when no node is dated); `undated_count`
 * is how many graph nodes carry no timestamp (a dangling edge endpoint) and so float
 * outside the axis.
 */
export interface Timeline {
  events: number[];
  min_ms: number | null;
  max_ms: number | null;
  undated_count: number;
}

/**
 * `graph.at` result: the spawn graph as it stood AS-OF a timestamp. Same shape as
 * {@link Subtree}, but the members are the time-travel snapshot — nodes whose
 * `created_at_ms <= T` (an undated dangling node is ALWAYS present) and edges whose
 * CHILD is present as-of T (an edge's time is its child's spawn time). Both are sorted
 * by stable id.
 */
export interface GraphSnapshot {
  nodes: ThreadNode[];
  edges: SpawnEdge[];
}

/** One side of a changed-node field delta: `[old, new]`, post-sanitize. */
export type ChangedValue = string | number | null;

/** A changed node's per-field `{field: [old, new]}` map (declaration order). */
export type ChangedFields = Record<string, [ChangedValue, ChangedValue]>;

/**
 * `graph.diff` result: the structural delta between two spawn-graph snapshots, mirroring
 * the sidecar's `_project_diff`. Node deltas are sorted id lists; edge deltas are
 * status-FREE {@link SpawnEdge}s (edge identity is the (parent, child) pair), sorted by
 * (parent, child); `changed_nodes` is `{id: {field: [old, new]}}`. In the time-travel
 * variant (`graph.diff{as_of_a?, as_of_b?}`) `changed_nodes` is always empty — the two
 * snapshots view one immutable corpus, so a node in both is byte-identical — but the
 * field is carried for parity with the sidecar's two-index diff.
 */
export interface CorpusDiffDto {
  added_nodes: string[];
  removed_nodes: string[];
  added_edges: SpawnEdge[];
  removed_edges: SpawnEdge[];
  changed_nodes: Record<string, ChangedFields>;
}

/** `export.plan` result: a dry-run tally of what a full export would write (no write). */
export interface ExportPlan {
  /** Distinct graph nodes (threads UNION edge endpoints, incl. any dangling parent). */
  node_count: number;
  /** Directed spawn edges. */
  edge_count: number;
  /** Conversations (thread rows) whose transcripts would be exported. */
  conversation_count: number;
  /** Estimated exported size in bytes (a content-byte estimate). */
  est_bytes: number;
}

/**
 * `export.run` result: the verdict of an actual export. `ok` is the overall success (a
 * write happened AND both fidelity gates passed); `written_path` is present only when a
 * file was written; `graph_gate` / `transcript_gate` are the independent structural /
 * token-fidelity gate verdicts.
 */
export interface ExportResult {
  ok: boolean;
  written_path?: string;
  graph_gate: boolean;
  transcript_gate: boolean;
}

/**
 * The data-access surface the cockpit UI codes against. Both the mock and the real
 * (Tauri `invoke`) adapter implement this; the app never imports one directly, only
 * the `ipc` singleton from `./index.ts`.
 */
export interface IpcClient {
  healthPing(): Promise<HealthInfo>;
  corpusStats(): Promise<CorpusStats>;
  graphRoots(params?: RootsParams): Promise<ThreadNode[]>;
  graphChildren(threadId: string): Promise<ThreadNode[]>;
  graphSubtree(threadId: string, depth?: number): Promise<Subtree>;
  graphAncestors(threadId: string): Promise<ThreadNode[]>;
  searchQuery(params: SearchParams): Promise<SearchResult>;
  threadGet(threadId: string): Promise<ThreadMeta>;
  conversationGet(id: string): Promise<Conversation>;

  // -- Phase-3 time-travel + export surface --------------------------------------
  // OPTIONAL on the base contract so the not-yet-wired real (Tauri) adapter still
  // satisfies IpcClient during this contract-freeze step (adding them as REQUIRED
  // would break `real.ts`, a concrete object literal). The mock implements all six
  // (see FullIpcClient / createMockIpc); a later integrate step wires the Rust
  // commands and can then tighten them to required.
  /** `graph.rollup`: per-node subtree token/count/depth aggregates. */
  graphRollup?(): Promise<RollupTable>;
  /** `graph.timeline`: the node-creation event axis + undated count. */
  graphTimeline?(): Promise<Timeline>;
  /** `graph.at`: the spawn graph as-of `asOfMs` (undated nodes always present). */
  graphAt?(asOfMs: number): Promise<GraphSnapshot>;
  /** `graph.diff`: structural delta between two as-of snapshots (an omitted side = now). */
  graphDiff?(asOfA?: number, asOfB?: number): Promise<CorpusDiffDto>;
  /** `export.plan`: dry-run tally of a full export (no write). */
  exportPlan?(dest?: string): Promise<ExportPlan>;
  /** `export.run`: write the export and return the gate verdict. */
  exportRun?(destPath: string): Promise<ExportResult>;
}

/**
 * The FULL data surface: {@link IpcClient} with the six Phase-3 time-travel / export
 * methods made REQUIRED. The mock (`createMockIpc`) is typed as this — it is the
 * reference implementation the UI and backend build against — so callers reach the new
 * methods without an optional-chaining dance. `FullIpcClient` is assignable to
 * `IpcClient`, so it drops straight into the `ipc` singleton.
 */
export type FullIpcClient = IpcClient &
  Required<
    Pick<
      IpcClient,
      | "graphRollup"
      | "graphTimeline"
      | "graphAt"
      | "graphDiff"
      | "exportPlan"
      | "exportRun"
    >
  >;
