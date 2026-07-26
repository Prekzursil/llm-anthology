/**
 * The data-access entry point. Everything in the app imports `ipc` from here and
 * never touches the mock or real adapter directly.
 *
 * RUNTIME ADAPTER SELECTION (replaces the old compile-time `USE_REAL_IPC` flag).
 * The real adapter calls `window.__TAURI_INTERNALS__.invoke`, which only exists inside
 * the Tauri webview. With a hard-coded `USE_REAL_IPC = true`, opening the cockpit in a
 * plain browser threw `TypeError: Cannot read properties of undefined (reading
 * 'invoke')` on first paint and every pane rendered dead. That is not a hypothetical:
 * it is what the very first visual capture of this UI recorded, and it is a large part
 * of why the UI shipped through four phases without ever being looked at.
 *
 * So the choice is made from the ENVIRONMENT: inside Tauri -> the real engine; anywhere
 * else (vite dev, `vite preview`, a screenshot harness, a design review) -> the
 * in-memory mock forest. The cockpit is now previewable, auditable and designable
 * without a Rust build.
 *
 * WHY LAZY, NOT EAGER. Resolving at module-evaluation time would be simpler, but it
 * makes correctness depend on Tauri having injected `__TAURI_INTERNALS__` before this
 * module is evaluated. If that ordering ever slipped, the SHIPPED app would silently
 * bind to the MOCK and present fabricated conversations as if they were the user's real
 * corpus — a catastrophic, near-invisible failure. Deferring to first property access
 * removes the ordering assumption entirely; the cost is one `Proxy` indirection on a
 * surface whose every call already crosses a process boundary.
 */

import { mockIpc } from "./mock";
import { realIpc } from "./real";
import type { IpcClient } from "./types";

/** Shape of the global Tauri v2 injects into the webview. */
type MaybeTauriGlobal = { __TAURI_INTERNALS__?: { invoke?: unknown } };

/**
 * True only inside the Tauri webview. Probes for the `invoke` function itself rather
 * than merely the namespace object, so a partially-initialised global cannot be
 * mistaken for a live engine.
 */
export function isTauriRuntime(): boolean {
  const g = globalThis as MaybeTauriGlobal;
  return typeof g.__TAURI_INTERNALS__?.invoke === "function";
}

/** Pick the adapter for the CURRENT environment. Exported for tests. */
export function selectIpc(): IpcClient {
  return isTauriRuntime() ? realIpc : mockIpc;
}

let resolved: IpcClient | null = null;

/** Resolve once, then reuse — the environment cannot change mid-session. */
export function currentIpc(): IpcClient {
  resolved ??= selectIpc();
  return resolved;
}

/** Test-only: drop the memoised adapter so a test can re-probe the environment. */
export function __resetIpcForTests(): void {
  resolved = null;
}

/**
 * The app-wide client. Property access is forwarded to whichever adapter matches the
 * runtime, resolved on FIRST use (see the lazy rationale above).
 */
export const ipc: IpcClient = new Proxy({} as IpcClient, {
  get(_target, prop, receiver) {
    return Reflect.get(currentIpc() as object, prop, receiver);
  },
  has(_target, prop) {
    return Reflect.has(currentIpc() as object, prop);
  },
});

export type {
  Conversation,
  ConversationAvailable,
  ConversationBlock,
  ConversationStub,
  ConversationTurn,
  CorpusStats,
  HealthInfo,
  IpcClient,
  RootOrder,
  RootsParams,
  SearchHit,
  SearchParams,
  SearchResult,
  SpawnEdge,
  Subtree,
  ThreadMeta,
  ThreadNode,
} from "./types";
