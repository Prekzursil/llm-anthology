// @vitest-environment happy-dom
/**
 * `main.ts` — the boot shim, all 24 lines of it, through its two seams.
 *
 * WHY A MOCKED `app.ts`. The shim's job is to CONSTRUCT {@link CockpitApp} once and hand the
 * document a failure line when that construction throws — not to re-test the app. Importing
 * the real `app.ts` here would couple boot's spec to every panel the app wires, so the class
 * is replaced with a mock whose constructor/init behaviour each test sets. The mock is also
 * what lets the FAILURE path run at all: no constructor input makes the real app throw, and
 * inventing one (a missing DOM id, say) would pin boot's error text to an app detail.
 *
 * WHY happy-dom. The module touches `document.readyState`, `document.getElementById`,
 * `window.addEventListener` and fires a real `DOMContentLoaded` — none of which exist under
 * the suite's `node` default. Per-file opt-in per `cockpit/vitest.config.ts`.
 *
 * THE RE-IMPORT SEAM. `main.ts` runs at import time, so every case imports it fresh after
 * `vi.resetModules()`; `readyState` is forced with a getter so each case controls which of
 * the two boot arms (`loading` → deferred, else → immediate) actually runs.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

/** Mock-side state the hoisted `vi.mock` factory closes over (factories cannot see outer `let`s). */
const boot = vi.hoisted(() => ({
  constructed: 0,
  initCalls: 0,
  /** Set before import to make the mocked constructor throw — the boot-failure arm. */
  error: null as Error | null,
}));

vi.mock("./app", () => ({
  CockpitApp: class MockCockpitApp {
    constructor() {
      boot.constructed += 1;
      if (boot.error !== null) throw boot.error;
    }
    init(): Promise<void> {
      boot.initCalls += 1;
      return Promise.resolve();
    }
  },
}));

function setReadyState(state: DocumentReadyState): void {
  Object.defineProperty(document, "readyState", { configurable: true, get: () => state });
}

async function importMain(): Promise<void> {
  await import("./main");
}

beforeEach(() => {
  vi.resetModules();
  boot.constructed = 0;
  boot.initCalls = 0;
  boot.error = null;
  // Clear the body's CHILDREN only: `document.replaceChildren()` would detach <body> itself.
  document.body.replaceChildren();
  setReadyState("complete");
});

describe("main boot", () => {
  it("boots immediately when the document is already parsed", async () => {
    setReadyState("complete");
    await importMain();
    expect(boot.constructed).toBe(1);
    expect(boot.initCalls).toBe(1);
  });

  it("defers boot to DOMContentLoaded while the document is still loading", async () => {
    setReadyState("loading");
    await importMain();
    expect(boot.constructed).toBe(0);
    expect(boot.initCalls).toBe(0);
    window.dispatchEvent(new Event("DOMContentLoaded"));
    expect(boot.constructed).toBe(1);
    expect(boot.initCalls).toBe(1);
  });

  it("reports a boot failure in the #health line AND re-throws it", async () => {
    const health = document.createElement("p");
    health.id = "health";
    document.body.append(health);
    boot.error = new Error("boom");

    await expect(importMain()).rejects.toThrow("boom");
    expect(health.textContent).toBe("cockpit failed to start: Error: boom");
    expect(boot.initCalls).toBe(0);
  });

  it("still re-throws a boot failure when no #health line exists", async () => {
    boot.error = new Error("no health anywhere");
    await expect(importMain()).rejects.toThrow("no health anywhere");
  });
});
