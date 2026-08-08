// @vitest-environment happy-dom
/**
 * The discovery panel's DOM SHELL — what the first run actually puts on screen.
 *
 * `discoveryPanel.test.ts` states, correctly, that every DECISION lives in a pure function or
 * the DOM-free controller. What it leaves unasserted is the other half of the contract: that
 * the shell RENDERS those decisions, and — the part that has already gone wrong once — that it
 * does not throw one away. The measured defect the `needsAttention` flag exists for was not a
 * wrong decision; the controller composed the right message and the shell discarded it.
 *
 * Its own file rather than a `@vitest-environment` flip on `discoveryPanel.test.ts`, for the
 * reason `vitest.config.ts` records: a DOM environment is opt-in per file here, and the
 * sibling suite is a node suite by design. `scrubber.test.ts` and `virtualList.test.ts` set
 * the same precedent.
 *
 * WHAT THIS STILL CANNOT SETTLE, inline rather than in a trailing caveat: happy-dom does NO
 * layout, so nothing here proves the panel is visible, sized, or legible — `#discovery:empty`
 * is a CSS rule and CSS is not evaluated. "Collapsed" below means the container was emptied
 * and its ARIA role removed, which is the DOM precondition for that rule, not the pixels.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  BuildParams,
  BuildStatus,
  CreateCorpusResult,
  DiscoveryFinding,
  DiscoveryResult,
  OpenCorpusResult,
} from "../ipc/types";
import {
  DiscoveryPanel,
  EXPORT_NO_IMPORT_REASON,
  NOTHING_FOUND_LABEL,
  type DiscoveryDeps,
  type DiscoveryIpc,
  type DiscoveryRow,
} from "./discoveryPanel";

/** The native save dialog, stubbed at the module boundary (see `corpusBarShell.test.ts`). */
const saveDialog = vi.hoisted(() =>
  vi.fn(async (_options?: Record<string, unknown>): Promise<string | null> => null),
);
vi.mock("@tauri-apps/plugin-dialog", () => ({ save: saveDialog }));

// ---------------------------------------------------------------------------
// fixtures
// ---------------------------------------------------------------------------

const NOW = 1_800_000_000_000;
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

function codexStore(over: Partial<DiscoveryFinding> = {}): DiscoveryFinding {
  return finding({
    provider: "codex",
    kind: "session_store",
    path: "C:\\Users\\me\\.codex",
    count: 2043,
    detail: { items_root: "C:\\Users\\me\\.codex\\sessions", rollouts_zst: 2043 },
    ...over,
  });
}

function builtIndex(over: Partial<DiscoveryFinding> = {}): DiscoveryFinding {
  return finding({
    provider: "anthology",
    kind: "built_index",
    path: "C:\\Users\\me\\Documents\\anthology.db",
    count: 1284,
    detail: {},
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

// ---------------------------------------------------------------------------
// harness
// ---------------------------------------------------------------------------

/**
 * One macrotask boundary, which drains the microtask queue to exhaustion. Enough for the
 * fire-and-forget click handlers (`() => void this.controller.activate(...)`) whose whole
 * chain resolves as microtasks; `vi.waitFor` is used instead wherever a real timer is in play.
 */
function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

interface Shell {
  panel: DiscoveryPanel;
  container: HTMLElement;
  ready: string[];
  ipc: DiscoveryIpc & { builds: BuildParams[]; created: string[]; opened: string[] };
}

function mount(
  over: Partial<DiscoveryIpc> = {},
  deps: Partial<DiscoveryDeps> = { now: (): number => NOW },
  result: DiscoveryResult = scan([builtIndex(), codexStore(), finding()]),
): Shell {
  const container = document.createElement("div");
  container.id = "discovery";
  document.body.append(container);

  const builds: BuildParams[] = [];
  const created: string[] = [];
  const opened: string[] = [];
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

  const panel = new DiscoveryPanel(ipc, container, (p) => void ready.push(p), deps);
  return { panel, container, ready, ipc };
}

/** Every rendered group section, in render order. */
function sections(shell: Shell): HTMLElement[] {
  return [...shell.container.querySelectorAll<HTMLElement>("section.discovery-group")];
}

/** The section for one `"<provider>/<kind>"` group, found by its heading. */
function section(shell: Shell, heading: string): HTMLElement {
  const found = sections(shell).find(
    (s) => s.querySelector(".discovery-group-title")?.textContent?.startsWith(heading) === true,
  );
  if (found === undefined) throw new Error(`no group section headed "${heading}"`);
  return found;
}

function text(root: ParentNode, selector: string): string {
  return root.querySelector(selector)?.textContent ?? "";
}

beforeEach(() => {
  saveDialog.mockReset();
  saveDialog.mockResolvedValue(null);
  document.body.replaceChildren();
});

// ---------------------------------------------------------------------------
// the skeleton
// ---------------------------------------------------------------------------

describe("the panel skeleton", () => {
  it("builds a labelled group with a live status region", async () => {
    const shell = mount();
    await shell.panel.scan();

    expect(shell.container.getAttribute("role")).toBe("group");
    expect(shell.container.getAttribute("aria-labelledby")).toBe("discovery-title");
    expect(text(shell.container, "h2#discovery-title")).toBe("Found on this computer");

    const status = shell.container.querySelector(".discovery-status");
    expect(status?.getAttribute("role")).toBe("status");
    expect(status?.getAttribute("aria-live")).toBe("polite");
  });

  it("reuses the SAME status node across repaints, so announcements are heard", async () => {
    // The whole reason the skeleton is built once: a `role="status"` region only announces
    // text written into a node that is already in the accessibility tree. Replacing the
    // element per paint would silence every message.
    const shell = mount();
    await shell.panel.scan();
    const first = shell.container.querySelector(".discovery-status");

    shell.panel.setCorpusAttached("C:\\existing.db");
    expect(shell.container.querySelector(".discovery-status")).toBe(first);
  });

  it("shows the scan notes", async () => {
    const shell = mount({}, { now: (): number => NOW }, scan([builtIndex()], { errors: ["x: nope"] }));
    await shell.panel.scan();
    expect(text(shell.container, ".discovery-notes")).toContain("Scanned 5 locations in 1.8s.");
    expect(text(shell.container, ".discovery-notes")).toContain("1 location was skipped");
  });

  it("names the manual fallback when the scan found nothing", async () => {
    const shell = mount({}, { now: (): number => NOW }, scan([]));
    await shell.panel.scan();
    expect(text(shell.container, ".discovery-empty")).toBe(NOTHING_FOUND_LABEL);
    expect(sections(shell)).toHaveLength(0);
  });

  it("renders no empty-state paragraph when there ARE findings", async () => {
    const shell = mount();
    await shell.panel.scan();
    expect(shell.container.querySelector(".discovery-empty")).toBeNull();
    expect(sections(shell).length).toBeGreaterThan(0);
  });

  it("takes default deps when the app supplies none", async () => {
    // `app.ts:296` constructs the panel with three arguments, so the whole default block —
    // `Date.now`, the real `setTimeout` sleep, the native save dialog — is what actually ships.
    const container = document.createElement("div");
    document.body.append(container);
    const panel = new DiscoveryPanel(
      {
        async discoverSources(): Promise<DiscoveryResult> {
          return scan([builtIndex()]);
        },
        async openCorpus(p: string): Promise<OpenCorpusResult> {
          return { ok: true, index: p };
        },
        async createCorpus(p: string): Promise<CreateCorpusResult> {
          return { index_path: p, created: true };
        },
        async corpusBuild(): Promise<{ job_id: string }> {
          return { job_id: "j" };
        },
        async corpusBuildStatus(): Promise<BuildStatus> {
          return { state: "done", indexed_conversations: 0, errors: [] };
        },
      },
      container,
      () => {},
    );
    await panel.scan();
    expect(text(container, ".discovery-group-title")).toBe("anthology · corpus index (1)");
  });
});

// ---------------------------------------------------------------------------
// groups and rows
// ---------------------------------------------------------------------------

describe("group and row rendering", () => {
  it("heads each group with its provider, kind and TOTAL count", async () => {
    const shell = mount();
    await shell.panel.scan();
    expect(sections(shell).map((s) => text(s, ".discovery-group-title"))).toEqual([
      "anthology · corpus index (1)",
      "codex · session store (1)",
      "chatgpt · downloaded export (1)",
    ]);
  });

  it("shows the file name, its containing directory, and the full path on hover", async () => {
    // The parent is load-bearing rather than decorative: a real scan returns 25 files ALL
    // named conversations.json, so the name alone distinguishes none of them.
    const shell = mount();
    await shell.panel.scan();
    const row = section(shell, "chatgpt");

    expect(text(row, ".discovery-name")).toBe("conversations.json");
    expect(text(row, ".discovery-parent")).toBe("C:\\Users\\me\\Downloads\\a");
    expect(row.querySelector<HTMLElement>(".discovery-name")?.title).toBe(
      "C:\\Users\\me\\Downloads\\a\\conversations.json",
    );
    expect(row.querySelector<HTMLElement>(".discovery-parent")?.title).toBe(
      "C:\\Users\\me\\Downloads\\a\\conversations.json",
    );
  });

  it("appends the generic detail line to the summary when there is one", async () => {
    const shell = mount();
    await shell.panel.scan();
    expect(text(section(shell, "codex"), ".discovery-summary")).toBe(
      "2,043 sessions · 1 hour ago · high confidence · items_root sessions · rollouts_zst 2,043",
    );
  });

  it("shows the summary ALONE when the finding reports no detail", async () => {
    // `builtIndex()` here carries an empty `detail`, so a naive join would leave a trailing
    // separator dangling after "high confidence".
    const shell = mount();
    await shell.panel.scan();
    expect(text(section(shell, "anthology"), ".discovery-summary")).toBe(
      "1,284 conversations · 1 hour ago · high confidence",
    );
  });

  it("writes every string with textContent, so a hostile path cannot inject markup", async () => {
    // Paths, provider names and engine errors are all attacker-influenceable text bound for
    // the UI. The proof is that the angle brackets survive as TEXT and no element was created.
    const hostile = "C:\\tmp\\<img src=x onerror=alert(1)>.json";
    const shell = mount({}, { now: (): number => NOW }, scan([finding({ path: hostile })]));
    await shell.panel.scan();

    expect(text(section(shell, "chatgpt"), ".discovery-name")).toBe(
      "<img src=x onerror=alert(1)>.json",
    );
    expect(shell.container.querySelector("img")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// the two truncations, rendered as two different things
// ---------------------------------------------------------------------------

describe("the two truncations", () => {
  function twelveExports(): DiscoveryFinding[] {
    return Array.from({ length: 12 }, (_, i) =>
      finding({ path: `/d${i}/conversations.json`, newest_mtime: NOW_SEC - i }),
    );
  }

  it("renders the UI's own cap as a real, working control", async () => {
    const shell = mount({}, { now: (): number => NOW }, scan(twelveExports()));
    await shell.panel.scan();

    const more = section(shell, "chatgpt").querySelector<HTMLButtonElement>(".discovery-more");
    expect(more?.textContent).toBe("+7 more");
    expect(more?.getAttribute("aria-expanded")).toBe("false");
    expect(section(shell, "chatgpt").querySelectorAll(".discovery-row")).toHaveLength(5);

    more?.click();
    expect(section(shell, "chatgpt").querySelectorAll(".discovery-row")).toHaveLength(12);
    const back = section(shell, "chatgpt").querySelector<HTMLButtonElement>(".discovery-more");
    expect(back?.textContent).toBe("Show fewer");
    expect(back?.getAttribute("aria-expanded")).toBe("true");

    back?.click();
    expect(section(shell, "chatgpt").querySelectorAll(".discovery-row")).toHaveLength(5);
  });

  it("renders NO control for a group that is already whole", async () => {
    const shell = mount();
    await shell.panel.scan();
    expect(section(shell, "codex").querySelector(".discovery-more")).toBeNull();
  });

  it("renders the ENGINE's cap as a sentence that is not a control", async () => {
    // Items exist on disk that are in no list here and that no click can reveal. Putting that
    // text on a button would promise a reveal that cannot happen.
    const shell = mount(
      {},
      { now: (): number => NOW },
      scan(twelveExports(), { truncated_groups: ["chatgpt/export_file"] }),
    );
    await shell.panel.scan();

    const note = section(shell, "chatgpt").querySelector(".discovery-note");
    expect(note?.tagName).toBe("P");
    expect(note?.textContent).toContain("not shown at all");
    expect(section(shell, "chatgpt").querySelector("button.discovery-note")).toBeNull();
  });

  it("keeps the engine's sentence after the group is expanded", async () => {
    const shell = mount(
      {},
      { now: (): number => NOW },
      scan(twelveExports(), { truncated_groups: ["chatgpt/export_file"] }),
    );
    await shell.panel.scan();
    section(shell, "chatgpt").querySelector<HTMLButtonElement>(".discovery-more")?.click();

    expect(text(section(shell, "chatgpt"), ".discovery-note")).toContain("not shown at all");
    expect(text(section(shell, "chatgpt"), ".discovery-more")).toBe("Show fewer");
  });

  it("renders no engine sentence for a group the engine left alone", async () => {
    const shell = mount();
    await shell.panel.scan();
    expect(section(shell, "chatgpt").querySelector(".discovery-note")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// what a row offers
// ---------------------------------------------------------------------------

describe("row actions", () => {
  it("offers a built index an Open button that attaches it", async () => {
    const shell = mount();
    await shell.panel.scan();

    const open = section(shell, "anthology").querySelector<HTMLButtonElement>(".discovery-action");
    expect(open?.textContent).toBe("Open");
    expect(open?.disabled).toBe(false);

    open?.click();
    await flush();
    expect(shell.ipc.opened).toEqual(["C:\\Users\\me\\Documents\\anthology.db"]);
    expect(shell.ready).toEqual(["C:\\Users\\me\\Documents\\anthology.db"]);
  });

  it("gives a downloaded export a stated reason and NO button", async () => {
    const shell = mount();
    await shell.panel.scan();
    const row = section(shell, "chatgpt");

    expect(text(row, ".discovery-reason")).toBe(EXPORT_NO_IMPORT_REASON);
    expect(row.querySelector("button.discovery-action")).toBeNull();
  });

  it("re-labels the import to say where the data will land", async () => {
    const shell = mount();
    await shell.panel.scan();
    expect(text(section(shell, "codex"), ".discovery-action")).toBe("Import…");

    shell.panel.setCorpusAttached("C:\\existing.db");
    expect(text(section(shell, "codex"), ".discovery-action")).toBe("Import into open corpus");
  });

  it("disables every row control while an engine call is in flight", async () => {
    let release = (): void => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const shell = mount(
      {
        async openCorpus(indexPath: string): Promise<OpenCorpusResult> {
          shell.ipc.opened.push(indexPath);
          await gate;
          return { ok: true, index: indexPath };
        },
      },
      { now: (): number => NOW },
      scan([builtIndex(), ...Array.from({ length: 12 }, (_, i) => finding({ path: `/d${i}/c.json` }))]),
    );
    await shell.panel.scan();
    section(shell, "anthology").querySelector<HTMLButtonElement>(".discovery-action")?.click();
    await flush();

    expect(text(shell.container, ".discovery-status")).toBe("Opening anthology.db…");
    const controls = [
      ...shell.container.querySelectorAll<HTMLButtonElement>(
        ".discovery-row .discovery-action, .discovery-more",
      ),
    ];
    expect(controls.length).toBeGreaterThan(0);
    expect(controls.every((b) => b.disabled)).toBe(true);

    release();
    await flush();
  });

  it("scopes a row's action away from the Dismiss button that shares its class", async () => {
    // `.discovery-action` is reused for the dismiss button's STYLE, and that button is created
    // FIRST, so a bare selector finds it rather than a row. The module says so; this is the
    // assertion that keeps it true.
    const shell = mount();
    await shell.panel.scan();
    expect(shell.container.querySelector(".discovery-action")?.className).toContain(
      "discovery-dismiss",
    );
    expect(
      shell.container.querySelector(".discovery-row .discovery-action")?.textContent,
    ).toBe("Open");
  });

  it("puts the reason in a tooltip on a button that is present but unpressable", () => {
    // A DIRECT call to the private renderer, and deliberately so: `DiscoveryAction.enabled`
    // documents a "button that cannot be pressed yet", but no arm of `deriveAction`
    // (`discoveryPanel.ts:367-388`) currently returns `enabled: false` with a kind other than
    // "none", so this guard is defensive and has no route through the public API. It is
    // exercised here rather than deleted, because deleting it would silently drop the tooltip
    // the moment a future action does use that state.
    const shell = mount();
    const row: DiscoveryRow = {
      finding: codexStore(),
      name: ".codex",
      parent: "C:\\Users\\me",
      summary: "2,043 sessions · 1 hour ago · high confidence",
      detail: "",
      action: {
        kind: "import",
        label: "Import…",
        enabled: false,
        reason: "the engine is still starting up",
        build: null,
      },
    };
    const rendered = (
      shell.panel as unknown as { renderRow(r: DiscoveryRow, busy: boolean): HTMLElement }
    ).renderRow(row, false);

    const button = rendered.querySelector<HTMLButtonElement>("button.discovery-action");
    expect(button?.textContent).toBe("Import…");
    expect(button?.disabled).toBe(true);
    expect(button?.title).toBe("the engine is still starting up");
  });
});

// ---------------------------------------------------------------------------
// collapsing — and the outcome that must survive it
// ---------------------------------------------------------------------------

/** True when the container has been handed back to the graph pane. */
function collapsed(shell: Shell): boolean {
  return (
    shell.container.children.length === 0 &&
    !shell.container.hasAttribute("role") &&
    !shell.container.hasAttribute("aria-labelledby")
  );
}

describe("collapsing", () => {
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

  const chooseNew = { now: (): number => NOW, chooseDestination: async (): Promise<string | null> => "C:\\new.db" };

  it("hands the pane back to the graph after a corpus is opened", async () => {
    const shell = mount();
    await shell.panel.scan();
    expect(collapsed(shell)).toBe(false);

    section(shell, "anthology").querySelector<HTMLButtonElement>(".discovery-action")?.click();
    await vi.waitFor(() => expect(collapsed(shell)).toBe(true));
  });

  it("collapses silently after a CLEAN import, with no dismissal to find", async () => {
    const shell = mount({}, chooseNew);
    await shell.panel.scan();
    section(shell, "codex").querySelector<HTMLButtonElement>(".discovery-action")?.click();
    await vi.waitFor(() => expect(collapsed(shell)).toBe(true));
    expect(shell.ipc.builds).toHaveLength(1);
  });

  it("stays open, reporting the skipped files, when the import was NOT clean", async () => {
    const shell = mount(skipping(40), chooseNew);
    await shell.panel.scan();
    section(shell, "codex").querySelector<HTMLButtonElement>(".discovery-action")?.click();

    await vi.waitFor(() =>
      expect(text(shell.container, ".discovery-status")).toContain("40 files could not be read"),
    );
    expect(collapsed(shell)).toBe(false);
    const dismiss = shell.container.querySelector<HTMLButtonElement>(".discovery-dismiss");
    expect(dismiss?.hidden).toBe(false);
  });

  it("keeps the Dismiss button hidden while there is nothing to acknowledge", async () => {
    const shell = mount();
    await shell.panel.scan();
    expect(shell.container.querySelector<HTMLButtonElement>(".discovery-dismiss")?.hidden).toBe(
      true,
    );
  });

  it("collapses only once the report has been dismissed", async () => {
    const shell = mount(skipping(40), chooseNew);
    await shell.panel.scan();
    section(shell, "codex").querySelector<HTMLButtonElement>(".discovery-action")?.click();
    await vi.waitFor(() =>
      expect(text(shell.container, ".discovery-status")).toContain("40 files could not be read"),
    );

    shell.container.querySelector<HTMLButtonElement>(".discovery-dismiss")?.click();
    expect(collapsed(shell)).toBe(true);
  });

  /**
   * The user-visible face of the defect fixed in `emitRegroup`.
   *
   * Before the fix, expanding a group while an unread report was on screen cleared
   * `needsAttention`; the phase was still `done`, so the very next paint took the collapse
   * branch and the whole panel vanished. A user who clicked "show me more rows" lost the only
   * statement anywhere in the cockpit of how many files the import could not read.
   */
  it("does NOT vanish when a group is expanded while a report is unread", async () => {
    const shell = mount(
      skipping(40),
      chooseNew,
      scan([codexStore(), ...Array.from({ length: 12 }, (_, i) => finding({ path: `/d${i}/c.json` }))]),
    );
    await shell.panel.scan();
    section(shell, "codex").querySelector<HTMLButtonElement>(".discovery-action")?.click();
    await vi.waitFor(() =>
      expect(text(shell.container, ".discovery-status")).toContain("40 files could not be read"),
    );

    section(shell, "chatgpt").querySelector<HTMLButtonElement>(".discovery-more")?.click();

    expect(collapsed(shell)).toBe(false);
    expect(text(shell.container, ".discovery-status")).toContain("40 files could not be read");
    expect(section(shell, "chatgpt").querySelectorAll(".discovery-row")).toHaveLength(12);
    expect(shell.container.querySelector<HTMLButtonElement>(".discovery-dismiss")?.hidden).toBe(
      false,
    );
  });

  it("does NOT vanish when the top bar attaches a corpus while a report is unread", async () => {
    const shell = mount(skipping(40), chooseNew);
    await shell.panel.scan();
    section(shell, "codex").querySelector<HTMLButtonElement>(".discovery-action")?.click();
    await vi.waitFor(() =>
      expect(text(shell.container, ".discovery-status")).toContain("40 files could not be read"),
    );

    // `app.ts:190-196` runs exactly this when the corpus bar attaches something.
    shell.panel.setCorpusAttached("C:\\somewhere-else.db");

    expect(collapsed(shell)).toBe(false);
    expect(text(shell.container, ".discovery-status")).toContain("40 files could not be read");
  });

  it("collapses on destroy, leaving no orphaned role in the accessibility tree", async () => {
    // An empty `role="group"` pointing `aria-labelledby` at a removed heading id would
    // announce a labelled group containing nothing.
    const shell = mount();
    await shell.panel.scan();
    shell.panel.destroy();
    expect(collapsed(shell)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// the import path, through the shell's own seams
// ---------------------------------------------------------------------------

describe("importing through the shell", () => {
  it("asks the NATIVE save dialog where to put the new index", async () => {
    // The default `chooseDestination`, which is what the app gets. A webview
    // `<input type="file">` yields no filesystem path under Tauri v2, so this is the only way
    // to name a destination.
    saveDialog.mockResolvedValue("C:\\Users\\me\\anthology.db");
    const shell = mount({}, { now: (): number => NOW });
    await shell.panel.scan();
    section(shell, "codex").querySelector<HTMLButtonElement>(".discovery-action")?.click();

    await vi.waitFor(() => expect(shell.ipc.builds).toHaveLength(1));
    expect(saveDialog).toHaveBeenCalledTimes(1);
    expect(saveDialog.mock.calls[0]?.[0]).toMatchObject({
      title: "Create corpus index",
      defaultPath: "anthology.db",
    });
    expect(shell.ipc.created).toEqual(["C:\\Users\\me\\anthology.db"]);
    expect(shell.ipc.builds[0]).toEqual({
      sessions_root: "C:\\Users\\me\\.codex\\sessions",
      codex_home: "C:\\Users\\me\\.codex",
    });
  });

  it("does nothing when the save dialog is dismissed", async () => {
    saveDialog.mockResolvedValue(null);
    const shell = mount({}, { now: (): number => NOW });
    await shell.panel.scan();
    section(shell, "codex").querySelector<HTMLButtonElement>(".discovery-action")?.click();
    await flush();

    expect(shell.ipc.created).toEqual([]);
    expect(shell.ipc.builds).toEqual([]);
    expect(collapsed(shell)).toBe(false);
  });

  it("paints the climbing progress through the DEFAULT setTimeout sleep", async () => {
    // The default `sleep` is `(ms) => new Promise((r) => setTimeout(r, ms))` — a real timer,
    // exercised here rather than replaced, so the poll loop is proven to advance on its own.
    let polls = 0;
    const shell = mount(
      {
        async corpusBuildStatus(): Promise<BuildStatus> {
          polls += 1;
          return polls <= 2
            ? { state: "running", indexed_conversations: polls * 100, errors: [] }
            : { state: "done", indexed_conversations: 200, errors: [] };
        },
      },
      {
        now: (): number => NOW,
        chooseDestination: async (): Promise<string | null> => "C:\\new.db",
        limits: { intervalMs: 0, maxElapsedMs: 60_000 },
      },
    );
    await shell.panel.scan();
    section(shell, "codex").querySelector<HTMLButtonElement>(".discovery-action")?.click();

    await vi.waitFor(() => expect(polls).toBe(3));
    await vi.waitFor(() => expect(collapsed(shell)).toBe(true));
  });

  it("reports a failed import in the status line instead of collapsing", async () => {
    const shell = mount(
      {
        async corpusBuildStatus(): Promise<BuildStatus> {
          return { state: "failed", error: "disk is full", indexed_conversations: 0, errors: [] };
        },
      },
      { now: (): number => NOW, chooseDestination: async (): Promise<string | null> => "C:\\new.db" },
    );
    await shell.panel.scan();
    section(shell, "codex").querySelector<HTMLButtonElement>(".discovery-action")?.click();

    await vi.waitFor(() =>
      expect(text(shell.container, ".discovery-status")).toBe("Import failed: disk is full"),
    );
    expect(collapsed(shell)).toBe(false);
  });
});
