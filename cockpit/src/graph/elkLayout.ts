/**
 * ELK layout run INSIDE a Web Worker, with a hard timeout guard.
 *
 * This is the one impure boundary of the graph layer (it spins up a worker and talks
 * to elkjs at runtime), so it is intentionally kept thin and is NOT unit-tested — the
 * data mapping it wraps lives in the pure `./layout.ts`, which is.
 *
 * SOTA render decisions honoured here:
 *   * elkjs is pinned to an EXACT version (see package.json) and runs off the main
 *     thread via `elk-worker.min.js` loaded as a Vite `?url` asset.
 *   * `elk.layout` is wrapped in `Promise.race` against a timeout; on timeout the
 *     worker is TERMINATED (elkjs's `terminateWorker`) to kill the known ELK
 *     infinite-loop, and a fresh ELK instance is spun up so the next call still works.
 */

import ElkConstructor, { type ELK, type ElkNode } from "elkjs/lib/elk-api";
import elkWorkerUrl from "elkjs/lib/elk-worker.min.js?url";

/** Default ceiling for a single layout run before the worker is killed. */
export const DEFAULT_LAYOUT_TIMEOUT_MS = 8000;

export class LayoutTimeoutError extends Error {
  constructor(ms: number) {
    super(`ELK layout timed out after ${ms}ms (worker terminated)`);
    this.name = "LayoutTimeoutError";
  }
}

export class ElkLayoutEngine {
  private elk: ELK;

  constructor(private readonly timeoutMs: number = DEFAULT_LAYOUT_TIMEOUT_MS) {
    this.elk = this.spawn();
  }

  private spawn(): ELK {
    // Pass BOTH workerUrl and an explicit factory: the factory uses the native
    // browser `Worker` (never elkjs's Node-only default), and `workerUrl` is the
    // Vite-emitted asset URL for the pinned worker script.
    return new ElkConstructor({
      workerUrl: elkWorkerUrl,
      workerFactory: (url) => new Worker(url as string, { type: "classic" }),
    });
  }

  /**
   * Lay out `graph`, rejecting (and terminating the worker) if it exceeds the
   * configured timeout. Resolves with the ELK result graph (nodes carry x/y/size,
   * edges carry routed sections) — feed it to {@link extractLayout}.
   */
  async layout(graph: ElkNode): Promise<ElkNode> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const guard = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => {
        this.terminate();
        reject(new LayoutTimeoutError(this.timeoutMs));
      }, this.timeoutMs);
    });
    try {
      const result = await Promise.race([this.elk.layout(graph), guard]);
      return result as ElkNode;
    } finally {
      if (timer !== undefined) clearTimeout(timer);
    }
  }

  /** Kill the current worker and replace the ELK instance with a fresh one. */
  terminate(): void {
    this.elk.terminateWorker();
    this.elk = this.spawn();
  }

  /** Kill the worker for good (call on teardown). */
  dispose(): void {
    this.elk.terminateWorker();
  }
}
