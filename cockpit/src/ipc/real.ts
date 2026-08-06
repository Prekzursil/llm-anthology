/**
 * REAL implementation of {@link IpcClient}, backed by the Tauri commands the Rust side
 * (`src-tauri/src/lib.rs`) exposes. Each command proxies ONE JSON-RPC call through to
 * the Python sidecar (`llm_anthology/sidecar.py`, launched as `python -m llm_anthology.sidecar --index
 * <path>`) over stdio and returns the sidecar's JSON-RPC `result` verbatim.
 *
 * INTEGRATION SEAM. The Rust work-unit landed PER-METHOD commands (`health_ping`,
 * `corpus_stats`, `graph_roots`, `graph_children`, `graph_subtree`, `graph_ancestors`,
 * `search_query`, `thread_get`, `conversation_get`), each taking a single `params`
 * argument (`Option<Value>`) that it forwards to the matching JSON-RPC method. This ONE
 * file adapts the {@link IpcClient} surface onto those command names; the rest of the
 * app is unaffected, and the mock <-> real choice stays the single flag flip in
 * `./index.ts`. (The corpus must first be attached engine-side via the Rust
 * `open_corpus` command — a launch/settings concern outside this data surface.)
 *
 * `invoke` only reaches a backend inside the Tauri webview, so importing this module in
 * a plain browser/build is inert until `USE_REAL_IPC` is flipped.
 */

import { invoke } from "@tauri-apps/api/core";

import type {
  Conversation,
  CorpusDiffDto,
  CorpusStats,
  ExportPlan,
  ExportResult,
  GraphSnapshot,
  HealthInfo,
  IpcClient,
  OpenCorpusResult,
  RollupTable,
  RootsParams,
  SearchParams,
  SearchResult,
  Subtree,
  ThreadMeta,
  ThreadNode,
  Timeline,
} from "./types";

/**
 * Invoke one per-method Tauri command, wrapping `params` in the `{ params }` shape the
 * Rust command signature expects. `params` is `unknown` (not `Record<string, unknown>`)
 * so an interface-typed argument passes without missing-index-signature friction.
 */
async function cmd<T>(command: string, params: unknown = {}): Promise<T> {
  return invoke<T>(command, { params });
}

export const realIpc: IpcClient = {
  /**
   * `open_corpus` does NOT take the `{ params }` wrapper the data commands use — its Rust
   * signature is `open_corpus(state, index_path: String)`, so the argument is passed by
   * name. Tauri maps the camelCase key onto the snake_case parameter.
   */
  openCorpus(indexPath: string): Promise<OpenCorpusResult> {
    return invoke<OpenCorpusResult>("open_corpus", { indexPath });
  },
  healthPing(): Promise<HealthInfo> {
    return cmd<HealthInfo>("health_ping");
  },
  corpusStats(): Promise<CorpusStats> {
    return cmd<CorpusStats>("corpus_stats");
  },
  graphRoots(params: RootsParams = {}): Promise<ThreadNode[]> {
    return cmd<ThreadNode[]>("graph_roots", params);
  },
  graphChildren(threadId: string): Promise<ThreadNode[]> {
    return cmd<ThreadNode[]>("graph_children", { thread_id: threadId });
  },
  graphSubtree(threadId: string, depth?: number): Promise<Subtree> {
    const params: Record<string, unknown> = { thread_id: threadId };
    if (depth !== undefined) params.depth = depth;
    return cmd<Subtree>("graph_subtree", params);
  },
  graphAncestors(threadId: string): Promise<ThreadNode[]> {
    return cmd<ThreadNode[]>("graph_ancestors", { thread_id: threadId });
  },
  searchQuery(params: SearchParams): Promise<SearchResult> {
    return cmd<SearchResult>("search_query", params);
  },
  threadGet(threadId: string): Promise<ThreadMeta> {
    return cmd<ThreadMeta>("thread_get", { thread_id: threadId });
  },
  conversationGet(id: string): Promise<Conversation> {
    return cmd<Conversation>("conversation_get", { id });
  },

  // -- Phase-3 time-travel + export surface --------------------------------------
  // Each proxies one JSON-RPC method through its matching Rust command (see
  // `src-tauri/src/lib.rs`), mirroring the `graph.*` / `export.*` param names the
  // sidecar (`llm_anthology/sidecar.py`) requires. The mock implements the same six.
  graphRollup(): Promise<RollupTable> {
    return cmd<RollupTable>("graph_rollup");
  },
  graphTimeline(): Promise<Timeline> {
    return cmd<Timeline>("graph_timeline");
  },
  graphAt(asOfMs: number): Promise<GraphSnapshot> {
    return cmd<GraphSnapshot>("graph_at", { as_of_ms: asOfMs });
  },
  graphDiff(asOfA?: number, asOfB?: number): Promise<CorpusDiffDto> {
    // An omitted operand means "now" (the full corpus) — mirroring the sidecar's
    // `_diff_operand`, so `graphDiff()` is the empty self-diff.
    const params: Record<string, unknown> = {};
    if (asOfA !== undefined) params.as_of_a = asOfA;
    if (asOfB !== undefined) params.as_of_b = asOfB;
    return cmd<CorpusDiffDto>("graph_diff", params);
  },
  exportPlan(_dest?: string): Promise<ExportPlan> {
    // `export.plan` is a dry run over the loaded corpus and takes no params (the
    // sidecar ignores any dest); the optional arg is kept for contract parity.
    return cmd<ExportPlan>("export_plan");
  },
  exportRun(destPath: string): Promise<ExportResult> {
    return cmd<ExportResult>("export_run", { dest_path: destPath });
  },
};
