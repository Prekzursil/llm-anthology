/**
 * The discovery panel's DECISIONS — what the first run offers, in what order, and what it
 * refuses to offer.
 *
 * Why these are testable at all: vitest here runs `environment: "node"`
 * (`vitest.config.ts`), so there is no `document`. Every rule below therefore lives in a
 * pure function or the DOM-free controller, with `DiscoveryPanel` doing nothing but the
 * native save dialog and a `textContent` paint — the same seam `ui/corpusBar` uses.
 *
 * WHAT THESE TESTS CANNOT SETTLE, stated here rather than implied: the real
 * `sources.discover` round-trip, the real ingest, and the panel's appearance all need a
 * running Tauri webview. Everything below runs against injected fakes, so it proves the
 * decisions are right GIVEN the payload shape — not that the payload shape is right. The
 * shape itself is pinned by reading `llm_anthology/discover.py` and the Rust commands, and
 * is cited at each place it matters.
 */
import { describe, expect, it, vi } from "vitest";

import type {
  BuildParams,
  BuildStatus,
  CreateCorpusResult,
  DiscoveryFinding,
  DiscoveryResult,
  OpenCorpusResult,
} from "../ipc/types";
import {
  buildOutcomeMessage,
  buildProgressLabel,
  capNote,
  countLabel,
  DEFAULT_POLL_LIMITS,
  deriveAction,
  deriveBuildParams,
  detailSummary,
  DiscoveryPanelController,
  expandLabel,
  EXPORT_NO_IMPORT_REASON,
  formatDetailValue,
  groupDigits,
  groupFindings,
  kindLabel,
  mtimeToMs,
  NOTHING_FOUND_LABEL,
  pathSummary,
  pollOutcome,
  relativeAge,
  scanNotes,
  UNKNOWN_KIND_REASON,
  type DiscoveryDeps,
  type DiscoveryGroup,
  type DiscoveryIpc,
  type DiscoveryView,
} from "./discoveryPanel";

// ---------------------------------------------------------------------------
// fixtures — shaped from the MEASURED payload, not invented
// ---------------------------------------------------------------------------

/** A fixed "now" so every relative age in these tests is deterministic. */
const NOW = 1_800_000_000_000;
/** The same instant in the UNIX SECONDS that discovery actually reports. */
const NOW_SEC = NOW / 1000;

function finding(over: Partial<DiscoveryFinding> = {}): DiscoveryFinding {
  return {
    provider: "chatgpt",
    kind: "export_file",
    path: "C:\\Users\\me\\Downloads\\a\\conversations.json",
    count: 1,
    newest_mtime: NOW_SEC - 3600,
    confidence: "high",
    detail: { size_bytes: 4_100_000 },
    ...over,
  };
}

/** The Codex shape: `report="base"`, so `path` is the home and `items_root` is distinct. */
function codexStore(over: Partial<DiscoveryFinding> = {}): DiscoveryFinding {
  return finding({
    provider: "codex",
    kind: "session_store",
    path: "C:\\Users\\me\\.codex",
    count: 2043,
    detail: {
      rollouts_jsonl: 0,
      rollouts_zst: 2043,
      ingestable: 0,
      state_db: "C:\\Users\\me\\.codex\\state_5.sqlite",
      items_root: "C:\\Users\\me\\.codex\\sessions",
    },
    ...over,
  });
}

/** The Claude Code shape: `report="subdir"`, so `path` and `items_root` are the SAME. */
/**
 * A Grok Build store as `discover.py` reports it: subdir-reporting, so `path` IS the sessions
 * root `grok.ingest_sessions` takes and `items_root` is the same directory. Structurally
 * identical to a Claude Code finding, which is precisely why shape alone can no longer decide
 * importability.
 */
function grokStore(over: Partial<DiscoveryFinding> = {}): DiscoveryFinding {
  return finding({
    provider: "grok",
    kind: "session_store",
    path: "C:\\Users\\me\\.grok\\sessions",
    count: 68,
    detail: {
      ingestable: 68,
      items_root: "C:\\Users\\me\\.grok\\sessions",
      sessions: 68,
    },
    ...over,
  });
}

function claudeCodeStore(over: Partial<DiscoveryFinding> = {}): DiscoveryFinding {
  return finding({
    provider: "claude-code",
    kind: "session_store",
    path: "C:\\Users\\me\\.claude\\projects",
    count: 162,
    detail: {
      "*.jsonl": 162,
      ingestable: 162,
      project_dirs: 9,
      items_root: "C:\\Users\\me\\.claude\\projects",
    },
    ...over,
  });
}

/**
 * A subdir-reporting store the engine has NO ingest path for.
 *
 * `claudeCodeStore` used to serve this role, and three tests below leaned on it that way.
 * That stopped being valid the moment the engine gained `claude_root`: the fixture became
 * importable, so tests asserting a refusal through it were asserting a fact about Claude
 * Code rather than about the refusal branch, and they went red for the RIGHT reason.
 *
 * Deliberately an invented provider rather than the next real one. A refusal test wants a
 * store the app genuinely cannot build from, and pinning that to a real provider means the
 * test breaks again the day that provider is wired — which is how `claudeCodeStore` ended
 * up here. `discover.py` can report a store this app has no ingest for, so the branch is
 * real even while no shipped provider occupies it.
 */
function unimportableStore(over: Partial<DiscoveryFinding> = {}): DiscoveryFinding {
  return finding({
    provider: "future-provider",
    kind: "session_store",
    path: "C:\\Users\\me\\.future\\sessions",
    count: 7,
    detail: { ingestable: 7, items_root: "C:\\Users\\me\\.future\\sessions" },
    ...over,
  });
}

function builtIndex(over: Partial<DiscoveryFinding> = {}): DiscoveryFinding {
  return finding({
    provider: "anthology",
    kind: "built_index",
    path: "C:\\Users\\me\\Documents\\anthology.db",
    count: 1284,
    detail: { conversations: 1284, tables: ["conversations", "conversations_fts"] },
    ...over,
  });
}

function scan(
  findings: DiscoveryFinding[],
  stats: Partial<DiscoveryResult["stats"]> = {},
): DiscoveryResult {
  return {
    findings,
    stats: {
      elapsed_seconds: 1.78,
      roots_scanned: 5,
      dirs_visited: 2188,
      files_examined: 39512,
      budget_exhausted: false,
      truncated_groups: [],
      errors: [],
      ...stats,
    },
  };
}

const OPEN_CTX = { corpusAttached: false };

// ---------------------------------------------------------------------------
// time
// ---------------------------------------------------------------------------

describe("mtimeToMs", () => {
  it("converts UNIX SECONDS to ms", () => {
    // Discovery is the only surface in this app reporting seconds; everything else is _ms.
    expect(mtimeToMs(1786047481.5)).toBe(1786047481500);
  });

  it("reads 0 as unknown rather than 1970", () => {
    // `discover.py:552` writes 0.0 — never null — when nothing datable was seen.
    expect(mtimeToMs(0)).toBeNull();
  });

  it("rejects a negative or non-finite mtime", () => {
    expect(mtimeToMs(-1)).toBeNull();
    expect(mtimeToMs(Number.NaN)).toBeNull();
  });
});

describe("relativeAge", () => {
  it("says so plainly when there is no date", () => {
    expect(relativeAge(null, NOW)).toBe("date unknown");
  });

  it("scales through the units", () => {
    expect(relativeAge(NOW - 30_000, NOW)).toBe("just now");
    expect(relativeAge(NOW - 5 * 60_000, NOW)).toBe("5 minutes ago");
    expect(relativeAge(NOW - 3 * 3_600_000, NOW)).toBe("3 hours ago");
    expect(relativeAge(NOW - 3 * 86_400_000, NOW)).toBe("3 days ago");
    expect(relativeAge(NOW - 90 * 86_400_000, NOW)).toBe("3 months ago");
    expect(relativeAge(NOW - 800 * 86_400_000, NOW)).toBe("2 years ago");
  });

  it("singularises exactly one unit", () => {
    expect(relativeAge(NOW - 86_400_000, NOW)).toBe("1 day ago");
  });

  it("treats a future mtime as now instead of counting down", () => {
    // Clock skew, or a copy that preserved a remote mtime. "in 3 days" is not actionable.
    expect(relativeAge(NOW + 86_400_000, NOW)).toBe("just now");
  });
});

// ---------------------------------------------------------------------------
// paths, counts, detail
// ---------------------------------------------------------------------------

describe("pathSummary", () => {
  it("splits a Windows path", () => {
    expect(pathSummary("C:\\Users\\me\\Downloads\\x\\conversations.json")).toEqual({
      name: "conversations.json",
      parent: "C:\\Users\\me\\Downloads\\x",
    });
  });

  it("splits a POSIX path", () => {
    expect(pathSummary("/home/me/.codex/sessions")).toEqual({
      name: "sessions",
      parent: "/home/me/.codex",
    });
  });

  it("ignores a trailing separator on a directory", () => {
    expect(pathSummary("C:\\Users\\me\\.codex\\").name).toBe(".codex");
  });

  it("treats a bare name as its own basename", () => {
    expect(pathSummary("anthology.db")).toEqual({ name: "anthology.db", parent: "" });
  });
});

describe("groupDigits", () => {
  it("groups thousands without reading the host locale", () => {
    // toLocaleString() would emit "2 043" under some locales and "2,043" under others,
    // making this assertion machine-dependent.
    expect(groupDigits(2043)).toBe("2,043");
    expect(groupDigits(999)).toBe("999");
    expect(groupDigits(1_284_000)).toBe("1,284,000");
  });

  it("shows a non-finite count as itself rather than as a grouped lie", () => {
    // Every number here arrives off the wire, so NaN/Infinity is reachable from a malformed
    // payload. The digit regex would turn NaN into "NaN" and Infinity into "Infinity"
    // anyway, but only after Math.trunc — which yields NaN, so the guard is what keeps this
    // from printing a plausible-looking number for a count nobody measured.
    expect(groupDigits(Number.NaN)).toBe("NaN");
    expect(groupDigits(Number.POSITIVE_INFINITY)).toBe("Infinity");
  });

  it("groups a negative count without losing the sign", () => {
    expect(groupDigits(-2043)).toBe("-2,043");
  });
});

describe("countLabel", () => {
  it("counts conversations for an index, sessions for a store, files for an export", () => {
    expect(countLabel(builtIndex())).toBe("1,284 conversations");
    expect(countLabel(codexStore())).toBe("2,043 sessions");
    expect(countLabel(finding())).toBe("1 file");
  });
});

describe("kindLabel", () => {
  it("phrases the three known kinds", () => {
    expect(kindLabel("built_index")).toBe("corpus index");
    expect(kindLabel("session_store")).toBe("session store");
    expect(kindLabel("export_file")).toBe("downloaded export");
  });

  it("shows an unknown kind verbatim rather than hiding it", () => {
    // Adding a provider/kind is a table edit in the engine (discover.py:48-51).
    expect(kindLabel("future_thing")).toBe("future_thing");
  });
});

describe("formatDetailValue", () => {
  it("omits an empty string, because that MEANS the marker was not found", () => {
    // discover.py:564 writes `state_db: ""` for a marker file it did not find.
    expect(formatDetailValue("")).toBeNull();
  });

  it("renders a boolean as yes/no rather than as a bare true/false", () => {
    // `detail` is rendered key-agnostically, so any provider may put a flag in it. `false`
    // must survive as a rendered value: returning null for it would drop the key entirely
    // and make "we checked and it is off" indistinguishable from "we never looked".
    expect(formatDetailValue(true)).toBe("yes");
    expect(formatDetailValue(false)).toBe("no");
  });

  it("shortens a path value to its final segment", () => {
    expect(formatDetailValue("C:\\Users\\me\\.codex\\state_5.sqlite")).toBe("state_5.sqlite");
  });

  it("leaves a string that is NOT a path completely alone", () => {
    // The other arm of the same rule, and the one a path-shortener gets wrong: a plain value
    // has no final segment to take, so shortening it would have to invent one. Reachable from
    // any provider key that carries a name rather than a location.
    expect(formatDetailValue("gpt-5-codex")).toBe("gpt-5-codex");
    expect(formatDetailValue("high")).toBe("high");
  });

  it("groups numbers and joins lists", () => {
    expect(formatDetailValue(2043)).toBe("2,043");
    expect(formatDetailValue(["conversations", "conversations_fts"])).toBe(
      "conversations, conversations_fts",
    );
  });

  it("renders a list of NON-strings without falling back to [object Object]", () => {
    // `detail` is `Record<string, unknown>` off the wire and rendered key-agnostically, so a
    // provider may report an array of anything. Each element gets `String(v)` rather than the
    // whole array getting a default `toString`, which is what keeps a numeric list readable.
    expect(formatDetailValue([1024, 2048])).toBe("1024, 2048");
    expect(formatDetailValue(["zst", 2043, true])).toBe("zst, 2043, true");
  });

  it("omits a value it cannot honestly render on one line", () => {
    expect(formatDetailValue(null)).toBeNull();
    expect(formatDetailValue({ nested: true })).toBeNull();
    expect(formatDetailValue([])).toBeNull();
  });
});

describe("detailSummary", () => {
  it("renders whatever keys a provider reports, sorted, without knowing them", () => {
    // The point of the generic renderer: this key set belongs to Claude Code and shares
    // nothing with Codex's, and a new provider's would share nothing with either.
    expect(detailSummary(claudeCodeStore().detail)).toBe(
      "*.jsonl 162 · ingestable 162 · items_root projects · project_dirs 9",
    );
  });

  it("drops the absent marker instead of printing a blank", () => {
    const summary = detailSummary({ state_db: "", rollouts_zst: 12 });
    expect(summary).toBe("rollouts_zst 12");
  });

  it("is empty for an empty detail dict", () => {
    expect(detailSummary({})).toBe("");
  });
});

// ---------------------------------------------------------------------------
// what a finding lets you DO
// ---------------------------------------------------------------------------

describe("deriveBuildParams", () => {
  it("derives both parameters from a base-reporting store", () => {
    // discover.py:550-551: report="base" names the HOME as `path`, with the sessions tree
    // carried separately as detail.items_root.
    expect(deriveBuildParams(codexStore())).toEqual({
      build: {
        sessions_root: "C:\\Users\\me\\.codex\\sessions",
        codex_home: "C:\\Users\\me\\.codex",
      },
      missing: "",
    });
  });

  it("derives a GROK-ONLY import: grok_root alone, no Codex paths", () => {
    // Grok is subdir-reporting like Claude Code — path === items_root — but unlike it, the
    // engine CAN ingest it: `loaders.load_corpus` takes `grok_root`, and `codex_home` is
    // optional because an unnamed home simply means no state graph to merge. So the shape
    // rule that refuses Claude Code must NOT refuse this.
    const derived = deriveBuildParams(grokStore());

    expect(derived.build).toEqual({ grok_root: "C:\\Users\\me\\.grok\\sessions" });
    expect(derived.missing).toBe("");
  });

  it("does NOT invent a codex_home for a Grok store", () => {
    // Passing one would merge a Codex state graph into a Grok-only import, and passing the
    // LIVE default is the exact behaviour that once let an automated probe read real
    // private sessions.
    const derived = deriveBuildParams(grokStore());
    expect(derived.build?.codex_home).toBeUndefined();
    expect(derived.build?.sessions_root).toBeUndefined();
  });

  it("derives a CLAUDE-CODE-ONLY import: claude_root alone, no Codex paths", () => {
    // This test used to assert the OPPOSITE — that the import was refused with "cannot
    // import a claude-code store yet". That was correct when written: the adapter existed
    // but `loaders.load_corpus` never called it, so offering the import would have been
    // inventing a capability. The engine now takes `claude_root`, measured end to end
    // against `corpus.build` — accepted ALONE with no other source named, and refusing
    // UNC, relative, non-existent and non-string roots with the same wording `grok_root`
    // uses. So the refusal became the stale half, and leaving it would mean the UI
    // rejecting an import the engine is ready to serve.
    //
    // Structurally identical to the Grok line: `discover.py:644` sets `path = scan_root`
    // for report="subdir", and that IS the projects root `claude_code.ingest_sessions`
    // takes, so no derivation is needed beyond naming it.
    const derived = deriveBuildParams(claudeCodeStore());

    expect(derived.build).toEqual({ claude_root: "C:\\Users\\me\\.claude\\projects" });
    expect(derived.missing).toBe("");
  });

  it("does NOT invent a codex_home for a Claude Code store either", () => {
    // Same reason as the Grok case: passing one merges a Codex state graph into an import
    // that has none, and passing the LIVE default is the behaviour that once let an
    // automated probe read real private sessions.
    const derived = deriveBuildParams(claudeCodeStore());
    expect(derived.build?.codex_home).toBeUndefined();
    expect(derived.build?.sessions_root).toBeUndefined();
    expect(derived.build?.grok_root).toBeUndefined();
  });

  it("refuses when the scan reported no item root at all", () => {
    const derived = deriveBuildParams(codexStore({ detail: { rollouts_zst: 5 } }));
    expect(derived.build).toBeNull();
    expect(derived.missing).toContain("no item root");
  });

  it("treats a trailing separator and letter case as the same path", () => {
    // Windows paths are case-insensitive; a trailing slash must not fake a distinct home.
    const derived = deriveBuildParams(
      unimportableStore({ detail: { items_root: "c:/users/me/.future/sessions/" } }),
    );
    expect(derived.build).toBeNull();
  });
});

describe("deriveAction", () => {
  it("offers a built index straight to open_corpus", () => {
    expect(deriveAction(builtIndex(), OPEN_CTX)).toMatchObject({
      kind: "open",
      enabled: true,
      build: null,
    });
  });

  it("offers an importable store the derived build parameters", () => {
    const action = deriveAction(codexStore(), OPEN_CTX);
    expect(action.kind).toBe("import");
    expect(action.enabled).toBe(true);
    expect(action.build).toEqual({
      sessions_root: "C:\\Users\\me\\.codex\\sessions",
      codex_home: "C:\\Users\\me\\.codex",
    });
  });

  it("does NOT gate the import on detail.ingestable", () => {
    // The measured live store reads `ingestable: 0` because discover.py:291-295 marks the
    // compressed form not-ingestable, while ingest_sessions actually globs BOTH forms
    // (codex_rollout.py:416-419) and the store holds 2043 .zst against 0 plain .jsonl.
    // Gating on that counter would hide a working import of the entire history.
    expect(codexStore().detail.ingestable).toBe(0);
    expect(deriveAction(codexStore(), OPEN_CTX).kind).toBe("import");
  });

  it("changes the import label to say where the data will land", () => {
    expect(deriveAction(codexStore(), { corpusAttached: false }).label).toBe("Import…");
    expect(deriveAction(codexStore(), { corpusAttached: true }).label).toBe(
      "Import into open corpus",
    );
  });

  it("offers a downloaded export NO action, because no import RPC exists for one", () => {
    // The sidecar's dispatch table has exactly one ingest verb, corpus.build, and it runs
    // loaders.load_corpus — Codex rollouts plus a Codex state DB, nothing else.
    const action = deriveAction(finding(), OPEN_CTX);
    expect(action.kind).toBe("none");
    expect(action.enabled).toBe(false);
    expect(action.build).toBeNull();
    expect(action.reason).toBe(EXPORT_NO_IMPORT_REASON);
  });

  it("offers a store whose parameters are not derivable NO action, and says why", () => {
    const action = deriveAction(unimportableStore(), OPEN_CTX);
    expect(action.kind).toBe("none");
    expect(action.build).toBeNull();
    expect(action.reason).toMatch(/^Detected, but /);
  });

  it("offers an unrecognised kind NO action rather than guessing one", () => {
    expect(deriveAction(finding({ kind: "future_thing" }), OPEN_CTX).reason).toBe(
      UNKNOWN_KIND_REASON,
    );
  });
});

// ---------------------------------------------------------------------------
// grouping and ranking
// ---------------------------------------------------------------------------

describe("groupFindings ordering", () => {
  it("ranks openable before importable before merely present", () => {
    // Even though the export here is the NEWEST thing in the scan.
    const groups = groupFindings(
      scan([
        finding({ newest_mtime: NOW_SEC }),
        codexStore({ newest_mtime: NOW_SEC - 10_000 }),
        builtIndex({ newest_mtime: NOW_SEC - 999_999 }),
      ]),
      { ...OPEN_CTX, nowMs: NOW },
    );
    expect(groups.map((g) => g.kind)).toEqual([
      "built_index",
      "session_store",
      "export_file",
    ]);
  });

  it("sorts a kind it does not recognise LAST rather than first", () => {
    // Adding a kind is a table edit in the engine (discover.py:48-51), so an unknown one is
    // expected rather than impossible. It must land after everything actionable: ranking it 0
    // by accident would put a row the panel can do nothing with at the top of a first run.
    const groups = groupFindings(
      scan([
        finding({ provider: "future", kind: "brand_new_kind", newest_mtime: NOW_SEC }),
        builtIndex({ newest_mtime: NOW_SEC - 999_999 }),
      ]),
      { ...OPEN_CTX, nowMs: NOW },
    );
    expect(groups.map((g) => g.kind)).toEqual(["built_index", "brand_new_kind"]);
  });

  it("orders equal-rank groups by recency", () => {
    const groups = groupFindings(
      scan([
        finding({ provider: "gemini", newest_mtime: NOW_SEC - 100 }),
        finding({ provider: "chatgpt", newest_mtime: NOW_SEC - 5 }),
      ]),
      { ...OPEN_CTX, nowMs: NOW },
    );
    expect(groups.map((g) => g.provider)).toEqual(["chatgpt", "gemini"]);
  });

  it("puts the newest finding first WITHIN a group", () => {
    // The engine sorts survivors by (kind, provider, PATH) — discover.py:819-820 — so wire
    // order is not presentation order and the UI has to re-sort.
    const groups = groupFindings(
      scan([
        finding({ path: "/a/conversations.json", newest_mtime: NOW_SEC - 900 }),
        finding({ path: "/b/conversations.json", newest_mtime: NOW_SEC - 10 }),
        finding({ path: "/c/conversations.json", newest_mtime: NOW_SEC - 500 }),
      ]),
      { ...OPEN_CTX, nowMs: NOW },
    );
    expect(groups[0].rows.map((r) => r.finding.path)).toEqual([
      "/b/conversations.json",
      "/c/conversations.json",
      "/a/conversations.json",
    ]);
  });

  it("breaks an mtime tie by path so a re-render is byte-identical", () => {
    const groups = groupFindings(
      scan([
        finding({ path: "/z/conversations.json", newest_mtime: NOW_SEC }),
        finding({ path: "/a/conversations.json", newest_mtime: NOW_SEC }),
      ]),
      { ...OPEN_CTX, nowMs: NOW },
    );
    expect(groups[0].rows.map((r) => r.finding.path)).toEqual([
      "/a/conversations.json",
      "/z/conversations.json",
    ]);
  });

  it("reaches the SAME row order from the opposite wire order", () => {
    // The claim above is "byte-identical", which is a claim about the comparator being a total
    // order — not merely about one input. The engine sorts by (kind, provider, PATH)
    // (discover.py:819-820), so wire order is arbitrary from this UI's point of view, and the
    // previous test only exercises the comparator in one direction.
    const groups = groupFindings(
      scan([
        finding({ path: "/a/conversations.json", newest_mtime: NOW_SEC }),
        finding({ path: "/z/conversations.json", newest_mtime: NOW_SEC }),
      ]),
      { ...OPEN_CTX, nowMs: NOW },
    );
    expect(groups[0].rows.map((r) => r.finding.path)).toEqual([
      "/a/conversations.json",
      "/z/conversations.json",
    ]);
  });

  it("keeps BOTH of two findings that are identical, rather than collapsing them", () => {
    // Two roots can overlap, so the scan can report one file twice. The comparator has to
    // answer 0 for that pair — anything else makes `Array.prototype.sort`'s behaviour
    // unspecified — and neither copy may be silently dropped, because this UI does no
    // deduplication and a vanishing row would misreport what is on disk.
    const dup = finding({ path: "/dup/conversations.json", newest_mtime: NOW_SEC });
    const groups = groupFindings(scan([dup, { ...dup }]), { ...OPEN_CTX, nowMs: NOW });
    expect(groups[0].totalCount).toBe(2);
    expect(groups[0].rows.map((r) => r.finding.path)).toEqual([
      "/dup/conversations.json",
      "/dup/conversations.json",
    ]);
  });

  it("breaks an equal-rank, equal-recency GROUP tie by key, from either wire order", () => {
    // Between groups the same total-order requirement applies one level up. Two providers'
    // export groups with the same newest mtime must land in a fixed order whichever way the
    // scan listed them, or the panel reshuffles itself between two identical scans.
    //
    // The comparator's THIRD arm (`: 0`, equal keys) is unreachable and stays deliberately
    // untested rather than faked: `groupFindings` buckets findings into a `Map` keyed by
    // `"<provider>/<kind>"` and pushes exactly one group per entry, so every `group.key` in
    // the sorted array is distinct by construction. Two findings that would share a key share
    // a BUCKET instead, and become one group. The settling change, if that arm ever needs
    // covering, is a `groupFindings` that can emit two groups for one key — which would be a
    // defect in itself.
    const gemini = finding({ provider: "gemini", newest_mtime: NOW_SEC });
    const chatgpt = finding({ provider: "chatgpt", newest_mtime: NOW_SEC });
    const forward = groupFindings(scan([gemini, chatgpt]), { ...OPEN_CTX, nowMs: NOW });
    const reverse = groupFindings(scan([chatgpt, gemini]), { ...OPEN_CTX, nowMs: NOW });

    expect(forward.map((g) => g.key)).toEqual(["chatgpt/export_file", "gemini/export_file"]);
    expect(reverse.map((g) => g.key)).toEqual(forward.map((g) => g.key));
  });

  it("holds rows back rather than losing them when the cap is zero", () => {
    // `maxRows` is an injected seam (`DiscoveryDeps.maxRows`) and `?? MAX_ROWS_PER_GROUP`
    // keeps a literal 0, so a zero cap is reachable through the public API. Nothing may be
    // lost by it: the counts and the expand control still name everything held back, and the
    // group ordering must survive having no row to read a date from.
    const groups = groupFindings(
      scan([
        finding({ provider: "gemini", path: "/g/conversations.json", newest_mtime: NOW_SEC }),
        finding({ provider: "chatgpt", path: "/c/conversations.json", newest_mtime: NOW_SEC }),
      ]),
      { ...OPEN_CTX, nowMs: NOW, maxRows: 0 },
    );
    expect(groups.map((g) => g.key)).toEqual(["chatgpt/export_file", "gemini/export_file"]);
    expect(groups[0].rows).toEqual([]);
    expect(groups[0].totalCount).toBe(1);
    expect(groups[0].hiddenCount).toBe(1);
    expect(groups[0].expandLabel).toBe("+1 more");
  });

  it("carries an undated finding without crashing or faking a date", () => {
    const groups = groupFindings(scan([finding({ newest_mtime: 0 })]), {
      ...OPEN_CTX,
      nowMs: NOW,
    });
    expect(groups[0].rows[0].summary).toContain("date unknown");
  });
});

// ---------------------------------------------------------------------------
// the TWO truncations — the whole point of keeping them apart
// ---------------------------------------------------------------------------

describe("expandLabel / capNote", () => {
  it("offers no toggle when the group is whole and unexpanded", () => {
    expect(expandLabel(0, false)).toBe("");
  });

  it("labels the UI's own collapsing with what a click would reveal", () => {
    expect(expandLabel(20, false)).toBe("+20 more");
  });

  it("offers the way back once expanded", () => {
    // The bug this exists to prevent: an expanded group with nothing left hidden rendered
    // no control at all, so the expansion could not be undone.
    expect(expandLabel(0, true)).toBe("Show fewer");
  });

  it("quotes the engine's cap from the data rather than restating 25", () => {
    // When a group was capped, the surviving count IS the cap — so this cannot drift if
    // DEFAULT_MAX_PER_GROUP ever changes.
    expect(capNote(25)).toContain("the newest 25");
    expect(capNote(25)).toContain("not shown at all");
  });

  it("never phrases the engine's cap as something a click could reveal", () => {
    expect(capNote(25)).not.toContain("more");
  });
});

describe("groupFindings truncation", () => {
  /** The real census: 25 near-identical ChatGPT exports in one group. */
  function twentyFiveExports(): DiscoveryFinding[] {
    return Array.from({ length: 25 }, (_, i) =>
      finding({
        path: `C:\\Users\\me\\Downloads\\export-${i}\\conversations.json`,
        newest_mtime: NOW_SEC - i * 86_400,
      }),
    );
  }

  it("caps the 25-row group so it cannot swamp the panel", () => {
    const groups = groupFindings(scan(twentyFiveExports()), { ...OPEN_CTX, nowMs: NOW });
    expect(groups[0].totalCount).toBe(25);
    expect(groups[0].rows).toHaveLength(5);
    expect(groups[0].hiddenCount).toBe(20);
    expect(groups[0].backendTruncated).toBe(false);
  });

  it("keeps the NEWEST rows when it caps", () => {
    const groups = groupFindings(scan(twentyFiveExports()), { ...OPEN_CTX, nowMs: NOW });
    expect(groups[0].rows[0].finding.path).toContain("export-0");
    expect(groups[0].rows[4].finding.path).toContain("export-4");
  });

  it("carries the backend cap in a SEPARATE field from its own collapsing", () => {
    // stats.truncated_groups is keyed "<provider>/<kind>" (discover.py:785). Both
    // truncations are live here at once, and each has to keep its own field: one is a
    // button, the other is a sentence no button can satisfy.
    const groups = groupFindings(
      scan(twentyFiveExports(), { truncated_groups: ["chatgpt/export_file"] }),
      { ...OPEN_CTX, nowMs: NOW },
    );
    expect(groups[0].key).toBe("chatgpt/export_file");
    expect(groups[0].backendTruncated).toBe(true);
    expect(groups[0].hiddenCount).toBe(20);
    expect(groups[0].expandLabel).toBe("+20 more");
    expect(groups[0].capNote).toContain("not shown at all");
  });

  it("does not mark a group the backend left alone", () => {
    const groups = groupFindings(
      scan([...twentyFiveExports(), codexStore()], {
        truncated_groups: ["chatgpt/export_file"],
      }),
      { ...OPEN_CTX, nowMs: NOW },
    );
    const store = groups.find((g) => g.key === "codex/session_store");
    expect(store?.backendTruncated).toBe(false);
    expect(store?.capNote).toBe("");
    expect(store?.expandLabel).toBe("");
  });

  it("shows every row once expanded, and swaps +N for a way back", () => {
    const groups = groupFindings(scan(twentyFiveExports()), {
      ...OPEN_CTX,
      nowMs: NOW,
      expanded: new Set(["chatgpt/export_file"]),
    });
    expect(groups[0].rows).toHaveLength(25);
    expect(groups[0].hiddenCount).toBe(0);
    expect(groups[0].expanded).toBe(true);
    expect(groups[0].expandLabel).toBe("Show fewer");
  });

  it("still reports the BACKEND cap on an expanded group", () => {
    // Expanding reveals what the UI held back; it cannot reveal what the scan never
    // returned, so this sentence must survive the expansion while +N does not.
    const groups = groupFindings(
      scan(twentyFiveExports(), { truncated_groups: ["chatgpt/export_file"] }),
      { ...OPEN_CTX, nowMs: NOW, expanded: new Set(["chatgpt/export_file"]) },
    );
    expect(groups[0].capNote).toContain("not shown at all");
    expect(groups[0].expandLabel).toBe("Show fewer");
  });
});

// ---------------------------------------------------------------------------
// scan-level notes
// ---------------------------------------------------------------------------

describe("scanNotes", () => {
  it("reports the cost of the scan", () => {
    expect(scanNotes(scan([]))[0]).toBe("Scanned 5 locations in 1.8s.");
  });

  it("counts skipped locations without dumping them at the user", () => {
    // A real scan produced 7 of these; the raw strings are WinError noise about
    // directories the user never asked to be searched.
    const notes = scanNotes(
      scan([], { errors: Array.from({ length: 7 }, (_, i) => `C:\\x${i}: [WinError 5] denied`) }),
    );
    expect(notes.some((n) => n === "7 locations were skipped (missing, or not readable).")).toBe(
      true,
    );
    expect(notes.join(" ")).not.toContain("WinError");
  });

  it("singularises one skipped location", () => {
    const notes = scanNotes(scan([], { errors: ["C:\\x: denied"] }));
    expect(notes.some((n) => n.includes("1 location was skipped"))).toBe(true);
  });

  it("says nothing about skipping when nothing was skipped", () => {
    expect(scanNotes(scan([])).join(" ")).not.toContain("skipped");
  });

  it("reports an exhausted file budget as a THIRD kind of incompleteness", () => {
    // Distinct from either truncation: the walk stopped early, so entire directories may
    // never have been looked at.
    const notes = scanNotes(scan([], { budget_exhausted: true }));
    expect(notes.some((n) => n.includes("reached its file limit"))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// the poll's stopping rule
// ---------------------------------------------------------------------------

describe("pollOutcome", () => {
  it("keeps polling a running job inside the ceiling", () => {
    expect(pollOutcome("running", 1000, DEFAULT_POLL_LIMITS)).toBe("continue");
  });

  it("stops on every terminal state", () => {
    expect(pollOutcome("done", 0, DEFAULT_POLL_LIMITS)).toBe("terminal");
    expect(pollOutcome("failed", 0, DEFAULT_POLL_LIMITS)).toBe("terminal");
  });

  it("stops on idle, which is what the engine reports when it holds no job", () => {
    // sidecar.py:824. Treating idle as "keep waiting" would spin forever.
    expect(pollOutcome("idle", 0, DEFAULT_POLL_LIMITS)).toBe("terminal");
  });

  it("stops a job wedged in running once the ceiling is reached", () => {
    expect(pollOutcome("running", DEFAULT_POLL_LIMITS.maxElapsedMs, DEFAULT_POLL_LIMITS)).toBe(
      "timeout",
    );
  });
});

describe("buildOutcomeMessage", () => {
  function status(over: Partial<BuildStatus> = {}): BuildStatus {
    return { state: "done", indexed_conversations: 1200, errors: [], ...over };
  }

  it("reports a completed import with the real indexed count", () => {
    expect(buildOutcomeMessage(status())).toBe(
      "Import finished — 1,200 conversations in the corpus.",
    );
  });

  it("reports skipped files even on SUCCESS", () => {
    // A build that quietly skipped 40 unreadable rollouts and called itself done would
    // otherwise look like a clean import of everything.
    expect(buildOutcomeMessage(status({ errors: ["a: bad", "b: bad"] }))).toContain(
      "2 files could not be read",
    );
  });

  it("singularises exactly one skipped file", () => {
    // Three agreements in one sentence — "1 file … was skipped" — and the plural form reads as
    // a bug report against the app rather than against the file.
    expect(buildOutcomeMessage(status({ errors: ["only.jsonl: bad"] }))).toBe(
      "Import finished — 1,200 conversations in the corpus. 1 file could not be read and was skipped.",
    );
  });

  it("carries a failure through engineErrorText", () => {
    const text = buildOutcomeMessage(status({ state: "failed", error: "disk is full" }));
    expect(text).toBe("Import failed: disk is full");
  });

  it("still says something when a failed build names no reason", () => {
    // `BuildStatus.error` is optional, so a failure with the field absent is a shape the
    // engine can actually send. "Import failed" alone with a dangling colon, or the literal
    // "undefined", would both be worse than naming the gap.
    const text = buildOutcomeMessage(status({ state: "failed", indexed_conversations: 0 }));
    expect(text).toBe("Import failed: the engine reported no reason");
  });

  it("never leaks the engine's internal not-attached instruction", () => {
    // ui/errors exists because that string was reaching users verbatim.
    const text = buildOutcomeMessage(
      status({ state: "failed", error: "no corpus attached: call open_corpus first" }),
    );
    expect(text).not.toContain("open_corpus");
  });

  it("reports an unexpected terminal state as what it saw, not as success", () => {
    expect(buildOutcomeMessage(status({ state: "idle" }))).toContain("state “idle”");
  });
});

describe("buildProgressLabel", () => {
  it("shows the live indexed count", () => {
    expect(buildProgressLabel({ state: "running", indexed_conversations: 4210, errors: [] })).toBe(
      "Importing… 4,210 conversations indexed so far.",
    );
  });
});

// ---------------------------------------------------------------------------
// the controller
// ---------------------------------------------------------------------------

interface Harness {
  controller: DiscoveryPanelController;
  views: DiscoveryView[];
  ready: string[];
  sleeps: number[];
  ipc: DiscoveryIpc & { builds: BuildParams[]; created: string[]; opened: string[] };
}

/**
 * A destination the harness's `openCorpus` REFUSES to attach.
 *
 * Threaded through the PATH rather than added as a harness flag, so the failure arm is
 * reachable from the seams the tests already use (`chooseDestination` picks it) while the
 * call recording below stays intact. An `over.openCorpus` override cannot do that job here:
 * it replaces the recorder, so `opened` would go silent and a test could no longer assert
 * that the create-then-open sequence ran at all — which is the thing that distinguishes
 * "the engine declined" from "we never asked".
 */
const UNATTACHABLE = "C:\\refuses-to-attach.db";

function harness(
  over: Partial<DiscoveryIpc> = {},
  deps: Partial<DiscoveryDeps> = {},
  result: DiscoveryResult = scan([builtIndex(), codexStore(), claudeCodeStore(), finding()]),
): Harness {
  const builds: BuildParams[] = [];
  const created: string[] = [];
  const opened: string[] = [];
  const sleeps: number[] = [];
  const views: DiscoveryView[] = [];
  const ready: string[] = [];

  const ipc = {
    builds,
    created,
    opened,
    async discoverSources(): Promise<DiscoveryResult> {
      return result;
    },
    async openCorpus(indexPath: string): Promise<OpenCorpusResult> {
      opened.push(indexPath);
      // `ok: false` is a REFUSAL, not a throw: `open_corpus` answers that shape, so the
      // controller has to notice it rather than rely on a rejected promise.
      if (indexPath === UNATTACHABLE) return { ok: false, index: "" };
      return { ok: true, index: indexPath };
    },
    async createCorpus(indexPath: string): Promise<CreateCorpusResult> {
      created.push(indexPath);
      return { index_path: indexPath, created: true };
    },
    async corpusBuild(params: BuildParams): Promise<{ job_id: string }> {
      builds.push(params);
      return { job_id: "build-1" };
    },
    async corpusBuildStatus(): Promise<BuildStatus> {
      return { state: "done", indexed_conversations: 7, errors: [] };
    },
    ...over,
  };

  const controller = new DiscoveryPanelController(
    ipc,
    {
      now: deps.now ?? ((): number => NOW),
      sleep: deps.sleep ?? (async (ms: number): Promise<void> => void sleeps.push(ms)),
      chooseDestination: deps.chooseDestination ?? (async (): Promise<string | null> => "C:\\new.db"),
      limits: deps.limits,
      maxRows: deps.maxRows,
    },
    (view) => views.push(view),
    (indexPath) => ready.push(indexPath),
  );
  return { controller, views, ready, sleeps, ipc };
}

describe("DiscoveryPanelController.scan", () => {
  it("shows a pending phase BEFORE the scan resolves", async () => {
    // Warm ~1.8s / cold ~7.5s on the measured machine. A panel that painted only on
    // completion is a blank rectangle for that whole time.
    const h = harness();
    await h.controller.scan();
    expect(h.views[0].phase).toBe("scanning");
    expect(h.views[0].busy).toBe(true);
    expect(h.controller.current.phase).toBe("ready");
    expect(h.controller.current.busy).toBe(false);
  });

  it("presents the groups and the scan notes", async () => {
    const h = harness();
    await h.controller.scan();
    expect(h.controller.current.groups.map((g) => g.key)).toEqual([
      "anthology/built_index",
      "claude-code/session_store",
      "codex/session_store",
      "chatgpt/export_file",
    ]);
    expect(h.controller.current.notes[0]).toContain("Scanned 5 locations");
  });

  it("gives a zero-finding scan a real empty state naming the manual fallback", async () => {
    const h = harness({}, {}, scan([]));
    await h.controller.scan();
    expect(h.controller.current.groups).toHaveLength(0);
    expect(h.controller.current.emptyLabel).toBe(NOTHING_FOUND_LABEL);
    expect(h.controller.current.emptyLabel).toContain("Open corpus…");
  });

  it("reports a scan failure through engineErrorText rather than raw", async () => {
    const h = harness({
      async discoverSources(): Promise<DiscoveryResult> {
        throw new Error("no corpus attached: call open_corpus first");
      },
    });
    await h.controller.scan();
    expect(h.controller.current.phase).toBe("error");
    expect(h.controller.current.status).not.toContain("open_corpus");
  });

  it("ignores a re-entrant scan while one is in flight", async () => {
    const seen = vi.fn(async (): Promise<DiscoveryResult> => scan([]));
    const h = harness({ discoverSources: seen });
    await Promise.all([h.controller.scan(), h.controller.scan()]);
    expect(seen).toHaveBeenCalledTimes(1);
  });

  it("abandons a scan whose result arrives after the panel was destroyed", async () => {
    // The disposal window the build poll already guards twice, on the path that opens it
    // first: a cold scan is ~7.5s on the measured machine, so a corpus attached from the top
    // bar can tear the panel down while `sources.discover` is still in flight. Painting the
    // result then would rebuild a skeleton into a container the app has already emptied.
    let panel: DiscoveryPanelController | null = null;
    const h = harness({
      async discoverSources(): Promise<DiscoveryResult> {
        panel?.destroy();
        return scan([builtIndex()]);
      },
    });
    panel = h.controller;
    await h.controller.scan();

    // Only the pending phase, which is emitted BEFORE the await and so is legitimate.
    expect(h.views.map((v) => v.phase)).toEqual(["scanning"]);
    expect(h.controller.current.groups).toEqual([]);
    expect(h.controller.current.notes).toEqual([]);
  });
});

describe("DiscoveryPanelController.activate", () => {
  it("opens a built index and announces the corpus", async () => {
    const h = harness();
    await h.controller.activate(builtIndex());
    expect(h.ipc.opened).toEqual(["C:\\Users\\me\\Documents\\anthology.db"]);
    expect(h.ready).toEqual(["C:\\Users\\me\\Documents\\anthology.db"]);
    expect(h.controller.current.phase).toBe("done");
  });

  it("treats ok:false as a failure rather than a silent success", async () => {
    const h = harness({
      async openCorpus(): Promise<OpenCorpusResult> {
        return { ok: false, index: "" };
      },
    });
    await h.controller.activate(builtIndex());
    expect(h.controller.current.phase).toBe("error");
    expect(h.ready).toEqual([]);
  });

  it("does NOTHING but state the reason for an export file", async () => {
    const h = harness();
    await h.controller.activate(finding());
    expect(h.ipc.opened).toEqual([]);
    expect(h.ipc.created).toEqual([]);
    expect(h.ipc.builds).toEqual([]);
    expect(h.controller.current.status).toBe(EXPORT_NO_IMPORT_REASON);
  });

  it("does NOTHING but state the reason for a store whose parameters are missing", async () => {
    // The requirement in full: do not guess, do not pass a default, say what is missing.
    const h = harness();
    await h.controller.activate(unimportableStore());
    expect(h.ipc.builds).toEqual([]);
    expect(h.controller.current.status).toContain("cannot import a future-provider store yet");
  });

  it("creates and attaches an index before importing when nothing is open", async () => {
    // corpus.build is only forwarded once a corpus is attached (lib.rs:35-45) and the
    // engine needs that index to exist on disk (sidecar.py:717-720).
    const h = harness();
    await h.controller.activate(codexStore());
    expect(h.ipc.created).toEqual(["C:\\new.db"]);
    expect(h.ipc.opened).toEqual(["C:\\new.db"]);
    expect(h.ipc.builds).toEqual([
      { sessions_root: "C:\\Users\\me\\.codex\\sessions", codex_home: "C:\\Users\\me\\.codex" },
    ]);
  });

  it("IMPORTS a Claude Code store end to end, sending claude_root and nothing else",
    async () => {
      // The journey this whole change exists for, asserted where a user actually stands
      // rather than at `deriveBuildParams`. The engine gaining `claude_root` was never
      // enough on its own: this controller was the last hop, and it hard-refused any
      // subdir store that was not Grok, so a user would have been told "this app cannot
      // import a claude-code store yet" by a UI sitting on top of an engine that could.
      // That is the unreachable-data-plane shape the adapter itself was in for months —
      // complete, tested, and reachable from nothing — moved one layer out.
      //
      // `claude_root` ALONE is the assertion that matters. `corpus.build` was measured
      // accepting it with no other source named, and inventing a `codex_home` here would
      // merge a state graph this import does not have; defaulting it to the live `~/.codex`
      // is precisely the behaviour that once let an automated probe read real private
      // sessions.
      const h = harness();
      await h.controller.activate(claudeCodeStore());

      expect(h.ipc.builds).toEqual([{ claude_root: "C:\\Users\\me\\.claude\\projects" }]);
      expect(h.controller.current.status).not.toContain("cannot import");
    });

  it("treats a dismissed destination picker as a no-op, not an error", async () => {
    const h = harness({}, { chooseDestination: async (): Promise<string | null> => null });
    await h.controller.activate(codexStore());
    expect(h.ipc.created).toEqual([]);
    expect(h.ipc.builds).toEqual([]);
    expect(h.controller.current.phase).toBe("ready");
    expect(h.controller.current.status).toBe("");
  });

  it("skips the create step when a corpus is already attached", async () => {
    const h = harness();
    h.controller.setCorpusAttached("C:\\existing.db");
    await h.controller.activate(codexStore());
    expect(h.ipc.created).toEqual([]);
    expect(h.ipc.opened).toEqual([]);
    expect(h.ipc.builds).toHaveLength(1);
  });

  it("announces the empty corpus BEFORE the import finishes filling it", async () => {
    const h = harness();
    await h.controller.activate(codexStore());
    // Once for the freshly-created index, once when the build reaches a terminal state.
    expect(h.ready).toEqual(["C:\\new.db", "C:\\new.db"]);
  });

  it("stops when the engine CREATED the index but declined to attach it", async () => {
    // The dangerous middle of importStore: create succeeded, so an index now exists on disk,
    // but the attach did not. Continuing would fire `corpus.build` with nothing attached and
    // surface the engine's internal "call open_corpus first" — the leak ui/errors exists to
    // stop. It must stop here instead, and it must NOT announce a corpus it does not have.
    const h = harness({}, { chooseDestination: async (): Promise<string | null> => UNATTACHABLE });
    await h.controller.activate(codexStore());
    expect(h.ipc.created).toEqual([UNATTACHABLE]);
    expect(h.ipc.opened).toEqual([UNATTACHABLE]);
    expect(h.ipc.builds).toEqual([]);
    expect(h.ready).toEqual([]);
    expect(h.controller.current.phase).toBe("error");
    expect(h.controller.current.status).toContain("Import failed");
    expect(h.controller.current.busy).toBe(false);
  });

  it("reports a build that could not be started, carrying the engine's reason", async () => {
    // The other way into the same catch, and the likelier one in the field: the index is
    // attached and `corpus.build` itself rejects.
    const h = harness({
      async corpusBuild(): Promise<{ job_id: string }> {
        throw new Error("disk is full");
      },
    });
    h.controller.setCorpusAttached("C:\\existing.db");
    await h.controller.activate(codexStore());
    expect(h.controller.current.phase).toBe("error");
    // `Error:` and not just the message, because `engineErrorText` renders through
    // `String(err)` (`ui/errors.ts:45`) so that a non-Error throwable still says something.
    // Pinned rather than loosened to `toContain`: this is the exact text a user reads.
    expect(h.controller.current.status).toBe("Import failed: Error: disk is full");
    expect(h.controller.current.busy).toBe(false);
  });

  it("never leaks the engine's not-attached instruction out of a failed import", async () => {
    // `ui/errors` REPLACES this one outright rather than prefixing it, so the assertion is
    // that "Import failed" is gone too — the user is told what to do, not what the RPC said.
    // Getting this backwards is how the raw string reached users in the first place.
    const h = harness({
      async corpusBuild(): Promise<{ job_id: string }> {
        throw new Error("no corpus attached: call open_corpus first");
      },
    });
    h.controller.setCorpusAttached("C:\\existing.db");
    await h.controller.activate(codexStore());
    expect(h.controller.current.phase).toBe("error");
    expect(h.controller.current.status).not.toContain("open_corpus");
    expect(h.controller.current.status).toContain("Open corpus");
  });

  it("ignores a re-entrant activate while one is in flight", async () => {
    // Single-flight, for the same reason `scan` is: a double-click on Import would otherwise
    // start a second build the engine refuses by naming a job id the user never saw.
    let release = (): void => {};
    const gate = new Promise<void>((r) => { release = r; });
    const h = harness({
      async openCorpus(indexPath: string): Promise<OpenCorpusResult> {
        await gate;
        return { ok: true, index: indexPath };
      },
    });
    const first = h.controller.activate(builtIndex());
    await h.controller.activate(builtIndex()); // dropped, not queued
    release();
    await first;
    expect(h.ready).toEqual(["C:\\Users\\me\\Documents\\anthology.db"]);
  });

  it("does nothing at all once destroyed", async () => {
    const h = harness();
    h.controller.destroy();
    await h.controller.activate(builtIndex());
    await h.controller.scan();
    expect(h.ipc.opened).toEqual([]);
    expect(h.views).toEqual([]);
  });

  it("abandons an import when the panel is destroyed while the picker is open", async () => {
    // The picker is a NATIVE dialog, so it can sit open indefinitely while the app moves on.
    // Creating an index after that would leave a file behind for a panel that is gone.
    const h = harness({}, {
      chooseDestination: async (): Promise<string | null> => {
        h.controller.destroy();
        return "C:\\new.db";
      },
    });
    await h.controller.activate(codexStore());
    expect(h.ipc.created).toEqual([]);
    expect(h.ipc.builds).toEqual([]);
  });
});

describe("DiscoveryPanelController build polling", () => {
  /** A status fake that reports `running` N times, then `done`. */
  function climbing(runs: number): DiscoveryIpc["corpusBuildStatus"] {
    let polls = 0;
    return async (): Promise<BuildStatus> => {
      polls += 1;
      return polls <= runs
        ? { state: "running", indexed_conversations: polls * 100, errors: [] }
        : { state: "done", indexed_conversations: runs * 100, errors: [] };
    };
  }

  it("polls until the job leaves running, then STOPS", async () => {
    const h = harness({ corpusBuildStatus: climbing(3) });
    await h.controller.activate(codexStore());
    // Three running polls -> three sleeps; the fourth poll is terminal and sleeps no more.
    expect(h.sleeps).toEqual([750, 750, 750]);
    expect(h.controller.current.phase).toBe("done");
    expect(h.controller.current.status).toContain("Import finished");
  });

  it("shows the climbing indexed count while it runs", async () => {
    const h = harness({ corpusBuildStatus: climbing(2) });
    await h.controller.activate(codexStore());
    const progress = h.views.filter((v) => v.phase === "building").map((v) => v.status);
    expect(progress).toContain("Importing… 100 conversations indexed so far.");
    expect(progress).toContain("Importing… 200 conversations indexed so far.");
  });

  it("terminates immediately when the build is already done on the first poll", async () => {
    const h = harness();
    await h.controller.activate(codexStore());
    expect(h.sleeps).toEqual([]);
  });

  it("stops watching at the ceiling instead of polling a wedged job forever", async () => {
    // The clock jumps a minute per read, so the 30-minute ceiling arrives after 30 polls
    // even though the engine never leaves "running".
    let clock = NOW;
    const h = harness(
      {
        async corpusBuildStatus(): Promise<BuildStatus> {
          return { state: "running", indexed_conversations: 1, errors: [] };
        },
      },
      {
        now: (): number => {
          const value = clock;
          clock += 60_000;
          return value;
        },
      },
    );
    await h.controller.activate(codexStore());
    expect(h.controller.current.phase).toBe("error");
    expect(h.controller.current.status).toContain("stopped watching");
    expect(h.controller.current.status).toContain("import continues");
    expect(h.sleeps.length).toBeLessThan(35);
  });

  it("reloads the app even on a FAILED build, because the graph may already have changed", async () => {
    const h = harness({
      async corpusBuildStatus(): Promise<BuildStatus> {
        return { state: "failed", error: "rollout tree vanished", indexed_conversations: 3, errors: [] };
      },
    });
    await h.controller.activate(codexStore());
    expect(h.controller.current.phase).toBe("error");
    expect(h.controller.current.status).toContain("Import failed");
    expect(h.ready).toEqual(["C:\\new.db", "C:\\new.db"]);
  });

  it("stops polling once destroyed", async () => {
    // The engine never leaves "running", so only the disposal can end this loop. Destroying
    // from inside the first sleep is what unmounting the panel mid-import looks like.
    let polls = 0;
    let panel: DiscoveryPanelController | null = null;
    const h = harness(
      {
        async corpusBuildStatus(): Promise<BuildStatus> {
          polls += 1;
          return { state: "running", indexed_conversations: polls, errors: [] };
        },
      },
      { sleep: async (): Promise<void> => panel?.destroy() },
    );
    panel = h.controller;
    await h.controller.activate(codexStore());
    expect(polls).toBe(1);
  });

  it("does not announce a reload for a corpus that was detached mid-import", async () => {
    // `setCorpusAttached(null)` is on the public surface (`DiscoveryPanel` forwards it), and
    // an import is minutes of work, so the corpus can genuinely be gone by the time the build
    // reaches a terminal state. `onCorpusReady` means "this index changed, reload it" — firing
    // it with a path nothing is attached to would drive the app to reload a corpus it dropped.
    let panel: DiscoveryPanelController | null = null;
    const h = harness({
      async corpusBuildStatus(): Promise<BuildStatus> {
        panel?.setCorpusAttached(null);
        return { state: "done", indexed_conversations: 7, errors: [] };
      },
    });
    panel = h.controller;
    await h.controller.scan();
    await h.controller.activate(codexStore());

    expect(h.controller.current.phase).toBe("done");
    expect(h.controller.current.status).toContain("Import finished");
    // Only the create-time announcement. The terminal one is correctly withheld.
    expect(h.ready).toEqual(["C:\\new.db"]);
  });

  it("stops between the poll and the emit when destroyed mid-request", async () => {
    // The other disposal window: destroyed while `build_status` is in flight. Emitting after
    // that would repaint a panel the app has already torn down.
    let panel: DiscoveryPanelController | null = null;
    const h = harness({
      async corpusBuildStatus(): Promise<BuildStatus> {
        panel?.destroy();
        return { state: "done", indexed_conversations: 9, errors: [] };
      },
    });
    panel = h.controller;
    await h.controller.activate(codexStore());
    expect(h.controller.current.phase).not.toBe("done");
    // Only the create-time announcement; the terminal one never fired.
    expect(h.ready).toEqual(["C:\\new.db"]);
  });
});

// ---------------------------------------------------------------------------
// delivering the outcome
// ---------------------------------------------------------------------------

describe("a terminal import outcome the user must actually read", () => {
  /** A done build that skipped `n` files. */
  function skipping(n: number): Partial<DiscoveryIpc> {
    return {
      async corpusBuildStatus(): Promise<BuildStatus> {
        return {
          state: "done",
          indexed_conversations: 2003,
          errors: Array.from({ length: n }, (_, i) => `f${i}.jsonl: unreadable`),
        };
      },
    };
  }

  it("marks a SUCCESSFUL import that skipped files as needing attention", async () => {
    // The measured defect: the shell collapses on `done` BEFORE writing the status line, so
    // this message was composed and discarded and a partial import looked complete.
    // `buildOutcomeMessage` is the only formatter of `BuildStatus.errors` in the cockpit, so
    // the count had nowhere else to surface. The flag is what holds the panel open; that the
    // DOM then shows it is asserted in `tools/smoke_boot.mjs`, which vitest cannot do here.
    const h = harness(skipping(40));
    await h.controller.activate(codexStore());
    expect(h.controller.current.phase).toBe("done");
    expect(h.controller.current.needsAttention).toBe(true);
    expect(h.controller.current.status).toContain("40 files could not be read");
    expect(h.controller.current.status).toContain("2,003 conversations");
  });

  it("leaves a CLEAN import unmarked, so it still collapses silently", async () => {
    // Half of the requirement, and the half a careless fix breaks: nothing was skipped, so
    // there is nothing to read and no reason to keep the pane off the graph.
    const h = harness();
    await h.controller.activate(codexStore());
    expect(h.controller.current.phase).toBe("done");
    expect(h.controller.current.needsAttention).toBe(false);
  });

  it("does not mark a FAILED build, whose phase already keeps the panel open", async () => {
    const h = harness({
      async corpusBuildStatus(): Promise<BuildStatus> {
        return { state: "failed", error: "disk is full", indexed_conversations: 0, errors: [] };
      },
    });
    await h.controller.activate(codexStore());
    expect(h.controller.current.phase).toBe("error");
    expect(h.controller.current.needsAttention).toBe(false);
  });

  it("clears the mark when the user acknowledges it", async () => {
    const h = harness(skipping(3));
    await h.controller.activate(codexStore());
    const before = h.views.length;
    h.controller.acknowledge();
    expect(h.controller.current.needsAttention).toBe(false);
    // It emits, because the shell only collapses on a repaint.
    expect(h.views.length).toBe(before + 1);
    // And the outcome text is left intact: acknowledging is not forgetting.
    expect(h.controller.current.status).toContain("3 files could not be read");
  });

  it("acknowledging nothing is a no-op that does not repaint", async () => {
    const h = harness();
    await h.controller.activate(codexStore());
    const before = h.views.length;
    h.controller.acknowledge();
    h.controller.acknowledge();
    expect(h.views.length).toBe(before);
  });

  it("never lets a later transition inherit the mark", async () => {
    // `emit` merges patches, so without a per-transition reset the marker would survive into
    // an unrelated success and pin the panel open forever. Import 40 skipped files, then open
    // a corpus: that open must collapse.
    const h = harness(skipping(40));
    await h.controller.activate(codexStore());
    expect(h.controller.current.needsAttention).toBe(true);
    await h.controller.activate(builtIndex());
    expect(h.controller.current.phase).toBe("done");
    expect(h.controller.current.needsAttention).toBe(false);
  });

  /**
   * The other half of that rule, and the half a per-transition reset gets WRONG.
   *
   * `emit`'s blanket `needsAttention: false` is correct for a TRANSITION — a new phase with a
   * new status supersedes the old outcome. It is wrong for a RE-DERIVATION: `setCorpusAttached`
   * and `toggleGroup` patch only `groups`, leaving `phase` and `status` exactly as they were,
   * so clearing the marker there does not supersede the report — it deletes it. And because
   * the phase is still `done`, the next paint takes the collapse branch
   * (`discoveryPanel.ts:1099`) and the panel disappears, taking with it the ONLY place in the
   * cockpit that formats `BuildStatus.errors`.
   *
   * That contradicts this file's own stated delivery guarantee (`discoveryPanel.ts:961-966`:
   * "an import that skipped files cannot vanish before someone dismissed the report of it").
   */
  describe("an unread report survives a re-derivation that supersedes nothing", () => {
    /** A scan whose export group is over the row cap, so "+N more" is a real control. */
    function scanWithCappedGroup(): DiscoveryResult {
      return scan([
        codexStore(),
        ...Array.from({ length: 12 }, (_, i) =>
          finding({ path: `/d${i}/conversations.json`, newest_mtime: NOW_SEC - i }),
        ),
      ]);
    }

    /** By key, not by index: the store outranks the exports, so `groups[0]` is not it. */
    function exports(h: Harness): DiscoveryGroup | undefined {
      return h.controller.current.groups.find((g) => g.key === "chatgpt/export_file");
    }

    it("survives expanding a group, which changes no outcome at all", async () => {
      const h = harness(skipping(40), {}, scanWithCappedGroup());
      await h.controller.scan();
      await h.controller.activate(codexStore());
      expect(h.controller.current.needsAttention).toBe(true);
      expect(exports(h)?.expandLabel).toBe("+7 more");

      // Exactly what a user does next: the panel is open, the report is on screen, and
      // "+7 more" is sitting right underneath it, enabled.
      h.controller.toggleGroup("chatgpt/export_file");

      expect(exports(h)?.rows).toHaveLength(12);
      expect(h.controller.current.status).toContain("40 files could not be read");
      expect(h.controller.current.needsAttention).toBe(true);
    });

    it("survives collapsing it again", async () => {
      const h = harness(skipping(40), {}, scanWithCappedGroup());
      await h.controller.scan();
      await h.controller.activate(codexStore());
      h.controller.toggleGroup("chatgpt/export_file");
      h.controller.toggleGroup("chatgpt/export_file");
      expect(h.controller.current.needsAttention).toBe(true);
    });

    it("survives the corpus bar re-labelling the rows", async () => {
      // `app.ts:190-196` calls `setCorpusAttached` whenever the TOP BAR attaches a corpus, so
      // opening one by hand while the report is up runs this path. The re-label changes what
      // an import would mean; it does not tell the user which files were skipped, so it is
      // not entitled to throw that away.
      const h = harness(skipping(40));
      await h.controller.scan();
      await h.controller.activate(codexStore());
      expect(h.controller.current.needsAttention).toBe(true);

      h.controller.setCorpusAttached("C:\\somewhere-else.db");

      expect(h.controller.current.status).toContain("40 files could not be read");
      expect(h.controller.current.needsAttention).toBe(true);
    });

    it("is still the ACKNOWLEDGEMENT that clears it, not the re-derivation", async () => {
      // The fix must not make the marker permanent: dismissing is what releases the panel.
      const h = harness(skipping(40), {}, scanWithCappedGroup());
      await h.controller.scan();
      await h.controller.activate(codexStore());
      h.controller.toggleGroup("chatgpt/export_file");
      h.controller.acknowledge();
      expect(h.controller.current.needsAttention).toBe(false);
      expect(h.controller.current.status).toContain("40 files could not be read");
    });

    it("does not resurrect a mark on a group toggle after a CLEAN import", async () => {
      // The inverse guard: preserving must copy the CURRENT value, not force it true.
      const h = harness({}, {}, scanWithCappedGroup());
      await h.controller.scan();
      await h.controller.activate(codexStore());
      expect(h.controller.current.needsAttention).toBe(false);
      h.controller.toggleGroup("chatgpt/export_file");
      expect(h.controller.current.needsAttention).toBe(false);
    });
  });
});

describe("DiscoveryPanelController.setCorpusAttached", () => {
  it("re-labels the import once a corpus is open", async () => {
    const h = harness();
    await h.controller.scan();
    const before = h.controller.current.groups.find((g) => g.key === "codex/session_store");
    expect(before?.rows[0].action.label).toBe("Import…");
    h.controller.setCorpusAttached("C:\\existing.db");
    const after = h.controller.current.groups.find((g) => g.key === "codex/session_store");
    expect(after?.rows[0].action.label).toBe("Import into open corpus");
  });

  it("does not emit before there is anything to re-derive", () => {
    const h = harness();
    h.controller.setCorpusAttached("C:\\existing.db");
    expect(h.views).toHaveLength(0);
  });
});

describe("DiscoveryPanelController.toggleGroup", () => {
  it("expands and collapses a capped group", async () => {
    const many = Array.from({ length: 12 }, (_, i) =>
      finding({ path: `/d${i}/conversations.json`, newest_mtime: NOW_SEC - i }),
    );
    const h = harness({}, {}, scan(many));
    await h.controller.scan();
    expect(h.controller.current.groups[0].rows).toHaveLength(5);
    h.controller.toggleGroup("chatgpt/export_file");
    expect(h.controller.current.groups[0].rows).toHaveLength(12);
    h.controller.toggleGroup("chatgpt/export_file");
    expect(h.controller.current.groups[0].rows).toHaveLength(5);
  });

  it("survives a toggle before any scan has produced something to group", () => {
    // Unlike `setCorpusAttached`, this one re-derives unconditionally, so the no-scan case
    // reaches `regroup` with no result at all. It must answer an empty list rather than
    // throw: nothing in the shell can guarantee the ordering of a boot-time toggle.
    const h = harness();
    h.controller.toggleGroup("chatgpt/export_file");
    expect(h.controller.current.groups).toEqual([]);
  });
});
