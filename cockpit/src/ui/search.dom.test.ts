// @vitest-environment happy-dom
/**
 * {@link SearchPanel} — the debounced search box wired to `ipc.searchQuery`, through real
 * `input`/`change`/`click` events on a mounted DOM.
 *
 * WHY THE IPC IS A ONE-METHOD STUB. The panel's contract is the SEARCH seam: which params it
 * sends, what it renders per hit, and how it survives slow/stale/failing queries. A real mock
 * IPC would drag in fixture corpora the panel never asked for; the stub returns exactly what
 * each case needs so the assertion lands on the PANEL, not the fixture.
 *
 * WHY FAKE TIMERS ONLY FOR DEBOUNCE CASES. `DEBOUNCE_MS = 180` is the panel's timing
 * contract (coalesce typing; never wait after a deliberate filter click). Fake timers pin
 * both sides without sleeps; every other case keeps real timers so `Date.now()` inside
 * `relativeWhen` stays honest.
 *
 * The panel's `run` is private, so the non-debounce cases reach it through a structural cast
 * — the alternative (dispatching `input` + waiting 180 real ms per case) would re-test the
 * debounce, which has its own block above, in every row of this table.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { IpcClient, SearchHit, SearchParams, SearchResult } from "../ipc";
import { engineErrorText } from "./errors";
import { hitLabel, relativeWhen, resultStatus, searchParams } from "./searchPresent";
import { SearchPanel } from "./search";

function makeHit(over: Partial<SearchHit> = {}): SearchHit {
  return {
    conversation_id: "c1",
    thread_id: "t1",
    snippet: "hello world",
    score: 1,
    provider: "openai",
    ts_ms: Date.now() - 1000,
    ...over,
  };
}

type SearchImpl = (params: SearchParams) => Promise<SearchResult>;

interface Rig {
  panel: SearchPanel;
  input: HTMLInputElement;
  viewport: HTMLElement;
  status: HTMLElement;
  filter: HTMLSelectElement | null;
  searchQuery: ReturnType<typeof vi.fn>;
}

function makePanel(withFilter: boolean, impl?: SearchImpl): Rig {
  const input = document.createElement("input");
  input.type = "search";
  const viewport = document.createElement("div");
  const status = document.createElement("p");
  const filter = withFilter ? document.createElement("select") : null;
  document.body.append(input, viewport, status);
  if (filter !== null) document.body.append(filter);

  const searchQuery = vi.fn(
    impl ?? (async () => ({ hits: [], total: 0, took_ms: 0 }) as SearchResult),
  );
  const panel = new SearchPanel(
    { searchQuery } as unknown as IpcClient,
    input,
    viewport,
    status,
    filter,
  );
  return { panel, input, viewport, status, filter, searchQuery };
}

/** Drive one private `run()` to completion (the debounce has its own describe block). */
function run(panel: SearchPanel): Promise<void> {
  return (panel as unknown as { run(): Promise<void> }).run();
}

let rig: Rig = makePanel(false);

function type(text: string): void {
  rig.input.value = text;
  rig.input.dispatchEvent(new Event("input", { bubbles: true }));
}

beforeEach(() => {
  rig = makePanel(false);
});

afterEach(() => {
  rig.panel.destroy();
  document.body.replaceChildren();
  vi.useRealTimers();
});

describe("SearchPanel debounce", () => {
  it("coalesces rapid typing into one query", async () => {
    vi.useFakeTimers();
    rig = makePanel(false, async () => ({ hits: [], total: 0, took_ms: 1 }) as SearchResult);
    type("h");
    type("he");
    type("hel");
    expect(rig.searchQuery).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(180);
    expect(rig.searchQuery).toHaveBeenCalledTimes(1);
    expect(rig.searchQuery.mock.calls[0]?.[0]).toEqual(searchParams("hel", "", 200));
  });

  it("destroy cancels a pending debounce", async () => {
    vi.useFakeTimers();
    rig = makePanel(false);
    type("ghost");
    rig.panel.destroy();
    await vi.advanceTimersByTimeAsync(500);
    expect(rig.searchQuery).not.toHaveBeenCalled();
  });
});

describe("SearchPanel run", () => {
  it("renders hits, status, and the provider/label/time wiring per row", async () => {
    const ts = Date.now() - 3_600_000;
    const hit = makeHit({ snippet: "a fine\nconversation", ts_ms: ts });
    rig = makePanel(false, async () => ({ hits: [hit], total: 1, took_ms: 7 }) as SearchResult);
    rig.input.value = "fine";
    await run(rig.panel);
    expect(rig.status.textContent).toBe(
      resultStatus({ total: 1, shown: 1, tookMs: 7, provider: "" }),
    );
    const rows = rig.viewport.querySelectorAll(".hit-row");
    expect(rows).toHaveLength(1);
    const main = rows[0]?.querySelector<HTMLButtonElement>("button.hit-main");
    expect(main).not.toBeNull();
    expect(main?.querySelector(".provider-dot")?.className).toContain("provider-openai");
    expect(main?.querySelector(".hit-snippet")?.textContent).toBe(hitLabel(hit));
    const when = main?.querySelector("time");
    expect(when?.textContent).toBe(relativeWhen(ts, Date.now()));
    expect(when?.getAttribute("datetime")).toBe(new Date(ts).toISOString());
  });

  it("falls back to untitled · id and the unknown provider class for a bare hit", async () => {
    const hit = makeHit({
      snippet: "  \n\t ",
      provider: "",
      ts_ms: undefined,
      conversation_id: "cx",
    });
    rig = makePanel(false, async () => ({ hits: [hit], total: 1, took_ms: 0 }) as SearchResult);
    rig.input.value = "bare";
    await run(rig.panel);
    const main = rig.viewport.querySelector("button.hit-main");
    expect(main?.querySelector(".provider-dot")?.className).toContain("provider-unknown");
    expect(main?.querySelector(".hit-snippet")?.textContent).toBe("untitled · cx");
    const when = main?.querySelector("time");
    expect(when?.textContent).toBe("");
    expect(when?.hasAttribute("datetime")).toBe(false);
  });

  it("sanitizes a provider name into the CSS class suffix", async () => {
    const hit = makeHit({ provider: "Gemini 2.5 Pro!" });
    rig = makePanel(false, async () => ({ hits: [hit], total: 1, took_ms: 0 }) as SearchResult);
    rig.input.value = "gem";
    await run(rig.panel);
    expect(rig.viewport.querySelector(".provider-dot")?.className).toContain(
      "provider-gemini-2-5-pro-",
    );
  });

  it("locating and reading a hit fire their own handlers with that hit", async () => {
    const hit = makeHit();
    rig = makePanel(false, async () => ({ hits: [hit], total: 1, took_ms: 0 }) as SearchResult);
    const onHit = vi.fn();
    const onRead = vi.fn();
    rig.panel.setHitHandler(onHit);
    rig.panel.setReadHandler(onRead);
    rig.input.value = "hello";
    await run(rig.panel);
    rig.viewport.querySelector<HTMLButtonElement>("button.hit-main")?.click();
    rig.viewport.querySelector<HTMLButtonElement>("button.hit-read")?.click();
    expect(onHit).toHaveBeenCalledWith(hit);
    expect(onRead).toHaveBeenCalledWith(hit);
  });

  it("row clicks are harmless when no handlers are installed", async () => {
    const hit = makeHit();
    rig = makePanel(false, async () => ({ hits: [hit], total: 1, took_ms: 0 }) as SearchResult);
    rig.input.value = "hello";
    await run(rig.panel); // no setHitHandler / setReadHandler calls
    expect(() => {
      rig.viewport.querySelector<HTMLButtonElement>("button.hit-main")?.click();
      rig.viewport.querySelector<HTMLButtonElement>("button.hit-read")?.click();
    }).not.toThrow();
  });

  it("an emptied box clears the list and status without querying", async () => {
    const hit = makeHit();
    rig = makePanel(false, async () => ({ hits: [hit], total: 1, took_ms: 0 }) as SearchResult);
    rig.input.value = "hello";
    await run(rig.panel);
    rig.input.value = "   ";
    await run(rig.panel);
    expect(rig.searchQuery).toHaveBeenCalledTimes(1); // only the first, populated run
    expect(rig.status.textContent).toBe("");
    expect(rig.viewport.getAttribute("data-empty")).toBe("Search across every provider at once.");
    expect(rig.viewport.querySelectorAll(".hit-row")).toHaveLength(0);
  });

  it("routes a failed query through the shared error presenter", async () => {
    const err = new Error("engine offline");
    rig = makePanel(false, () => Promise.reject(err));
    rig.input.value = "fail";
    await run(rig.panel);
    expect(rig.status.textContent).toBe(engineErrorText(err, "Search failed"));
    expect(rig.viewport.querySelectorAll(".hit-row")).toHaveLength(0);
  });

  it("ignores a stale success that lands after a newer query finished", async () => {
    let resolveA!: (r: SearchResult) => void;
    rig = makePanel(false, (p) =>
      p.q === "a"
        ? new Promise<SearchResult>((res) => {
            resolveA = res;
          })
        : Promise.resolve({
            hits: [makeHit({ snippet: "B" })],
            total: 1,
            took_ms: 2,
          } as SearchResult),
    );
    rig.input.value = "a";
    // Fire-and-forget, exactly as the debounce does (`void this.run()`): query A must stay
    // pending, so awaiting it here would deadlock the test against resolveA below.
    void run(rig.panel);
    await Promise.resolve(); // let the executor assign resolveA
    rig.input.value = "ab";
    await run(rig.panel);
    expect(rig.status.textContent).toBe(
      resultStatus({ total: 1, shown: 1, tookMs: 2, provider: "" }),
    );
    resolveA({ hits: [makeHit({ snippet: "A" })], total: 1, took_ms: 9 } as SearchResult);
    await vi.waitFor(() => {
      expect(rig.viewport.querySelector(".hit-snippet")?.textContent).toBe("B");
    });
  });

  it("ignores a stale FAILURE that lands after a newer query finished", async () => {
    let rejectA!: (e: Error) => void;
    rig = makePanel(false, (p) =>
      p.q === "a"
        ? new Promise<SearchResult>((_res, rej) => {
            rejectA = rej;
          })
        : Promise.resolve({
            hits: [makeHit({ snippet: "B" })],
            total: 1,
            took_ms: 3,
          } as SearchResult),
    );
    rig.input.value = "a";
    void run(rig.panel); // pending by design — see the stale-success case above
    await Promise.resolve();
    rig.input.value = "ab";
    await run(rig.panel);
    const before = rig.status.textContent;
    rejectA(new Error("late failure"));
    await new Promise((r) => setTimeout(r, 10));
    expect(rig.status.textContent).toBe(before);
  });
});

describe("SearchPanel provider filter", () => {
  it("rebuilds choices from the corpus, keeping a still-valid selection", () => {
    rig = makePanel(true);
    rig.panel.setProviders({ codex: 5, grok: 3 });
    const options = [...rig.filter!.options].map((o) => o.value);
    expect(options).toEqual(["", "codex", "grok"]);
    expect(rig.filter!.hidden).toBe(false);

    rig.filter!.value = "grok";
    rig.panel.setProviders({ codex: 5, grok: 3, openai: 9 });
    expect(rig.filter!.value).toBe("grok"); // still present → kept
    rig.panel.setProviders({ codex: 5, openai: 9 }); // grok vanished
    expect(rig.filter!.value).toBe(""); // falls back to everything
  });

  it("hides the filter for a single-provider corpus", () => {
    rig = makePanel(true);
    rig.panel.setProviders({ codex: 4 });
    expect(rig.filter!.options).toHaveLength(0);
    expect(rig.filter!.hidden).toBe(true);
  });

  it("sends the active provider and skips the debounce on a deliberate change", async () => {
    vi.useFakeTimers();
    rig = makePanel(true, async () => ({ hits: [], total: 0, took_ms: 0 }) as SearchResult);
    rig.panel.setProviders({ codex: 5, grok: 3 });
    rig.filter!.value = "grok";
    rig.input.value = "q";
    type("q");
    rig.filter!.dispatchEvent(new Event("change", { bubbles: true }));
    // The change ran immediately — no timer advance happened yet.
    expect(rig.searchQuery).toHaveBeenCalledTimes(1);
    expect(rig.searchQuery.mock.calls[0]?.[0]).toEqual(searchParams("q", "grok", 200));
    await vi.advanceTimersByTimeAsync(300);
    // The scheduled debounce from `type` ALSO ran afterwards — a click is not de-duped against
    // typing, it only never WAITS. Two calls total, both with the provider.
    expect(rig.searchQuery).toHaveBeenCalledTimes(2);
  });

  it("queries without a provider field when no filter was supplied", async () => {
    rig = makePanel(false);
    rig.input.value = "solo";
    await run(rig.panel);
    expect(rig.searchQuery.mock.calls[0]?.[0]).toEqual(searchParams("solo", "", 200));
  });

  it("setProviders is a harmless no-op when there is no filter element", () => {
    expect(() => rig.panel.setProviders({ a: 1, b: 2 })).not.toThrow();
  });
});
