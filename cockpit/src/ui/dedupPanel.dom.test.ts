// @vitest-environment happy-dom
/**
 * {@link DedupPanel} — the DOM half of the duplicate-copies report.
 *
 * WHY A SECOND FILE. `dedupPanel.test.ts` specifies the pure derivations and the DOM-free
 * controller and must stay on the suite default (`environment: "node"`). This file opts in
 * per-file to `happy-dom`, the pattern `ui/scrubber.test.ts` and `ui/virtualList.test.ts`
 * already use; `cockpit/vitest.config.ts` records the measurement showing a GLOBAL flip takes
 * four unrelated test files down.
 *
 * WHY THE TAURI DIALOG IS MOCKED. `defaultChooseCodexHome` is the panel's own fallback picker
 * and the ONLY code here that leaves the process. It is not injectable — it is the `??`
 * fallback for `deps.chooseCodexHome` — so the only way to specify it is to mock the plugin and
 * construct the panel WITHOUT that dep, which is also exactly how `app.ts` constructs it. Its
 * `string | string[] | null` return is the reason the array case exists in the source: a
 * signature change there would otherwise put `codex_home: "['C:\\…']"` on the wire.
 *
 * WHAT THIS FILE IS FOR, given the class is meant to be decision-free:
 *
 *   1. THE WIRING. Three controls (scan a candidate, choose a folder, expand the list) are
 *      wired to controller methods. A listener that was never attached is a dead button, and a
 *      controller test cannot see it — every actuation below is a real `click`.
 *   2. THE FOUR EMPTY READINGS, as distinct DOM. The module's central claim is that a
 *      never-scanned panel and a clean store must not be able to look identical, and it makes
 *      that true through a per-kind class name. That is only checkable against real nodes.
 *   3. WHAT A ROW IS ALLOWED TO SHOW. A non-canonical copy travels as a PATH ONLY, so its row
 *      must carry no size/date/store element at all — an absence, which is precisely the kind
 *      of thing a snapshot would happily record as correct while it silently regressed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  DedupScanResult,
  DedupSession,
  DiscoveryFinding,
  DiscoveryResult,
} from "../ipc/types";
import {
  DedupPanel,
  IDENTITY_BASIS_NOTE,
  NEVER_SCANNED_LABEL,
  REPORT_ONLY_NOTE,
  type DedupDeps,
  type DedupIpc,
} from "./dedupPanel";

/**
 * The native directory dialog, stubbed. Hoisted because `vi.mock` is lifted above the imports,
 * so a plain `const` would be in its temporal dead zone when the factory runs.
 */
const dialog = vi.hoisted(() => ({
  /** What the next `open()` resolves to — the plugin's real `string | string[] | null`. */
  picked: null as string | string[] | null,
  /** Every options object the panel asked with, so the ASK itself can be asserted. */
  asks: [] as unknown[],
}));

vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: async (options: unknown): Promise<string | string[] | null> => {
    dialog.asks.push(options);
    return dialog.picked;
  },
}));

// ---------------------------------------------------------------------------
// fixtures — same shapes as the node file, which cites the engine for each
// ---------------------------------------------------------------------------

/*
 * These repeat `dedupPanel.test.ts`'s `session`/`pair`/`scanResult`/`finding` builders, and the
 * repetition is FORCED rather than lazy. Importing them from that file would execute its
 * `describe` blocks inside this file's module graph, re-running ~70 node-environment tests under
 * happy-dom — the two files run under DIFFERENT environments by design, so they cannot share one
 * graph. Lifting them into a `dedupPanel.fixtures.ts` would instead add a non-test `.ts` module
 * to the coverage denominator. So: do not "fix" this by importing across the two test files.
 */

const NOW = 1_800_000_000_000;
const HOME = "C:\\Users\\owner\\.codex";

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
    detail: { items_root: `${HOME}\\sessions` },
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
// harness
// ---------------------------------------------------------------------------

function must<T extends Element>(root: ParentNode, selector: string): T {
  const el = root.querySelector<T>(selector);
  if (el === null) throw new Error(`no element matched ${selector}`);
  return el;
}

function all<T extends Element>(root: ParentNode, selector: string): T[] {
  return [...root.querySelectorAll<T>(selector)];
}

/**
 * Drain the promise chain a click started — the handlers are `() => void controller.scan(…)`,
 * so nothing is returned for a test to await. Every fake resolves without a timer, so the
 * chain's microtasks all drain BEFORE the next macrotask, which makes one `setTimeout(0)` a
 * deterministic barrier rather than a sleep.
 */
function settle(): Promise<void> {
  return new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });
}

interface Fake extends DedupIpc {
  /** Make the NEXT `dedup.scan` block, so the in-flight paint can be observed. */
  arm(): void;
  /** Let an armed `dedup.scan` finish. Throws rather than no-op if nothing is armed. */
  release(): void;
}

function fakeIpc(over: Partial<DedupIpc> = {}): Fake {
  const state: { release: (() => void) | null; wait: Promise<void> | null } = {
    release: null, wait: null,
  };
  const base: DedupIpc = {
    async dedupScan() {
      if (state.wait !== null) {
        const wait = state.wait;
        state.wait = null;
        await wait;
      }
      return scanResult();
    },
    async dedupSessions() {
      return [pair()];
    },
    async discoverSources() {
      return discovery([finding()]);
    },
    ...over,
  };
  // Methods, never a getter: `Object.assign` copies a getter's VALUE at assign time, so a
  // `get release()` here would snapshot `null` forever and `release?.()` would silently do
  // nothing — leaving the scan pending and the test asserting a busy state it never left.
  // Measured: that exact shape passed the in-flight assertions and failed the released ones.
  return Object.assign(base, {
    arm(): void {
      state.wait = new Promise<void>((resolve) => {
        state.release = resolve;
      });
    },
    release(): void {
      if (state.release === null) throw new Error("release() with nothing armed");
      state.release();
      state.release = null;
    },
  });
}

/** The three-argument construction: an injected clock, picker and row cap. */
function mount(
  over: Partial<DedupIpc> = {},
  deps: Partial<DedupDeps> = {},
): { ipc: Fake; panel: DedupPanel; container: HTMLElement } {
  const ipc = fakeIpc(over);
  const container = document.createElement("div");
  document.body.append(container);
  const panel = new DedupPanel(ipc, container, {
    now: () => NOW,
    chooseCodexHome: async () => HOME,
    // A cap of 2 rather than the shipped 8: the collapse rule is the same code, and
    // `MAX_GROUP_ROWS` itself is already pinned in `dedupPanel.test.ts`. Small fixtures keep
    // what each assertion is about visible.
    maxRows: 2,
    ...deps,
  });
  return { ipc, panel, container };
}

beforeEach(() => {
  document.body.replaceChildren();
  dialog.picked = null;
  dialog.asks.length = 0;
});

afterEach(() => {
  document.body.replaceChildren();
});

// ---------------------------------------------------------------------------
// the skeleton
// ---------------------------------------------------------------------------

describe("DedupPanel skeleton", () => {
  it("paints the charter and the never-scanned reading before anything is measured", async () => {
    const { panel, container } = mount();
    await panel.load();

    expect(must(container, "h2.dedup-title").id).toBe("dedup-title");
    expect(container.getAttribute("role")).toBe("group");
    expect(container.getAttribute("aria-labelledby")).toBe("dedup-title");

    // Both standing facts are painted ABOVE any result rather than left as a footnote: what
    // this panel will never do, and what a duplicate claim rests on.
    const charter = must(container, ".dedup-charter");
    expect(charter.textContent).toContain(REPORT_ONLY_NOTE);
    expect(charter.textContent).toContain(IDENTITY_BASIS_NOTE);

    const empty = must(container, ".dedup-empty");
    expect(empty.textContent).toBe(NEVER_SCANNED_LABEL);
    expect(empty.classList.contains("dedup-empty-never-scanned")).toBe(true);
  });

  it("keeps ONE status element across repaints, so the live region can announce", async () => {
    // A `role="status"` node replaced on every paint lands each message in an element that was
    // not yet in the accessibility tree, and none of them is ever announced. `load()` paints at
    // least twice (in-flight, then the result), so identity across those two is the check.
    const { panel, container } = mount();
    const pending = panel.load();
    const first = must(container, ".dedup-status");
    expect(first.getAttribute("role")).toBe("status");
    expect(first.getAttribute("aria-live")).toBe("polite");
    expect(first.textContent).toMatch(/Looking for/i);

    await pending;
    expect(must(container, ".dedup-status")).toBe(first);
  });

  it("empties the container and drops its landmark roles on destroy", async () => {
    const { panel, container } = mount();
    await panel.load();
    expect(container.children.length).toBeGreaterThan(0);

    panel.destroy();
    expect(container.children.length).toBe(0);
    expect(container.getAttribute("role")).toBeNull();
    expect(container.getAttribute("aria-labelledby")).toBeNull();
  });

  it("does not rebuild itself when a scan lands after destroy", async () => {
    // The teardown race, at the DOM level: `ensureShell` would happily re-create the whole
    // skeleton inside a container the host has already reclaimed.
    const { ipc, panel, container } = mount();
    await panel.load();
    ipc.arm();
    must<HTMLButtonElement>(container, ".dedup-scan").click();
    panel.destroy();
    ipc.release();
    await settle();
    expect(container.children.length).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// choosing where to scan
// ---------------------------------------------------------------------------

describe("DedupPanel candidates", () => {
  it("offers each discovered home with its full path and what discovery measured", async () => {
    const { panel, container } = mount();
    await panel.load();
    expect(must(container, ".dedup-candidate-name").textContent).toBe(HOME);
    expect(must(container, ".dedup-candidate-summary").textContent)
      .toBe("2,043 sessions · 15 minutes ago · high confidence");
    expect(must(container, ".dedup-status").textContent).toBe("");
  });

  it("scans the home whose button was clicked, and only when it was clicked", async () => {
    // NO SCAN IS EVER AUTOMATIC — an automated probe really did read the owner's live Codex
    // sessions through a defaulted home, so `load()` must leave the engine untouched.
    const scans: string[] = [];
    const { panel, container } = mount({
      async dedupScan(codexHome) {
        scans.push(codexHome);
        return scanResult();
      },
    });
    await panel.load();
    expect(scans).toEqual([]);

    must<HTMLButtonElement>(container, ".dedup-scan").click();
    await settle();
    expect(scans).toEqual([HOME]);
    expect(all(container, ".dedup-row:not(.dedup-row-unidentified)")).toHaveLength(1);
  });

  it("changes the manual button's words according to whether anything was found", async () => {
    const found = mount();
    await found.panel.load();
    expect(must(found.container, ".dedup-pick").textContent).toBe("Scan another folder…");

    const none = mount({ discoverSources: async () => discovery([]) });
    await none.panel.load();
    expect(must(none.container, ".dedup-pick").textContent).toBe("Choose Codex folder…");
    expect(must(none.container, ".dedup-status").textContent).toMatch(/choose a folder/i);
  });

  it("disables every control while a scan is running", async () => {
    const { ipc, panel, container } = mount();
    await panel.load();
    ipc.arm();
    must<HTMLButtonElement>(container, ".dedup-scan").click();

    expect(must<HTMLButtonElement>(container, ".dedup-scan").disabled).toBe(true);
    expect(must<HTMLButtonElement>(container, ".dedup-pick").disabled).toBe(true);
    expect(must(container, ".dedup-status").textContent).toBe(`Scanning ${HOME}…`);

    ipc.release();
    await settle();
    expect(must<HTMLButtonElement>(container, ".dedup-scan").disabled).toBe(false);
    expect(must<HTMLButtonElement>(container, ".dedup-pick").disabled).toBe(false);
  });

  it("reports a refusal in the status line instead of starting a scan", async () => {
    const { panel, container } = mount({}, { chooseCodexHome: async () => "\\\\host\\share" });
    await panel.load();
    must<HTMLButtonElement>(container, ".dedup-pick").click();
    await settle();
    expect(must(container, ".dedup-status").textContent).toMatch(/network/i);
    expect(all(container, ".dedup-row")).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// the panel's own folder picker
// ---------------------------------------------------------------------------

describe("DedupPanel default folder picker", () => {
  /** Constructed with TWO arguments, so every `deps` default is the real one. */
  function bare(over: Partial<DedupIpc> = {}): { panel: DedupPanel; container: HTMLElement } {
    const container = document.createElement("div");
    document.body.append(container);
    return { panel: new DedupPanel(fakeIpc(over), container), container };
  }

  it("asks the OS for a directory, not a file", async () => {
    // A webview `<input type="file">` yields no filesystem path under Tauri v2, so the native
    // directory dialog is the only thing that can produce a `codex_home` at all.
    dialog.picked = HOME;
    const scans: string[] = [];
    const { panel, container } = bare({
      async dedupScan(codexHome) {
        scans.push(codexHome);
        return scanResult();
      },
    });
    await panel.load();
    must<HTMLButtonElement>(container, ".dedup-pick").click();
    await settle();

    expect(dialog.asks).toHaveLength(1);
    expect(dialog.asks[0]).toMatchObject({ directory: true, multiple: false });
    expect(scans).toEqual([HOME]);
  });

  it("uses the real clock when none is injected", async () => {
    // The default `now` is `() => Date.now()`. Asserted through the candidate summary, which is
    // the only place the clock is observable: a finding stamped five minutes before real now
    // must read as five minutes ago, which a frozen or zero clock could not produce.
    const { panel, container } = bare({
      async discoverSources() {
        return discovery([finding({ newest_mtime: (Date.now() - 300_000) / 1000 })]);
      },
    });
    await panel.load();
    expect(must(container, ".dedup-candidate-summary").textContent).toContain("5 minutes ago");
  });

  it("treats a dismissed dialog as a no-op", async () => {
    dialog.picked = null;
    const scans: string[] = [];
    const { panel, container } = bare({
      async dedupScan(codexHome) {
        scans.push(codexHome);
        return scanResult();
      },
    });
    await panel.load();
    must<HTMLButtonElement>(container, ".dedup-pick").click();
    await settle();
    expect(scans).toEqual([]);
    expect(must(container, ".dedup-empty").classList.contains("dedup-empty-never-scanned"))
      .toBe(true);
  });

  it("unwraps a one-element array rather than sending its text form on the wire", async () => {
    // `multiple: false` narrows the plugin's return to one path, but the array arm is handled
    // anyway: a plugin signature change would otherwise put `codex_home: "['C:\\…']"` on the
    // wire, and the engine would refuse a path that looks almost right.
    dialog.picked = [HOME];
    const scans: string[] = [];
    const { panel, container } = bare({
      async dedupScan(codexHome) {
        scans.push(codexHome);
        return scanResult();
      },
    });
    await panel.load();
    must<HTMLButtonElement>(container, ".dedup-pick").click();
    await settle();
    expect(scans).toEqual([HOME]);
  });

  it("treats an EMPTY array as a dismissal, not as a home named ''", async () => {
    // `picked[0]` is `undefined` here; without the `?? null` it would reach `checkCodexHome`
    // as undefined and the panel would report a refusal for something the user never chose.
    dialog.picked = [];
    const scans: string[] = [];
    const { panel, container } = bare({
      async dedupScan(codexHome) {
        scans.push(codexHome);
        return scanResult();
      },
    });
    await panel.load();
    must<HTMLButtonElement>(container, ".dedup-pick").click();
    await settle();
    expect(scans).toEqual([]);
    expect(must(container, ".dedup-status").textContent).toBe("");
  });
});

// ---------------------------------------------------------------------------
// the four empty readings, as four different DOM states
// ---------------------------------------------------------------------------

describe("DedupPanel empty readings", () => {
  async function scanned(
    scan: Partial<DedupScanResult>,
    sessions: DedupSession[],
  ): Promise<HTMLElement> {
    const { panel, container } = mount({
      dedupScan: async () => scanResult(scan),
      dedupSessions: async () => sessions,
    });
    await panel.load();
    must<HTMLButtonElement>(container, ".dedup-scan").click();
    await settle();
    return container;
  }

  it("gives a store with no files its own class and names the folder it looked in", async () => {
    const container = await scanned({ session_count: 0, copy_count: 0, duplicate_count: 0 }, []);
    const empty = must(container, ".dedup-empty");
    expect(empty.classList.contains("dedup-empty-no-files")).toBe(true);
    expect(empty.textContent).toContain(HOME);
    expect(empty.textContent).not.toMatch(/no duplicate|no redundant/i);
  });

  it("gives the genuinely clean store a DIFFERENT class from the empty one", async () => {
    // The whole point of the per-kind class: a stylesheet or a screenshot must not be able to
    // render "we found nothing there" and "your store is clean" identically.
    const container = await scanned(
      { session_count: 9, copy_count: 9, duplicate_count: 0 },
      [session()],
    );
    const empty = must(container, ".dedup-empty");
    expect(empty.classList.contains("dedup-empty-no-duplicates")).toBe(true);
    expect(empty.textContent).toMatch(/redundant/i);
  });

  it("paints NO empty element at all once there is a list", async () => {
    const container = await scanned({ session_count: 2, duplicate_count: 1 }, [pair()]);
    expect(container.querySelector(".dedup-empty")).toBeNull();
    expect(all(container, ".dedup-row")).toHaveLength(1);
    expect(must(container, ".dedup-notes").textContent).toMatch(/This scan read/);
  });
});

// ---------------------------------------------------------------------------
// what a row is allowed to show
// ---------------------------------------------------------------------------

describe("DedupPanel rows", () => {
  async function withSessions(
    sessions: DedupSession[],
    scan: Partial<DedupScanResult> = {},
  ): Promise<HTMLElement> {
    const { panel, container } = mount({
      dedupScan: async () => scanResult(scan),
      dedupSessions: async () => sessions,
    });
    await panel.load();
    must<HTMLButtonElement>(container, ".dedup-scan").click();
    await settle();
    return container;
  }

  it("shows the kept copy's measurements and gives the other copies NONE", async () => {
    // `duplicate_paths` is paths only (`sidecar.py:1423`), so the other rows must carry no
    // detail ELEMENT at all — an empty one would still be a place for a future guess to land.
    const container = await withSessions([pair()]);
    const row = must(container, ".dedup-row");
    expect(must(row, ".dedup-headline").textContent).toBe("2 copies of one session");
    expect(must(row, ".dedup-basis").textContent).toContain(pair().session_id);

    const kept = must(row, ".dedup-copy-kept");
    expect(must(kept, ".dedup-copy-role").textContent).toBe("Kept in view");
    expect(must(kept, ".dedup-copy-detail").textContent)
      .toBe("live store · 403.2 KB · 5 minutes ago");

    const other = must(row, ".dedup-copy-other");
    expect(must(other, ".dedup-copy-role").textContent).toBe("Also on disk");
    expect(other.querySelector(".dedup-copy-detail")).toBeNull();
  });

  it("writes a path as TEXT and repeats it as the hover title", async () => {
    // `canonical_path` embeds the owner's username, is absent from `redact.MetadataView`, and
    // is arbitrary bytes off disk. It reaches the DOM as text and never as markup.
    const hostile = "C:\\<img src=x onerror=alert(1)>\\rollout.jsonl";
    const container = await withSessions([pair({ canonical_path: hostile })]);
    const path = must<HTMLElement>(container, ".dedup-copy-kept .dedup-copy-path");
    expect(path.querySelector("img")).toBeNull();
    expect(path.textContent).toBe(hostile);
    expect(path.title).toBe(hostile);
  });

  it("carries the truncation warning only on the flagged row, with a plain-text mark", async () => {
    const container = await withSessions(
      [
        pair({ session_id: "id-flagged", has_larger_copy: true, size_bytes: 1_204 }),
        pair({ session_id: "id-clean" }),
      ],
      { flagged_truncated: 1 },
    );
    const rows = all(container, ".dedup-row");
    const warning = must(rows[0], ".dedup-warning");
    expect(warning.textContent?.startsWith("! ")).toBe(true);
    expect(warning.textContent).toContain(pair().duplicate_paths[0]);
    expect(rows[1].querySelector(".dedup-warning")).toBeNull();
  });

  it("puts unidentified files under their own heading, and omits it when there are none", async () => {
    const clean = await withSessions([pair()]);
    expect(clean.querySelector(".dedup-subtitle")).toBeNull();
    expect(all(clean, ".dedup-row-unidentified")).toEqual([]);

    const container = await withSessions(
      [pair(), session({ session_id: "", is_identified: false })],
      { unidentified: 1 },
    );
    expect(must(container, "h3.dedup-subtitle").textContent)
      .toBe("Files with no readable session id");
    const odd = all(container, ".dedup-row-unidentified");
    expect(odd).toHaveLength(1);
    expect(must(odd[0], ".dedup-headline").textContent).toBe("One file, no session id");
    expect(must(odd[0], ".dedup-basis").textContent).toMatch(/no session id/i);
  });

  it("counts the single-copy sessions in words that agree with the number", async () => {
    const one = await withSessions([pair(), session({ session_id: "solo" })]);
    expect(must(one, ".dedup-singles").textContent)
      .toBe("1 other session is stored in a single file, with nothing redundant to report.");

    const many = await withSessions([
      pair(),
      session({ session_id: "solo-a" }),
      session({ session_id: "solo-b" }),
    ]);
    expect(must(many, ".dedup-singles").textContent)
      .toBe("2 other sessions are stored in a single file, with nothing redundant to report.");
  });

  it("omits the singles line entirely when every session is duplicated", async () => {
    const container = await withSessions([pair()]);
    expect(container.querySelector(".dedup-singles")).toBeNull();
  });

  it("expands and collapses the capped list without re-scanning", async () => {
    const many = [
      pair({ session_id: "id-a" }),
      pair({ session_id: "id-b" }),
      pair({ session_id: "id-c" }),
    ];
    const scans: string[] = [];
    const { panel, container } = mount({
      async dedupScan(codexHome) {
        scans.push(codexHome);
        return scanResult({ session_count: 3, duplicate_count: 3 });
      },
      dedupSessions: async () => many,
    });
    await panel.load();
    must<HTMLButtonElement>(container, ".dedup-scan").click();
    await settle();

    expect(all(container, ".dedup-row")).toHaveLength(2);
    const toggle = must<HTMLButtonElement>(container, ".dedup-more");
    expect(toggle.textContent).toBe("+1 more");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");

    toggle.click();
    expect(all(container, ".dedup-row")).toHaveLength(3);
    const expanded = must<HTMLButtonElement>(container, ".dedup-more");
    expect(expanded.textContent).toBe("Show fewer");
    expect(expanded.getAttribute("aria-expanded")).toBe("true");

    expanded.click();
    expect(all(container, ".dedup-row")).toHaveLength(2);
    expect(must(container, ".dedup-more").textContent).toBe("+1 more");
    // Every hidden row was already in hand — expanding must never go back to the engine.
    expect(scans).toEqual([HOME]);
  });

  it("offers no toggle when the whole list already fits", async () => {
    const container = await withSessions([pair()]);
    expect(container.querySelector(".dedup-more")).toBeNull();
  });

  it("disables the toggle while a second scan is in flight", async () => {
    const { ipc, panel, container } = mount({
      dedupSessions: async () => [
        pair({ session_id: "id-a" }),
        pair({ session_id: "id-b" }),
        pair({ session_id: "id-c" }),
      ],
    });
    await panel.load();
    must<HTMLButtonElement>(container, ".dedup-scan").click();
    await settle();
    expect(must<HTMLButtonElement>(container, ".dedup-more").disabled).toBe(false);

    ipc.arm();
    must<HTMLButtonElement>(container, ".dedup-scan").click();
    expect(must<HTMLButtonElement>(container, ".dedup-more").disabled).toBe(true);
    ipc.release();
    await settle();
    expect(must<HTMLButtonElement>(container, ".dedup-more").disabled).toBe(false);
  });
});
