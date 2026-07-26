/**
 * Claude native-export -> IR. 1:1 with llm_anthology/adapters/claude.py.
 *
 * Claude stores a message TREE (parent_message_uuid), and regenerations create
 * sibling branches. The active-path walk descends toward the subtree holding the
 * GLOBALLY newest message, not merely the newest immediate child — the latter
 * abandons live threads. It walks EVERY root (an orphaned parent link starts a
 * second real root) and sweeps in nodes reachable from no root. On real data the
 * naive walk dropped 42 messages across 18 conversations.
 *
 * design_chats/*.json are a DIFFERENT shape (messages[] with role + a content
 * dict, not chat_messages[] with sender + a content list); parseConversation would
 * yield a silently EMPTY conversation, so isDesignChat/parseDesignChat handle them.
 */
import type { Block, Branch, Citation, Conversation } from "../ir.js";
import { block, conversation, turn } from "../ir.js";

const HIDDEN_CONTENT_TYPES = new Set(["token_budget"]);

type Rec = Record<string, unknown>;

/** Coerce a display field to str. Claude's display_content is sometimes a
 *  structured list, not a string; the real content still lives in data{}. */
function s(x: unknown): string {
  return typeof x === "string" ? x : "";
}

function get(o: unknown, k: string): unknown {
  return o !== null && typeof o === "object" ? (o as Rec)[k] : undefined;
}

/** Python truthiness for the `a or b or ""` idiom over export fields. */
function or(...vals: unknown[]): string {
  for (const v of vals) if (v) return typeof v === "string" ? v : String(v);
  return "";
}

export function parseExport(data: unknown): Conversation[] {
  const convs = Array.isArray(data) ? data : [data];
  return convs.map(parseConversation);
}

export function isDesignChat(data: unknown): boolean {
  return (
    data !== null &&
    typeof data === "object" &&
    Array.isArray((data as Rec)["messages"]) &&
    !("chat_messages" in (data as Rec))
  );
}

export function parseConversation(conv: unknown): Conversation {
  const messages = (get(conv, "chat_messages") as Rec[]) || [];
  const turns = [];
  for (const [msg, branch] of activePath(messages)) {
    const role = get(msg, "sender") === "human" ? "human" : "assistant";
    turns.push(
      turn(role, blocksFromMessage(msg), {
        uuid: or(get(msg, "uuid")),
        timestamp: or(get(msg, "created_at")),
        branch,
      }),
    );
  }
  return conversation(or(get(conv, "uuid")), or(get(conv, "name")) || "(untitled)", "claude", {
    turns,
    created_at: or(get(conv, "created_at")),
    updated_at: or(get(conv, "updated_at")),
    account: accountStr(get(conv, "account")),
    meta: { summary: or(get(conv, "summary")) },
  });
}

function accountStr(acc: unknown): string {
  if (acc !== null && typeof acc === "object") {
    return or(get(acc, "uuid"), get(acc, "email_address"), get(acc, "email"));
  }
  return typeof acc === "string" ? acc : "";
}

function ts(m: unknown): string {
  return or(get(m, "created_at"));
}

/**
 * Newest created_at anywhere inside each message's subtree. Iterative (a long
 * chain would blow the recursion limit) and cycle-safe. Needed because the LOCALLY
 * newest child is often an abandoned regeneration while the live thread continues
 * under an older sibling.
 */
function subtreeMaxTs(messages: Rec[], children: Map<unknown, Rec[]>): Map<unknown, string> {
  const memo = new Map<unknown, string>();
  for (const start of messages) {
    if (memo.has(get(start, "uuid"))) continue;
    const stack: Array<[Rec, boolean]> = [[start, false]];
    const onpath = new Set<unknown>();
    while (stack.length) {
      const [m, expanded] = stack.pop()!;
      const u = get(m, "uuid");
      if (expanded) {
        let best = ts(m);
        for (const k of children.get(u) ?? []) {
          const v = memo.get(get(k, "uuid"));
          if (v !== undefined && v > best) best = v;
        }
        memo.set(u, best);
        onpath.delete(u);
        continue;
      }
      if (memo.has(u) || onpath.has(u)) continue;
      onpath.add(u);
      stack.push([m, true]);
      for (const k of children.get(u) ?? []) {
        if (!memo.has(get(k, "uuid"))) stack.push([k, false]);
      }
    }
  }
  return memo;
}

/** A STABLE sort by ts() — Python's sorted() is stable and the active-path branch
 *  indices depend on siblings keeping their input order among equal timestamps. */
function sortedByTs<T>(arr: T[], key: (x: T) => string): T[] {
  return arr
    .map((v, i) => [v, i] as const)
    .sort((a, b) => {
      const ka = key(a[0]);
      const kb = key(b[0]);
      return ka < kb ? -1 : ka > kb ? 1 : a[1] - b[1];
    })
    .map(([v]) => v);
}

function activePath(messages: Rec[]): Array<[Rec, Branch | null]> {
  const byId = new Map<unknown, Rec>();
  for (const m of messages) byId.set(get(m, "uuid"), m);
  const children = new Map<unknown, Rec[]>();
  for (const m of messages) {
    const p = get(m, "parent_message_uuid");
    if (!children.has(p)) children.set(p, []);
    children.get(p)!.push(m);
  }

  const roots = messages.filter((m) => {
    const p = get(m, "parent_message_uuid");
    return p === null || p === undefined || !byId.has(p);
  });
  const submax = subtreeMaxTs(messages, children);
  const path: Array<[Rec, Branch | null]> = [];
  const seen = new Set<unknown>();

  // Walk EVERY root, chronologically. An orphaned parent link starts a second root
  // whose subtree is still real conversation content.
  for (const root of sortedByTs(roots, ts)) {
    let cur: Rec | null = root;
    while (cur !== null && !seen.has(get(cur, "uuid"))) {
      seen.add(get(cur, "uuid"));
      const sibs = children.get(get(cur, "parent_message_uuid")) ?? [];
      let branch: Branch | null = null;
      if (sibs.length > 1) {
        const ordered = sortedByTs(sibs, ts);
        branch = { index: ordered.indexOf(cur) + 1, total: sibs.length };
      }
      path.push([cur, branch]);
      const kids: Rec[] = children.get(get(cur, "uuid")) ?? [];
      // descend toward the subtree holding the globally newest message, NOT merely
      // the newest immediate child (that abandons live threads)
      cur = kids.length ? argmaxKid(kids, submax) : null;
    }
  }

  // Sweep ONLY nodes reachable from no root at all (e.g. an orphaned cycle). Branch
  // siblings are also "unvisited" but intentionally off the active path.
  const reachable = new Set<unknown>();
  const stack = [...roots];
  while (stack.length) {
    const m = stack.pop()!;
    const u = get(m, "uuid");
    if (reachable.has(u)) continue;
    reachable.add(u);
    stack.push(...(children.get(u) ?? []));
  }
  const orphans = messages.filter(
    (m) => !reachable.has(get(m, "uuid")) && !seen.has(get(m, "uuid")),
  );
  for (const m of sortedByTs(orphans, ts)) {
    // Duplicate UUIDs can both pass the pre-filter before the first is added to seen.
    if (seen.has(get(m, "uuid"))) continue;
    seen.add(get(m, "uuid"));
    path.push([m, null]);
  }
  return path;
}

/** max(kids, key=lambda k: (submax[k] or ts(k), ts(k))) with Python tie-breaking:
 *  on equal keys `max` keeps the FIRST, so iterate forward and use strict >. */
function argmaxKid(kids: Rec[], submax: Map<unknown, string>): Rec {
  let best = kids[0]!;
  let bestKey: [string, string] = [submax.get(get(best, "uuid")) ?? ts(best), ts(best)];
  for (let i = 1; i < kids.length; i++) {
    const k = kids[i]!;
    const key: [string, string] = [submax.get(get(k, "uuid")) ?? ts(k), ts(k)];
    if (key[0] > bestKey[0] || (key[0] === bestKey[0] && key[1] > bestKey[1])) {
      best = k;
      bestKey = key;
    }
  }
  return best;
}

function blocksFromMessage(m: Rec): Block[] {
  const blocks: Block[] = [];
  // uploaded files/attachments render above the message text in claude.ai
  for (const a of (get(m, "attachments") as Rec[]) || []) {
    blocks.push(
      block("attachment", {
        text: or(get(a, "file_name")) || "attachment",
        data: {
          file_name: get(a, "file_name"),
          file_type: get(a, "file_type"),
          file_size: get(a, "file_size"),
          extracted_content: get(a, "extracted_content"),
        },
      }),
    );
  }
  for (const f of (get(m, "files") as Rec[]) || []) {
    blocks.push(
      block("file", {
        text: or(get(f, "file_name")) || "file",
        data: { file_name: get(f, "file_name"), file_uuid: get(f, "file_uuid") },
      }),
    );
  }

  for (const item of (get(m, "content") as Rec[]) || []) {
    const t = get(item, "type");
    if (typeof t === "string" && HIDDEN_CONTENT_TYPES.has(t)) continue;
    if (t === "text") {
      blocks.push(
        block("text", {
          text: s(get(item, "text")),
          citations: (get(item, "citations") as Citation[]) || [],
        }),
      );
    } else if (t === "thinking") {
      blocks.push(
        block("thinking", {
          text: s(get(item, "thinking")),
          data: {
            hidden: Boolean(get(item, "thinking_hidden") || get(item, "hidden")),
            summaries: get(item, "summaries") || [],
          },
        }),
      );
    } else if (t === "tool_use") {
      blocks.push(
        block("tool_use", {
          text: s(get(item, "display_content")),
          data: {
            name: or(get(item, "name")),
            input: get(item, "input"),
            integration_name: get(item, "integration_name"),
            icon_name: get(item, "icon_name"),
            id: get(item, "id"),
          },
        }),
      );
    } else if (t === "tool_result") {
      blocks.push(
        block("tool_result", {
          text: s(get(item, "display_content")),
          data: {
            name: or(get(item, "name")),
            content: get(item, "content"),
            is_error: Boolean(get(item, "is_error")),
            integration_name: get(item, "integration_name"),
            icon_name: get(item, "icon_name"),
            tool_use_id: get(item, "tool_use_id"),
          },
        }),
      );
    } else {
      // never silently drop an unknown block — pass it through for later handling
      blocks.push(block("unknown", { data: { orig_type: t ?? null, x_raw: item } }));
    }
  }
  return blocks;
}

export function parseDesignChat(data: Rec): Conversation {
  const turns = [];
  for (const m of (get(data, "messages") as Rec[]) || []) {
    if (m === null || typeof m !== "object") continue;
    const role = ["user", "human"].includes(s(get(m, "role")).toLowerCase()) ? "human" : "assistant";
    const blocks = designBlocks(get(m, "content"));
    if (blocks.length) {
      turns.push(turn(role, blocks, { uuid: s(get(m, "uuid")), timestamp: s(get(m, "created_at")) }));
    }
  }
  return conversation(s(get(data, "uuid")), s(get(data, "title")) || "(untitled design chat)", "claude", {
    turns,
    created_at: s(get(data, "created_at")),
    updated_at: s(get(data, "updated_at")),
    meta: { kind: "design_chat" },
  });
}

function designBlocks(content: unknown): Block[] {
  if (typeof content === "string") {
    return content.trim() ? [block("text", { text: content })] : [];
  }
  if (content === null || typeof content !== "object") return [];
  const blocks: Block[] = [];
  for (const b of (get(content, "contentBlocks") as Rec[]) || []) {
    if (b === null || typeof b !== "object") continue;
    if (s(get(b, "type")) === "text" && s(get(b, "text")).trim()) {
      blocks.push(block("text", { text: s(get(b, "text")) }));
    } else {
      // never silently drop an unknown block
      blocks.push(block("unknown", { data: { orig_type: s(get(b, "type")), x_raw: b } }));
    }
  }
  if (blocks.length) return blocks;
  const flat = s(get(content, "content"));
  return flat.trim() ? [block("text", { text: flat })] : [];
}
