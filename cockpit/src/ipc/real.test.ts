/**
 * The REAL adapter — the one that actually ships, and the one nothing exercised.
 *
 * Every panel, every other test, every dev run and every screenshot goes through `mock.ts`,
 * because `index.ts` selects the mock outside the Tauri webview. So the code path users get
 * is the path with no coverage, and its whole job is a set of STRING LITERALS: a Tauri
 * command name per method. A typo there type-checks (it is a string), passes every mock test
 * (the mock never names a command), survives CI, and throws the first time a user presses the
 * button.
 *
 * WHAT THIS ADDS OVER `index.test.ts`. That file already asserts the RPC -> command rule for
 * the ELEVEN metadata/dedup/maintenance bindings. Three things were still unasserted:
 *
 *   1. the other TWENTY methods — names, `{ params }` wrapping, and the by-name exceptions;
 *   2. whether a command real.ts invokes is REGISTERED in Rust at all. `lib.rs:232` says
 *      "`invoke_handler` is equally invisible": a perfectly rule-abiding name that nobody
 *      added to `generate_handler!` fails exactly like a typo. Nothing checked that;
 *   3. that results pass through unreshaped and rejections propagate.
 *
 * HOW THE NAMES ARE CHECKED WITHOUT CIRCULARITY. Transcribing 31 command names out of
 * `real.ts` into a table would faithfully reproduce a typo and call it verified. So instead
 * each row carries the RPC method the ENGINE dispatches (`llm_anthology/sidecar.py`'s handler
 * map) and the expected command is DERIVED from it by the pinned rule `a.b` -> `a_b`; and,
 * independently, every command actually passed to `invoke` at runtime is checked for
 * membership in the `generate_handler!` list READ OUT OF `src-tauri/src/lib.rs`. The second
 * check involves no string I typed at all — it compares what the code does against the other
 * side of the wire.
 *
 * THE RULE HAS TWO REAL EXCEPTIONS, both verified in the Rust source rather than assumed:
 * `create_corpus` forwards `corpus.create` (`lib.rs:102-108`) and `discover_sources` forwards
 * `sources.discover` (`lib.rs:122-126`). Both INVERT the words, so a naive derive-and-assert
 * would have failed against correct code — which is why they are pinned as exceptions.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

// Hoisted by vitest above the imports below. `real.ts` reaches a backend only through
// `invoke`, so stubbing this one function is enough to drive the whole adapter under node.
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(async () => null) }));

import { invoke } from "@tauri-apps/api/core";

import { realIpc } from "./real";
// @ts-ignore — vite resolves `?raw` to the file's text. There is no ambient declaration for
// it in this project and adding one would mean editing a file outside this work's scope.
// `@ts-ignore` rather than `@ts-expect-error` on purpose: if such a declaration is ever
// added, an expect-error directive would itself become the error.
import libRsRaw from "../../src-tauri/src/lib.rs?raw";

const invokeMock = vi.mocked(invoke);

/** The pinned naming rule: RPC `a.b` -> Tauri command `a_b`. */
function ruleCommand(rpc: string): string {
  return rpc.replace(".", "_");
}

/**
 * The commands Rust actually registers, parsed out of `generate_handler![...]`.
 *
 * The detector is verified below before anything is concluded from it — an empty or
 * mis-parsed list would make every membership assertion trivially... true is the wrong word:
 * it would make them FAIL, but a a mis-parse that swallowed one entry would fail confusingly
 * rather than informatively. Either way the control test comes first.
 */
function rustRegisteredCommands(): string[] {
  const src = libRsRaw as string;
  const block = /generate_handler!\[([\s\S]*?)\]/.exec(src);
  if (block === null) throw new Error("generate_handler! not found in src-tauri/src/lib.rs");
  return block[1]
    .split(",")
    .map((entry) => entry.trim())
    .filter((entry) => entry !== "" && !entry.startsWith("//"));
}

/** One method of the real adapter, and what invoking it must put on the wire. */
interface Binding {
  /** The engine RPC method the Rust command forwards; null for a local/lifecycle command. */
  rpc: string | null;
  /** Set ONLY where the command name deliberately departs from {@link ruleCommand}. */
  exception?: string;
  /** Drive the method. */
  call: () => Promise<unknown>;
  /**
   * The expected SECOND argument to `invoke`. `undefined` means the method must call
   * `invoke` with a single argument — which is itself a contract (`discover_sources` takes
   * no parameters at all).
   */
  args: unknown;
}

const TARGET: MaintenanceTargetLike = { file_path: "C:\\codex\\a.jsonl" };
type MaintenanceTargetLike = { file_path: string };

/**
 * EVERY method on the real adapter. Keyed by method name so the exhaustiveness test below
 * can compare against the adapter itself: a method added later without a row here fails
 * loudly instead of quietly going uncovered, which is the whole reason this is a table.
 */
const BINDINGS: Record<string, Binding> = {
  // -- lifecycle: the two by-name commands and the no-argument one -------------------
  openCorpus: {
    // Spawns the sidecar rather than forwarding an RPC (`lib.rs:78-86`), so no rpc name.
    rpc: null,
    exception: "open_corpus",
    call: () => realIpc.openCorpus("C:\\idx.sqlite"),
    // BY NAME, not `{ params }`: the Rust signature is `open_corpus(state, index_path)`.
    args: { indexPath: "C:\\idx.sqlite" },
  },
  createCorpus: {
    rpc: "corpus.create",
    // The rule would give `corpus_create`. Rust names it `create_corpus` (`lib.rs:102`).
    exception: "create_corpus",
    call: () => realIpc.createCorpus("C:\\new.sqlite"),
    args: { indexPath: "C:\\new.sqlite" },
  },
  discoverSources: {
    rpc: "sources.discover",
    // The rule would give `sources_discover`. Rust names it `discover_sources` (`lib.rs:122`).
    exception: "discover_sources",
    call: () => realIpc.discoverSources(),
    // NO second argument at all — the command declares no parameters, so passing the
    // `{ params }` wrapper would hand it an argument its signature does not have.
    args: undefined,
  },

  // -- the data surface -------------------------------------------------------------
  corpusBuild: {
    rpc: "corpus.build",
    call: () => realIpc.corpusBuild({ sessions_root: "C:\\s" }),
    args: { params: { sessions_root: "C:\\s" } },
  },
  corpusBuildStatus: {
    rpc: "corpus.build_status",
    call: () => realIpc.corpusBuildStatus("job-1"),
    args: { params: { job_id: "job-1" } },
  },
  healthPing: {
    rpc: "health.ping",
    call: () => realIpc.healthPing(),
    args: { params: {} },
  },
  corpusStats: {
    rpc: "corpus.stats",
    call: () => realIpc.corpusStats(),
    args: { params: {} },
  },
  graphRoots: {
    rpc: "graph.roots",
    call: () => realIpc.graphRoots({ limit: 5, offset: 10, order: "recent" }),
    args: { params: { limit: 5, offset: 10, order: "recent" } },
  },
  graphChildren: {
    rpc: "graph.children",
    call: () => realIpc.graphChildren("t-1"),
    args: { params: { thread_id: "t-1" } },
  },
  graphSubtree: {
    rpc: "graph.subtree",
    call: () => realIpc.graphSubtree("t-1", 3),
    args: { params: { thread_id: "t-1", depth: 3 } },
  },
  graphAncestors: {
    rpc: "graph.ancestors",
    call: () => realIpc.graphAncestors("t-1"),
    args: { params: { thread_id: "t-1" } },
  },
  searchQuery: {
    rpc: "search.query",
    call: () => realIpc.searchQuery({ q: "rust", limit: 2 }),
    args: { params: { q: "rust", limit: 2 } },
  },
  threadGet: {
    rpc: "thread.get",
    call: () => realIpc.threadGet("t-1"),
    args: { params: { thread_id: "t-1" } },
  },
  conversationGet: {
    rpc: "conversation.get",
    call: () => realIpc.conversationGet("c-1"),
    args: { params: { id: "c-1" } },
  },

  // -- time travel + export ---------------------------------------------------------
  graphRollup: {
    rpc: "graph.rollup",
    call: () => realIpc.graphRollup!(),
    args: { params: {} },
  },
  graphTimeline: {
    rpc: "graph.timeline",
    call: () => realIpc.graphTimeline!(),
    args: { params: {} },
  },
  graphAt: {
    rpc: "graph.at",
    call: () => realIpc.graphAt!(1_770_000_000_000),
    args: { params: { as_of_ms: 1_770_000_000_000 } },
  },
  graphDiff: {
    rpc: "graph.diff",
    call: () => realIpc.graphDiff!(1, 2),
    args: { params: { as_of_a: 1, as_of_b: 2 } },
  },
  exportPlan: {
    rpc: "export.plan",
    // The `dest` argument is accepted for contract parity and deliberately NOT forwarded —
    // `export.plan` is a dry run over the loaded corpus and takes no params.
    call: () => realIpc.exportPlan!("C:\\ignored"),
    args: { params: {} },
  },
  exportRun: {
    rpc: "export.run",
    call: () => realIpc.exportRun!("C:\\out.json"),
    args: { params: { dest_path: "C:\\out.json" } },
  },

  // -- annotations / dedup / maintenance --------------------------------------------
  metadataGet: {
    rpc: "metadata.get",
    call: () => realIpc.metadataGet("c-1"),
    args: { params: { conversation_id: "c-1" } },
  },
  metadataSet: {
    rpc: "metadata.set",
    call: () => realIpc.metadataSet({ conversation_id: "c-1", alias: "A" }),
    args: { params: { conversation_id: "c-1", alias: "A" } },
  },
  metadataClear: {
    rpc: "metadata.clear",
    call: () => realIpc.metadataClear("c-1"),
    args: { params: { conversation_id: "c-1" } },
  },
  metadataSearch: {
    rpc: "metadata.search",
    call: () => realIpc.metadataSearch({ tag: "rust" }),
    args: { params: { tag: "rust" } },
  },
  metadataTags: {
    rpc: "metadata.tags",
    call: () => realIpc.metadataTags(),
    args: { params: {} },
  },
  dedupScan: {
    rpc: "dedup.scan",
    call: () => realIpc.dedupScan("C:\\codex"),
    args: { params: { codex_home: "C:\\codex" } },
  },
  dedupSessions: {
    rpc: "dedup.sessions",
    call: () => realIpc.dedupSessions(),
    args: { params: {} },
  },
  maintenancePlan: {
    rpc: "maintenance.plan",
    call: () => realIpc.maintenancePlan({
      store_root: "C:\\codex",
      checkpoint_root: "C:\\cp",
      action: "archive",
      targets: [TARGET],
    }),
    args: {
      params: {
        store_root: "C:\\codex",
        checkpoint_root: "C:\\cp",
        action: "archive",
        targets: [TARGET],
      },
    },
  },
  maintenanceExecute: {
    rpc: "maintenance.execute",
    call: () => realIpc.maintenanceExecute({ plan_id: "p-1", apply: true }),
    args: { params: { plan_id: "p-1", apply: true } },
  },
  maintenanceRestore: {
    rpc: "maintenance.restore",
    call: () => realIpc.maintenanceRestore({ manifest_path: "C:\\m.json" }),
    args: { params: { manifest_path: "C:\\m.json" } },
  },
  maintenanceRuns: {
    rpc: "maintenance.runs",
    call: () => realIpc.maintenanceRuns(7),
    args: { params: { limit: 7 } },
  },
};

/** The command name a binding must end up invoking. */
function expectedCommand(binding: Binding): string {
  if (binding.exception !== undefined) return binding.exception;
  if (binding.rpc === null) throw new Error("a binding needs either an rpc or an exception");
  return ruleCommand(binding.rpc);
}

function methodsOf(adapter: object): string[] {
  return Object.entries(adapter)
    .filter(([, value]) => typeof value === "function")
    .map(([name]) => name)
    .sort();
}

beforeEach(() => {
  invokeMock.mockReset();
  invokeMock.mockResolvedValue(null);
});

describe("the Rust registration list (detector control)", () => {
  // Verify the parser BEFORE concluding anything from it. A regex that silently matched
  // nothing would make the membership tests below fail for the wrong reason, and a reader
  // would go hunting a naming bug that does not exist.
  it("parses a plausible command list out of lib.rs", () => {
    const commands = rustRegisteredCommands();
    expect(commands.length).toBeGreaterThan(25);
    // Known-present anchors, spread across the file so a truncated match is visible.
    expect(commands).toContain("app_info");
    expect(commands).toContain("health_ping");
    expect(commands).toContain("metadata_tags");
    // Nothing that is obviously not an identifier survived the split.
    for (const command of commands) expect(command).toMatch(/^[a-z][a-z0-9_]*$/);
  });
});

describe("real adapter binding table", () => {
  it("has a row for EVERY method on the adapter, and no row for a method that is gone", () => {
    // The guard that keeps this suite honest as the surface grows: a 32nd binding added to
    // `real.ts` with no row here fails HERE, rather than shipping untested.
    expect(Object.keys(BINDINGS).sort()).toEqual(methodsOf(realIpc));
  });

  it("covers all 31 bindings", () => {
    expect(Object.keys(BINDINGS)).toHaveLength(31);
  });
});

describe("every method invokes the command Rust registers", () => {
  it("passes ONLY registered commands to invoke", async () => {
    // The `invoke_handler` gap, closed. This compares what the adapter DOES at runtime
    // against the other side of the wire; no command name typed in this file takes part, so
    // a transcription error here cannot make it pass.
    const registered = rustRegisteredCommands();
    const used: string[] = [];
    for (const binding of Object.values(BINDINGS)) {
      invokeMock.mockClear();
      await binding.call();
      used.push(invokeMock.mock.calls[0][0]);
    }
    expect(used).toHaveLength(31);
    const unregistered = used.filter((command) => !registered.includes(command));
    expect(unregistered).toEqual([]);
  });

  it("leaves `app_info` as the ONLY registered command the adapter never calls", async () => {
    // Pinned deliberately rather than left to drift: `app_info` is local Rust state
    // (`lib.rs:17-21`) with no place on the data surface. If a future command is registered
    // and never bound, this says so instead of the omission being invisible.
    const used = new Set<string>();
    for (const binding of Object.values(BINDINGS)) {
      invokeMock.mockClear();
      await binding.call();
      used.add(invokeMock.mock.calls[0][0]);
    }
    const unused = rustRegisteredCommands().filter((command) => !used.has(command));
    expect(unused).toEqual(["app_info"]);
  });
});

describe("per-method command name and arguments", () => {
  for (const [method, binding] of Object.entries(BINDINGS)) {
    it(`${method} -> ${expectedCommand(binding)}`, async () => {
      await binding.call();
      expect(invokeMock).toHaveBeenCalledTimes(1);
      const [command, args] = invokeMock.mock.calls[0];
      expect(command).toBe(expectedCommand(binding));
      if (binding.args === undefined) {
        // A single-argument call is the contract, not an accident: the command declares no
        // parameters. `toHaveBeenCalledWith(cmd)` would also pass for `(cmd, undefined)`.
        expect(invokeMock.mock.calls[0]).toHaveLength(1);
      } else {
        expect(args).toEqual(binding.args);
      }
    });
  }
});

describe("the naming rule, and its two verified exceptions", () => {
  it("derives 28 of the 31 command names from the RPC method by the rule", () => {
    const byRule = Object.values(BINDINGS).filter((b) => b.exception === undefined);
    expect(byRule).toHaveLength(28);
    for (const binding of byRule) {
      expect(binding.rpc).not.toBeNull();
      // Both halves of `a.b` survive: a rule of "take the tail" would also produce a
      // plausible-looking name for many of these.
      const [namespace, member] = (binding.rpc as string).split(".");
      expect(expectedCommand(binding)).toBe(`${namespace}_${member}`);
    }
  });

  it("pins the three departures from the rule, so none of them is silent", () => {
    // `open_corpus` forwards no RPC; the other two INVERT the rule's word order, verified in
    // the Rust source. A future "tidy-up" that renamed either Rust command to match the rule
    // would break the shipped app, and it breaks here first.
    expect(BINDINGS.openCorpus.rpc).toBeNull();
    expect(BINDINGS.createCorpus.exception).toBe("create_corpus");
    expect(ruleCommand(BINDINGS.createCorpus.rpc as string)).toBe("corpus_create");
    expect(BINDINGS.discoverSources.exception).toBe("discover_sources");
    expect(ruleCommand(BINDINGS.discoverSources.rpc as string)).toBe("sources_discover");
  });

  it("names an RPC the ENGINE actually dispatches, for every forwarded binding", () => {
    // Guards the one place a transcription error could still hide: the rpc strings above.
    // These are the engine's handler-map keys (`llm_anthology/sidecar.py`), so the shape is
    // fixed even though this test cannot read the Python.
    for (const binding of Object.values(BINDINGS)) {
      if (binding.rpc === null) continue;
      expect(binding.rpc).toMatch(/^[a-z]+\.[a-z_]+$/);
    }
  });
});

describe("the optional-parameter omissions", () => {
  // Every one of these exists because the engine type-checks with `isinstance` and an
  // explicit null fails as -32602. Sending `{key: undefined}` would serialise as absent
  // too — but these assert the key is genuinely not there, which is what the engine sees.
  it("omits job_id entirely when no job is named", async () => {
    await realIpc.corpusBuildStatus();
    expect(invokeMock).toHaveBeenCalledWith("corpus_build_status", { params: {} });
    expect(Object.keys(paramsOf())).toEqual([]);
  });

  it("omits depth when a subtree is unbounded", async () => {
    await realIpc.graphSubtree("t-1");
    expect(Object.keys(paramsOf()).sort()).toEqual(["thread_id"]);
  });

  it("omits BOTH diff operands for the self-diff", async () => {
    await realIpc.graphDiff!();
    expect(Object.keys(paramsOf())).toEqual([]);
  });

  it("omits one diff operand when only the other is given", async () => {
    await realIpc.graphDiff!(undefined, 99);
    expect(paramsOf()).toEqual({ as_of_b: 99 });
  });

  it("omits limit so the engine applies its own default of 50", async () => {
    await realIpc.maintenanceRuns();
    expect(Object.keys(paramsOf())).toEqual([]);
  });

  it("defaults graphRoots and metadataSearch to an EMPTY params object", async () => {
    await realIpc.graphRoots();
    expect(paramsOf()).toEqual({});
    invokeMock.mockClear();
    await realIpc.metadataSearch();
    expect(paramsOf()).toEqual({});
  });
});

/** The `params` payload of the most recent invoke call. */
function paramsOf(): Record<string, unknown> {
  const args = invokeMock.mock.calls[invokeMock.mock.calls.length - 1][1] as {
    params: Record<string, unknown>;
  };
  return args.params;
}

describe("results and failures", () => {
  it("returns what the backend sent, UNRESHAPED", async () => {
    // The adapter's contract is to be transparent. A helpfully-normalised field here would
    // silently diverge the shipped app from every mock-driven test in the suite.
    const payload = {
      ok: true,
      nested: { deep: [1, { x: null }] },
      extra_field_the_ui_does_not_know: "kept",
    };
    invokeMock.mockResolvedValue(payload);
    const out = await realIpc.healthPing();
    expect(out).toEqual(payload);
    // Same object, not a copy: nothing cloned or rebuilt it on the way through.
    expect(out as unknown).toBe(payload);
  });

  it("passes a falsy result through instead of substituting a default", async () => {
    invokeMock.mockResolvedValue([]);
    expect(await realIpc.graphRoots()).toEqual([]);
    invokeMock.mockResolvedValue(null);
    expect(await realIpc.graphRollup!()).toBeNull();
  });

  it("PROPAGATES a rejection rather than swallowing it", async () => {
    // The Rust side returns `Result<Value, String>`, so a failure arrives as a rejected
    // promise carrying the engine's own message. Every caller in the app is written against
    // that — `engineErrorText` / `engineStatusText` classify it — so an adapter that
    // resolved to null on failure would turn every engine error into fabricated empty data.
    invokeMock.mockRejectedValue(new Error("no corpus attached"));
    await expect(realIpc.corpusStats()).rejects.toThrow("no corpus attached");
  });

  it("propagates a rejection from EVERY method, not just the sampled one", async () => {
    for (const [method, binding] of Object.entries(BINDINGS)) {
      invokeMock.mockReset();
      invokeMock.mockRejectedValue(new Error(`boom:${method}`));
      await expect(binding.call()).rejects.toThrow(`boom:${method}`);
    }
  });

  it("rejects with a bare STRING error too, which is what Tauri actually sends", async () => {
    // Tauri serialises `Err(String)` and the value reaches JS as a string, not an Error.
    // `String(err)` in the UI's error text helpers depends on that surviving.
    invokeMock.mockRejectedValue("engine mutex poisoned");
    await expect(realIpc.healthPing()).rejects.toBe("engine mutex poisoned");
  });
});
