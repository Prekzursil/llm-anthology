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
import { afterEach, describe, expect, it, vi } from "vitest";

// `real.ts` reaches a backend only through this one function, so stubbing it lets the REAL
// adapter be driven under node and its Tauri command names asserted. Hoisted by vitest above
// the imports below. Nothing in `./index` touches this module — adapter selection probes the
// `__TAURI_INTERNALS__` global, not the package — so the stub cannot skew the tests above.
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(async () => null) }));

import { invoke } from "@tauri-apps/api/core";

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

/** Every function-valued own property of an adapter, sorted. */
function methodsOf(adapter: object): string[] {
  return Object.entries(adapter)
    .filter(([, value]) => typeof value === "function")
    .map(([name]) => name)
    .sort();
}

describe("adapter surface parity", () => {
  it("exposes the SAME method set on the mock and the real adapter", () => {
    // The asymmetric failure `./index.ts` documents has a quieter cousin: a method
    // implemented on ONE adapter only. tsc cannot catch it while the interface marks any
    // method optional, and the app then works in every dev run and throws in the shipped
    // build (or the reverse). This is the mechanical check for that drift.
    expect(methodsOf(mockIpc)).toEqual(methodsOf(realIpc));
  });

  it("carries the 11 metadata/dedup/maintenance methods on both", () => {
    const expected = [
      "dedupScan",
      "dedupSessions",
      "maintenanceExecute",
      "maintenancePlan",
      "maintenanceRestore",
      "maintenanceRuns",
      "metadataClear",
      "metadataGet",
      "metadataSearch",
      "metadataSet",
      "metadataTags",
    ];
    expect(expected).toHaveLength(11);
    for (const adapter of [mockIpc, realIpc]) {
      const names = methodsOf(adapter);
      for (const method of expected) expect(names).toContain(method);
    }
  });

  it("binds NEITHER research method on either adapter", () => {
    // The engine registers `research.synthesize` / `research.extract_entities`, but there is
    // no Tauri command for them (`cockpit/src-tauri/src/lib.rs:322-333`), so a binding here
    // would type-check, pass every mock test, and throw only when a user pressed the button.
    // Pinned on BOTH adapters because the parity test above would stay green if someone
    // helpfully added them to both.
    for (const adapter of [mockIpc, realIpc]) {
      const names = methodsOf(adapter);
      expect(names).not.toContain("researchSynthesize");
      expect(names).not.toContain("researchExtractEntities");
    }
  });
});

/**
 * The RPC -> Tauri command mapping, asserted against the REAL adapter.
 *
 * This is the one failure mode on this surface that NOTHING else can see: a command-name
 * typo type-checks (it is a string literal), passes every mock test (the mock never names a
 * command) and fails only when a user presses a button in the shipped app. Each expected
 * command name below is DERIVED from the RPC method name by the pinned rule rather than
 * transcribed, so a rename cannot quietly re-baseline the test to whatever the code does.
 */
describe("real adapter Tauri command names", () => {
  const invokeMock = vi.mocked(invoke);

  /** The pinned rule: RPC `a.b` -> command `a_b`. */
  function command(rpc: string): string {
    return rpc.replace(/\./g, "_");
  }

  const cases: Array<[rpc: string, call: () => Promise<unknown>, params: unknown]> = [
    ["metadata.get", () => realIpc.metadataGet("c-a"), { conversation_id: "c-a" }],
    [
      "metadata.set",
      () => realIpc.metadataSet({ conversation_id: "c-a", alias: "A" }),
      { conversation_id: "c-a", alias: "A" },
    ],
    ["metadata.clear", () => realIpc.metadataClear("c-a"), { conversation_id: "c-a" }],
    ["metadata.search", () => realIpc.metadataSearch({ tag: "rust" }), { tag: "rust" }],
    ["metadata.search", () => realIpc.metadataSearch(), {}],
    ["metadata.tags", () => realIpc.metadataTags(), {}],
    ["dedup.scan", () => realIpc.dedupScan("C:\\codex"), { codex_home: "C:\\codex" }],
    ["dedup.sessions", () => realIpc.dedupSessions(), {}],
    [
      "maintenance.plan",
      () =>
        realIpc.maintenancePlan({
          store_root: "C:\\store",
          checkpoint_root: "C:\\cp",
          action: "delete",
          targets: [{ file_path: "C:\\store\\a.jsonl" }],
        }),
      {
        store_root: "C:\\store",
        checkpoint_root: "C:\\cp",
        action: "delete",
        targets: [{ file_path: "C:\\store\\a.jsonl" }],
      },
    ],
    [
      "maintenance.execute",
      () => realIpc.maintenanceExecute({ plan_id: "plan-1", confirmation: "DELETE 1 FILE" }),
      { plan_id: "plan-1", confirmation: "DELETE 1 FILE" },
    ],
    [
      "maintenance.restore",
      () => realIpc.maintenanceRestore({ manifest_path: "C:\\cp\\m.json", apply: true }),
      { manifest_path: "C:\\cp\\m.json", apply: true },
    ],
    ["maintenance.runs", () => realIpc.maintenanceRuns(10), { limit: 10 }],
    ["maintenance.runs", () => realIpc.maintenanceRuns(), {}],
  ];

  it.each(cases)("%s forwards to its snake_case command", async (rpc, call, params) => {
    invokeMock.mockClear();
    await call();
    expect(invokeMock).toHaveBeenCalledTimes(1);
    // Every data command takes the single `{ params }` wrapper its Rust signature declares.
    expect(invokeMock).toHaveBeenCalledWith(command(rpc), { params });
  });

  it("OMITS an absent optional rather than sending null", async () => {
    // `_opt_int` rejects a non-int with -32602, so an explicit null is NOT "use the default"
    // — the same trap `corpusBuildStatus` documents for `job_id`. The engine then applies its
    // own default of 50 (`llm_anthology/sidecar.py:1614`).
    invokeMock.mockClear();
    await realIpc.maintenanceRuns();
    expect(invokeMock).toHaveBeenCalledWith("maintenance_runs", { params: {} });
    invokeMock.mockClear();
    await realIpc.metadataSearch();
    expect(invokeMock).toHaveBeenCalledWith("metadata_search", { params: {} });
  });

  it("forwards metadata.set VERBATIM, preserving the partial-update tri-state", async () => {
    // Normalising the undefined fields to null/"" here would silently CLEAR tags and notes
    // on every per-field edit (`llm_anthology/sidecar.py:1335-1337`).
    invokeMock.mockClear();
    await realIpc.metadataSet({ conversation_id: "c-a", alias: "only the alias" });
    const [, args] = invokeMock.mock.calls[0] as [string, { params: Record<string, unknown> }];
    expect(Object.keys(args.params).sort()).toEqual(["alias", "conversation_id"]);
    expect("tags" in args.params).toBe(false);
    expect("notes" in args.params).toBe(false);
  });
});
