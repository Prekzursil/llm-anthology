/**
 * `ElkLayoutEngine` — the timeout guard around the one impure boundary of the graph layer.
 *
 * The module's header says it is "intentionally kept thin and is NOT unit-tested"; the lean
 * gate (gate 3, 100% over everything under `src/`) supersedes that choice, so this file
 * tests the thin logic that IS here: the Promise.race timeout, the kill-and-respawn
 * recovery, and the teardown paths.
 *
 * WHY BOTH ELKJS IMPORTS ARE MOCKED. `elkLayout.ts` constructs a real elkjs instance whose
 * `workerFactory` calls the browser `Worker` constructor with the Vite asset URL — under the
 * suite's node default that either builds nothing (node's `Worker` is a worker_threads
 * loader that cannot run the classic browser script) or hangs. The engine under test is the
 * GUARD, not elkjs, so `elkjs/lib/elk-api` is replaced by a mock class whose `layout` and
 * `terminateWorker` each test drives, and the `?url` asset import is answered with a string
 * (it is only ever passed INTO the constructor as data).
 *
 * NOT node-only by accident: no DOM is touched, so this file keeps the suite default
 * (`environment: "node"`) rather than paying happy-dom's startup for nothing.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

interface MockElk {
  layout: ReturnType<typeof vi.fn>;
  terminateWorker: ReturnType<typeof vi.fn>;
}

/** Hoisted because `vi.mock` factories may only close over `vi` and `vi.hoisted` values. */
const mock = vi.hoisted(() => {
  const instances: Array<MockElk & { opts: { workerFactory?: (url: string) => unknown } }> = [];
  return { instances };
});

vi.mock("elkjs/lib/elk-api", () => ({
  default: class {
    layout = vi.fn();
    terminateWorker = vi.fn();
    opts: { workerFactory?: (url: string) => unknown };
    constructor(opts: { workerFactory?: (url: string) => unknown }) {
      this.opts = opts;
      mock.instances.push({
        layout: this.layout,
        terminateWorker: this.terminateWorker,
        opts,
      });
    }
  },
}));

vi.mock("elkjs/lib/elk-worker.min.js?url", () => ({ default: "mocked-elk-worker-url" }));

import { ElkLayoutEngine, LayoutTimeoutError, DEFAULT_LAYOUT_TIMEOUT_MS } from "./elkLayout";

/** The current (most recently spawned) mock ELK instance. */
function current(): MockElk {
  const elk = mock.instances[mock.instances.length - 1];
  if (elk === undefined) throw new Error("no ELK instance spawned yet");
  return elk;
}

/** The captured constructor options of the first spawned instance. */
function firstOpts(): { workerFactory?: (url: string) => unknown } {
  const first = mock.instances[0];
  if (first === undefined) throw new Error("no ELK instance spawned yet");
  return first.opts;
}

/** A promise that never settles, standing in for ELK's known infinite-loop layout run. */
function never(): Promise<never> {
  return new Promise<never>(() => {});
}

beforeEach(() => {
  mock.instances.length = 0;
});

describe("ElkLayoutEngine", () => {
  it("exposes the documented default timeout", () => {
    expect(DEFAULT_LAYOUT_TIMEOUT_MS).toBe(8000);
  });

  it("resolves with the worker's result and does not kill the worker", async () => {
    const engine = new ElkLayoutEngine(60);
    const graph = { id: "root" } as never;
    current().layout.mockResolvedValueOnce(graph);
    await expect(engine.layout(graph)).resolves.toBe(graph);
    // The timer was cleared in `finally`: waiting past the ceiling must fire nothing.
    await new Promise((r) => setTimeout(r, 90));
    expect(current().terminateWorker).not.toHaveBeenCalled();
    expect(mock.instances).toHaveLength(1);
  });

  it("rejects with LayoutTimeoutError on timeout, kills the worker, and respawns", async () => {
    const engine = new ElkLayoutEngine(10);
    current().layout.mockImplementationOnce(never);
    await expect(engine.layout({ id: "root" } as never)).rejects.toThrow(LayoutTimeoutError);
    // The KILLED instance is the first one; `current()` is already its replacement.
    expect(mock.instances[0]?.terminateWorker).toHaveBeenCalledTimes(1);
    // Recovery: a FRESH instance exists and still answers the next layout.
    expect(mock.instances).toHaveLength(2);
    current().layout.mockResolvedValueOnce({ id: "again" } as never);
    await expect(engine.layout({ id: "again" } as never)).resolves.toEqual({ id: "again" });
  });

  it("names the timeout and the ceiling in the error", () => {
    const err = new LayoutTimeoutError(1234);
    expect(err.name).toBe("LayoutTimeoutError");
    expect(err.message).toBe("ELK layout timed out after 1234ms (worker terminated)");
  });

  it("terminate() kills and respawns so the engine keeps working", async () => {
    const engine = new ElkLayoutEngine();
    expect(mock.instances).toHaveLength(1);
    engine.terminate();
    expect(mock.instances[0]?.terminateWorker).toHaveBeenCalledTimes(1);
    expect(mock.instances).toHaveLength(2);
    current().layout.mockResolvedValueOnce({ id: "post-terminate" } as never);
    await expect(engine.layout({ id: "x" } as never)).resolves.toEqual({ id: "post-terminate" });
  });

  it("dispose() kills the worker for good (no respawn)", () => {
    const engine = new ElkLayoutEngine();
    engine.dispose();
    expect(current().terminateWorker).toHaveBeenCalledTimes(1);
    expect(mock.instances).toHaveLength(1);
  });

  it("spawns through the browser Worker factory with the Vite asset URL", () => {
    // The factory the engine passes elkjs is the one boundary this file does not own —
    // it must build a CLASSIC worker from the `?url` asset, never elkjs's Node default.
    const built: Array<{ url: string; opts: { type: string } }> = [];
    class FakeWorker {
      constructor(url: string, opts: { type: string }) {
        built.push({ url, opts });
      }
    }
    vi.stubGlobal("Worker", FakeWorker);
    const engine = new ElkLayoutEngine(30);
    const factory = firstOpts().workerFactory;
    expect(factory).toBeTypeOf("function");
    factory?.("asset://elk-worker.min.js");
    expect(built).toEqual([{ url: "asset://elk-worker.min.js", opts: { type: "classic" } }]);
    engine.dispose();
    vi.unstubAllGlobals();
  });
});
