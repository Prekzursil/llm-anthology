/**
 * IPC wire contract (engine <-> UI).
 *
 * These interfaces mirror, field-for-field, the DTOs the committed Python sidecar
 * (`aisr/sidecar.py`) emits over stdio NDJSON JSON-RPC 2.0. Optional fields marked
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
}
