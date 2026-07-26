/**
 * ChatGPT native export (conversations.json) -> IR. 1:1 with llm_anthology/adapters/chatgpt.py.
 *
 * UNLIKE the other two providers, ChatGPT stores a MESSAGE TREE, not a list. The
 * rendered thread is current_node -> parent -> ... -> root, reversed. Every other
 * child of a branching node is an abandoned regeneration and must not be rendered.
 *
 * current_node is authoritative when usable, but real exports carry conversations
 * whose current_node is null/absent/stale; the naive walk then returns [] and the
 * whole conversation renders blank, so it falls back to the newest leaf.
 */
import type { Block, Conversation, Turn } from "../ir.js";
import { block, conversation, turn } from "../ir.js";

type Rec = Record<string, unknown>;

const HIDDEN_ROLES = new Set(["system"]);
const THINKING_TYPES = new Set(["thoughts", "reasoning_recap"]);

function s(x: unknown): string {
  return typeof x === "string" ? x : "";
}
function get(o: unknown, k: string): unknown {
  return o !== null && typeof o === "object" ? (o as Rec)[k] : undefined;
}

export function parseExport(data: unknown): Conversation[] {
  const convs = Array.isArray(data) ? data : [data];
  return convs.filter((c) => c !== null && typeof c === "object").map(parseConversation);
}

export function parseConversation(conv: Rec): Conversation {
  const mapping = (get(conv, "mapping") as Rec) || {};
  const turns: Turn[] = [];
  for (const msg of activePath(mapping, get(conv, "current_node"))) {
    const role = roleOf(msg);
    if (role === null) continue;
    const blocks = blocksFromMessage(msg);
    if (!blocks.length) continue;
    const last: Turn | undefined = turns[turns.length - 1];
    // consecutive assistant nodes are ONE visual turn until end_turn
    if (last && last.role === role && role === "assistant" && !last.branch) {
      last.blocks.push(...blocks);
    } else {
      turns.push(turn(role, blocks, { uuid: s(get(msg, "id")), timestamp: tsOf(msg) }));
    }
  }
  return conversation(
    s(get(conv, "id")) || s(get(conv, "conversation_id")),
    s(get(conv, "title")) || "(untitled)",
    "chatgpt",
    {
      turns,
      created_at: tsTop(get(conv, "create_time")),
      updated_at: tsTop(get(conv, "update_time")),
      meta: {},
    },
  );
}

function tsOf(msg: unknown): string {
  return tsTop(get(msg, "create_time"));
}

function tsTop(v: unknown): string {
  if (typeof v === "number" && Number.isFinite(v)) {
    try {
      // Python datetime.fromtimestamp(v, tz=utc).isoformat() -> "...+00:00"
      return new Date(v * 1000).toISOString().replace(/\.\d{3}Z$/, "+00:00");
    } catch {
      return "";
    }
  }
  return s(v);
}

function walkUp(mapping: Rec, startId: unknown): Rec[] {
  const chain: Rec[] = [];
  const seen = new Set<string>();
  let nid: unknown = startId;
  while (typeof nid === "string" && nid in mapping && !seen.has(nid)) {
    seen.add(nid);
    const node = (mapping[nid] as Rec) || {};
    const msg = get(node, "message");
    if (msg !== null && typeof msg === "object") chain.push(msg as Rec);
    nid = get(node, "parent");
  }
  chain.reverse();
  return chain;
}

function activePath(mapping: Rec, currentNode: unknown): Rec[] {
  let chain = walkUp(mapping, currentNode);
  if (!chain.length) chain = walkUp(mapping, fallbackTip(mapping));
  return chain;
}

/** The most likely active tip when current_node is unusable: prefer a leaf (no
 *  children) and, among candidates, the newest by create_time. */
function fallbackTip(mapping: Rec): unknown {
  let best: [boolean, number, unknown] | null = null;
  for (const [nid, node] of Object.entries(mapping)) {
    if (node === null || typeof node !== "object") continue;
    const msg = get(node, "message");
    if (msg === null || typeof msg !== "object") continue;
    const rawTs = get(msg, "create_time");
    const isLeaf = ((get(node, "children") as unknown[]) || []).length === 0;
    const cand: [boolean, number, unknown] = [
      isLeaf,
      typeof rawTs === "number" ? rawTs : -1,
      nid,
    ];
    if (best === null || cand[0] > best[0] || (cand[0] === best[0] && cand[1] > best[1])) {
      best = cand;
    }
  }
  return best ? best[2] : undefined;
}

/** human/assistant, or null when the message must not be rendered. */
function roleOf(msg: Rec): string | null {
  const role = s(get(get(msg, "author"), "role")).toLowerCase();
  if (HIDDEN_ROLES.has(role)) return null;
  if (get(get(msg, "metadata"), "is_visually_hidden_from_conversation")) return null;
  if (role === "user") return "human";
  return "assistant"; // assistant + tool both render on the model side
}

function blocksFromMessage(msg: Rec): Block[] {
  const content = (get(msg, "content") as Rec) || {};
  const ctype = s(get(content, "content_type"));
  const blocks: Block[] = [];

  if (THINKING_TYPES.has(ctype)) {
    for (const th of (get(content, "thoughts") as Rec[]) || []) {
      if (th !== null && typeof th === "object") {
        const body = s(get(th, "content")) || s(get(th, "summary"));
        if (body) blocks.push(block("thinking", { text: body, data: { summary: s(get(th, "summary")) } }));
      }
    }
    const body = s(get(content, "content"));
    if (body) blocks.push(block("thinking", { text: body }));
    return blocks;
  }

  if (ctype === "code") {
    const text = s(get(content, "text"));
    return text ? [block("code", { text, data: { language: s(get(content, "language")) } })] : [];
  }

  if (ctype === "execution_output") {
    const text = s(get(content, "text"));
    return text
      ? [block("tool_result", { text: "", data: { name: "execution_output", content: text } })]
      : [];
  }

  if (ctype === "text" || ctype === "multimodal_text" || ctype === "") {
    for (const part of (get(content, "parts") as unknown[]) || []) {
      if (typeof part === "string") {
        if (part.trim()) blocks.push(block("text", { text: part }));
      } else if (part !== null && typeof part === "object") {
        if (s(get(part, "content_type")) === "image_asset_pointer") {
          const ptr = s(get(part, "asset_pointer"));
          blocks.push(
            block("media", {
              text: ptr,
              data: { path: ptr.split("/").pop() ?? "", pointer: ptr },
            }),
          );
        } else {
          blocks.push(block("unknown", { data: { orig_type: s(get(part, "content_type")), x_raw: part } }));
        }
      }
    }
    return blocks;
  }

  // never silently drop an unrecognised content type
  return [block("unknown", { data: { orig_type: ctype, x_raw: content } })];
}
