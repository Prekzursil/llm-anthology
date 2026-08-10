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
  /**
   * The ADAPTER that produced this thread: "codex", "grok", "claude-code", ... — the same
   * meaning `SearchHit.provider` and `corpus.stats.providers` carry. This is the field the
   * provider palette keys on.
   */
  provider: string;
  /**
   * The MODEL VENDOR the session ran against ("openai"), which is a DIFFERENT fact and is
   * often empty. Never pass this to `providerTint`: measured over 250 real Codex rollouts
   * it is 'openai' 92.8% of the time and absent for the rest, so tinting by it painted
   * every Codex node the "unknown" grey. It used to be delivered as `provider`.
   */
  model_provider: string;
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
  /** The ADAPTER — see `ThreadNode.provider`. */
  provider: string;
  /** The MODEL VENDOR — see `ThreadNode.model_provider`. Not for tinting. */
  model_provider: string;
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
/**
 * `open_corpus` result: the engine was (re)spawned against `index`.
 *
 * This is a LIFECYCLE call, not a data read — it replaces the sidecar process, so every
 * cached read taken before it is stale afterwards.
 */
export interface OpenCorpusResult {
  ok: boolean;
  index: string;
}

/**
 * `corpus.create` result: an EMPTY index was initialised at `index_path`.
 *
 * A different shape from {@link OpenCorpusResult} on purpose — the engine returns
 * `{index_path, created}` here (`llm_anthology/sidecar.py:790`) and `{ok, index}` there —
 * so the two are not interchangeable. Creating does NOT attach; `openCorpus` still has to
 * follow (`cockpit/src-tauri/src/lib.rs:91-107`).
 */
export interface CreateCorpusResult {
  index_path: string;
  created: boolean;
}

/**
 * One thing auto-discovery found on this machine, mirroring `discover.Finding.as_dict`
 * (`llm_anthology/discover.py:152-185`).
 *
 * Two fields are easy to get wrong and both are load-bearing:
 *
 *   * `newest_mtime` is UNIX **SECONDS** (a float), not milliseconds — everything else in
 *     this app is `_ms`. It is `0.0`, never null, when nothing datable was seen
 *     (`discover.py:654`: `max(mtimes) if mtimes else 0.0`), so 0 means "unknown date"
 *     rather than 1970.
 *   * `detail` is an OPEN dict whose keys vary by provider: a built index carries
 *     `{tables, conversations}` (`discover.py:770-772`), a Codex store
 *     `{rollouts_jsonl, rollouts_zst, state_db, ingestable, items_root}`
 *     (`discover.py:309-313`), a Claude Code store `{"*.jsonl", ingestable, items_root,
 *     project_dirs}`, and an export file `{size_bytes, ambiguous_with?}`
 *     (`discover.py:720-722`). It must be rendered generically; assuming a fixed key set
 *     would silently drop whatever a newly-added provider reports.
 *
 * `kind` and `confidence` are typed as plain `string` rather than unions for the same
 * reason: adding a provider is a table edit in the engine (`discover.py:50-53`), and a
 * UI that narrowed them would have to be recompiled to keep displaying a new one.
 */
export interface DiscoveryFinding {
  provider: string;
  /** `built_index` | `session_store` | `export_file` (`discover.py:68-70`). */
  kind: string;
  /** ABSOLUTE local path. Not redacted on the wire — the sidecar only strips hidden
   *  unicode (`_sanitize_tree` -> `_clean`, `sidecar.py:306-318`) — so it is usable as an
   *  engine argument, and it embeds the local layout, so it is display-sensitive. */
  path: string;
  count: number;
  /** UNIX SECONDS (float). 0 means no datable item was seen. */
  newest_mtime: number;
  /** `high` | `medium` | `low` (`discover.py:72-74`). */
  confidence: string;
  detail: Record<string, unknown>;
}

/**
 * What one scan cost and whether it saw everything (`discover.ScanStats`,
 * `llm_anthology/discover.py:189-202`).
 *
 * `truncated_groups` holds `"<provider>/<kind>"` keys whose findings the ENGINE capped at
 * `DEFAULT_MAX_PER_GROUP` (`discover.py:108`, `:897`) — a truncation that happened before
 * the UI ever saw the data, and therefore a different fact from any collapsing the UI does
 * for display. `errors` is one string per location that could not be read.
 */
export interface DiscoveryStats {
  elapsed_seconds: number;
  roots_scanned: number;
  dirs_visited: number;
  files_examined: number;
  budget_exhausted: boolean;
  truncated_groups: string[];
  errors: string[];
}

/** `sources.discover` result. Answers with NO corpus attached — it is the first-run call. */
export interface DiscoveryResult {
  findings: DiscoveryFinding[];
  stats: DiscoveryStats;
}

/**
 * `corpus.build` parameters. ALL THREE are optional and none is defaulted by the engine —
 * every one is read with `_opt_str` (`llm_anthology/sidecar.py:866-890`). Optional is not
 * lax: the engine refuses when NEITHER root is named (`sidecar.py:869-872`), and omitting
 * `codex_home` means "no state graph" rather than "go find one", because defaulting it
 * would make the app read the user's live private store without being asked.
 */
export interface BuildParams {
  /**
   * The Codex date-nested `YYYY/MM/DD/rollout-*.jsonl` tree. OPTIONAL: every source is
   * opt-in by naming its root, and `corpus.build` refuses only when NONE is named. A
   * machine can hold a Grok store and no Codex store at all.
   */
  sessions_root?: string;
  /**
   * A Grok Build session store (`<enc-cwd>/<session-id>/`). Opt-in, like the above.
   */
  grok_root?: string;
  /**
   * A Claude Code store — the `projects/` tree under a Claude home. Opt-in, like the above.
   *
   * MEASURED against `corpus.build` rather than read off the engine, because the UI has to
   * match its refusals exactly: accepted ALONE with no other source named; UNC (both `\\`
   * and `//` spellings), relative, non-existent and non-string roots each refused -32602
   * with the same wording `grok_root` uses; and the UNC guard fires BEFORE the directory
   * check, so a network path never comes back as "must be an existing directory".
   *
   * An EMPTY STRING means ABSENT, not invalid — it falls through to the at-least-one-source
   * refusal rather than being validated. Same as `grok_root`. Do not send `""` expecting a
   * complaint about it.
   */
  claude_root?: string;
  /**
   * The Codex home whose `state_5.sqlite` spawn graph is merged in. OPTIONAL, and omitting
   * it means "no state graph" — NOT "go find one". The engine used to fall through to the
   * live `~/.codex` when this was absent, which is how an automated probe read the owner's
   * real sessions; that fallback is gone and omission is now the safe choice.
   */
  codex_home?: string;
}

/** `corpus.build` result: the job was ACCEPTED, not finished (`sidecar.py:834-836`). */
export interface BuildHandle {
  job_id: string;
  state: string;
  /**
   * All three roots, each echoed AS ITSELF, and each `""` when that source was not named
   * — so a Grok-only build carries `sessions_root: ""` alongside a populated `grok_root`.
   * MEASURED against the engine rather than assumed, because the mock previously
   * collapsed them and reported a Grok path in `sessions_root`, which means "the Codex
   * tree". Unlike {@link BuildStatus}, the start reply emits all three unconditionally;
   * that asymmetry is the engine's.
   */
  sessions_root: string;
  grok_root: string;
  claude_root: string;
  started_ms: number;
}

/**
 * `corpus.build_status` result (`llm_anthology/sidecar.py:990-993`).
 *
 * Poll-safe at any time, INCLUDING before any build has ever started — that answers
 * `{state: "idle", indexed_conversations, errors: []}` rather than erroring, which is why
 * every field except those three is optional here. `state` is `idle` | `running` | `done`
 * | `failed`; `indexed_conversations` is a live `COUNT(*)` off the index, so it climbs
 * while a build runs.
 */
export interface BuildStatus {
  state: string;
  indexed_conversations: number;
  errors: string[];
  job_id?: string;
  sessions_root?: string;
  /**
   * The other two roots the job covers, each present only when that source was named.
   *
   * They exist because `sessions_root` alone cannot describe a job: for a Grok-only or
   * Claude-Code-only build it is EMPTY, so a poller holding nothing but the status could
   * not tell what was being read. The engine recorded both on the job from the start and
   * projected neither, so they were written and read by nothing.
   */
  grok_root?: string;
  claude_root?: string;
  started_ms?: number;
  finished_ms?: number;
  /** Terminal failure text; present only on a failed build. */
  error?: string;
}

// ---------------------------------------------------------------------------
// NOT BOUND: the research plane (`research.*`)
// ---------------------------------------------------------------------------
//
// `research.synthesize` and `research.extract_entities` are registered by the engine
// (`llm_anthology/sidecar.py:622-623`) and deliberately have NO binding here, no Tauri
// command (`cockpit/src-tauri/src/lib.rs:322-333`) and no mock. They are deferred out of v1.
//
// THE REASON IS STRUCTURAL, NOT SCHEDULING. The research plane has no backend at all:
// `main()` constructs `Sidecar(conn)` with no backend arguments, so `research_backend` and
// `local_backend` both fall back to `research.MockBackend()` (`sidecar.py:587-590`), whose
// `synthesize` returns its `response` — default `""` (`llm_anthology/research.py:88-97`). The
// only two `synthesize` implementations in the package are that mock and the Protocol stub
// (`research.py:76`). So BOTH methods return an empty result in the shipped app BY
// CONSTRUCTION — including extraction, which routes through the same
// `self.research_backend` (`sidecar.py:1317`).
//
// Typing them would hand a panel a contract for a feature that cannot produce output, and
// wiring a real LLM backend touches a standing privacy rule about this corpus. Do NOT add
// `ResearchTier` / `ResearchSynthesis` / `ResearchEntities` back without that decision.

// ---------------------------------------------------------------------------
// annotations (`metadata.*`)
// ---------------------------------------------------------------------------

/**
 * One owner-authored annotation (`llm_anthology/sidecar.py:1350-1356`).
 *
 * LOCAL-ONLY BY DESIGN. Alias/tags/notes are deliberately absent from
 * `redact.MetadataView`, so they can never ride the cloud research plane
 * (`sidecar.py:1338-1344`) — they cross only the stdio wire to this UI.
 *
 * Every field is always present. An un-annotated conversation reads back as an EMPTY
 * annotation with `is_empty: true` rather than an error (`sidecar.py:1359-1360`), so a
 * panel can render it unconditionally without a null check.
 */
export interface Annotation {
  conversation_id: string;
  alias: string;
  /** Deterministically ordered by the store (`tests/test_sidecar_metadata.py:84`). */
  tags: string[];
  notes: string;
  /** True when alias, tags and notes are all empty. */
  is_empty: boolean;
}

/**
 * `metadata.set` parameters (`llm_anthology/sidecar.py:1365-1385`).
 *
 * PARTIAL UPDATE, and the tri-state is the whole point (`sidecar.py:1366-1368`):
 *
 *   * field OMITTED (`undefined`) -> left UNCHANGED;
 *   * field present but EMPTY (`""` / `[]`) -> CLEARED.
 *
 * The cockpit edits one field at a time, so a per-field call that sent `{alias}` alone
 * must not blank tags and notes — pinned by `tests/test_sidecar_metadata.py:89-97`. Never
 * send `null`: the engine type-checks with `isinstance(str)` / `isinstance(list)` and a
 * null fails as -32602 (`sidecar.py:1372-1377`).
 */
export interface MetadataSetParams {
  conversation_id: string;
  alias?: string;
  tags?: string[];
  notes?: string;
}

/**
 * `metadata.search` parameters (`llm_anthology/sidecar.py:1396-1405`).
 *
 * The two filters are ANDed, not ORed, and with NEITHER supplied the result is EMPTY — a
 * blank query deliberately does not dump the whole catalogue into the UI
 * (`sidecar.py:1398-1399`, `llm_anthology/metadata.py:501-502`).
 */
export interface MetadataSearchParams {
  /** Free text over the annotation, never over message bodies. */
  text?: string;
  tag?: string;
}

/**
 * One `metadata.search` row: the matching annotation JOINED to its display columns
 * (`llm_anthology/sidecar.py:1408-1422`).
 *
 * Nothing here is nullable. The join reads `conversations`, whose every column is
 * `NOT NULL DEFAULT ''` / `DEFAULT 0` (`llm_anthology/corpus.py:179-191`), so an absent
 * `account` / `thread_id` arrives as `""` and NOT as null — a renderer testing
 * `row.account === null` would never fire.
 *
 * Note the shape difference from {@link SearchHit}: this is an ANNOTATION search, so there
 * is no snippet and no score, and `created_at` / `updated_at` are ISO-ish STRINGS off the
 * index rather than the `_ms` epoch numbers the graph surface uses.
 */
export interface MetadataSearchRow {
  conversation_id: string;
  provider: string;
  account: string;
  title: string;
  created_at: string;
  updated_at: string;
  turn_count: number;
  thread_id: string;
  annotation: Annotation;
}

/**
 * One entry of the tag facet (`llm_anthology/sidecar.py:1428-1429`).
 *
 * Counts collapse CASE-INSENSITIVELY — 'Beta' and 'beta' are one entry with count 2 — and
 * `tag` is the lexicographically-first display form among the variants, ordered by the
 * casefolded tag (`llm_anthology/metadata.py:529-544`). So the facet never lists the same
 * tag twice, and the label shown is not necessarily the form the owner last typed.
 */
export interface TagCount {
  tag: string;
  count: number;
}

// ---------------------------------------------------------------------------
// dedup (`dedup.*`) — a VIEW, never a delete
// ---------------------------------------------------------------------------

/**
 * `dedup.scan` result (`llm_anthology/sidecar.py:1473-1480`).
 *
 * A scan is a READ: `dedup` contains no write/delete/move call, so nothing here can remove
 * one of the owner's files (`sidecar.py:1431-1436`). It does persist the derived view into
 * the attached index.
 *
 * `errors` is one string per store location that could not be read; a MISSING `codex_home`
 * is an empty result, not a failure (`tests/test_sidecar_dedup.py:101-107`).
 */
export interface DedupScanResult {
  /** Distinct LOGICAL sessions after collapsing copies. */
  session_count: number;
  /** Physical files seen on disk (>= `session_count`). */
  copy_count: number;
  /** `copy_count - session_count`: how many physical copies were collapsed away. */
  duplicate_count: number;
  /** Sessions whose canonical copy is SMALLER than one it demoted — see
   *  {@link DedupSession.has_larger_copy}. */
  flagged_truncated: number;
  /** Sessions with no recoverable session id (path-keyed singletons, never merged). */
  unidentified: number;
  errors: string[];
}

/**
 * One logical session and its physical copies (`llm_anthology/sidecar.py:1447-1457`).
 *
 * `store_kind` is a PLAIN STRING here — `dedup.PhysicalCopy.store_kind` is a `str`
 * (`llm_anthology/dedup.py:117`), one of `live` | `backup` | `mirror` | `other` | `unknown`
 * (`dedup.py:87-91`) — whereas the `maintenance.*` surface carries a real enum serialized
 * via `.value` ({@link MaintenanceStoreKind}). Same field name, two different engine types,
 * so do not share one narrowing helper between the surfaces.
 *
 * `canonical_path` and `duplicate_paths` are LOCAL filesystem paths that embed the owner's
 * username. They travel only over this stdio wire and are absent from
 * `redact.MetadataView` (`sidecar.py:1434-1436`) — display-sensitive.
 */
export interface DedupSession {
  session_id: string;
  /** The copy this view puts forward (the LIVE store outranks a mirror). */
  canonical_path: string;
  /** `live` | `backup` | `mirror` | `other` | `unknown` (`dedup.py:87-91`). */
  store_kind: string;
  size_bytes: number;
  /** Epoch ms, or null — `PhysicalCopy.last_write_ms` is `Optional[int]` (`dedup.py:118`). */
  last_write_ms: number | null;
  copy_count: number;
  /** The non-canonical copies, still fully retained as evidence (`dedup.py:138-141`). */
  duplicate_paths: string[];
  /** False when no session id could be recovered from the file OR its name. */
  is_identified: boolean;
  /**
   * True when the canonical copy is SMALLER than one it demoted, i.e. the copy shown is a
   * truncated prefix of a sibling (`dedup.py:149-164`).
   *
   * Store rank outranks size on purpose — a live store must never be demoted behind a
   * stale mirror — but that means a crash-truncated live rollout can win over a complete
   * backup. Nothing is lost on disk (`duplicate_paths` still lists the fuller copy), so
   * the condition is REPORTED rather than silently resolved and a UI can offer the other
   * copy. Ignoring this flag is how a UI shows the owner the shorter conversation.
   */
  has_larger_copy: boolean;
}

// ---------------------------------------------------------------------------
// maintenance (`maintenance.*`) — the ONLY destructive surface
// ---------------------------------------------------------------------------

/** `maintenance.plan` action (`llm_anthology/maintenance.py:141-147`). Closed: any other
 *  value is -32602 (`llm_anthology/sidecar.py:1567-1572`). */
export type MaintenanceActionName = "delete" | "archive" | "move" | "reconcile";

/**
 * A target's store classification (`llm_anthology/maintenance.py:158-165`), serialized from
 * a real `enum` via `.value` (`llm_anthology/sidecar.py:1505`).
 *
 * Every target the RPC edge builds is forced to `UNKNOWN` (`sidecar.py:1587`) — the client
 * cannot assert a store kind — so a plan's `allowed` entries read `"unknown"` unless the
 * engine itself classified them. Do not render this as "we could not tell".
 */
export type MaintenanceStoreKind = "unknown" | "live" | "backup" | "mirror" | "other";

/** One session file inside a plan (`llm_anthology/sidecar.py:1504-1506`,
 *  `llm_anthology/maintenance.py:182-190`). */
export interface MaintenanceCopy {
  session_id: string;
  file_path: string;
  store_kind: MaintenanceStoreKind;
  /** Epoch ms, or null (`maintenance.py:206`). */
  last_write_ms: number | null;
  size_bytes: number;
  /** Being written right now — raises a REVIEW warning (`maintenance.py:561-564`). */
  is_hot: boolean;
}

/** A target the safety model REFUSED, with the reason (`llm_anthology/sidecar.py:1516-1517`). */
export interface MaintenanceBlocked {
  target: MaintenanceCopy;
  reason: string;
  detail: string;
}

/**
 * One preview warning (`llm_anthology/sidecar.py:1518-1519`).
 *
 * A CLEAN PLAN IS NOT A QUIET PLAN. The engine emits a DANGEROUS warning for EVERY allowed
 * target plus a closing INFO summary (`llm_anthology/maintenance.py:558-560`, `:576-579`), so a 2-file
 * plan carries 3 warnings with nothing wrong. A UI that treats a non-empty `warnings` as
 * "something is broken" flags every healthy plan; filter on `severity` instead.
 */
export interface MaintenanceWarning {
  /** 0 INFO | 1 REVIEW | 2 DANGEROUS (`maintenance.py:150-155`). */
  severity: number;
  severity_name: "INFO" | "REVIEW" | "DANGEROUS";
  message: string;
}

/** One planned source -> destination move (`llm_anthology/sidecar.py:1520-1521`). */
export interface PlannedMove {
  session_id: string;
  source: string;
  destination: string;
}

/** One target offered to `maintenance.plan` (`llm_anthology/sidecar.py:1577-1588`). */
export interface MaintenanceTarget {
  /** REQUIRED and non-empty, else -32602 (`sidecar.py:1581-1583`). */
  file_path: string;
  /** Defaults to `""` when omitted (`sidecar.py:1585`). */
  session_id?: string;
  /** Defaults to 0 when omitted (`sidecar.py:1588`). */
  size_bytes?: number;
}

/**
 * `maintenance.plan` parameters (`llm_anthology/sidecar.py:1551-1592`).
 *
 * `store_root` and `checkpoint_root` are REQUIRED non-empty strings; every root is rejected
 * at the RPC edge if UNC or relative (`sidecar.py:1561-1565`), because merely resolving
 * `\\host\share` on Windows initiates an outbound SMB/NTLM authentication. `targets` must
 * be a NON-EMPTY array (`sidecar.py:1575-1576`).
 */
export interface MaintenancePlanParams {
  store_root: string;
  checkpoint_root: string;
  action: MaintenanceActionName;
  targets: MaintenanceTarget[];
  /** Required in practice for `archive` / `move`; ignored for `delete`. */
  destination_root?: string;
}

/**
 * `maintenance.plan` result: a PURE preview under a single-use handle
 * (`llm_anthology/sidecar.py:1508-1525`). No filesystem mutation happens
 * (`tests/test_sidecar_maintenance.py:154-167`).
 *
 * THE CLIENT NEVER SENDS THIS BACK. `maintenance` validates paths against the roots carried
 * INSIDE the preview, so a forged preview rebuilt from client JSON could name its own
 * store/checkpoint/destination root and be honoured. The server therefore keeps its OWN
 * preview object and `maintenance.execute` takes only the opaque `plan_id`
 * (`sidecar.py:1488-1500`). Treat every field here as DISPLAY-ONLY.
 */
export interface MaintenancePreview {
  /** The single-use handle to pass to `maintenance.execute`. Ids are `plan-1`, `plan-2`, … */
  plan_id: string;
  action: MaintenanceActionName;
  store_root: string;
  /**
   * The EFFECTIVE destination, not the one requested (`maintenance.py:452-461`):
   * a `delete` quarantines under `<checkpoint_root>/deleted` and a `reconcile` under
   * `<destination_root>/reconciled`. This is the path the files really go to.
   */
  destination_root: string;
  checkpoint_root: string;
  allowed: MaintenanceCopy[];
  blocked: MaintenanceBlocked[];
  warnings: MaintenanceWarning[];
  plan: PlannedMove[];
  /** Always true (`maintenance.py:587`). */
  requires_checkpoint: boolean;
  /** Always true (`maintenance.py:588`). */
  requires_typed_confirmation: boolean;
  /**
   * The exact phrase the operator must type, e.g. `"DELETE 2 FILES"` / `"ARCHIVE 1 FILE"`
   * (`maintenance.py:463-470`). Derived from the ALLOWED count, never from what the caller
   * offered — so a plan that changed since the operator last looked changes the phrase too.
   * Echo it verbatim; do not reconstruct it client-side.
   */
  required_typed_confirmation: string;
}

/**
 * `maintenance.execute` parameters (`llm_anthology/sidecar.py:1600-1617`).
 *
 * `apply` DEFAULTS TO FALSE, so the destructive act is always an explicit second step
 * (`tests/test_sidecar_maintenance.py:209-219`). The handle is consumed once the engine
 * ACCEPTS the run, so a completed plan cannot be replayed — but a REFUSED confirmation
 * leaves it usable, so a typo is correctable without re-planning (`sidecar.py:1618-1620`,
 * `tests/test_sidecar_maintenance.py:194-206`).
 */
export interface MaintenanceExecuteParams {
  plan_id: string;
  /** Must equal {@link MaintenancePreview.required_typed_confirmation} to apply. */
  confirmation?: string;
  apply?: boolean;
}

/**
 * `maintenance.restore` parameters (`llm_anthology/sidecar.py:1625-1636`).
 *
 * `apply` defaults to false here too, so a caller can see what a restore would do first.
 * `manifest_path` is rejected at the edge if UNC or relative (`sidecar.py:1630`).
 */
export interface MaintenanceRestoreParams {
  /** A `manifest_path` the engine itself issued from a prior `maintenance.execute`. */
  manifest_path: string;
  apply?: boolean;
  /**
   * Restore the accounted moves anyway and REPORT the rest in
   * {@link MaintenanceResult.unaccounted}. Without it an unaccounted move refuses the whole
   * batch rather than guessing (`maintenance.py:794-799`).
   */
  skip_unaccounted?: boolean;
}

/**
 * `maintenance.execute` / `maintenance.restore` result (`llm_anthology/sidecar.py:1529-1535`).
 *
 * `manifest_path` is `""` — NOT null and NOT absent — on a dry run
 * (`llm_anthology/maintenance.py:670`, `tests/test_sidecar_maintenance.py:216-217`), so
 * truthiness is the correct test for "did this write a checkpoint".
 */
export interface MaintenanceResult {
  /** False for a dry run: nothing on disk changed. */
  executed: boolean;
  /** The checkpoint manifest; `""` when nothing was written. Feed it to `maintenance.restore`. */
  manifest_path: string;
  moves: PlannedMove[];
  /** Recorded originals a restore could not account for (`maintenance.py:285`). */
  unaccounted: string[];
}

/**
 * One row of the destructive-run audit ledger (`llm_anthology/maintenance.py:849-858`,
 * schema at `:823-836`). Newest first; `manifest_path` breaks ties (`maintenance.py:870-871`).
 *
 * Only an APPLIED run lands here — a dry run does not
 * (`tests/test_sidecar_maintenance.py:305-312`).
 */
export interface MaintenanceRun {
  manifest_path: string;
  action: string;
  /** `pending` | `executed` | `restored` (`maintenance.py:715`, `:720`, `:808`). */
  status: string;
  recorded_at_ms: number;
  moved_count: number;
  blocked_count: number;
  store_root: string;
}

// ---------------------------------------------------------------------------
// RPC error codes
// ---------------------------------------------------------------------------
//
// THE CODE ARRIVES AS TEXT, NOT AS A FIELD. The sidecar answers a failure with a JSON-RPC
// envelope `{code, message}` (`llm_anthology/sidecar.py:299-303`), but the Rust bridge
// flattens the whole envelope into a STRING —
// `Err(format!("rpc error (id {id}): {err}"))` (`cockpit/src-tauri/src/sidecar.rs:161`) —
// and Tauri's `invoke` rejects with that string. So there is no typed error object to
// narrow: a caller that needs the code has to parse it back out. {@link rpcErrorCode} is
// that parser, and it is the ONLY sanctioned way to read a code — do not hand-roll a
// second regex.

/** A well-formed request whose params were wrong (`llm_anthology/sidecar.py:684`). */
export const RPC_INVALID_PARAMS = -32602;
/** An unhandled engine fault (`llm_anthology/sidecar.py:677`), carrying `{detail}`. */
export const RPC_INTERNAL_ERROR = -32603;
/** No corpus attached — call `openCorpus` first (`llm_anthology/sidecar.py:252`). */
export const RPC_CORPUS_NOT_INDEXED = -32000;
/** `llm_anthology/sidecar.py:253`. */
export const RPC_THREAD_NOT_FOUND = -32001;
/** SQLite lock/busy; the envelope carries `retry_ms` (`llm_anthology/sidecar.py:254`). */
export const RPC_DB_BUSY = -32002;
/**
 * A maintenance request the safety model REFUSED (`llm_anthology/sidecar.py:255-257`).
 *
 * DISTINCT FROM {@link RPC_INVALID_PARAMS} and the distinction is the whole point: the
 * params were well-formed and the operation was DECLINED — unconfirmed, outside the store
 * root, or a single-use plan that has already been spent. A Maintenance panel must branch
 * on this to say "that plan expired, re-plan" instead of a generic failure toast, because
 * the two need completely different next actions from the operator.
 */
export const RPC_MAINTENANCE_REFUSED = -32003;
/**
 * A second `corpus.build` while one is still running (`llm_anthology/sidecar.py:259-261`).
 *
 * THE CORRECT UI IS NOT A RETRY. The engine's own comment is explicit: only one ingest may
 * own the index at a time, so "the client should poll corpus.build_status, not retry". A
 * generic failure toast with a Retry button here trains the operator to do exactly the wrong
 * thing, and each retry earns the same refusal while the real build is progressing fine.
 */
export const RPC_BUILD_IN_PROGRESS = -32004;
/**
 * `corpus.build` cannot run against THIS engine (`llm_anthology/sidecar.py:262-264`): the
 * attached index has no on-disk file (an in-memory database), so the build worker has
 * nothing to reopen.
 *
 * Explicitly NOT RETRYABLE — retrying is guaranteed to fail until a different corpus is
 * attached, so this must not be offered a retry affordance either.
 */
export const RPC_BUILD_UNAVAILABLE = -32005;
/**
 * `corpus.create` was pointed at a path where a file already exists
 * (`llm_anthology/sidecar.py:265-267`).
 *
 * Distinct from {@link RPC_INVALID_PARAMS} deliberately: the path is perfectly VALID, it is
 * simply taken. That distinction is the whole reason the code exists — it lets a UI offer
 * "open that one instead?" or "pick another name", where "bad path" would be a lie.
 */
export const RPC_CORPUS_EXISTS = -32006;

/**
 * Recover the JSON-RPC code from a rejection, or null when the text carries none.
 *
 * Null is NOT "no error" — it means the failure did not come from the engine's error
 * envelope at all (a transport fault, a mutex poisoning, `no corpus attached` raised by the
 * Rust bridge itself at `lib.rs:45`, or a mock/JS `Error`). Treat null as "unclassified
 * failure", never as success.
 *
 * A missing checkpoint manifest is worth knowing about specifically: `read_checkpoint`
 * opens the file directly (`llm_anthology/maintenance.py:626`), so a bad path raises
 * `FileNotFoundError`, which is NOT a `MaintenanceRefused` and escapes the refusal mapping
 * — it arrives as {@link RPC_INTERNAL_ERROR}, not {@link RPC_MAINTENANCE_REFUSED}.
 */
export function rpcErrorCode(error: unknown): number | null {
  const text =
    error instanceof Error ? error.message : typeof error === "string" ? error : "";
  const match = /"code"\s*:\s*(-?\d+)/.exec(text);
  return match === null ? null : Number(match[1]);
}

export interface IpcClient {
  /**
   * Attach the engine to the corpus index at `indexPath`, replacing any corpus already
   * open. Every other method on this interface fails with "no corpus attached" until this
   * has succeeded once, so this is the app's entry point rather than an optional extra.
   */
  openCorpus(indexPath: string): Promise<OpenCorpusResult>;

  // -- first-run auto-discovery + ingest -----------------------------------------
  // REQUIRED, unlike the optional Phase-3 block below. These are the only route a fresh
  // install has to a usable corpus, and an adapter that silently omitted one would
  // disable autodetection without any type error — the same invisible-dead-path failure
  // `ipc/index.ts` documents for the mock/real mix-up.

  /**
   * Find AI session data already on this machine. Takes no arguments and needs NO corpus:
   * it runs on a throwaway index-less engine (`cockpit/src-tauri/src/lib.rs:111-125`)
   * precisely because it is the call made when nothing is attached yet.
   */
  discoverSources(): Promise<DiscoveryResult>;
  /** Initialise an EMPTY index at `indexPath`. Refuses to clobber an existing file. */
  createCorpus(indexPath: string): Promise<CreateCorpusResult>;
  /** START an ingest into the ATTACHED index; returns a handle, not a result. */
  corpusBuild(params: BuildParams): Promise<BuildHandle>;
  /** Poll an ingest. Safe before any build (answers `{state:"idle"}`). */
  corpusBuildStatus(jobId?: string): Promise<BuildStatus>;
  healthPing(): Promise<HealthInfo>;
  corpusStats(): Promise<CorpusStats>;
  graphRoots(params?: RootsParams): Promise<ThreadNode[]>;
  graphSubtree(threadId: string, depth?: number): Promise<Subtree>;
  // NOT BOUND (DECISION G-17): `graph.children` and `graph.ancestors`. The engine serves
  // both and Rust registers both commands, but no panel, view or `app.ts` path ever asked
  // for a node's direct children or its ancestor chain — the graph pane renders from
  // `graphRoots` + `graphSubtree`, and the reader walks `threadGet`. Declaring them here
  // obliged both adapters to carry a method nothing called. Re-add WITH the caller.
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

  // -- annotations / dedup / maintenance ------------------------------------------
  //
  // ELEVEN methods. REQUIRED, unlike the OPTIONAL Phase-3 block above. That block is
  // optional for one stated reason — `real.ts` did not implement it yet at the time — and
  // that reason does not apply here: both adapters land all eleven in the same change, so
  // requiring them costs nothing and buys the thing optionality throws away. A UI panel
  // calls `ipc.dedupScan(...)` with no optional-chaining dance, and an adapter that FORGOT
  // one fails at `tsc` instead of at first paint. The mock is typed {@link FullIpcClient},
  // so the compiler now forces the mock to keep pace with this interface.
  //
  // The engine registers 13 methods in these four groups; the two `research.*` ones are
  // deliberately absent — see the NOT BOUND note above.
  //
  // NAMING: RPC `a.b` -> Tauri command `a_b` -> this camelCase method. `dedup.scan` ->
  // `dedup_scan` -> `dedupScan`; `maintenance.restore` -> `maintenance_restore` ->
  // `maintenanceRestore`. A deviation is invisible to tsc AND to vitest (the mock never
  // names a command) and surfaces only when a real button is pressed.

  /** `metadata.get`: one conversation's annotation. Un-annotated answers `is_empty: true`. */
  metadataGet(conversationId: string): Promise<Annotation>;
  /** `metadata.set`: PARTIAL update — omitted leaves unchanged, `""`/`[]` clears. */
  metadataSet(params: MetadataSetParams): Promise<Annotation>;
  /** `metadata.clear`: drop the whole annotation. An absent row is a silent no-op. */
  metadataClear(conversationId: string): Promise<Annotation>;
  /** `metadata.search`: search ANNOTATIONS (never bodies). Filters ANDed; neither -> `[]`. */
  metadataSearch(params?: MetadataSearchParams): Promise<MetadataSearchRow[]>;
  /** `metadata.tags`: the tag facet, case-collapsed and deterministically ordered. */
  metadataTags(): Promise<TagCount[]>;

  /**
   * `dedup.scan`: scan the Codex stores under `codexHome`, consolidate, persist the view.
   *
   * `codexHome` is REQUIRED and must never be defaulted or guessed by the UI. That is a
   * safety choice, not ceremony: an automated probe really did read the owner's live Codex
   * sessions through a similar fallback (`llm_anthology/sidecar.py:1460-1465`). A scan of
   * private data has to be something the operator named.
   */
  dedupScan(codexHome: string): Promise<DedupScanResult>;
  /** `dedup.sessions`: the persisted dedup view. `[]` before any scan. */
  dedupSessions(): Promise<DedupSession[]>;

  /**
   * `maintenance.plan`: build a preview under a single-use handle. PURE — no filesystem
   * mutation. This is the ONLY way to obtain a `plan_id`.
   */
  maintenancePlan(params: MaintenancePlanParams): Promise<MaintenancePreview>;
  /**
   * `maintenance.execute`: run a handle the SERVER issued.
   *
   * THE ONLY DESTRUCTIVE CALL ON THIS INTERFACE. `apply` defaults to false, so the default
   * invocation is a dry run; applying additionally requires `confirmation` to equal the
   * preview's `required_typed_confirmation` verbatim.
   */
  maintenanceExecute(params: MaintenanceExecuteParams): Promise<MaintenanceResult>;
  /** `maintenance.restore`: roll a checkpoint back. `apply` defaults to false here too. */
  maintenanceRestore(params: MaintenanceRestoreParams): Promise<MaintenanceResult>;
  /** `maintenance.runs`: the applied-run audit ledger, newest first (engine default 50). */
  maintenanceRuns(limit?: number): Promise<MaintenanceRun[]>;
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
