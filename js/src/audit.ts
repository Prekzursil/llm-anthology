/**
 * Hidden-character forensic audit across EVERY text surface of a conversation.
 * 1:1 with llm_anthology/audit.py.
 *
 * Scanning only block.text under-reported by roughly 5x: an injected payload is most
 * likely to sit in uploaded-document text (attachment.extracted_content), tool input
 * or output, a citation title, or the conversation TITLE — none of which were checked.
 */
import type { Conversation } from "./ir.js";
import { scanInvisibles } from "./sanitize.js";

const STR_KEYS = ["extracted_content", "file_name", "name", "integration_name"] as const;
const BLOB_KEYS = ["input", "content"] as const;

/** Every string a hidden codepoint could hide in. */
export function* auditTexts(conv: Conversation): Generator<string> {
  yield conv.title || "";
  yield conv.account || "";
  for (const t of conv.turns) {
    for (const b of t.blocks) {
      yield b.text || "";
      const d = b.data;
      for (const k of STR_KEYS) {
        if (typeof d[k] === "string") yield d[k] as string;
      }
      for (const k of BLOB_KEYS) {
        const v = d[k];
        if (typeof v === "string") {
          yield v;
        } else if (v !== undefined && v !== null) {
          try {
            const s = JSON.stringify(v);
            if (s !== undefined) yield s;
          } catch {
            /* unserialisable payload: skip, matching the Python TypeError path */
          }
        }
      }
      for (const c of b.citations) {
        if (c !== null && typeof c === "object") {
          for (const k of ["title", "url"] as const) {
            if (typeof c[k] === "string") yield c[k] as string;
          }
        }
      }
    }
  }
}

/** Every flagged invisible codepoint found anywhere in the conversation. */
export function hiddenCharHits(conv: Conversation): string[] {
  const hits: string[] = [];
  for (const s of auditTexts(conv)) {
    for (const [, cp] of scanInvisibles(s)) hits.push(cp);
  }
  return hits;
}
