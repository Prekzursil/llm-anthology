/**
 * The data-access entry point. Everything in the app imports `ipc` from here and
 * never touches the mock or real adapter directly.
 *
 *   ┌──────────────────────────────────────────────────────────────────────┐
 *   │  THE ONE-LINE SWAP.  Flip `USE_REAL_IPC` to `true` at the integrate    │
 *   │  stage to route the cockpit at the real Tauri sidecar instead of the   │
 *   │  in-memory mock forest. Nothing else changes.                          │
 *   └──────────────────────────────────────────────────────────────────────┘
 *
 * It is typed `boolean` (not the literal `false`) so `tsc` keeps type-checking BOTH
 * adapters instead of dead-code-eliminating the real path.
 */

import { mockIpc } from "./mock";
import { realIpc } from "./real";
import type { IpcClient } from "./types";

const USE_REAL_IPC: boolean = true;

export const ipc: IpcClient = USE_REAL_IPC ? realIpc : mockIpc;

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
