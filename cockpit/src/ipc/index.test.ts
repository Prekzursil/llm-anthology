/**
 * Contract for the runtime IPC adapter selection.
 *
 * This is a SAFETY test, not a style test. Two failures are possible and they are not
 * symmetric:
 *
 *   - picking MOCK inside Tauri  -> the shipped app silently shows FABRICATED
 *     conversations instead of the user's real corpus. Catastrophic and near-invisible.
 *   - picking REAL outside Tauri -> an immediate, loud TypeError on first paint. That
 *     is the bug this module was introduced to fix (recorded by the first-ever visual
 *     capture of the cockpit).
 *
 * Both directions are pinned below, plus the laziness property that makes the first
 * failure impossible even if Tauri injects its global after this module is evaluated.
 */
import { afterEach, describe, expect, it } from "vitest";

import { mockIpc } from "./mock";
import { realIpc } from "./real";
import { __resetIpcForTests, currentIpc, ipc, isTauriRuntime, selectIpc } from "./index";

type MaybeTauriGlobal = { __TAURI_INTERNALS__?: unknown };

function installTauriGlobal(value: unknown): void {
  (globalThis as MaybeTauriGlobal).__TAURI_INTERNALS__ = value;
}

function removeTauriGlobal(): void {
  delete (globalThis as MaybeTauriGlobal).__TAURI_INTERNALS__;
}

afterEach(() => {
  removeTauriGlobal();
  __resetIpcForTests();
});

describe("isTauriRuntime", () => {
  it("is false in a plain browser/node environment (no global at all)", () => {
    removeTauriGlobal();
    expect(isTauriRuntime()).toBe(false);
  });

  it("is true when Tauri has injected a real invoke function", () => {
    installTauriGlobal({ invoke: () => Promise.resolve(null) });
    expect(isTauriRuntime()).toBe(true);
  });

  it("is false for a PARTIALLY initialised global (namespace present, no invoke)", () => {
    // Probing the namespace alone would mis-detect this as a live engine and bind the
    // real adapter to a backend that cannot answer.
    installTauriGlobal({});
    expect(isTauriRuntime()).toBe(false);
  });

  it("is false when invoke is present but not callable", () => {
    installTauriGlobal({ invoke: "not-a-function" });
    expect(isTauriRuntime()).toBe(false);
  });
});

describe("selectIpc", () => {
  it("returns the MOCK adapter outside Tauri", () => {
    removeTauriGlobal();
    expect(selectIpc()).toBe(mockIpc);
  });

  it("returns the REAL adapter inside Tauri", () => {
    installTauriGlobal({ invoke: () => Promise.resolve(null) });
    expect(selectIpc()).toBe(realIpc);
  });
});

describe("currentIpc memoisation", () => {
  it("resolves once and reuses the same adapter", () => {
    removeTauriGlobal();
    const first = currentIpc();
    // Even if the environment appears to change afterwards, the session keeps ONE
    // adapter — swapping mid-session would mix fabricated and real data.
    installTauriGlobal({ invoke: () => Promise.resolve(null) });
    expect(currentIpc()).toBe(first);
    expect(currentIpc()).toBe(mockIpc);
  });
});

describe("the exported `ipc` proxy is LAZY", () => {
  it("does not bind an adapter at module-evaluation time", () => {
    // The crux: this module was imported at the top of the file, BEFORE any Tauri
    // global existed. If binding were eager, `ipc` would be permanently mock here and
    // a late-injecting Tauri build would silently ship mock data.
    __resetIpcForTests();
    installTauriGlobal({ invoke: () => Promise.resolve(null) });
    expect(currentIpc()).toBe(realIpc);
  });

  it("forwards property access to the resolved adapter", () => {
    removeTauriGlobal();
    expect(typeof ipc.healthPing).toBe("function");
    expect(ipc.healthPing).toBe(mockIpc.healthPing);
  });

  it("supports the `in` operator via the has trap", () => {
    removeTauriGlobal();
    expect("healthPing" in ipc).toBe(true);
    expect("definitelyNotAMethod" in ipc).toBe(false);
  });
});
