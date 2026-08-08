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
  Annotation,
  BuildHandle,
  BuildParams,
  BuildStatus,
  Conversation,
  CorpusDiffDto,
  CorpusStats,
  CreateCorpusResult,
  DedupScanResult,
  DedupSession,
  DiscoveryResult,
  ExportPlan,
  ExportResult,
  GraphSnapshot,
  HealthInfo,
  IpcClient,
  MaintenanceExecuteParams,
  MaintenancePlanParams,
  MaintenancePreview,
  MaintenanceRestoreParams,
  MaintenanceResult,
  MaintenanceRun,
  MetadataSearchParams,
  MetadataSearchRow,
  MetadataSetParams,
  OpenCorpusResult,
  RollupTable,
  RootsParams,
  SearchParams,
  SearchResult,
  Subtree,
  TagCount,
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

  /**
   * `discover_sources` takes NO arguments (`src-tauri/src/lib.rs:120`) — the engine
   * exposes no `roots` parameter on purpose, so passing the `{ params }` wrapper the data
   * commands use would hand the command an argument its signature does not declare.
   */
  discoverSources(): Promise<DiscoveryResult> {
    return invoke<DiscoveryResult>("discover_sources");
  },
  /** By-name, like `open_corpus`: the Rust signature is `create_corpus(index_path: String)`. */
  createCorpus(indexPath: string): Promise<CreateCorpusResult> {
    return invoke<CreateCorpusResult>("create_corpus", { indexPath });
  },
  corpusBuild(params: BuildParams): Promise<BuildHandle> {
    return cmd<BuildHandle>("corpus_build", params);
  },
  corpusBuildStatus(jobId?: string): Promise<BuildStatus> {
    // `job_id` is OPTIONAL and proves the poll is reading the job it started; a poll that
    // raced a newer build gets -32602 rather than another job's progress
    // (`llm_anthology/sidecar.py:809-827`). Omitted entirely when absent, because an
    // explicit null would fail the string check there.
    const params: Record<string, unknown> = {};
    if (jobId !== undefined) params.job_id = jobId;
    return cmd<BuildStatus>("corpus_build_status", params);
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

  // -- annotations / dedup / maintenance ------------------------------------------
  //
  // ELEVEN methods. Each proxies ONE JSON-RPC method through the Tauri command named by the
  // pinned rule (RPC `a.b` -> command `a_b`), forwarding the sidecar's snake_case param names
  // verbatim. The command names below are the contract with `src-tauri/src/lib.rs`; a typo
  // here type-checks, passes vitest (the mock never sees a command name) and fails only at
  // the moment a user presses the button, so they are spelled out literally rather than
  // derived. `index.test.ts` asserts each one against the rule.
  //
  // The engine also registers `research.synthesize` / `research.extract_entities`. They are
  // deliberately NOT bound here and have no Tauri command
  // (`cockpit/src-tauri/src/lib.rs:322-333`) — see the NOT BOUND note in `./types.ts`. A
  // binding without a command type-checks and dies only on the button press, so adding one
  // back is worse than the missing feature.
  //
  // An OPTIONAL param is OMITTED rather than sent as null wherever the engine type-checks
  // it with `isinstance`, because an explicit null fails that check as -32602 — the same
  // reason `corpusBuildStatus` above omits `job_id`.

  metadataGet(conversationId: string): Promise<Annotation> {
    return cmd<Annotation>("metadata_get", { conversation_id: conversationId });
  },
  /**
   * The params object is forwarded AS-IS, which is what preserves the partial-update
   * tri-state: a field absent from `MetadataSetParams` is absent on the wire and the engine
   * leaves it unchanged, while an explicit `""` / `[]` clears it
   * (`llm_anthology/sidecar.py:1335-1337`). Normalising the undefined fields to null or ""
   * here would silently blank the other two on every per-field edit.
   */
  metadataSet(params: MetadataSetParams): Promise<Annotation> {
    return cmd<Annotation>("metadata_set", params);
  },
  metadataClear(conversationId: string): Promise<Annotation> {
    return cmd<Annotation>("metadata_clear", { conversation_id: conversationId });
  },
  metadataSearch(params: MetadataSearchParams = {}): Promise<MetadataSearchRow[]> {
    return cmd<MetadataSearchRow[]>("metadata_search", params);
  },
  metadataTags(): Promise<TagCount[]> {
    return cmd<TagCount[]>("metadata_tags");
  },

  dedupScan(codexHome: string): Promise<DedupScanResult> {
    return cmd<DedupScanResult>("dedup_scan", { codex_home: codexHome });
  },
  dedupSessions(): Promise<DedupSession[]> {
    return cmd<DedupSession[]>("dedup_sessions");
  },

  maintenancePlan(params: MaintenancePlanParams): Promise<MaintenancePreview> {
    return cmd<MaintenancePreview>("maintenance_plan", params);
  },
  maintenanceExecute(params: MaintenanceExecuteParams): Promise<MaintenanceResult> {
    return cmd<MaintenanceResult>("maintenance_execute", params);
  },
  maintenanceRestore(params: MaintenanceRestoreParams): Promise<MaintenanceResult> {
    return cmd<MaintenanceResult>("maintenance_restore", params);
  },
  maintenanceRuns(limit?: number): Promise<MaintenanceRun[]> {
    // `_opt_int` rejects a non-int (and a bool) with -32602, so an absent limit is omitted
    // and the engine applies its own default of 50 (`llm_anthology/sidecar.py:1614`).
    const params: Record<string, unknown> = {};
    if (limit !== undefined) params.limit = limit;
    return cmd<MaintenanceRun[]>("maintenance_runs", params);
  },
};
