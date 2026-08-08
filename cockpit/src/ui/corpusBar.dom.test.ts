// @vitest-environment happy-dom
/**
 * The corpus bar's DOM SHELL — the half `corpusBar.test.ts` states it cannot reach.
 *
 * That file's header says "the shell's picker path is NOT covered here — it needs a live
 * Tauri webview". Half of that is true and half was a limitation of the runner rather than of
 * the code: the picker CALL is `@tauri-apps/plugin-dialog`'s `open`, which is a module import
 * and therefore mockable, and `isTauriRuntime` reads nothing but
 * `globalThis.__TAURI_INTERNALS__.invoke` (`ipc/index.ts:39-42`), which a test can set. What
 * genuinely still needs a webview is whether the OS dialog appears — that, and only that, is
 * left to `tools/smoke_boot.mjs`.
 *
 * Split into its OWN file rather than added to `corpusBar.test.ts` because that file asserts
 * `globalThis.localStorage` is undefined "under the node runner" on purpose
 * (`corpusBar.test.ts:228-236`); `vitest.config.ts` names that exact test as the reason a
 * global DOM flip is not an option. A per-file `@vitest-environment` docblock is the seam the
 * config documents, and `scrubber.test.ts` and `virtualList.test.ts` already use it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OpenCorpusResult } from "../ipc/types";
import {
  CorpusBar,
  CORPUS_STORAGE_KEY,
  makeCorpusStore,
  NO_CORPUS_LABEL,
  PICKER_UNAVAILABLE_MESSAGE,
  type CorpusIpc,
  type CorpusStore,
} from "./corpusBar";

/**
 * The native picker, stubbed at the module boundary. `vi.hoisted` because `vi.mock`'s factory
 * is hoisted above the imports and would otherwise close over an uninitialised binding.
 */
const openDialog = vi.hoisted(() =>
  vi.fn(async (_options?: Record<string, unknown>): Promise<string | null> => null),
);
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: openDialog }));

/** The shape Tauri v2 injects into the webview; the same probe `ipc/index.ts` reads. */
type MaybeTauriGlobal = { __TAURI_INTERNALS__?: { invoke?: unknown } };

/** Make `isTauriRuntime()` answer true, as it does inside the desktop app. */
function enterTauriWebview(): void {
  (globalThis as MaybeTauriGlobal).__TAURI_INTERNALS__ = { invoke: (): void => {} };
}

/**
 * Let the click handler's fire-and-forget chain finish.
 *
 * `CorpusBar` binds `() => void this.pick()` (`corpusBar.ts:381`), so `click()` returns before
 * anything has been awaited and there is no promise for a test to hold. ONE macrotask boundary
 * is enough and is not a magic number: every link in that chain — the mocked picker, the fake
 * IPC — resolves as a microtask, and the microtask queue is drained to exhaustion before a
 * `setTimeout` callback runs.
 */
function settle(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

interface Shell {
  bar: CorpusBar;
  button: HTMLButtonElement;
  label: HTMLElement;
  error: HTMLElement;
  /** Paths the engine was actually asked to attach. */
  asked: string[];
  /** Paths announced through `onOpened`. */
  opened: string[];
}

const okResult = async (indexPath: string): Promise<OpenCorpusResult> => ({
  ok: true,
  index: indexPath,
});

/**
 * Build the three elements the shell paints into and wire a bar to them. Omitting `store`
 * exercises the DEFAULT (`localCorpusStore()`), which under this environment is a real store
 * over happy-dom's `localStorage`.
 */
function mount(
  openCorpus: (indexPath: string) => Promise<OpenCorpusResult> = okResult,
  store?: CorpusStore,
): Shell {
  const button = document.createElement("button");
  const label = document.createElement("span");
  const error = document.createElement("span");
  document.body.append(button, label, error);

  const asked: string[] = [];
  const opened: string[] = [];
  const ipc: CorpusIpc = {
    openCorpus(indexPath: string): Promise<OpenCorpusResult> {
      asked.push(indexPath);
      return openCorpus(indexPath);
    },
  };
  const onOpened = (indexPath: string): void => void opened.push(indexPath);
  const bar =
    store === undefined
      ? new CorpusBar(ipc, button, label, error, onOpened)
      : new CorpusBar(ipc, button, label, error, onOpened, store);
  return { bar, button, label, error, asked, opened };
}

beforeEach(() => {
  openDialog.mockReset();
  openDialog.mockResolvedValue(null);
  document.body.replaceChildren();
  localStorage.clear();
});

afterEach(() => {
  Reflect.deleteProperty(globalThis, "__TAURI_INTERNALS__");
});

describe("CorpusBar first paint", () => {
  it("shows the no-corpus state and an enabled button before anything happens", () => {
    const shell = mount();
    expect(shell.label.textContent).toBe(NO_CORPUS_LABEL);
    expect(shell.error.textContent).toBe("");
    expect(shell.button.disabled).toBe(false);
  });

  it("leaves NO title attribute when there is no path to put in one", () => {
    // An empty `title` means REMOVE, not set-to-empty: a bare `title=""` would still make the
    // label a hover target that reveals nothing.
    const shell = mount();
    expect(shell.label.hasAttribute("title")).toBe(false);
  });
});

describe("CorpusBar outside the Tauri webview", () => {
  it("explains that the preview is showing sample data instead of opening a picker", async () => {
    // `isTauriRuntime()` is false here because nothing set `__TAURI_INTERNALS__`. A webview
    // `<input type="file">` yields no filesystem path under Tauri v2, so there is genuinely
    // nothing to open — and `ipc/index.ts` has already bound this environment to the mock.
    const shell = mount();
    shell.button.click();
    await settle();

    expect(shell.error.textContent).toBe(PICKER_UNAVAILABLE_MESSAGE);
    expect(openDialog).not.toHaveBeenCalled();
    expect(shell.asked).toEqual([]);
    expect(shell.label.textContent).toBe(NO_CORPUS_LABEL);
  });
});

describe("CorpusBar inside the Tauri webview", () => {
  beforeEach(enterTauriWebview);

  it("asks the picker for a FILE, with the corpus-index extensions", async () => {
    // The index is a single SQLite file handed straight to `sqlite3.connect`, not a
    // directory. `directory: true` would let the user hand the engine a folder, which is the
    // failure `src-tauri/src/lib.rs:59` exists to reject after the fact.
    const shell = mount();
    shell.button.click();
    await settle();

    expect(openDialog).toHaveBeenCalledTimes(1);
    const options = openDialog.mock.calls[0]?.[0];
    expect(options).toMatchObject({
      multiple: false,
      directory: false,
      title: "Open corpus index",
    });
    expect(options?.filters).toEqual([
      { name: "Corpus index", extensions: ["db", "sqlite3", "sqlite"] },
    ]);
  });

  it("attaches the picked index and repaints the label with the path as its tooltip", async () => {
    openDialog.mockResolvedValue("C:\\Users\\me\\corpora\\anthology.db");
    const shell = mount(okResult, makeCorpusStore(localStorage));
    shell.button.click();
    await settle();

    expect(shell.asked).toEqual(["C:\\Users\\me\\corpora\\anthology.db"]);
    expect(shell.label.textContent).toBe("anthology.db");
    expect(shell.label.getAttribute("title")).toBe("C:\\Users\\me\\corpora\\anthology.db");
    expect(shell.error.textContent).toBe("");
    expect(shell.opened).toEqual(["C:\\Users\\me\\corpora\\anthology.db"]);
  });

  it("remembers the attached index through the DEFAULT store, with no store injected", async () => {
    // The constructor's `store = localCorpusStore()` default is what the app actually gets
    // (`app.ts:185-197` passes five arguments), so the remembering it provides is only real if
    // the default is exercised rather than replaced.
    openDialog.mockResolvedValue("/home/me/corpora/anthology.db");
    const shell = mount();
    shell.button.click();
    await settle();

    expect(shell.label.textContent).toBe("anthology.db");
    expect(localStorage.getItem(CORPUS_STORAGE_KEY)).toBe("/home/me/corpora/anthology.db");
  });

  it("treats a dismissed picker as a no-op, painting nothing at all", async () => {
    openDialog.mockResolvedValue(null);
    const shell = mount();
    shell.button.click();
    await settle();

    expect(shell.asked).toEqual([]);
    expect(shell.label.textContent).toBe(NO_CORPUS_LABEL);
    expect(shell.label.hasAttribute("title")).toBe(false);
    expect(shell.error.textContent).toBe("");
  });

  it("reports a picker that throws WITHOUT asking the engine to attach anything", async () => {
    openDialog.mockRejectedValue(new Error("open_corpus dialog plugin is not registered"));
    const shell = mount();
    shell.button.click();
    await settle();

    // Routed through `openFailureMessage`, so the internal command name is scrubbed here too.
    expect(shell.error.textContent).toContain("Could not open that corpus");
    expect(shell.error.textContent).not.toContain("open_corpus");
    expect(shell.asked).toEqual([]);
  });

  it("shows a failed attach beside the button and keeps the no-corpus label", async () => {
    openDialog.mockResolvedValue("C:\\gone.db");
    const shell = mount(async () => {
      throw new Error("unable to open database file");
    });
    shell.button.click();
    await settle();

    expect(shell.error.textContent).toContain("unable to open database file");
    expect(shell.label.textContent).toBe(NO_CORPUS_LABEL);
    expect(shell.opened).toEqual([]);
  });

  it("disables the button for the duration of the open, then re-enables it", async () => {
    // Single-flight is enforced in the controller; the DISABLE is what stops the user from
    // queueing the second click in the first place.
    let release = (): void => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    openDialog.mockResolvedValue("/x/y.db");
    const shell = mount(async (indexPath) => {
      await gate;
      return { ok: true, index: indexPath };
    });

    shell.button.click();
    await settle();
    expect(shell.button.disabled).toBe(true);

    release();
    await settle();
    expect(shell.button.disabled).toBe(false);
    expect(shell.label.textContent).toBe("y.db");
  });
});

describe("CorpusBar.restore", () => {
  it("re-attaches the remembered corpus and paints it, with no click and no picker", async () => {
    // Boot on a machine that already had a corpus: the label has to come up naming it.
    localStorage.setItem(CORPUS_STORAGE_KEY, "/home/me/corpora/anthology.db");
    const shell = mount(okResult, makeCorpusStore(localStorage));
    await shell.bar.restore();

    expect(shell.asked).toEqual(["/home/me/corpora/anthology.db"]);
    expect(shell.label.textContent).toBe("anthology.db");
    expect(shell.label.getAttribute("title")).toBe("/home/me/corpora/anthology.db");
    expect(openDialog).not.toHaveBeenCalled();
  });

  it("paints the restore failure and forgets the dead path", async () => {
    localStorage.setItem(CORPUS_STORAGE_KEY, "/gone/anthology.db");
    const shell = mount(async () => {
      throw new Error("unable to open database file");
    }, makeCorpusStore(localStorage));
    await shell.bar.restore();

    expect(shell.error.textContent).toContain("anthology.db");
    expect(shell.error.textContent).toContain("Choose a corpus to continue.");
    expect(shell.label.textContent).toBe(NO_CORPUS_LABEL);
    expect(localStorage.getItem(CORPUS_STORAGE_KEY)).toBeNull();
  });
});

describe("CorpusBar.destroy", () => {
  it("stops the button from doing anything at all", async () => {
    enterTauriWebview();
    openDialog.mockResolvedValue("/x/y.db");
    const shell = mount();

    shell.bar.destroy();
    shell.button.click();
    await settle();

    expect(openDialog).not.toHaveBeenCalled();
    expect(shell.asked).toEqual([]);
    expect(shell.label.textContent).toBe(NO_CORPUS_LABEL);
  });
});
