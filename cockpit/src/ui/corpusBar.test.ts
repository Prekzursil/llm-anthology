/**
 * The corpus bar's DECISIONS — everything that determines what the user sees and whether
 * the engine is asked to attach anything.
 *
 * Why these are testable at all: vitest here runs `environment: "node"`
 * (`vitest.config.ts`), so there is no `document` and no `localStorage`. The label rules,
 * the failure wording, the persistence guard and the whole attach/cancel/restore flow
 * therefore live in pure functions and a DOM-free controller, with the DOM shell
 * (`CorpusBar`) doing nothing but the native picker call and a `textContent` paint. That
 * is the same seam `virtualList`'s `emptyStateLabel` uses, and it is the reason this
 * behaviour has tests rather than a promise.
 *
 * The shell's picker path is NOT covered here — it needs a live Tauri webview — so the
 * one thing these tests cannot settle is whether the native dialog actually opens.
 */
import { describe, expect, it, vi } from "vitest";

import { createMockIpc } from "../ipc/mock";
import type { AppInfo, AppLocations, OpenCorpusResult } from "../ipc/types";
import {
  basenameOf,
  corpusLabel,
  CorpusBarController,
  localCorpusStore,
  makeCorpusStore,
  NO_CORPUS_LABEL,
  openFailureMessage,
  restoreFailureMessage,
  type CorpusBarView,
  type CorpusIpc,
  type CorpusStore,
  type WebStorageLike,
} from "./corpusBar";

// ---------------------------------------------------------------------------
// pure derivations
// ---------------------------------------------------------------------------

describe("basenameOf", () => {
  it("takes the last segment of a POSIX path", () => {
    expect(basenameOf("/home/me/corpora/anthology.db")).toBe("anthology.db");
  });

  it("takes the last segment of a Windows path", () => {
    // The native picker returns backslash paths on this platform, while the engine and
    // its fixtures use POSIX ones — both have to shorten.
    expect(basenameOf("C:\\Users\\me\\corpora\\anthology.db")).toBe("anthology.db");
  });

  it("ignores trailing separators", () => {
    expect(basenameOf("/var/data/corpus.sqlite3/")).toBe("corpus.sqlite3");
    expect(basenameOf("D:\\data\\corpus.sqlite\\\\")).toBe("corpus.sqlite");
  });

  it("returns a bare filename unchanged", () => {
    expect(basenameOf("anthology.db")).toBe("anthology.db");
  });

  it("returns empty for a path that is nothing but separators", () => {
    expect(basenameOf("///")).toBe("");
  });
});

describe("corpusLabel", () => {
  it("reports the no-corpus state for null", () => {
    expect(corpusLabel(null)).toEqual({ label: NO_CORPUS_LABEL, title: "" });
  });

  it("treats a blank path as no corpus rather than an empty label", () => {
    // An empty `title` is what tells the shell to REMOVE the attribute; a blank path that
    // slipped through would otherwise render as an unlabelled, hoverable nothing.
    expect(corpusLabel("   ")).toEqual({ label: NO_CORPUS_LABEL, title: "" });
  });

  it("shows the basename prominently and keeps the full path as the tooltip", () => {
    expect(corpusLabel("/home/me/corpora/anthology.db")).toEqual({
      label: "anthology.db",
      title: "/home/me/corpora/anthology.db",
    });
  });

  it("falls back to the whole path when it has no basename", () => {
    expect(corpusLabel("///")).toEqual({ label: "///", title: "///" });
  });
});

describe("openFailureMessage", () => {
  it("replaces the engine dead-end string instead of echoing it", () => {
    // This exact string (src-tauri/src/lib.rs:45) is what the app used to display with no
    // way to act on it. It names an internal command, so it must never reach the user.
    const msg = openFailureMessage(
      new Error("no corpus attached: call open_corpus first"),
    );
    expect(msg).not.toContain("open_corpus");
    expect(msg).toContain("choose a corpus index file");
  });

  it("keeps a real cause but scrubs the internal command name out of it", () => {
    const msg = openFailureMessage(
      new Error("open_corpus failed: unable to open database file"),
    );
    expect(msg).not.toContain("open_corpus");
    expect(msg).toContain("unable to open database file");
  });

  it("surfaces the engine's missing-file prose verbatim", () => {
    // src-tauri/src/lib.rs:61 — `format!("no corpus index at {index_path}")`. It already
    // names the path and implies the fix, so wrapping it would print the path twice.
    const msg = openFailureMessage(new Error("no corpus index at C:\\me\\gone.db"));
    expect(msg).toBe("no corpus index at C:\\me\\gone.db");
  });

  it("surfaces the engine's folder prose verbatim", () => {
    // src-tauri/src/lib.rs:59 — the other half of the validation, kept distinct because
    // "it is a folder" and "it is not there" need different fixes from the user.
    const msg = openFailureMessage(
      new Error("C:\\me\\corpora is a folder, not a corpus index file"),
    );
    expect(msg).toBe("C:\\me\\corpora is a folder, not a corpus index file");
  });

  it("still wraps an INTERNAL failure, which has no user-facing prose of its own", () => {
    // The other branch of the same rule: a bare `spawn ENOENT` with no context is worse
    // than a wrapped one, so verbatim passthrough must NOT be the default.
    expect(openFailureMessage("spawn ENOENT")).toBe(
      "Could not open that corpus: spawn ENOENT",
    );
  });

  it("degrades to a bare sentence when the error carries no message", () => {
    expect(openFailureMessage(new Error(""))).toBe("Could not open that corpus.");
  });
});

describe("restoreFailureMessage", () => {
  it("names the remembered file, since the user did not ask for this open", () => {
    const msg = restoreFailureMessage(
      "/home/me/corpora/anthology.db",
      new Error("unable to open database file"),
    );
    expect(msg).toContain("anthology.db");
    expect(msg).toContain("unable to open database file");
    expect(msg).toContain("Choose a corpus to continue.");
  });

  it("keeps the engine's prose verbatim and appends only the next action", () => {
    // The live case after a corpus is moved between launches. The engine message already
    // names the path, so the filename must NOT be repeated in front of it.
    const msg = restoreFailureMessage(
      "/home/me/corpora/anthology.db",
      new Error("no corpus index at /home/me/corpora/anthology.db"),
    );
    expect(msg).toBe(
      "no corpus index at /home/me/corpora/anthology.db. Choose a corpus to continue.",
    );
  });

  it("does not double the full stop when the engine prose already ends in one", () => {
    const msg = restoreFailureMessage(
      "/x/y.db",
      new Error("no corpus index at /x/y.db."),
    );
    expect(msg).toBe("no corpus index at /x/y.db. Choose a corpus to continue.");
  });

  it("scrubs the internal command name here too", () => {
    const msg = restoreFailureMessage("/x/y.db", new Error("open_corpus exploded"));
    expect(msg).not.toContain("open_corpus");
    expect(msg).toContain("y.db");
  });

  it("still names the file when the error carries no message", () => {
    const msg = restoreFailureMessage("/x/y.db", new Error(""));
    expect(msg).toBe("Could not reopen y.db. Choose a corpus to continue.");
  });

  it("shows the whole remembered path when it has no basename to show", () => {
    // `basenameOf` answers "" for a path that is nothing but separators, and a remembered
    // value comes back out of Web Storage unvalidated, so it can be anything that was ever
    // written there. Reporting "Could not reopen ." would name nothing at all.
    const msg = restoreFailureMessage("///", new Error("unable to open database file"));
    expect(msg).toBe(
      "Could not reopen ///: unable to open database file. Choose a corpus to continue.",
    );
  });
});

// ---------------------------------------------------------------------------
// persistence guard
// ---------------------------------------------------------------------------

/** A working in-memory Web Storage stand-in. */
function fakeStorage(seed: Record<string, string> = {}): WebStorageLike {
  const data = new Map(Object.entries(seed));
  return {
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
    removeItem: (k) => void data.delete(k),
  };
}

/** A Web Storage that refuses every operation, as a hardened webview's does. */
function throwingStorage(): WebStorageLike {
  const boom = (): never => {
    throw new Error("SecurityError: storage is disabled");
  };
  return { getItem: boom, setItem: boom, removeItem: boom };
}

describe("makeCorpusStore", () => {
  it("round-trips through a working storage", () => {
    const storage = fakeStorage();
    const store = makeCorpusStore(storage, "k");
    expect(store.read()).toBeNull();
    store.write("/x/y.db");
    expect(store.read()).toBe("/x/y.db");
    store.clear();
    expect(store.read()).toBeNull();
  });

  it("degrades to a no-op when there is no storage at all", () => {
    const store = makeCorpusStore(null, "k");
    expect(store.read()).toBeNull();
    expect(() => store.write("/x/y.db")).not.toThrow();
    expect(() => store.clear()).not.toThrow();
    expect(store.read()).toBeNull();
  });

  it("swallows a storage that throws on every access", () => {
    // Losing a remembered path is a lost convenience; it must never take the bar down.
    const store = makeCorpusStore(throwingStorage(), "k");
    expect(store.read()).toBeNull();
    expect(() => store.write("/x/y.db")).not.toThrow();
    expect(() => store.clear()).not.toThrow();
  });
});

describe("localCorpusStore", () => {
  it("reads null under the node runner, where localStorage does not exist", () => {
    // A genuine assertion about this environment, not a tautology: `globalThis.localStorage`
    // is undefined in the node test runner, so this exercises the absent-storage branch
    // through the real default factory the shell uses.
    expect(globalThis.localStorage).toBeUndefined();
    const store = localCorpusStore();
    expect(store.read()).toBeNull();
    expect(() => store.write("/x/y.db")).not.toThrow();
  });

  it("survives a webview where merely TOUCHING localStorage throws", () => {
    // A DIFFERENT failure from `throwingStorage` above, and the reason the probe itself is
    // wrapped rather than only the calls: with site data disabled some webviews throw on the
    // property READ, so there is never an object to call `getItem` on. The observable proof
    // that the guard caught it is that the factory still returns a working no-op store —
    // `read()` answers null AFTER a write, which only the storage-is-null store does.
    const before = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get(): never {
        throw new Error("SecurityError: access to storage is denied");
      },
    });
    try {
      const store = localCorpusStore();
      store.write("/x/y.db");
      expect(store.read()).toBeNull();
      store.clear();
      expect(store.read()).toBeNull();
    } finally {
      if (before === undefined) Reflect.deleteProperty(globalThis, "localStorage");
      else Object.defineProperty(globalThis, "localStorage", before);
    }
  });
});

// ---------------------------------------------------------------------------
// controller
// ---------------------------------------------------------------------------

/** A recording store over an in-memory map, so writes/clears are assertable. */
function recordingStore(initial: string | null = null): CorpusStore & {
  value: string | null;
  writes: string[];
  clears: number;
} {
  return {
    value: initial,
    writes: [],
    clears: 0,
    read() {
      return this.value;
    },
    write(path: string) {
      this.value = path;
      this.writes.push(path);
    },
    clear() {
      this.value = null;
      this.clears += 1;
    },
  };
}

/** Wire a controller to spies and return everything a test needs to assert on. */
function harness(
  openCorpus: (indexPath: string) => Promise<OpenCorpusResult>,
  store: CorpusStore = recordingStore(),
) {
  const views: CorpusBarView[] = [];
  const opened: string[] = [];
  const spy = vi.fn(openCorpus);
  const controller = new CorpusBarController(
    { openCorpus: spy },
    store,
    (v) => views.push(v),
    (p) => opened.push(p),
  );
  return { controller, views, opened, spy, store };
}

const okResult = async (indexPath: string): Promise<OpenCorpusResult> => ({
  ok: true,
  index: indexPath,
});

describe("CorpusBarController.open", () => {
  it("treats a dismissed picker as a no-op, not an error", () => {
    // `open(null)` is the user pressing Cancel. Nothing may be attached, nothing painted.
    const h = harness(okResult);
    const before = h.controller.current;
    return h.controller.open(null).then(() => {
      expect(h.spy).not.toHaveBeenCalled();
      expect(h.views).toEqual([]);
      expect(h.controller.current).toBe(before);
      expect(h.controller.current.error).toBe("");
    });
  });

  it("attaches, labels, persists and notifies on success", async () => {
    const h = harness(okResult);
    await h.controller.open("/home/me/corpora/anthology.db");

    expect(h.spy).toHaveBeenCalledWith("/home/me/corpora/anthology.db");
    expect(h.controller.current).toEqual({
      path: "/home/me/corpora/anthology.db",
      label: "anthology.db",
      title: "/home/me/corpora/anthology.db",
      error: "",
      busy: false,
    });
    expect(h.store.read()).toBe("/home/me/corpora/anthology.db");
    expect(h.opened).toEqual(["/home/me/corpora/anthology.db"]);
  });

  it("marks itself busy while the open is in flight, then clears it", async () => {
    const h = harness(okResult);
    await h.controller.open("/x/y.db");
    expect(h.views.map((v) => v.busy)).toEqual([true, false]);
  });

  it("reports a failure without persisting or notifying", async () => {
    const h = harness(async () => {
      throw new Error("unable to open database file");
    });
    await h.controller.open("/x/broken.db");

    expect(h.controller.current.error).toContain("unable to open database file");
    expect(h.controller.current.busy).toBe(false);
    expect(h.store.read()).toBeNull();
    expect(h.opened).toEqual([]);
  });

  it("treats ok:false as a failure even though the call resolved", async () => {
    const h = harness(async (indexPath) => ({ ok: false, index: indexPath }));
    await h.controller.open("/x/refused.db");

    expect(h.controller.current.error).not.toBe("");
    expect(h.controller.current.path).toBeNull();
    expect(h.opened).toEqual([]);
  });

  it("keeps the incumbent corpus attached when a LATER open fails", async () => {
    // The engine spawns the replacement before dropping the incumbent
    // (src-tauri/src/lib.rs:53-56), so the old corpus really is still attached. Showing
    // "No corpus open" here would misreport the engine's state.
    let fail = false;
    const h = harness(async (indexPath) => {
      if (fail) throw new Error("unable to open database file");
      return { ok: true, index: indexPath };
    });
    await h.controller.open("/good/first.db");
    fail = true;
    await h.controller.open("/bad/second.db");

    expect(h.controller.current.path).toBe("/good/first.db");
    expect(h.controller.current.label).toBe("first.db");
    expect(h.controller.current.error).toContain("unable to open database file");
    expect(h.opened).toEqual(["/good/first.db"]);
  });

  it("ignores a re-entrant open while one is already in flight", async () => {
    let release = (): void => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const h = harness(async (indexPath) => {
      await gate;
      return { ok: true, index: indexPath };
    });

    const first = h.controller.open("/first.db");
    await h.controller.open("/second.db"); // single-flight: dropped
    release();
    await first;

    expect(h.spy).toHaveBeenCalledTimes(1);
    expect(h.spy).toHaveBeenCalledWith("/first.db");
    expect(h.controller.current.path).toBe("/first.db");
  });
});

describe("CorpusBarController.restore", () => {
  it("does nothing on a first launch with nothing remembered", async () => {
    const h = harness(okResult, recordingStore(null));
    await h.controller.restore();
    expect(h.spy).not.toHaveBeenCalled();
    expect(h.controller.current.label).toBe(NO_CORPUS_LABEL);
  });

  it("ignores a remembered blank path rather than attaching nothing", async () => {
    const h = harness(okResult, recordingStore("   "));
    await h.controller.restore();
    expect(h.spy).not.toHaveBeenCalled();
  });

  it("re-attaches the remembered corpus so the user need not re-pick", async () => {
    const h = harness(okResult, recordingStore("/home/me/anthology.db"));
    await h.controller.restore();

    expect(h.spy).toHaveBeenCalledWith("/home/me/anthology.db");
    expect(h.controller.current.label).toBe("anthology.db");
    expect(h.opened).toEqual(["/home/me/anthology.db"]);
  });

  it("forgets a remembered path that no longer opens, and says why", async () => {
    // Not a crash and not an infinite retry: the dead path is dropped so the NEXT launch
    // starts clean, and the bar degrades to the no-corpus state with an explanation.
    const store = recordingStore("/gone/anthology.db");
    const h = harness(async () => {
      throw new Error("unable to open database file");
    }, store);
    await h.controller.restore();

    expect(store.clears).toBe(1);
    expect(store.read()).toBeNull();
    expect(h.controller.current.path).toBeNull();
    expect(h.controller.current.label).toBe(NO_CORPUS_LABEL);
    expect(h.controller.current.error).toContain("anthology.db");
    expect(h.controller.current.error).toContain("Choose a corpus to continue.");
  });
});

describe("CorpusBarController against the real mock adapter", () => {
  it("round-trips through createMockIpc, which records what it was asked to open", async () => {
    // The mock is the reference implementation the UI is built against, so the controller
    // is exercised against it rather than only against hand-written fakes.
    const mock = createMockIpc();
    const views: CorpusBarView[] = [];
    const controller = new CorpusBarController(
      mock,
      makeCorpusStore(fakeStorage()),
      (v) => views.push(v),
      () => {},
    );

    expect(mock.openedIndex).toBeNull();
    await controller.open("/home/me/corpora/anthology.db");

    expect(mock.openedIndex).toBe("/home/me/corpora/anthology.db");
    expect(controller.current.label).toBe("anthology.db");
    expect(controller.current.error).toBe("");
  });

  it("serves its fixture forest WITHOUT waiting for an open", async () => {
    // Deliberate: `ipc/index.ts` binds every non-Tauri environment (vite dev, preview,
    // the screenshot harness, a design review) to this mock, and the native picker only
    // exists inside the Tauri webview. A mock that gated its fixtures on openCorpus would
    // leave those environments with no corpus and no way to get one.
    const mock = createMockIpc();
    expect(mock.openedIndex).toBeNull();
    expect((await mock.graphRoots()).length).toBeGreaterThan(0);
    expect((await mock.healthPing()).corpus_ready).toBe(true);
  });
});

describe("CF-1: where the default corpus index lives", () => {
  // The Rust `app_info` command resolves the DECISION G-10 app-data locations against the
  // REAL environment on every call, and until this change nothing on the TypeScript side
  // read the answer — the resolution, its remote-drive rejection and its five paths were
  // all dead below the Tauri boundary. A first-run user saw "No corpus open" and a file
  // picker, with no indication of where the app keeps its own index.
  //
  // Surfaced through the EXISTING label tooltip rather than new markup: `index.html` is
  // outside this change, and while nothing is attached that tooltip is empty anyway, so
  // nothing is displaced. It is a hint, not an action — the bar does NOT auto-attach a
  // file the user never picked.
  const LOCATIONS: AppLocations = {
    data_dir: "C:\\Users\\u\\AppData\\Local\\Cockpit",
    index_path: "C:\\Users\\u\\AppData\\Local\\Cockpit\\corpus.sqlite",
    logs_dir: "C:\\Users\\u\\AppData\\Local\\Cockpit\\logs",
    cache_dir: "C:\\Users\\u\\AppData\\Local\\Cockpit\\cache",
    exports_dir: "C:\\Users\\u\\AppData\\Local\\Cockpit\\exports",
    grant_roots: ["C:\\Users\\u\\AppData\\Local\\Cockpit"],
  };

  function info(over: Partial<AppInfo> = {}): AppInfo {
    return {
      name: "Cockpit",
      version: "0.0.0",
      engine: "not-wired",
      locations: LOCATIONS,
      locations_error: null,
      diagnostics: null,
      ...over,
    };
  }

  function barWith(appInfo?: () => Promise<AppInfo>, remembered: string | null = null) {
    const views: CorpusBarView[] = [];
    const store: CorpusStore = { read: () => remembered, write: () => {}, clear: () => {} };
    const ipc: CorpusIpc = {
      // `{ok, index}` — NOT `{index_path}`, which is `corpus.create`'s shape. The first
      // draft used the wrong one; because vitest does not typecheck, the fake resolved,
      // `result.ok` came back undefined, and `attach` took its FAILURE path. Two tests
      // then failed for a reason that had nothing to do with what they were testing.
      openCorpus: async (indexPath: string): Promise<OpenCorpusResult> => ({
        ok: true,
        index: indexPath,
      }),
      ...(appInfo === undefined ? {} : { appInfo }),
    };
    return {
      views,
      controller: new CorpusBarController(ipc, store, (v) => views.push(v), () => {}),
    };
  }

  it("tells a first-run user the default index path, in the label tooltip", async () => {
    const { controller } = barWith(async () => info());
    await controller.restore();
    expect(controller.current.label).toBe(NO_CORPUS_LABEL);
    expect(controller.current.title).toContain(LOCATIONS.index_path);
    // A hint, not an attach: nothing was opened and nothing was remembered.
    expect(controller.current.path).toBeNull();
    expect(controller.current.error).toBe("");
  });

  it("yields the tooltip to the REAL path once a corpus is attached", async () => {
    // Ordering matters and is why this is asserted separately: the hint is only useful
    // while nothing is open, and leaving it in place afterwards would make the tooltip
    // name a path the app is not using.
    const { controller } = barWith(async () => info());
    await controller.restore();
    await controller.open("D:\\picked.sqlite");
    expect(controller.current.title).toBe("D:\\picked.sqlite");
    expect(controller.current.title).not.toContain(LOCATIONS.index_path);
  });

  it("stays silent when resolution FAILED, rather than showing the reason as an error", async () => {
    // `locations_error` is a real branch — a stripped environment with no %USERPROFILE%
    // resolves to nothing (`lib.rs:26-29`). That is not something the reader can act on,
    // and it must not paint the failure line, which is reserved for a failed OPEN.
    const { controller } = barWith(async () =>
      info({ locations: null, locations_error: "no %USERPROFILE% in this environment" }),
    );
    await controller.restore();
    expect(controller.current.title).toBe("");
    expect(controller.current.error).toBe("");
  });

  it("survives an appInfo that REJECTS, and one that is not implemented at all", async () => {
    // Two distinct degradations, both fail-open. The rejection is the runtime case; the
    // absent method is the type-level one — `appInfo` is optional on `CorpusIpc`, so every
    // existing caller and every narrowed fake keeps compiling.
    const rejecting = barWith(async () => {
      throw new Error("backend gone");
    });
    await rejecting.controller.restore();
    expect(rejecting.controller.current.title).toBe("");
    expect(rejecting.controller.current.error).toBe("");

    const absent = barWith(undefined);
    await absent.controller.restore();
    expect(absent.controller.current.title).toBe("");
  });

  it("does not consult appInfo when there IS a remembered corpus to restore", async () => {
    // The hint exists for the empty case only, so the boot path of a returning user must
    // not pay for a command whose answer it would discard.
    const calls: number[] = [];
    const { controller } = barWith(async () => {
      calls.push(1);
      return info();
    }, "D:\\remembered.sqlite");
    await controller.restore();
    expect(controller.current.path).toBe("D:\\remembered.sqlite");
    expect(calls).toEqual([]);
  });
});
