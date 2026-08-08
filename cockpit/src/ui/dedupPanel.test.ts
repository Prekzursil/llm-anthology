/**
 * The dedup panel's DECISIONS — what a duplicate claim is allowed to say, where the
 * `codex_home` it scans comes from, and which of the four empty readings applies.
 *
 * Why these are testable at all: vitest runs `environment: "node"` here
 * (`cockpit/vitest.config.ts`), so there is no `document`. Every rule below lives in a pure
 * function or the DOM-free controller, with `DedupPanel` doing nothing but a native
 * directory dialog and a `textContent` paint — the seam `ui/discoveryPanel` and
 * `ui/corpusBar` already use.
 *
 * WHAT THESE TESTS CANNOT SETTLE, stated here rather than implied: no test here runs a real
 * `dedup.scan`, so they prove the panel reasons correctly GIVEN the wire shape — not that
 * the wire shape is right. The shape is pinned by reading `llm_anthology/dedup.py`,
 * `llm_anthology/sidecar.py:1407-1455` and `tests/test_sidecar_dedup.py`, and is cited at
 * each place it matters.
 */
import { describe, expect, it, vi } from "vitest";

import type {
  DedupScanResult,
  DedupSession,
  DiscoveryFinding,
  DiscoveryResult,
} from "../ipc/types";
import {
  ACCUMULATED_VIEW_NOTE,
  checkCodexHome,
  codexHomeCandidates,
  DedupPanelController,
  emptyReading,
  expandLabel,
  groupDigits,
  IDENTITY_BASIS_NOTE,
  MAX_GROUP_ROWS,
  NEVER_SCANNED_LABEL,
  partitionSessions,
  pathSummary,
  relativeAge,
  REPORT_ONLY_NOTE,
  scanNotes,
  truncationWarning,
  type DedupDeps,
  type DedupIpc,
  type DedupView,
} from "./dedupPanel";

// ---------------------------------------------------------------------------
// fixtures — shaped from the engine, not invented
// ---------------------------------------------------------------------------

const NOW = 1_800_000_000_000;
const HOME = "C:\\Users\\owner\\.codex";

/** A logical session with every field defaulted to the boring case. */
function session(over: Partial<DedupSession> = {}): DedupSession {
  return {
    session_id: "018f3a2c-0000-7c1e-9a01-aaaaaaaaaaaa",
    canonical_path: `${HOME}\\sessions\\2026\\08\\01\\rollout-a.jsonl`,
    store_kind: "live",
    size_bytes: 412_880,
    last_write_ms: NOW - 300_000,
    copy_count: 1,
    duplicate_paths: [],
    is_identified: true,
    has_larger_copy: false,
    ...over,
  };
}

/** A two-copy collapse: live canonical, one backup demoted. */
function pair(over: Partial<DedupSession> = {}): DedupSession {
  return session({
    copy_count: 2,
    duplicate_paths: [`${HOME}\\sessions_backup\\2026\\08\\01\\rollout-a.jsonl`],
    ...over,
  });
}

function scanResult(over: Partial<DedupScanResult> = {}): DedupScanResult {
  return {
    session_count: 2,
    copy_count: 3,
    duplicate_count: 1,
    flagged_truncated: 0,
    unidentified: 0,
    errors: [],
    ...over,
  };
}

function finding(over: Partial<DiscoveryFinding> = {}): DiscoveryFinding {
  return {
    provider: "codex",
    kind: "session_store",
    path: HOME,
    count: 2043,
    newest_mtime: (NOW - 900_000) / 1000,
    confidence: "high",
    detail: { items_root: `${HOME}\\sessions`, rollouts_zst: 2024 },
    ...over,
  };
}

function discovery(findings: DiscoveryFinding[]): DiscoveryResult {
  return {
    findings,
    stats: {
      elapsed_seconds: 1.8,
      roots_scanned: 12,
      dirs_visited: 400,
      files_examined: 9000,
      budget_exhausted: false,
      truncated_groups: [],
      errors: [],
    },
  };
}

// ---------------------------------------------------------------------------
// formatting
// ---------------------------------------------------------------------------

// NOTE: sizes are formatted by `ui/exportPanel`'s `formatBytes`, which this module imports
// rather than re-implementing. Its contract is specified in `exportPanel.test.ts`; the
// assertions below only pin how this panel USES it (e.g. that a kept copy shows its size at
// all, and that a zero-byte rollout — a real row in `ipc/mock.ts:551` — still renders).

describe("groupDigits", () => {
  it("groups thousands without reading the host locale", () => {
    // Hand-rolled for the same reason `ui/discoveryPanel` hand-rolls its own:
    // `toLocaleString()` emits "2,043" on one machine and "2 043" on another, so any
    // assertion over this text would pass or fail depending on whose box ran it.
    expect(groupDigits(2043)).toBe("2,043");
    expect(groupDigits(0)).toBe("0");
  });

  it("passes a non-finite value through rather than grouping garbage", () => {
    expect(groupDigits(Number.NaN)).toBe("NaN");
  });
});

describe("relativeAge", () => {
  it("says the date is unknown rather than showing 1970", () => {
    // `last_write_ms` is `Optional[int]` (`dedup.py:118`) and the mock carries a real null
    // (`ipc/mock.ts:553`). `new Date(null)` is 1 January 1970, which reads as a real date.
    expect(relativeAge(null, NOW)).toBe("date unknown");
  });

  it("reports ordinary ages", () => {
    expect(relativeAge(NOW - 30_000, NOW)).toBe("just now");
    expect(relativeAge(NOW - 300_000, NOW)).toBe("5 minutes ago");
    expect(relativeAge(NOW - 7_200_000, NOW)).toBe("2 hours ago");
    expect(relativeAge(NOW - 3 * 86_400_000, NOW)).toBe("3 days ago");
  });

  it("scales past a month, which a long-lived store will contain", () => {
    // A real Codex home holds years of rollouts, so these two branches are the common case
    // for the oldest rows rather than an edge.
    expect(relativeAge(NOW - 60 * 86_400_000, NOW)).toBe("2 months ago");
    expect(relativeAge(NOW - 800 * 86_400_000, NOW)).toBe("2 years ago");
  });
});

describe("pathSummary", () => {
  it("splits either separator, because these are Windows paths off the wire", () => {
    expect(pathSummary(`${HOME}\\sessions\\rollout-a.jsonl`)).toEqual({
      name: "rollout-a.jsonl",
      parent: `${HOME}\\sessions`,
    });
    expect(pathSummary("/home/o/.codex/sessions/rollout-a.jsonl").name).toBe("rollout-a.jsonl");
  });
});

// ---------------------------------------------------------------------------
// where `codex_home` comes from
// ---------------------------------------------------------------------------

describe("checkCodexHome", () => {
  it("refuses a UNC path with the reason the engine would give", () => {
    // Refused BEFORE the call, because the engine's own refusal is a -32602 the user cannot
    // read, and because a UNC root makes the scan emit outbound SMB/NTLM
    // (`tests/test_sidecar_dedup.py:90-96`, `sidecar.py:496-497`).
    for (const bad of ["\\\\evil.example\\share\\codex", "//evil.example/share/codex"]) {
      const verdict = checkCodexHome(bad);
      expect(verdict.ok).toBe(false);
      if (!verdict.ok) expect(verdict.reason).toMatch(/network/i);
    }
  });

  it("refuses a relative or empty path", () => {
    for (const bad of ["relative/codex", ""]) {
      expect(checkCodexHome(bad).ok).toBe(false);
    }
  });

  it("accepts a drive-absolute or POSIX-absolute local path", () => {
    expect(checkCodexHome(HOME).ok).toBe(true);
    expect(checkCodexHome("/home/owner/.codex").ok).toBe(true);
  });
});

describe("codexHomeCandidates", () => {
  it("offers a discovered Codex store, whose `path` IS the codex home", () => {
    // `discover.py:298-302` gives the Codex `StoreSpec` `report="base"`, so its finding's
    // `path` is the BASE (the codex home) and `detail.items_root` is the sessions tree —
    // the same fact `discoveryPanel.deriveBuildParams` relies on to fill `codex_home`.
    const out = codexHomeCandidates(discovery([finding()]), NOW);
    expect(out).toHaveLength(1);
    expect(out[0].path).toBe(HOME);
    expect(out[0].summary).toContain("2,043");
  });

  it("offers nothing for a non-Codex store, because dedup only reads the Codex layout", () => {
    // `dedup.known_store_roots` builds `<home>/sessions` and `<home>/sessions_backup`
    // (`dedup.py:105-106`). Pointed at a Grok or Claude Code home those two directories do
    // not exist, so the scan returns an EMPTY result that reads as "no duplicates" while
    // actually meaning "wrong layout" — a false clean bill of health about the owner's data.
    const out = codexHomeCandidates(
      discovery([
        finding({ provider: "grok", path: "C:\\g\\sessions", detail: { items_root: "C:\\g\\sessions" } }),
        finding({ provider: "claude-code", path: "C:\\c\\projects", detail: { items_root: "C:\\c\\projects" } }),
      ]),
      NOW,
    );
    expect(out).toEqual([]);
  });

  it("ignores a built index and a downloaded export", () => {
    const out = codexHomeCandidates(
      discovery([
        finding({ kind: "built_index", detail: {} }),
        finding({ kind: "export_file", detail: {} }),
      ]),
      NOW,
    );
    expect(out).toEqual([]);
  });

  it("skips a Codex store whose shape does not prove `path` is a home", () => {
    // No `items_root`, or one equal to `path`, means the finding named the item tree itself
    // rather than a base — so nothing in it identifies a home and offering it would be a
    // guess. The settling change is a capability field on the finding.
    expect(codexHomeCandidates(discovery([finding({ detail: {} })]), NOW)).toEqual([]);
    expect(
      codexHomeCandidates(discovery([finding({ detail: { items_root: HOME } })]), NOW),
    ).toEqual([]);
  });

  it("offers the most recently used store first, then breaks the tie on path", () => {
    const older = finding({ path: "C:\\b\\.codex", newest_mtime: 1_000, detail: { items_root: "C:\\b\\.codex\\sessions" } });
    const newer = finding({ path: "C:\\z\\.codex", newest_mtime: 2_000, detail: { items_root: "C:\\z\\.codex\\sessions" } });
    expect(codexHomeCandidates(discovery([older, newer]), NOW).map((c) => c.path)).toEqual([
      "C:\\z\\.codex",
      "C:\\b\\.codex",
    ]);

    // Equal mtimes must still yield a stable order, or the list reshuffles between renders.
    const tieA = finding({ path: "C:\\a\\.codex", newest_mtime: 5_000, detail: { items_root: "C:\\a\\.codex\\sessions" } });
    const tieB = finding({ path: "C:\\c\\.codex", newest_mtime: 5_000, detail: { items_root: "C:\\c\\.codex\\sessions" } });
    expect(codexHomeCandidates(discovery([tieB, tieA]), NOW).map((c) => c.path)).toEqual([
      "C:\\a\\.codex",
      "C:\\c\\.codex",
    ]);
  });

  it("says the date is unknown when the scan saw nothing datable", () => {
    // `newest_mtime` is 0 — never null — when no datable item was seen (`discover.py:552`),
    // and 0 through `new Date()` is 1 January 1970.
    const out = codexHomeCandidates(discovery([finding({ newest_mtime: 0 })]), NOW);
    expect(out[0].summary).toContain("date unknown");
  });

  it("drops a candidate the engine would refuse anyway", () => {
    const unc = "\\\\host\\share\\.codex";
    const out = codexHomeCandidates(
      discovery([finding({ path: unc, detail: { items_root: `${unc}\\sessions` } })]),
      NOW,
    );
    expect(out).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// the four empty readings — the crux of this panel
// ---------------------------------------------------------------------------

describe("emptyReading", () => {
  it("does not call a never-scanned panel clean", () => {
    // `dedup.sessions` is `[]` before any scan (`tests/test_sidecar_dedup.py:170-171`).
    // That is the absence of a measurement, not the absence of duplicates, and saying "no
    // duplicates" here would be a claim about the owner's data that nothing supports.
    const reading = emptyReading(null, "");
    expect(reading.kind).toBe("never-scanned");
    expect(reading.label).toBe(NEVER_SCANNED_LABEL);
    expect(reading.label).not.toMatch(/no duplicate|no redundant/i);
  });

  it("reads an all-zero scan as nothing found THERE, not as a clean store", () => {
    // A MISSING home produces exactly this tally — zero sessions, zero copies, no errors
    // (`tests/test_sidecar_dedup.py:101-107`) — so it is indistinguishable from a real but
    // empty store. The likely cause is the wrong directory, and the message has to say so.
    const reading = emptyReading(scanResult({ session_count: 0, copy_count: 0, duplicate_count: 1 }), HOME);
    expect(reading.kind).toBe("no-files");
    expect(reading.label).toContain(HOME);
    expect(reading.label).not.toMatch(/no duplicate|no redundant/i);
  });

  it("reads sessions-but-no-duplicates as the genuinely clean result", () => {
    const reading = emptyReading(scanResult({ session_count: 40, copy_count: 40, duplicate_count: 0 }), HOME);
    expect(reading.kind).toBe("no-duplicates");
    expect(reading.label).toContain("40");
    expect(reading.label).toMatch(/redundant/i);
  });

  it("has no empty label at all once duplicates exist", () => {
    const reading = emptyReading(scanResult({ duplicate_count: 1 }), HOME);
    expect(reading.kind).toBe("none");
    expect(reading.label).toBe("");
  });
});

// ---------------------------------------------------------------------------
// what a row is allowed to claim
// ---------------------------------------------------------------------------

describe("partitionSessions", () => {
  it("counts a single-copy session but never lists it as a duplicate", () => {
    const out = partitionSessions([session(), pair()], { nowMs: NOW });
    expect(out.duplicates).toHaveLength(1);
    expect(out.singleCopyCount).toBe(1);
  });

  it("separates an unidentified file from the duplicate groups", () => {
    // `is_identified: false` means no id could be recovered from the file OR its name, so
    // `consolidate` keyed it by PATH and never merged it (`dedup.py:38-44`, `:174-180`).
    // Listing it among duplicates would report the one thing it is not.
    const out = partitionSessions(
      [session({ session_id: "", is_identified: false }), pair()],
      { nowMs: NOW },
    );
    expect(out.duplicates).toHaveLength(1);
    expect(out.unidentified).toHaveLength(1);
    expect(out.unidentified[0].kept.path).toContain("rollout-a.jsonl");
  });

  it("names which copy is kept and which are merely also on disk", () => {
    const out = partitionSessions([pair()], { nowMs: NOW });
    const row = out.duplicates[0];
    expect(row.kept.role).toBe("kept");
    expect(row.kept.detail).toContain("live");
    expect(row.kept.detail).toContain("403.2 KB");
    expect(row.others).toHaveLength(1);
    expect(row.others[0].role).toBe("other");
  });

  it("claims no size or date for a non-canonical copy, because the wire carries none", () => {
    // `duplicate_paths` is paths ONLY (`sidecar.py:1423`, `dedup.py:138-141`) — no size, no
    // mtime, no store kind. A row that formatted those would be inventing them.
    const out = partitionSessions([pair()], { nowMs: NOW });
    expect(out.duplicates[0].others[0].detail).toBe("");
  });

  it("states the identity basis rather than leaving the match unexplained", () => {
    const out = partitionSessions([pair()], { nowMs: NOW });
    expect(out.duplicates[0].basis).toContain(pair().session_id);
    expect(IDENTITY_BASIS_NOTE).toMatch(/session id/i);
  });

  it("puts a truncation-flagged group first, then the widest redundancy", () => {
    const flagged = pair({ session_id: "id-flagged", has_larger_copy: true, size_bytes: 1_204 });
    const wide = pair({
      session_id: "id-wide",
      copy_count: 4,
      duplicate_paths: ["p1", "p2", "p3"],
    });
    const narrow = pair({ session_id: "id-narrow" });
    const out = partitionSessions([narrow, wide, flagged], { nowMs: NOW });
    expect(out.duplicates.map((r) => r.session.session_id)).toEqual([
      "id-flagged",
      "id-wide",
      "id-narrow",
    ]);
  });

  it("puts the larger session first when the redundancy is the same width", () => {
    const big = pair({ session_id: "id-big", size_bytes: 900_000 });
    const small = pair({ session_id: "id-small", size_bytes: 1_000 });
    const out = partitionSessions([small, big], { nowMs: NOW });
    expect(out.duplicates.map((r) => r.session.session_id)).toEqual(["id-big", "id-small"]);
  });

  it("orders unidentified files by path so the section does not reshuffle", () => {
    const rows = [
      session({ session_id: "", is_identified: false, canonical_path: "C:\\z\\r.jsonl" }),
      session({ session_id: "", is_identified: false, canonical_path: "C:\\a\\r.jsonl" }),
    ];
    const out = partitionSessions(rows, { nowMs: NOW });
    expect(out.unidentified.map((r) => r.kept.path)).toEqual(["C:\\a\\r.jsonl", "C:\\z\\r.jsonl"]);
  });

  it("caps the list and reports how many it is holding back", () => {
    const many = Array.from({ length: MAX_GROUP_ROWS + 3 }, (_, i) =>
      pair({ session_id: `id-${i}` }),
    );
    const capped = partitionSessions(many, { nowMs: NOW });
    expect(capped.duplicates).toHaveLength(MAX_GROUP_ROWS);
    expect(capped.hiddenCount).toBe(3);
    expect(capped.expandLabel).toBe("+3 more");

    const all = partitionSessions(many, { nowMs: NOW, expanded: true });
    expect(all.duplicates).toHaveLength(MAX_GROUP_ROWS + 3);
    expect(all.hiddenCount).toBe(0);
    expect(all.expandLabel).toBe("Show fewer");
  });

  it("is stable over the same input", () => {
    const input = [pair({ session_id: "b" }), pair({ session_id: "a" })];
    const first = partitionSessions(input, { nowMs: NOW });
    const second = partitionSessions(input, { nowMs: NOW });
    expect(first.duplicates.map((r) => r.session.session_id)).toEqual(
      second.duplicates.map((r) => r.session.session_id),
    );
  });
});

describe("expandLabel", () => {
  it("offers nothing to toggle when nothing is hidden and nothing is expanded", () => {
    expect(expandLabel(0, false)).toBe("");
  });
});

describe("truncationWarning", () => {
  it("is silent unless the kept copy is the shorter one", () => {
    expect(truncationWarning(pair())).toBe("");
  });

  it("names the fuller copy when there is exactly one other copy it can only be", () => {
    // `has_larger_copy` means canonical.size < max(all copies) (`dedup.py:163-164`). With a
    // single other copy, that copy IS the larger one, so naming it is a deduction.
    const row = pair({ has_larger_copy: true, size_bytes: 1_204 });
    const text = truncationWarning(row);
    expect(text).toContain(row.duplicate_paths[0]);
    expect(text).toMatch(/shorter|truncated/i);
  });

  it("refuses to name a fuller copy it cannot identify", () => {
    // With three other copies the wire says one of them is larger but not WHICH — no size
    // travels for a non-canonical copy — so the warning must stay unnamed.
    const row = pair({
      has_larger_copy: true,
      size_bytes: 1_204,
      copy_count: 4,
      duplicate_paths: ["C:\\a", "C:\\b", "C:\\c"],
    });
    const text = truncationWarning(row);
    expect(text).toMatch(/one of/i);
    expect(text).not.toContain("C:\\a");
  });
});

describe("scanNotes", () => {
  it("attributes the tally to THIS scan and warns the list is cumulative", () => {
    // `save_sessions` is INSERT OR REPLACE and never deletes (`dedup.py:302-311`) while
    // `load_sessions` re-consolidates every row in the table (`dedup.py:313-317`). So after
    // scanning two homes the list holds both and the tally holds only the second — a
    // mismatch that is correct behaviour and would otherwise read as a bug.
    const notes = scanNotes(scanResult());
    expect(notes.join(" ")).toMatch(/this scan/i);
    expect(ACCUMULATED_VIEW_NOTE).toMatch(/earlier scan|previous scan|already scanned/i);
  });

  it("surfaces the truncation and unidentified counters", () => {
    const notes = scanNotes(scanResult({ flagged_truncated: 2, unidentified: 3 })).join(" ");
    expect(notes).toMatch(/2/);
    expect(notes).toMatch(/3/);
  });

  it("counts unreadable locations without pasting raw engine strings", () => {
    const notes = scanNotes(scanResult({ errors: ["C:\\x: [WinError 5] Access is denied"] })).join(" ");
    expect(notes).not.toContain("WinError");
  });

  it("says nothing extra for a clean scan", () => {
    const notes = scanNotes(scanResult({ flagged_truncated: 0, unidentified: 0, errors: [] }));
    expect(notes.join(" ")).not.toMatch(/skipped|truncat|unidentif/i);
  });

  it("agrees with itself about singular and plural", () => {
    // Every counter here is a count the user reads, and "1 files are repeats" is the exact
    // sloppiness that makes a report about someone's irreplaceable data look untrustworthy.
    const one = scanNotes(
      scanResult({
        copy_count: 1,
        session_count: 1,
        duplicate_count: 2,
        flagged_truncated: 1,
        unidentified: 1,
        errors: ["a", "b"],
      }),
    ).join(" ");
    expect(one).toContain("1 file holding 1 session");
    expect(one).toContain("2 files are repeats");
    expect(one).toContain("1 session shows");
    expect(one).toContain("1 file has");
    expect(one).toContain("2 locations were");
  });
});

describe("REPORT_ONLY_NOTE", () => {
  it("says plainly that this panel removes nothing", () => {
    // `dedup` contains no write/delete/move call (`sidecar.py:1400-1405`), and the only
    // destructive surface on the wire is `maintenance.execute` (`ipc/types.ts:986-992`).
    // A panel that merely omitted a delete button would still read as one that has not
    // grown one yet.
    expect(REPORT_ONLY_NOTE).toMatch(/nothing|never|no file/i);
    expect(REPORT_ONLY_NOTE).toMatch(/delet|remov/i);
  });
});

// ---------------------------------------------------------------------------
// controller
// ---------------------------------------------------------------------------

interface Harness {
  ipc: DedupIpc;
  deps: DedupDeps;
  views: DedupView[];
  controller: DedupPanelController;
}

function harness(over: Partial<DedupIpc> = {}, deps: Partial<DedupDeps> = {}): Harness {
  const ipc: DedupIpc = {
    dedupScan: vi.fn(async () => scanResult()),
    dedupSessions: vi.fn(async () => [pair()]),
    discoverSources: vi.fn(async () => discovery([finding()])),
    ...over,
  };
  const resolved: DedupDeps = {
    now: (): number => NOW,
    chooseCodexHome: vi.fn(async () => HOME),
    ...deps,
  };
  const views: DedupView[] = [];
  const controller = new DedupPanelController(ipc, resolved, (v) => views.push(v));
  return { ipc, deps: resolved, views, controller };
}

describe("DedupPanelController", () => {
  it("starts with nothing measured and says so", () => {
    const { controller } = harness();
    expect(controller.current.phase).toBe("idle");
    expect(controller.current.empty.kind).toBe("never-scanned");
    expect(controller.current.duplicates).toEqual([]);
  });

  it("loads only the Codex candidates a scan may be pointed at", async () => {
    const { controller } = harness();
    await controller.loadCandidates();
    expect(controller.current.candidates.map((c) => c.path)).toEqual([HOME]);
  });

  it("reports a discovery failure without leaking a raw engine string as the whole UI", async () => {
    const { controller } = harness({
      discoverSources: vi.fn(async () => {
        throw new Error("no corpus attached: call open_corpus first");
      }),
    });
    await controller.loadCandidates();
    expect(controller.current.status).toMatch(/Open corpus/);
  });

  it("scans the home it was given and shows what came back", async () => {
    const { controller, ipc } = harness();
    await controller.scan(HOME);
    expect(ipc.dedupScan).toHaveBeenCalledWith(HOME);
    expect(ipc.dedupSessions).toHaveBeenCalled();
    expect(controller.current.phase).toBe("ready");
    expect(controller.current.duplicates).toHaveLength(1);
    expect(controller.current.home).toBe(HOME);
  });

  it("never guesses a home — an empty one is refused before any engine call", async () => {
    const { controller, ipc } = harness();
    await controller.scan("");
    expect(ipc.dedupScan).not.toHaveBeenCalled();
    expect(controller.current.phase).toBe("error");
  });

  it("refuses a UNC home locally rather than emitting outbound SMB", async () => {
    const { controller, ipc } = harness();
    await controller.scan("\\\\host\\share\\.codex");
    expect(ipc.dedupScan).not.toHaveBeenCalled();
    expect(controller.current.status).toMatch(/network/i);
  });

  it("distinguishes an empty store from a clean one", async () => {
    const empty = harness({
      dedupScan: vi.fn(async () => scanResult({ session_count: 0, copy_count: 0, duplicate_count: 0 })),
      dedupSessions: vi.fn(async () => []),
    });
    await empty.controller.scan(HOME);
    expect(empty.controller.current.empty.kind).toBe("no-files");

    const clean = harness({
      dedupScan: vi.fn(async () => scanResult({ session_count: 9, copy_count: 9, duplicate_count: 0 })),
      dedupSessions: vi.fn(async () => [session()]),
    });
    await clean.controller.scan(HOME);
    expect(clean.controller.current.empty.kind).toBe("no-duplicates");
  });

  it("maps a scan failure through the shared error text", async () => {
    const { controller } = harness({
      dedupScan: vi.fn(async () => {
        throw new Error("no corpus attached: call open_corpus first");
      }),
    });
    await controller.scan(HOME);
    expect(controller.current.phase).toBe("error");
    expect(controller.current.status).toMatch(/Open corpus/);
    expect(controller.current.status).not.toMatch(/open_corpus/);
  });

  it("is single-flight, so a double-click cannot start two scans", async () => {
    let release = (): void => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    const dedupScan = vi.fn(async () => {
      await gate;
      return scanResult();
    });
    const { controller } = harness({ dedupScan });
    const first = controller.scan(HOME);
    await controller.scan(HOME);
    release();
    await first;
    expect(dedupScan).toHaveBeenCalledTimes(1);
  });

  it("asks for a directory when the user has no candidate to click", async () => {
    const chooseCodexHome = vi.fn(async () => HOME);
    const { controller, ipc } = harness({}, { chooseCodexHome });
    await controller.pickHome();
    expect(chooseCodexHome).toHaveBeenCalled();
    expect(ipc.dedupScan).toHaveBeenCalledWith(HOME);
  });

  it("treats a dismissed dialog as a no-op, not a failure", async () => {
    const { controller, ipc } = harness({}, { chooseCodexHome: vi.fn(async () => null) });
    await controller.pickHome();
    expect(ipc.dedupScan).not.toHaveBeenCalled();
    expect(controller.current.phase).toBe("idle");
  });

  it("re-derives the list when the user expands it, without re-scanning", async () => {
    const many = Array.from({ length: MAX_GROUP_ROWS + 2 }, (_, i) => pair({ session_id: `id-${i}` }));
    const dedupSessions = vi.fn(async () => many);
    const { controller, ipc } = harness({ dedupSessions });
    await controller.scan(HOME);
    expect(controller.current.duplicates).toHaveLength(MAX_GROUP_ROWS);
    controller.toggleExpanded();
    expect(controller.current.duplicates).toHaveLength(MAX_GROUP_ROWS + 2);
    expect(ipc.dedupScan).toHaveBeenCalledTimes(1);
    expect(dedupSessions).toHaveBeenCalledTimes(1);
  });

  it("emits nothing once destroyed", async () => {
    const { controller, views } = harness();
    controller.destroy();
    await controller.scan(HOME);
    expect(views).toEqual([]);
  });

  it("ignores an expand after destroy", () => {
    const { controller, views } = harness();
    controller.destroy();
    controller.toggleExpanded();
    expect(views).toEqual([]);
  });

  it("loads candidates once even if asked twice at the same moment", async () => {
    let release = (): void => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    const discoverSources = vi.fn(async () => {
      await gate;
      return discovery([finding()]);
    });
    const { controller } = harness({ discoverSources });
    const first = controller.loadCandidates();
    await controller.loadCandidates();
    release();
    await first;
    expect(discoverSources).toHaveBeenCalledTimes(1);
  });

  it("says so when discovery finds no Codex store to offer", async () => {
    const { controller } = harness({ discoverSources: vi.fn(async () => discovery([])) });
    await controller.loadCandidates();
    expect(controller.current.candidates).toEqual([]);
    expect(controller.current.status).toMatch(/choose a folder/i);
  });

  /*
   * THE TEARDOWN CONTRACT. Each of the three awaits in this controller can complete after the
   * panel is gone, and every one of them is followed by a disposed check. These tests destroy
   * the controller mid-flight and assert that nothing lands afterwards — without them the
   * guards are unexercised lines that could be deleted with the suite still green, which is
   * exactly how a paint-after-teardown crash gets shipped.
   */
  function gated<T>(value: () => T): { fn: () => Promise<T>; release: () => void } {
    let release = (): void => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    return {
      fn: async (): Promise<T> => {
        await gate;
        return value();
      },
      release: (): void => release(),
    };
  }

  it("does not land a candidate list on a destroyed panel", async () => {
    const g = gated(() => discovery([finding()]));
    const { controller, views } = harness({ discoverSources: g.fn });
    const pending = controller.loadCandidates();
    controller.destroy();
    g.release();
    await pending;
    expect(views.some((v) => v.candidates.length > 0)).toBe(false);
  });

  it("does not land a scan tally on a destroyed panel", async () => {
    const g = gated(() => scanResult());
    const { controller, views } = harness({ dedupScan: g.fn });
    const pending = controller.scan(HOME);
    controller.destroy();
    g.release();
    await pending;
    expect(views.some((v) => v.phase === "ready")).toBe(false);
  });

  it("does not land a session list on a destroyed panel", async () => {
    // The window BETWEEN the two engine calls, which the previous test cannot reach: there,
    // disposal already happened before `dedup.scan` resolved. Tearing down from inside the
    // `dedup.sessions` call puts it in the gap deterministically, with no tick-counting.
    let ref: DedupPanelController | null = null;
    const dedupSessions = vi.fn(async () => {
      ref?.destroy();
      return [pair()];
    });
    const h = harness({ dedupSessions });
    ref = h.controller;
    await h.controller.scan(HOME);
    expect(h.views.some((v) => v.duplicates.length > 0)).toBe(false);
  });

  it("cannot paint after teardown even from a path that forgets to check", async () => {
    // `emit` is the single choke point, and its own disposed guard is the last line of
    // defence — `pickHome`'s error arm calls it without a check of its own. Without this
    // test that guard is a deletable line and a paint-after-teardown crash ships.
    let ref: DedupPanelController | null = null;
    const chooseCodexHome = vi.fn(async () => {
      ref?.destroy();
      throw new Error("dialog closed by teardown");
    });
    const h = harness({}, { chooseCodexHome });
    ref = h.controller;
    await h.controller.pickHome();
    expect(h.views).toEqual([]);
  });

  it("does not scan a folder chosen after the panel was destroyed", async () => {
    const g = gated<string | null>(() => HOME);
    const { controller, ipc } = harness({}, { chooseCodexHome: g.fn });
    const pending = controller.pickHome();
    controller.destroy();
    g.release();
    await pending;
    expect(ipc.dedupScan).not.toHaveBeenCalled();
  });

  it("opens only one folder picker at a time", async () => {
    const g = gated<string | null>(() => null);
    const chooseCodexHome = vi.fn(g.fn);
    const { controller } = harness({}, { chooseCodexHome });
    const first = controller.pickHome();
    await controller.pickHome();
    g.release();
    await first;
    expect(chooseCodexHome).toHaveBeenCalledTimes(1);
  });

  it("reports a folder picker that fails instead of dying silently", async () => {
    const { controller, ipc } = harness(
      {},
      {
        chooseCodexHome: vi.fn(async () => {
          throw new Error("dialog plugin unavailable");
        }),
      },
    );
    await controller.pickHome();
    expect(ipc.dedupScan).not.toHaveBeenCalled();
    expect(controller.current.status).toMatch(/folder picker/i);
    expect(controller.current.busy).toBe(false);
  });
});
