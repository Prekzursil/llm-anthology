/**
 * Provider-agnostic conversation Intermediate Representation.
 *
 * Every adapter (Claude / ChatGPT / Gemini) parses its native export into this one
 * shape; the HTML and Markdown renderers consume only this. Versioned so a future
 * schema change is explicit. Unknown provider payloads survive via block.data.x_raw.
 *
 * 1:1 with llm_anthology/ir.py. The Python dataclasses default every optional field, so the
 * factory helpers here do the same rather than leaving `undefined` to leak into a
 * renderer that expects a string.
 */
export const IR_VERSION = 1;

export type BlockType =
  | "text"
  | "thinking"
  | "tool_use"
  | "tool_result"
  | "attachment"
  | "file"
  | "media"
  | "code"
  | "event"
  | "unknown";

export interface Citation {
  url?: string;
  title?: string;
  [k: string]: unknown;
}

/** One typed content block within a turn. */
export interface Block {
  /** text|thinking|tool_use|tool_result|attachment|file|media|code|event|unknown */
  type: string;
  /** display text (body for text/thinking; label otherwise) */
  text: string;
  /** type-specific payload (tool name/input/output, file_name, path, ...) */
  data: Record<string, unknown>;
  citations: Citation[];
}

export interface Branch {
  index: number;
  total: number;
}

/** One message turn on the active conversation path. */
export interface Turn {
  /** human | assistant */
  role: string;
  blocks: Block[];
  uuid: string;
  timestamp: string;
  /** {index, total} when the parent had siblings */
  branch: Branch | null;
}

export interface Conversation {
  id: string;
  title: string;
  /** claude | chatgpt | gemini */
  provider: string;
  /** the active (latest) chain */
  turns: Turn[];
  created_at: string;
  updated_at: string;
  account: string;
  /** provider extras, audit (hidden-char hits), etc. */
  meta: Record<string, unknown>;
  ir_version: number;
}

export function block(
  type: string,
  opts: { text?: string; data?: Record<string, unknown>; citations?: Citation[] } = {},
): Block {
  return {
    type,
    text: opts.text ?? "",
    data: opts.data ?? {},
    citations: opts.citations ?? [],
  };
}

export function turn(
  role: string,
  blocks: Block[] = [],
  opts: { uuid?: string; timestamp?: string; branch?: Branch | null } = {},
): Turn {
  return {
    role,
    blocks,
    uuid: opts.uuid ?? "",
    timestamp: opts.timestamp ?? "",
    branch: opts.branch ?? null,
  };
}

export function conversation(
  id: string,
  title: string,
  provider: string,
  opts: {
    turns?: Turn[];
    created_at?: string;
    updated_at?: string;
    account?: string;
    meta?: Record<string, unknown>;
  } = {},
): Conversation {
  return {
    id,
    title,
    provider,
    turns: opts.turns ?? [],
    created_at: opts.created_at ?? "",
    updated_at: opts.updated_at ?? "",
    account: opts.account ?? "",
    meta: opts.meta ?? {},
    ir_version: IR_VERSION,
  };
}
