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

import {
  RPC_INTERNAL_ERROR,
  RPC_INVALID_PARAMS,
  RPC_MAINTENANCE_REFUSED,
} from "./types";
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
  DiscoveryFinding,
  DiscoveryResult,
  ExportPlan,
  ExportResult,
  FullIpcClient,
  GraphSnapshot,
  HealthInfo,
  MaintenanceBlocked,
  MaintenanceCopy,
  MaintenanceExecuteParams,
  MaintenancePlanParams,
  MaintenancePreview,
  MaintenanceRestoreParams,
  MaintenanceResult,
  MaintenanceRun,
  MaintenanceWarning,
  MetadataSearchParams,
  MetadataSearchRow,
  MetadataSetParams,
  OpenCorpusResult,
  PlannedMove,
  RollupTable,
  RootsParams,
  SearchHit,
  SearchParams,
  SearchResult,
  SpawnEdge,
  Subtree,
  TagCount,
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

/**
 * The MODEL VENDOR a real store records for a given ADAPTER — the `model_provider` half of
 * the pair documented on `ThreadNode`. Derived rather than written onto all 15 fixture rows,
 * but deliberately realistic: a real Codex rollout records 'openai', never 'codex'.
 *
 * That realism is the point. It makes the mock reproduce the actual hazard — anything that
 * tints by `model_provider` collapses codex and chatgpt into one colour here too, rather
 * than looking correct against a fixture that conveniently stored the adapter name twice.
 * An unknown adapter (and a dangling node) yields "", the same as the engine.
 */
const VENDOR_OF_ADAPTER: Record<string, string> = {
  codex: "openai",
  chatgpt: "openai",
  claude: "anthropic",
  "claude-code": "anthropic",
  grok: "xai",
  gemini: "google",
};

function vendorOf(adapter: string | undefined): string {
  return adapter === undefined ? "" : VENDOR_OF_ADAPTER[adapter] ?? "";
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
 * (`llm_anthology/discover.py:108`). So this group is simultaneously the worst case for
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
 * on — `StoreSpec.report`, `"base"` | `"subdir"` — and that difference is the whole reason
 * both are here (`llm_anthology/discover.py:248`, applied at `:653`):
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
      // Nothing datable was seen. The engine reports 0.0, NOT null (`discover.py:654`),
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

// ---------------------------------------------------------------------------
// annotation / dedup / maintenance / research fixtures
// ---------------------------------------------------------------------------

/**
 * Seed annotations, keyed by conversation id (which is the thread id in this fixture, the
 * same identity `runSearch` and `conversationGet` assume).
 *
 * Chosen to EXERCISE the store's awkward rules rather than to look tidy:
 *
 *   * `orch` carries `Rust` and `ipc` carries `rust` — DIFFERENT casings on DIFFERENT
 *     conversations. `metadata.tags` collapses those case-insensitively into ONE facet
 *     entry with count 2, labelled with the lexicographically-first form
 *     (`llm_anthology/metadata.py:529-544`). A panel that grouped by the raw tag string
 *     would show two entries of count 1 here and look correct against any fixture that
 *     conveniently used one casing.
 *   * `ipc` has an EMPTY alias but non-empty tags, so `is_empty` is false for a row with
 *     nothing to show in an alias column.
 *   * `repro` has empty notes, so a notes column has a blank to render.
 *   * `search` is deliberately UNANNOTATED — `metadata.get` on it must read back an empty
 *     annotation rather than throw.
 */
const SEED_ANNOTATIONS: Array<[string, { alias: string; tags: string[]; notes: string }]> = [
  ["orch", { alias: "Cockpit orchestration", tags: ["Rust", "shipped"], notes: "the run that produced the IPC layer" }],
  ["ipc", { alias: "", tags: ["rust"], notes: "needle: revisit the mock fixture" }],
  ["repro", { alias: "Build failure repro", tags: ["triage"], notes: "" }],
];

/**
 * The dedup view a scan of a Codex home yields. WINDOWS paths, deliberately — like the
 * discovery fixture, these are local-filesystem paths on the only platform this ships on,
 * and a POSIX fixture would leave every separator split unexercised in preview.
 *
 * The four rows are the four cases a dedup panel has to survive:
 *
 *   1. a plain 2-copy collapse where the LIVE store is canonical;
 *   2. `has_larger_copy: true` — the canonical LIVE copy is SMALLER than the backup it
 *      demoted (a crash-truncated live rollout). The flag exists because the view would
 *      otherwise silently show the shorter conversation
 *      (`llm_anthology/dedup.py:149-164`); a panel that ignores it hides data the owner has;
 *   3. `is_identified: false` with `session_id: ""` — an unidentifiable file, kept as a
 *      path-keyed singleton and never merged;
 *   4. `last_write_ms: null` — `PhysicalCopy.last_write_ms` is `Optional[int]`
 *      (`dedup.py:118`), so a renderer that formats it unconditionally throws here.
 */
const MOCK_DEDUP_SESSIONS: DedupSession[] = [
  {
    session_id: "018f3a2c-0000-7c1e-9a01-aaaaaaaaaaaa",
    canonical_path: `${HOME}\\.codex\\sessions\\2026\\08\\01\\rollout-018f3a2c.jsonl`,
    store_kind: "live",
    size_bytes: 412_880,
    last_write_ms: T0 + 300_000,
    copy_count: 2,
    duplicate_paths: [
      `${HOME}\\.codex\\sessions_backup\\2026\\08\\01\\rollout-018f3a2c.jsonl`,
    ],
    is_identified: true,
    has_larger_copy: false,
  },
  {
    session_id: "018f3a2c-0000-7c1e-9a02-bbbbbbbbbbbb",
    canonical_path: `${HOME}\\.codex\\sessions\\2026\\08\\02\\rollout-018f3a2d.jsonl`,
    // The LIVE copy is the canonical one AND the smaller one: store rank outranks size, so
    // a truncated live rollout legitimately beats a complete backup. Reported, not resolved.
    store_kind: "live",
    size_bytes: 1_204,
    last_write_ms: T0 + 320_000,
    copy_count: 2,
    duplicate_paths: [
      `${HOME}\\.codex\\sessions_backup\\2026\\08\\02\\rollout-018f3a2d.jsonl`,
    ],
    is_identified: true,
    has_larger_copy: true,
  },
  {
    session_id: "",
    canonical_path: `${HOME}\\.codex\\sessions\\2026\\07\\30\\rollout-unnamed.jsonl`,
    store_kind: "mirror",
    size_bytes: 88_140,
    last_write_ms: T0 - 86_400_000,
    copy_count: 1,
    duplicate_paths: [],
    is_identified: false,
    has_larger_copy: false,
  },
  {
    session_id: "018f3a2c-0000-7c1e-9a04-dddddddddddd",
    canonical_path: `${HOME}\\.codex\\archive\\rollout-018f3a2f.jsonl`,
    store_kind: "unknown",
    size_bytes: 0,
    // Nothing datable was recorded for this copy.
    last_write_ms: null,
    copy_count: 1,
    duplicate_paths: [],
    is_identified: true,
    has_larger_copy: false,
  },
];

/**
 * The scan errors the fixture store reports.
 *
 * ONE entry, ALWAYS — the fixture models a store holding one rollout whose last line is torn,
 * which is exactly what a real scan reports: `scan_store` passes
 * `codex_rollout.ingest_sessions`' errors through verbatim and skips-and-logs a torn last line
 * (`llm_anthology/dedup.py:244-252`). The copy is lost from the tally, not the whole scan.
 *
 * WHY ALWAYS, and the trade-off. The mock has no filesystem, so it cannot DETECT an unreadable
 * file — the choice is between never populating `errors` and always populating it. Never was
 * the status quo and it left the panel's error list dead in every dev run, screenshot and
 * review while the engine could produce one. Always is the same call already made for
 * `has_larger_copy: true` and `last_write_ms: null` in the fixture above: prefer the value
 * that EXPOSES a rendering mistake over the one that flatters the code. The cost is real and
 * worth stating — the mock no longer exercises the zero-errors path. That is the degenerate
 * case (a panel that renders one error renders none correctly; the reverse does not hold), and
 * the engine still covers it against a clean store.
 *
 * NOTE this is a partial-parse error, NOT a missing store: a missing root is explicitly "an
 * empty result, not an error" (`dedup.py:249-250`), so a UI must not present this as "store not
 * found".
 */
const MOCK_DEDUP_ERRORS = [
  `${HOME}\\.codex\\sessions\\2026\\07\\29\\rollout-torn.jsonl: skipped 1 torn line at EOF`,
];

/** The scan tally implied by {@link MOCK_DEDUP_SESSIONS}, derived so the two cannot drift. */
function dedupTally(sessions: DedupSession[], errors: string[]): DedupScanResult {
  let copies = 0;
  let truncated = 0;
  let unidentified = 0;
  for (const s of sessions) {
    copies += s.copy_count;
    if (s.has_larger_copy) truncated += 1;
    if (!s.is_identified) unidentified += 1;
  }
  return {
    session_count: sessions.length,
    copy_count: copies,
    duplicate_count: copies - sessions.length,
    flagged_truncated: truncated,
    unidentified,
    errors,
  };
}

/**
 * The engine's tag canonicalisation (`llm_anthology/metadata.py:214-240`): collapse every
 * whitespace RUN to a single space (which trims, drops blanks and neutralises an embedded
 * newline — the wire separator — in one step), dedup CASE-INSENSITIVELY keeping the
 * FIRST-SEEN casing, then order by (casefold, exact string) so two callers who add the same
 * tags in a different order store identical bytes.
 */
function cleanTags(tags: string[]): string[] {
  const seen = new Map<string, string>();
  for (const raw of tags) {
    const tag = raw.split(/\s+/).filter((p) => p !== "").join(" ");
    if (tag === "") continue;
    const key = tag.toLowerCase();
    if (!seen.has(key)) seen.set(key, tag);
  }
  return [...seen.entries()]
    .sort(([ka, va], [kb, vb]) =>
      ka < kb ? -1 : ka > kb ? 1 : va < vb ? -1 : va > vb ? 1 : 0)
    .map(([, v]) => v);
}

/** Collapse whitespace runs in a free-text field, as `metadata.clean_text` does. */
function cleanText(value: string): string {
  return value.split(/\s+/).filter((p) => p !== "").join(" ");
}

/**
 * An error shaped like the one a REAL failure arrives as, so `rpcErrorCode` finds a code
 * here too.
 *
 * The mock has no JSON-RPC channel, so without this its rejections carry no code and a
 * panel's `rpcErrorCode(err) === RPC_MAINTENANCE_REFUSED` branch would be DEAD in every dev
 * run, preview and design review — the exact invisible-dead-path class `ipc/index.ts`
 * documents. The wire text is reproduced literally: the Rust bridge flattens the envelope
 * with `format!("rpc error (id {id}): {err}")` (`cockpit/src-tauri/src/sidecar.rs:161`) over
 * the sidecar's `{code, message}` (`llm_anthology/sidecar.py:299-303`). The id is a
 * fixed constant; no caller parses it (rpcErrorCode matches on the code, not the id).
 */
function rpcError(code: number, message: string): Error {
  return new Error(
    `rpc error (id 0): {"code":${code},"message":${JSON.stringify(message)}}`,
  );
}

/**
 * The mock's stand-in for the engine's `_reject_nonlocal_path`: refuse a UNC path and
 * anything that is not drive-absolute.
 *
 * Worth reproducing rather than skipping, because the reason is not cosmetic — merely
 * RESOLVING `\\host\share` on Windows initiates an outbound SMB/NTLM authentication, which
 * this offline-only tool forbids. A mock that accepted a UNC root would let a path-handling
 * bug reach the engine untested. Note that `C:/store/../evil` passes here exactly as it
 * passes the real edge: parent-traversal is the ENGINE's refusal, not the edge's
 * (`tests/test_sidecar_maintenance.py:103-119`).
 */
function requireLocalPath(value: string, label: string): void {
  if (value.startsWith("\\\\") || value.startsWith("//")) {
    throw rpcError(RPC_INVALID_PARAMS, `${label} must not be a UNC path: ${value}`);
  }
  if (!/^[A-Za-z]:[\\/]/.test(value)) {
    throw rpcError(RPC_INVALID_PARAMS, `${label} must be an absolute local path: ${value}`);
  }
}

/** Last path segment, either separator — the mock's `os.path.basename`. */
function winBasename(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] ?? "";
}

/** Join with a backslash, not doubling an existing trailing separator. */
function winJoin(...parts: string[]): string {
  return parts
    .filter((p) => p !== "")
    .map((p, i) => (i === 0 ? p.replace(/[\\/]+$/, "") : p.replace(/^[\\/]+|[\\/]+$/g, "")))
    .join("\\");
}

/** Case-insensitive path containment, the mock's `os.path.normcase` comparison. */
function normPath(path: string): string {
  return path.replace(/\//g, "\\").toLowerCase();
}

/** The engine's protected-store markers, verbatim (`llm_anthology/maintenance.py:295-299`). */
const PROTECTED_PATH_MARKERS = [
  "\\.codex\\sessions\\",
  "\\.codex\\state_5.sqlite",
  "\\.codex\\codex-sqlite\\",
];

/**
 * Does this path name the LIVE Codex store? A port of the engine's `_spells_protected`
 * (`llm_anthology/maintenance.py:384-406`): separators unified, lowercased, trailing
 * separators stripped and ONE appended, so a marker matches the directory it names and not
 * merely a prefix of a longer name (`...\sessionsfoo` must not match `\sessions\`).
 *
 * WHY THE MOCK NEEDS THIS AT ALL. `protected` is the live-store guard — the most important
 * reason a maintenance target is ever refused — and a mock that never produces it left the
 * panel's `protected` rendering path unexercised while showing an UNDELETABLE file as
 * deletable. Measured against the engine before this existed: engine blocked
 * `[duplicate-target, protected]` where the mock blocked only `[duplicate-target]`, and the
 * two sides demanded different confirmation phrases ("DELETE 1 FILE" vs "DELETE 2 FILES").
 *
 * DELIBERATE, DOCUMENTED GAP: the engine tests the literal string AND
 * `os.path.realpath(path)`, because the marker test is a substring match and
 * `\.codex\.\sessions\`, a doubled separator, an 8.3 short name or a directory junction each
 * name the protected file while matching no marker. Only the LITERAL half is portable here —
 * the mock has no filesystem to resolve — so a mock plan can ALLOW an obfuscated spelling
 * that the engine refuses. That direction is the safe one (the engine still refuses at
 * runtime), but it means this is not a validator: never treat a mock `allowed` as proof a
 * path is safe.
 */
function spellsProtected(filePath: string): boolean {
  const normalized =
    filePath.replace(/\//g, "\\").toLowerCase().replace(/\\+$/, "") + "\\";
  return PROTECTED_PATH_MARKERS.some((marker) => normalized.includes(marker));
}

/**
 * The effective destination root (`llm_anthology/maintenance.py:452-461`): a `delete`
 * quarantines under `<checkpoint_root>/deleted` (which is what makes a delete recoverable)
 * and a `reconcile` under `<destination_root>/reconciled`; `archive` and `move` go to the
 * requested destination as-is.
 */
function effectiveRoot(
  action: string,
  checkpointRoot: string,
  destinationRoot: string,
): string {
  if (action === "delete") return winJoin(checkpointRoot, "deleted");
  if (action === "reconcile") return winJoin(destinationRoot, "reconciled");
  return destinationRoot;
}

/** The phrase the operator must type (`llm_anthology/maintenance.py:463-470`), e.g.
 *  `"DELETE 2 FILES"` / `"ARCHIVE 1 FILE"`. A function of the ALLOWED count, never of what
 *  the caller offered, so a plan that changed changes the phrase too. */
function confirmationPhrase(action: string, allowedCount: number): string {
  return `${action.toUpperCase()} ${allowedCount} ${allowedCount === 1 ? "FILE" : "FILES"}`;
}

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
      model_provider: vendorOf(raw?.provider),
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
      model_provider: vendorOf(raw.provider),
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
  /**
   * The in-flight/last mock ingest, or null before any `corpusBuild`.
   *
   * `sessionsRoot` is REMEMBERED rather than re-derived because `build_status` reports the
   * root the job was started with (`llm_anthology/sidecar.py:1018` reads it off the job
   * snapshot). A status that invented its own path would let a panel read the ingest source
   * off the poll and show somewhere the build never touched.
   */
  // All three roots are held SEPARATELY rather than collapsed into one field: the engine
  // reports each as itself, and a single `sessionsRoot` is what let a Grok path be echoed
  // as the Codex tree.
  let buildJob:
    | { id: string; polls: number; sessionsRoot: string; grokRoot: string; claudeRoot: string }
    | null = null;

  // -- annotation / dedup / maintenance state -------------------------------------
  //
  // Per-CLIENT, like `lastOpenedIndex` above, so `createMockIpc()` in a test starts from a
  // clean store and one test's writes cannot leak into the next.

  /** conversation_id -> the stored annotation fields (already canonicalised). */
  const annotations = new Map<string, { alias: string; tags: string[]; notes: string }>(
    SEED_ANNOTATIONS.map(([cid, a]) => [
      cid,
      { alias: cleanText(a.alias), tags: cleanTags(a.tags), notes: cleanText(a.notes) },
    ]),
  );
  /** Whether `dedup.scan` has run — `dedup.sessions` is empty until it has. */
  let dedupScanned = false;
  /** Live single-use `maintenance.plan` handles. */
  const plans = new Map<string, MaintenancePreview>();
  let nextPlanId = 1;
  /** manifest_path -> the moves it recorded, for `maintenance.restore`. */
  const manifests = new Map<string, { moves: PlannedMove[]; restored: boolean }>();
  let nextManifest = 1;
  /** The applied-run audit ledger, newest LAST here and reversed on read. */
  const runs: MaintenanceRun[] = [];

  /** The wire annotation for `cid`; an unknown id is an EMPTY annotation, never an error. */
  function annotationOf(cid: string): Annotation {
    const stored = annotations.get(cid);
    const alias = stored?.alias ?? "";
    const tags = stored?.tags ?? [];
    const notes = stored?.notes ?? "";
    return {
      conversation_id: cid,
      alias,
      tags: [...tags],
      notes,
      is_empty: alias === "" && tags.length === 0 && notes === "",
    };
  }

  /**
   * The engine's `_req_str` + `metadata._check_id` guard: an empty or whitespace-only id is
   * a param error (`tests/test_sidecar_metadata.py:124-128`), not a silent no-op on a key
   * that could never be stored.
   */
  function requireConversationId(cid: string): string {
    if (typeof cid !== "string" || cid.trim() === "") {
      throw rpcError(RPC_INVALID_PARAMS, "conversation_id must be a non-empty string");
    }
    return cid;
  }

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
    // Newest first, id breaking ties — the engine's stated contract
    // (`ORDER BY c.created_at DESC, c.conversation_id`). The tiebreak matters: it is what
    // makes the order TOTAL, so LIMIT/OFFSET paging partitions the result set instead of
    // depending on sort stability. The engine used to say `ORDER BY rank`, which sorts by
    // nothing under a `detail=none` contentless index; this mock was already right.
    matches.sort((a, b) =>
      b.created_at_ms - a.created_at_ms || a.id.localeCompare(b.id));
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
     * anything in place cannot corrupt the fixture for the next scan — the panel does
     * sort, and a shared mutable fixture is how a "why did the order change on rescan?"
     * bug gets born.
     *
     * "Deep-ish" has to include the arrays INSIDE `stats`. This was
     * `stats: { ...DISCOVERY.stats }`, a shallow spread, so `errors` and
     * `truncated_groups` — both arrays, both rendered by the panel — stayed shared by
     * reference while the docstring above claimed otherwise. The promise held for
     * `findings` and failed for exactly the two fields most likely to be sorted for
     * display. Cross-test contamination is the worst shape for a fixture: one spec
     * mutating the error list changes what a later spec sees, so the failure surfaces
     * somewhere unrelated and depends on execution order.
     */
    async discoverSources(): Promise<DiscoveryResult> {
      return {
        findings: DISCOVERY.findings.map((f) => ({ ...f, detail: { ...f.detail } })),
        stats: {
          ...DISCOVERY.stats,
          truncated_groups: [...DISCOVERY.stats.truncated_groups],
          errors: [...DISCOVERY.stats.errors],
        },
      };
    },

    /**
     * Record the requested path and report it created. NO filesystem write, and — unlike
     * the engine — no clobber check, for the same reason `openCorpus` is not a gate here:
     * this adapter serves every non-Tauri environment, where there is no real index to
     * collide with.
     *
     * The PATH GUARD is reproduced, though, because it is not a filesystem question. The
     * engine rejects a UNC or relative `index_path` before touching the disk
     * (`_reject_nonlocal_path`, `llm_anthology/sidecar.py:781`), and a mock that accepted
     * `\\host\share\x.db` would let a UI offer a network destination that every dev run
     * blesses and the engine then refuses. The clobber check (`CORPUS_EXISTS`) and the
     * parent-directory check stay absent — those genuinely need a filesystem.
     */
    async createCorpus(indexPath: string): Promise<CreateCorpusResult> {
      requireLocalPath(indexPath, "index_path");
      return { index_path: indexPath, created: true };
    },

    /**
     * Accept an ingest and hand back a handle. `state` is always `"running"`: the reply
     * reports that the job was ACCEPTED, not that it finished (`sidecar.py:834-836`).
     *
     * The PARAMS CHECKS are the engine's, because they need no filesystem. Every source is
     * opt-in by naming its root and at least one must be named (`sidecar.py:869-872`), and
     * each named root is refused if UNC or relative (`sidecar.py:878-885`). What is NOT
     * reproduced is the engine's `os.path.isdir` on each root — that one is a real disk
     * question this adapter cannot answer.
     */
    async corpusBuild(params: BuildParams): Promise<BuildHandle> {
      const sessionsRoot = params.sessions_root ?? "";
      const grokRoot = params.grok_root ?? "";
      const claudeRoot = params.claude_root ?? "";
      if (sessionsRoot === "" && grokRoot === "" && claudeRoot === "") {
        throw rpcError(
          RPC_INVALID_PARAMS,
          "name at least one source: sessions_root (Codex), grok_root (Grok Build) " +
            "and/or claude_root (Claude Code)",
        );
      }
      if (sessionsRoot !== "") requireLocalPath(sessionsRoot, "sessions_root");
      if (grokRoot !== "") requireLocalPath(grokRoot, "grok_root");
      if (claudeRoot !== "") requireLocalPath(claudeRoot, "claude_root");
      // Each root is echoed AS ITSELF. This used to collapse all three into
      // `sessions_root` (`sessionsRoot || grokRoot || claudeRoot`) on the reasoning that a
      // Grok-only build "carries no sessions_root at all, so falling through mirrors the
      // engine". MEASURED against the engine, that is backwards: a Grok-only
      // `corpus.build` returns `sessions_root: ''` AND `grok_root: <path>` — all three
      // keys, `_clean`ed, with the unnamed ones empty. So the mock was reporting a Grok
      // path in the field that means "the Codex tree", and any UI reading `sessions_root`
      // to say what it imported said the wrong thing in dev and the right thing only
      // against the real engine.
      //
      // `claude_root` is carried for the same reason the other five divergences in this
      // file were fixed: a mock that accepts or reports a narrower shape than the engine
      // makes the real contract untestable here. The empty string means ABSENT rather
      // than invalid, matching the engine exactly — it falls through to the refusal above
      // instead of being validated.
      buildJob = { id: "mock-build-1", polls: 0, sessionsRoot, grokRoot, claudeRoot };
      return {
        job_id: buildJob.id,
        state: "running",
        sessions_root: sessionsRoot,
        grok_root: grokRoot,
        claude_root: claudeRoot,
        started_ms: T0,
      };
    },

    /**
     * Poll the mock ingest: `running` with a climbing `indexed_conversations` for
     * {@link MOCK_BUILD_POLLS} polls, then `done`. Reaching a terminal state is the point —
     * a mock that stayed `running` forever would make a poll loop that never stops look
     * correct.
     *
     * An UNNAMED poll before any build reads back `idle` rather than erroring, so the UI can
     * render unconditionally (`llm_anthology/sidecar.py:1013`). A poll that NAMES a `job_id`
     * is the opposite case and both engine branches are reproduced: naming a job when none
     * has started (`sidecar.py:1009-1012`), or naming one that is not the job in flight
     * (`sidecar.py:1014-1016`), is -32602. The whole point of the optional argument is to let a
     * client prove it is reading the job it started, which a mock that ignored it would
     * quietly defeat — a stale handle after a restart would read "idle" here and take a param
     * error from the engine.
     */
    async corpusBuildStatus(jobId?: string): Promise<BuildStatus> {
      if (buildJob === null) {
        if (jobId !== undefined) {
          throw rpcError(
            RPC_INVALID_PARAMS,
            `unknown job_id '${jobId}': no build has been started`,
          );
        }
        return { state: "idle", indexed_conversations: 0, errors: [] };
      }
      if (jobId !== undefined && jobId !== buildJob.id) {
        throw rpcError(
          RPC_INVALID_PARAMS,
          `unknown job_id '${jobId}'; the current job is '${buildJob.id}'`,
        );
      }
      buildJob.polls += 1;
      const running = buildJob.polls < MOCK_BUILD_POLLS;
      return {
        job_id: buildJob.id,
        state: running ? "running" : "done",
        sessions_root: buildJob.sessionsRoot,
        // Unlike the start reply, STATUS omits a root that was not named — measured, and
        // it is the module's stated convention that optional fields are the ones dropped
        // when falsy. The asymmetry is the engine's, not an invention here.
        ...(buildJob.grokRoot ? { grok_root: buildJob.grokRoot } : {}),
        ...(buildJob.claudeRoot ? { claude_root: buildJob.claudeRoot } : {}),
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

    // NO `graphChildren` / `graphAncestors` HERE — DECISION G-17 removed both from the
    // contract, because nothing in the app ever called either.
    //
    // `MockGraph.childrenOf` and `.collectAncestors` both STAY, for different reasons.
    // `childrenOf` has real internal callers (`fanOut`, `collectSubtree`, `rollup`).
    // `collectAncestors` is now reached only from `mock.test.ts`, and is kept DELIBERATELY:
    // it is the only implementation of the shared-ancestor BFS dedup, whose property (a
    // node above two parents is listed ONCE, nearest first — engine parity with
    // `_collect_ancestors`) is a real mock-fidelity check. That test calls it directly now,
    // the same way the `childrenOf`/`depth`/`fanOut` fixture test already does.
    async graphSubtree(threadId: string, depth?: number): Promise<Subtree> {
      const ids = graph.collectSubtree(threadId, depth);
      const idset = new Set(ids);
      return {
        nodes: ids.map((id) => graph.node(id)),
        edges: graph.edgesWithin(idset),
      };
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

    // NO `research.*` HERE, DELIBERATELY. A canned non-empty summary would be the worst
    // possible mock: the shipped app returns `""` from `MockBackend` by construction
    // (`llm_anthology/sidecar.py:587-590`, `research.py:88-97`), so a mock that invented prose
    // would make a research panel look FINISHED in every dev run and screenshot while being
    // permanently blank for the user. See the NOT BOUND note in `./types.ts`.

    // -- annotations ---------------------------------------------------------------

    async metadataGet(conversationId: string): Promise<Annotation> {
      return annotationOf(requireConversationId(conversationId));
    },

    /**
     * PARTIAL update, with the tri-state the engine defines: an OMITTED field is left
     * unchanged, an explicit `""` / `[]` clears it (`llm_anthology/sidecar.py:1366-1368`).
     * Getting this wrong here would make a per-field editor look correct in preview while
     * blanking the other two fields against the real engine.
     */
    async metadataSet(params: MetadataSetParams): Promise<Annotation> {
      const cid = requireConversationId(params.conversation_id);
      const current = annotations.get(cid) ?? { alias: "", tags: [], notes: "" };
      annotations.set(cid, {
        alias: params.alias === undefined ? current.alias : cleanText(params.alias),
        tags: params.tags === undefined ? current.tags : cleanTags(params.tags),
        notes: params.notes === undefined ? current.notes : cleanText(params.notes),
      });
      return annotationOf(cid);
    },

    /** Drop the whole annotation. An absent row is a silent no-op that still reads back as
     *  an empty annotation, mirroring the engine. */
    async metadataClear(conversationId: string): Promise<Annotation> {
      const cid = requireConversationId(conversationId);
      annotations.delete(cid);
      return annotationOf(cid);
    },

    /**
     * Search ANNOTATIONS, never message bodies. Three engine rules are reproduced because
     * each is separately easy to get wrong in a panel:
     *
     *   * with NEITHER filter the result is EMPTY — a blank query must not dump the
     *     catalogue (`llm_anthology/metadata.py:501-502`);
     *   * `tag` is a WHOLE-TAG exact match (the engine probes a sentinel-delimited
     *     `\ntag\n` needle, never a LIKE — `metadata.py:243-269`, `:456-463`), so `"rus"`
     *     does NOT find `"rust"`;
     *   * `text` IS a substring match, over the alias + tags + notes key (`metadata.py:470-480`).
     *
     * It is an INNER JOIN, so an annotation whose conversation is not in the index does not
     * appear; ordered by conversation_id.
     */
    async metadataSearch(params: MetadataSearchParams = {}): Promise<MetadataSearchRow[]> {
      const tagNeedle = cleanTags([params.tag ?? ""])[0]?.toLowerCase() ?? "";
      const textNeedle = cleanText(params.text ?? "").toLowerCase();
      if (tagNeedle === "" && textNeedle === "") return [];

      const rows: MetadataSearchRow[] = [];
      for (const cid of [...annotations.keys()].sort()) {
        const annotation = annotationOf(cid);
        if (tagNeedle !== "" && !annotation.tags.some((t) => t.toLowerCase() === tagNeedle)) {
          continue;
        }
        const searchKey = [annotation.alias, ...annotation.tags, annotation.notes]
          .join("\n")
          .toLowerCase();
        if (textNeedle !== "" && !searchKey.includes(textNeedle)) continue;
        const raw = graph.rawThread(cid);
        if (raw === undefined) continue; // INNER JOIN: no indexed conversation, no row
        const iso = new Date(raw.created_at_ms).toISOString();
        rows.push({
          conversation_id: cid,
          provider: raw.provider,
          // `conversations.account` is NOT NULL DEFAULT '' (`llm_anthology/corpus.py:183`),
          // so an unknown account is the EMPTY STRING on this surface — note that
          // `conversationGet` above reports the same fact as `null`, because that DTO's
          // field really is nullable. Two shapes, one underlying blank.
          account: "",
          title: raw.title,
          created_at: iso,
          updated_at: raw.updated_at_ms === undefined
            ? iso
            : new Date(raw.updated_at_ms).toISOString(),
          turn_count: raw.turn_count ?? 0,
          // The fixture's conversation id doubles as its thread id, as elsewhere in this mock.
          thread_id: raw.id,
          annotation,
        });
      }
      return rows;
    },

    /** The tag facet, case-collapsed with the lexicographically-first display form and
     *  ordered by the casefolded tag (`llm_anthology/metadata.py:529-544`). */
    async metadataTags(): Promise<TagCount[]> {
      const counts = new Map<string, number>();
      const display = new Map<string, string>();
      for (const cid of [...annotations.keys()].sort()) {
        for (const tag of annotations.get(cid)?.tags ?? []) {
          const key = tag.toLowerCase();
          counts.set(key, (counts.get(key) ?? 0) + 1);
          const shown = display.get(key);
          if (shown === undefined || tag < shown) display.set(key, tag);
        }
      }
      return [...counts.keys()]
        .sort()
        .map((key) => ({ tag: display.get(key) as string, count: counts.get(key) as number }));
    },

    // -- dedup ---------------------------------------------------------------------

    /**
     * Serve the fixture view and mark it scanned. NO filesystem access.
     *
     * `codexHome` is still VALIDATED (non-empty, local, non-UNC) rather than ignored: the
     * argument being required is a safety property of this call — an automated probe once
     * read the owner's live Codex store through a defaulted home
     * (`llm_anthology/sidecar.py:1460-1452`) — so a UI that forgot to collect it must fail
     * here too, not only against the engine.
     *
     * NOT REPRODUCIBLE HERE: the engine's "missing home -> empty result, not an error"
     * behaviour (`tests/test_sidecar_dedup.py:101-107`). The mock has no filesystem to
     * miss, so any accepted path yields the fixture.
     */
    async dedupScan(codexHome: string): Promise<DedupScanResult> {
      if (typeof codexHome !== "string" || codexHome === "") {
        throw rpcError(RPC_INVALID_PARAMS, "codex_home must be a non-empty string");
      }
      requireLocalPath(codexHome, "codex_home");
      dedupScanned = true;
      return dedupTally(MOCK_DEDUP_SESSIONS, [...MOCK_DEDUP_ERRORS]);
    },

    /**
     * The persisted view — EMPTY until a scan has run, exactly as the engine answers before
     * any `dedup.scan` (`tests/test_sidecar_dedup.py:170-171`).
     *
     * This one IS gated, unlike `openCorpus` above, and the difference is not an
     * inconsistency: `openCorpus` cannot be gated because attaching needs the native file
     * picker that only exists inside Tauri, so a gate there would strand every preview in
     * the empty state. `dedupScan` takes a plain string, so any environment can leave this
     * empty state in one call — which makes the honest pre-scan state affordable, and a
     * dedup panel needs that empty state anyway.
     */
    async dedupSessions(): Promise<DedupSession[]> {
      if (!dedupScanned) return [];
      // Fresh copies: the fixture is module-scoped and a panel that sorts in place would
      // otherwise reorder it for every later caller.
      return MOCK_DEDUP_SESSIONS.map((s) => ({ ...s, duplicate_paths: [...s.duplicate_paths] }));
    },

    // -- maintenance ---------------------------------------------------------------
    //
    // DESTRUCTIVE FOR REAL, INERT HERE. The engine's `maintenance.*` moves and deletes the
    // owner's session files; this mock performs NO filesystem operation of any kind — no
    // read, no write, no mkdir, no delete. Every plan, manifest and ledger row below lives
    // in the per-client maps above and disappears with the process. That is what makes it
    // safe to drive a Maintenance panel in a dev run, a screenshot pass or a design review.
    //
    // What it DOES reproduce faithfully is the gating, because the gates are the part a UI
    // gets wrong: the single-use handle, the typed-confirmation phrase, apply-defaults-to-
    // false, and the fact that a REFUSED confirmation leaves the handle usable while an
    // ACCEPTED run consumes it.

    async maintenancePlan(params: MaintenancePlanParams): Promise<MaintenancePreview> {
      const storeRoot = params.store_root;
      const checkpointRoot = params.checkpoint_root;
      for (const [label, value] of [
        ["store_root", storeRoot],
        ["checkpoint_root", checkpointRoot],
      ] as const) {
        if (typeof value !== "string" || value === "") {
          throw rpcError(RPC_INVALID_PARAMS, `${label} must be a non-empty string`);
        }
        requireLocalPath(value, label);
      }
      const destinationRoot = params.destination_root ?? "";
      if (destinationRoot !== "") requireLocalPath(destinationRoot, "destination_root");
      if (!["delete", "archive", "move", "reconcile"].includes(params.action)) {
        throw rpcError(RPC_INVALID_PARAMS, "action must be one of delete, archive, move, reconcile");
      }
      if (!Array.isArray(params.targets) || params.targets.length === 0) {
        throw rpcError(RPC_INVALID_PARAMS, "targets must be a non-empty list");
      }

      const root = effectiveRoot(params.action, checkpointRoot, destinationRoot);
      const allowed: MaintenanceCopy[] = [];
      const blocked: MaintenanceBlocked[] = [];
      const warnings: MaintenanceWarning[] = [];
      const plan: PlannedMove[] = [];
      const plannedSources = new Set<string>();
      const claimed = new Set<string>();

      for (const target of params.targets) {
        if (typeof target?.file_path !== "string" || target.file_path === "") {
          throw rpcError(RPC_INVALID_PARAMS, "each target needs a non-empty file_path");
        }
        // Every RPC-built target is forced to UNKNOWN — the client cannot assert a store
        // kind (`llm_anthology/sidecar.py:1587`).
        const copy: MaintenanceCopy = {
          session_id: target.session_id ?? "",
          file_path: target.file_path,
          store_kind: "unknown",
          last_write_ms: null,
          size_bytes: target.size_bytes ?? 0,
          is_hot: false,
        };
        const key = normPath(target.file_path);

        // `outside-store-root`: the LEXICAL half of the engine's `_classify`
        // (`llm_anthology/maintenance.py:345-347`). The realpath/symlink half needs a
        // filesystem and is deliberately absent here.
        if (!key.startsWith(normPath(storeRoot).replace(/\\+$/, "") + "\\")) {
          blocked.push({
            target: copy,
            reason: "outside-store-root",
            detail: `target '${target.file_path}' is not within the store root '${storeRoot}'`,
          });
          warnings.push({
            severity: 2,
            severity_name: "DANGEROUS",
            message: `Protected path blocked: ${target.file_path} (outside-store-root)`,
          });
          continue;
        }
        // `protected`: the LIVE-STORE guard. Checked BEFORE the duplicate rule and in this
        // order deliberately — the engine checks duplicate LAST so that a target which is
        // both a duplicate AND protected is reported as `protected`, "the more dangerous
        // reason ... worth recording" (`llm_anthology/maintenance.py:536-537`). Reversing
        // these two would mislabel exactly the worst case.
        if (spellsProtected(target.file_path)) {
          blocked.push({
            target: copy,
            reason: "protected",
            detail: `protected store path: '${target.file_path}'`,
          });
          // Not the `duplicate-target` REVIEW branch: every other block reason raises
          // DANGEROUS with this same wording (`maintenance.py:549-551`).
          warnings.push({
            severity: 2,
            severity_name: "DANGEROUS",
            message: `Protected path blocked: ${target.file_path} (protected)`,
          });
          continue;
        }
        // `duplicate-target`: two entries naming ONE physical file. The likeliest real UI
        // bug on this surface — feeding `dedup.sessions`' canonical AND duplicate paths
        // straight into a plan does exactly this — and the engine blocks the second rather
        // than planning one file twice (`maintenance.py:529-540`).
        if (plannedSources.has(key)) {
          blocked.push({
            target: copy,
            reason: "duplicate-target",
            detail:
              `target '${target.file_path}' is already planned by an earlier entry; ` +
              "one physical file is moved once",
          });
          warnings.push({
            severity: 1,
            severity_name: "REVIEW",
            message: `Duplicate target ignored: ${target.file_path}`,
          });
          continue;
        }

        plannedSources.add(key);
        // `size_bytes` on an ALLOWED target is MEASURED FROM DISK by the engine, never taken
        // from the caller (`llm_anthology/maintenance.py:419-449`, `_measured_size`) — the
        // maintenance panel sums it onto the confirm screen, so a client-dictated figure was
        // the one number on the dialog authorising a delete that a caller could inflate. The
        // mock has no filesystem to measure, so it reports the engine's OWN fallback for an
        // unmeasurable path: 0. Echoing the caller's value here would let the mock reproduce
        // exactly the trust the engine just removed, and a panel built against it would look
        // correct while displaying an attacker-chosen total.
        //
        // A BLOCKED target keeps the caller's value, because the engine deliberately leaves a
        // refused target unmeasured rather than stat-ing a path it declined.
        allowed.push({ ...copy, size_bytes: 0 });
        // EVERY allowed target raises a DANGEROUS warning (`maintenance.py:558-560`), so a
        // perfectly healthy plan is never warning-free.
        warnings.push({
          severity: 2,
          severity_name: "DANGEROUS",
          message: `Dangerous maintenance target: ${target.file_path}`,
        });
        // A deterministic `-N` suffix, not a GUID, so the destination can be SHOWN in the
        // preview and verified later (`maintenance.py:472-486`).
        const base = winBasename(target.file_path);
        const dot = base.lastIndexOf(".");
        const stem = dot > 0 ? base.slice(0, dot) : base;
        const ext = dot > 0 ? base.slice(dot) : "";
        let candidate = base;
        for (let n = 2; claimed.has(normPath(candidate)); n += 1) {
          candidate = `${stem}-${n}${ext}`;
        }
        claimed.add(normPath(candidate));
        plan.push({
          session_id: copy.session_id,
          source: target.file_path,
          destination: winJoin(root, candidate),
        });
      }

      warnings.push({
        severity: 0,
        severity_name: "INFO",
        message:
          `${params.action} preview: ${allowed.length} allowed, ${blocked.length} blocked; ` +
          "a checkpoint and a typed confirmation are required",
      });

      const planId = `plan-${nextPlanId}`;
      nextPlanId += 1;
      const preview: MaintenancePreview = {
        plan_id: planId,
        action: params.action,
        store_root: storeRoot,
        // The EFFECTIVE root, not the requested one — a delete reports
        // `<checkpoint_root>\deleted` (`maintenance.py:457`).
        destination_root: root,
        checkpoint_root: checkpointRoot,
        allowed,
        blocked,
        warnings,
        plan,
        requires_checkpoint: true,
        requires_typed_confirmation: true,
        required_typed_confirmation: confirmationPhrase(params.action, allowed.length),
      };
      plans.set(planId, preview);
      return preview;
    },

    async maintenanceExecute(params: MaintenanceExecuteParams): Promise<MaintenanceResult> {
      const planId = params.plan_id;
      if (typeof planId !== "string" || planId === "") {
        throw rpcError(RPC_INVALID_PARAMS, "plan_id must be a non-empty string");
      }
      const preview = plans.get(planId);
      if (preview === undefined) {
        throw rpcError(RPC_MAINTENANCE_REFUSED, `unknown or already-used plan_id '${planId}'; re-plan`);
      }
      const confirmation = params.confirmation ?? "";
      const apply = params.apply ?? false;
      // The confirmation is checked BEFORE the apply branch, so a DRY RUN needs the phrase
      // too (`llm_anthology/maintenance.py:645-648` runs ahead of `:669`). A panel that offers a
      // preview button without collecting the phrase is refused, not answered.
      if (confirmation.trim() === "") throw rpcError(RPC_MAINTENANCE_REFUSED, "Typed confirmation is required.");
      if (confirmation !== preview.required_typed_confirmation) {
        // Deliberately NOT consuming the handle: a typo must be correctable without forcing
        // a re-plan (`tests/test_sidecar_maintenance.py:194-206`).
        throw rpcError(RPC_MAINTENANCE_REFUSED, "Typed confirmation does not match the preview.");
      }
      plans.delete(planId); // accepted -> consumed, so a run can never be replayed
      if (!apply) {
        // A dry run returns the PLANNED MOVES, not an empty list
        // (`llm_anthology/maintenance.py:670`) — that is how a UI shows the destinations —
        // with an empty `manifest_path` because nothing was written.
        return { executed: false, manifest_path: "", moves: [...preview.plan], unaccounted: [] };
      }
      const manifestPath = winJoin(
        preview.checkpoint_root,
        `manifest-${nextManifest}.json`,
      );
      nextManifest += 1;
      manifests.set(manifestPath, { moves: [...preview.plan], restored: false });
      // Only an APPLIED run enters the ledger (`tests/test_sidecar_maintenance.py:305-312`).
      // `recorded_at_ms` steps by a fixed minute per run rather than reading the clock, so
      // the newest-first order is deterministic and testable.
      runs.push({
        manifest_path: manifestPath,
        action: preview.action,
        status: "executed",
        recorded_at_ms: T0 + runs.length * 60_000,
        moved_count: preview.plan.length,
        blocked_count: preview.blocked.length,
        store_root: preview.store_root,
      });
      return {
        executed: true,
        manifest_path: manifestPath,
        moves: [...preview.plan],
        unaccounted: [],
      };
    },

    async maintenanceRestore(params: MaintenanceRestoreParams): Promise<MaintenanceResult> {
      const manifestPath = params.manifest_path;
      if (typeof manifestPath !== "string" || manifestPath === "") {
        throw rpcError(RPC_INVALID_PARAMS, "manifest_path must be a non-empty string");
      }
      requireLocalPath(manifestPath, "manifest_path");
      const record = manifests.get(manifestPath);
      if (record === undefined) {
        throw rpcError(
          RPC_INTERNAL_ERROR,
          `no checkpoint manifest at '${manifestPath}'`,
        );
      }
      const skip = params.skip_unaccounted ?? false;
      if (typeof skip !== "boolean") {
        throw rpcError(RPC_INVALID_PARAMS, "skip_unaccounted must be a boolean");
      }
      const apply = params.apply ?? false;
      // The engine's FIRST check (`llm_anthology/maintenance.py:750-752`): a checkpoint that
      // was already restored cannot be restored again.
      if (record.restored) {
        throw rpcError(
          RPC_MAINTENANCE_REFUSED,
          `checkpoint '${manifestPath}' was already restored`,
        );
      }

      // THE ENGINE'S PER-ENTRY RULE, ported (`llm_anthology/maintenance.py:784-793`), applied
      // to a MODELLED filesystem state instead of a real one:
      //
      //   checkpoint copy MISSING + original PRESENT -> skipped (never moved / already back);
      //   checkpoint copy MISSING + original MISSING -> UNACCOUNTED;
      //   checkpoint copy PRESENT  + original MISSING -> a pending move BACK.
      //
      // The modelled state after an applied execute is: every original ABSENT (the execute
      // moved it away) and every checkpoint copy PRESENT. `skip_unaccounted` is taken as the
      // caller asserting the checkpoint copies are gone — the mock has no filesystem, so that
      // is the only signal available, and it is the API's own signal for this state. Applying
      // the engine's rule to that state yields ALL entries unaccounted and NO pending move,
      // which is exactly what the engine answers for the same manifest. An earlier version
      // marked "exactly one" entry unaccounted, which was an invented number: the probe
      // measured engine 2 / mock 1 on the same input.
      //
      // NOT the duplicate-entry guards (`maintenance.py:776-781`). Those refuse a manifest
      // whose entries repeat an original or a checkpoint copy, and they are unreachable here:
      // this mock's own `plan` already blocks `duplicate-target` and de-dupes destinations, so
      // a manifest it issued can never contain a repeat. Porting them would add a branch no
      // input can reach.
      //
      // DISCLOSED COST: because "unaccounted exists" is tied to the caller passing `skip`, the
      // engine's fail-closed REFUSAL (unaccounted and no skip) cannot fire here. A panel must
      // still handle a -32003 from restore, which a dev run will not produce.
      const checkpointPresent = !skip;
      const pending: PlannedMove[] = [];
      const unaccounted: string[] = [];
      for (const entry of record.moves) {
        if (!checkpointPresent) {
          unaccounted.push(entry.source);
          continue;
        }
        // DIRECTION IS INVERTED relative to the execute plan: restoring moves the CHECKPOINT
        // COPY back onto the ORIGINAL path, so the recorded `destination` becomes the source
        // and the recorded `source` becomes the destination. The mock used to echo the plan
        // unchanged, which reads as an arrow pointing the wrong way in any panel that renders
        // "moving X -> Y" — a divergence no structural diff can see, since both are strings.
        pending.push({
          session_id: entry.session_id,
          source: entry.destination,
          destination: entry.source,
        });
      }
      if (apply) record.restored = true;
      // NOTE the ledger is NOT updated here, matching the RPC surface: `record_run` is
      // called only from `maintenance.execute` (`llm_anthology/sidecar.py:1621-1622`), so a
      // restored run keeps `status: "executed"` in `maintenance.runs`.
      return {
        executed: apply,
        manifest_path: manifestPath,
        moves: pending,
        unaccounted,
      };
    },

    /** The audit ledger, NEWEST FIRST, capped at `limit` (engine default 50). */
    async maintenanceRuns(limit = 50): Promise<MaintenanceRun[]> {
      if (!Number.isInteger(limit) || limit < 0) {
        throw rpcError(RPC_INVALID_PARAMS, "limit must be a non-negative integer");
      }
      return [...runs].reverse().slice(0, limit);
    },
  };
}

/** The default mock client over the built-in synthetic forest. */
export const mockIpc: MockIpcClient = createMockIpc();

/** Exposed so tests can assert against the same data the default client serves. */
export const MOCK_THREADS = RAW_THREADS;
export const MOCK_EDGES = RAW_EDGES;
export { MOCK_DEDUP_SESSIONS };
