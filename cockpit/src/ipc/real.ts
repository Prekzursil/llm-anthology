/**
 * REAL implementation of {@link IpcClient}, backed by a Tauri command that proxies a
 * single JSON-RPC call through to the Python sidecar (`aisr/sidecar.py`) over stdio.
 *
 * INTEGRATION SEAM. The Rust work-unit owns `src-tauri/` and exposes the command
 * named {@link RPC_COMMAND}; it must accept `{ method: string, params: object }` and
 * return the sidecar's JSON-RPC `result` verbatim. If the Rust side lands a different
 * command name or per-method commands, this ONE file is where the integrate stage
 * adapts — the rest of the app is unaffected, and the mock <-> real choice stays the
 * single flag flip in `./index.ts`.
 *
 * Nothing here runs until `USE_REAL_IPC` is flipped: `invoke` only reaches a backend
 * inside the Tauri webview, so importing this module in a plain browser/build is inert.
 */

import { invoke } from "@tauri-apps/api/core";

import type {
  Conversation,
  CorpusStats,
  HealthInfo,
  IpcClient,
  RootsParams,
  SearchParams,
  SearchResult,
  Subtree,
  ThreadMeta,
  ThreadNode,
} from "./types";

/** The Tauri command the Rust side exposes to bridge one JSON-RPC request. */
export const RPC_COMMAND = "sidecar_rpc";

/**
 * Issue one JSON-RPC method call and return its typed `result`. `params` is `unknown`
 * (not `Record<string, unknown>`) so an interface-typed argument passes without the
 * missing-index-signature friction; it is wrapped in a fresh object literal for
 * `invoke`, which is assignable to Tauri's `InvokeArgs`.
 */
async function rpc<T>(method: string, params: unknown = {}): Promise<T> {
  return invoke<T>(RPC_COMMAND, { method, params });
}

export const realIpc: IpcClient = {
  healthPing(): Promise<HealthInfo> {
    return rpc<HealthInfo>("health.ping");
  },
  corpusStats(): Promise<CorpusStats> {
    return rpc<CorpusStats>("corpus.stats");
  },
  graphRoots(params: RootsParams = {}): Promise<ThreadNode[]> {
    return rpc<ThreadNode[]>("graph.roots", params);
  },
  graphChildren(threadId: string): Promise<ThreadNode[]> {
    return rpc<ThreadNode[]>("graph.children", { thread_id: threadId });
  },
  graphSubtree(threadId: string, depth?: number): Promise<Subtree> {
    const params: Record<string, unknown> = { thread_id: threadId };
    if (depth !== undefined) params.depth = depth;
    return rpc<Subtree>("graph.subtree", params);
  },
  graphAncestors(threadId: string): Promise<ThreadNode[]> {
    return rpc<ThreadNode[]>("graph.ancestors", { thread_id: threadId });
  },
  searchQuery(params: SearchParams): Promise<SearchResult> {
    return rpc<SearchResult>("search.query", params);
  },
  threadGet(threadId: string): Promise<ThreadMeta> {
    return rpc<ThreadMeta>("thread.get", { thread_id: threadId });
  },
  conversationGet(id: string): Promise<Conversation> {
    return rpc<Conversation>("conversation.get", { id });
  },
};
