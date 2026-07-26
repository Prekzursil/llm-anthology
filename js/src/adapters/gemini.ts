/**
 * Google Takeout "Gemini Apps" activity -> IR. 1:1 with llm_anthology/adapters/gemini.py.
 *
 * Takeout is a FLAT activity log: each record is one EXCHANGE (prompt + response),
 * with NO conversation id. Grouping comes from outside (the live web-app harvest or
 * a heuristic) and is passed in as `groups`; the adapter only turns records into IR
 * turns. Only "Prompted" is a real prompt/response exchange; the rest are feature
 * EVENTS, rendered as a single event turn rather than a forged model reply.
 */
import type { Block, Conversation } from "../ir.js";
import { block, conversation, turn } from "../ir.js";

type Rec = Record<string, unknown>;

const PROMPT_VERB = "Prompted";

function s(x: unknown): string {
  return typeof x === "string" ? x : "";
}
function get(o: unknown, k: string): unknown {
  return o !== null && typeof o === "object" ? (o as Rec)[k] : undefined;
}

export interface Group {
  id?: string;
  title?: string;
  turn_idxs?: number[];
}

/** [Conversation] — one per group, or a single conversation if ungrouped. */
export function parseAll(records: Rec[], groups?: Group[] | null): Conversation[] {
  if (!groups || !groups.length) {
    return [
      parseConversation(records, [...records.keys()], "Gemini activity (ungrouped)", "all"),
    ];
  }
  return groups.map((g) =>
    parseConversation(records, g.turn_idxs ?? [], g.title || "(untitled)", g.id || ""),
  );
}

export function parseConversation(
  records: Rec[],
  turnIdxs: number[],
  title = "",
  convId = "",
  account = "",
): Conversation {
  const turns = [];
  const gems: unknown[] = [];
  let firstTs = "";
  let lastTs = "";
  for (const i of turnIdxs) {
    if (i < 0 || i >= records.length) continue;
    const r = records[i] || {};
    const gem = get(r, "gem");
    if (gem && !gems.includes(gem)) gems.push(gem);
    const ts = s(get(r, "timestamp_iso")) || s(get(r, "timestamp"));
    firstTs = firstTs || ts;
    lastTs = ts || lastTs;
    turns.push(...turnsFromRecord(r));
  }
  return conversation(convId, title || "(untitled)", "gemini", {
    turns,
    created_at: firstTs,
    updated_at: lastTs,
    account,
    meta: gems.length ? { gems } : {},
  });
}

function attachmentBlocks(r: Rec): Block[] {
  const blocks: Block[] = [];
  for (const a of (get(r, "attachments") as unknown[]) || []) {
    if (a !== null && typeof a === "object") {
      blocks.push(
        block("attachment", {
          text: s(get(a, "name")) || "attachment",
          data: {
            file_name: s(get(a, "name")),
            path: s(get(a, "on_disk")),
            resolved: Boolean(get(a, "resolved")),
          },
        }),
      );
    } else {
      blocks.push(block("attachment", { text: s(a) || "attachment", data: { file_name: s(a) } }));
    }
  }
  for (const m of (get(r, "media") as unknown[]) || []) {
    const path = m !== null && typeof m === "object" ? s(get(m, "on_disk") || get(m, "name")) : s(m);
    blocks.push(block("media", { text: path, data: { path } }));
  }
  return blocks;
}

function turnsFromRecord(r: Rec) {
  const verb = s(get(r, "verb")) || PROMPT_VERB;
  const ts = s(get(r, "timestamp_iso")) || s(get(r, "timestamp"));

  if (verb !== PROMPT_VERB) {
    // a feature event (Used / Created Gemini Canvas / Gave feedback / Selected).
    // Render it as an explicit event, never as a fabricated model reply.
    const label = s(get(r, "title")) || s(get(r, "detail")) || verb;
    return [
      turn(
        "assistant",
        [
          block("event", { text: label, data: { name: verb, detail: s(get(r, "detail")) } }),
          ...attachmentBlocks(r),
        ],
        { timestamp: ts },
      ),
    ];
  }

  const prompt = s(get(r, "prompt")) || s(get(r, "title")) || s(get(r, "detail"));
  const human = turn("human", [...attachmentBlocks(r), block("text", { text: prompt })], {
    timestamp: ts,
  });
  const model = turn("assistant", [block("text", { text: s(get(r, "response_md")) })], {
    timestamp: ts,
  });
  return [human, model];
}
