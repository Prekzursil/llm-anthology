/**
 * What the transcript reader shows — the content decisions, split out from the DOM.
 *
 * Same reason as `emptyStateLabel` and `searchPresent`: vitest runs `environment: "node"`
 * here, so a rule inside a method that also calls `document.createElement` cannot be tested.
 *
 * The rule this module exists to enforce: NO BLOCK RENDERS AS NOTHING. `conversation.get`
 * was built and never wired, so nothing had ever decided what a `tool_use` or a `thinking`
 * block looks like. The obvious first cut — render `type === "text"` — would hide most of a
 * coding session while still looking like a finished transcript, which is the worst
 * available outcome for a reader.
 */

import type { ConversationBlock, ConversationStub } from "../ipc/types";

/**
 * Every block type the engine can emit, from `llm_anthology/ir.py` and the adapters.
 *
 * Kept as a list so a test can walk it: adding a type on the Python side without deciding
 * how it looks here fails that test instead of silently vanishing from the reader.
 */
export const BLOCK_KINDS = [
  "text",
  "thinking",
  "tool_use",
  "tool_result",
  "code",
  "attachment",
  "file",
  "media",
  "event",
  "unknown",
] as const;

/** Human labels for the block types that are not plain prose. "" means render bare. */
const BLOCK_LABELS: Record<string, string> = {
  text: "",
  thinking: "thinking",
  tool_use: "tool call",
  tool_result: "tool result",
  code: "code",
  attachment: "attachment",
  file: "file",
  media: "media",
  event: "event",
  unknown: "unrecognised",
};

/** One block, ready to render. */
export interface BlockDisplay {
  /** The block's own type, so the DOM can class it and CSS can style it. */
  kind: string;
  /** Badge text; "" for plain prose. */
  label: string;
  /** The text to show. Never empty. */
  body: string;
}

/**
 * Decide how one block appears.
 *
 * `Block.text` is the display text for every type — the body for text/thinking, a LABEL for
 * the others — so it is the first choice. When an adapter leaves it empty and puts the
 * content in `data` (which happens for tool blocks), the payload is shown rather than an
 * empty bubble that reads as a broken message. A type nobody has decided on is shown with
 * its raw type as the badge: visible and honestly unrecognised, never dropped.
 */
export function blockDisplay(block: ConversationBlock): BlockDisplay {
  const label = BLOCK_LABELS[block.type] ?? block.type;
  const text = (block.text ?? "").trim();
  if (text !== "") return { kind: block.type, label, body: text };
  const payload = block.data === undefined ? "" : JSON.stringify(block.data);
  return {
    kind: block.type,
    label,
    body: payload && payload !== "{}" ? payload : "(empty)",
  };
}

/** Who is speaking. An unexpected role is shown as itself rather than guessed at. */
export function roleLabel(role: string): string {
  if (role === "human") return "You";
  if (role === "assistant") return "Assistant";
  return role === "" ? "unknown" : role;
}

/**
 * A turn's time, formatted for reading rather than sorting: `YYYY-MM-DD HH:MM` in UTC.
 *
 * UTC because deriving a local calendar time here would make the output depend on the
 * machine, and a transcript is a record of when something happened, not of where it is read.
 * An unparseable or absent timestamp yields "" — an undated turn is real, and inventing a
 * time for it would be worse than an empty cell.
 */
export function turnWhen(timestamp: string): string {
  if (!timestamp) return "";
  const ms = Date.parse(timestamp);
  if (Number.isNaN(ms)) return "";
  return new Date(ms).toISOString().replace("T", " ").slice(0, 16);
}

/** The reader's heading. Never empty — an untitled conversation still needs identifying. */
export function readerTitle(conv: { id: string; title?: string }): string {
  const title = (conv.title ?? "").trim();
  return title === "" ? `untitled · ${conv.id}` : title;
}

/**
 * The line under the heading.
 *
 * It reports `parse_errors`, which the engine has always counted and nothing has ever
 * shown. A transcript that silently dropped unparseable lines is a confident lie about what
 * the session contained; saying "3 lines could not be parsed" costs nothing and makes the
 * gap visible where it matters.
 */
export function readerSubtitle(
  info: { provider: string; turns: number; parseErrors: number },
): string {
  const turns = `${info.turns} turn${info.turns === 1 ? "" : "s"}`;
  if (info.parseErrors <= 0) return `${info.provider} · ${turns}`;
  const lines = `${info.parseErrors} line${info.parseErrors === 1 ? "" : "s"}`;
  return `${info.provider} · ${turns} · ${lines} could not be parsed`;
}

/**
 * Why a conversation cannot be shown, in words a user can act on.
 *
 * The engine's reasons are precise but terse; "no reader for provider 'chatgpt'" reads as a
 * bug rather than as a known limit of that source format, which it is — an export keeps many
 * conversations in one file, so a stored path does not identify one.
 */
export function stubExplanation(stub: ConversationStub): string {
  const reason = (stub.reason ?? "").trim();
  const noReader = /^no reader for provider '?"?([^'"]*)'?"?$/.exec(reason);
  if (noReader !== null) {
    return `This conversation came from a ${noReader[1]} export, and an export holds many `
      + "conversations in one file — so its transcript cannot be re-read from the index "
      + "alone. Its metadata and search text are still indexed.";
  }
  if (reason === "rollout unavailable") {
    return "The session file this conversation was indexed from is no longer where the "
      + "index recorded it. It may have been moved, deleted, or written to a drive that "
      + "is not mounted.";
  }
  if (reason === "") return "This conversation has no readable transcript.";
  return `This conversation has no readable transcript: ${reason}`;
}
