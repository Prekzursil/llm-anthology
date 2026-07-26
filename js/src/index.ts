/**
 * llm-anthology — programmatic API.
 *
 * Parse a ChatGPT / Claude / Gemini export into a provider-agnostic IR, then render
 * each conversation to browser-faithful HTML and clean, portable Markdown. Fully
 * offline: nothing here opens a network connection.
 *
 * @example
 * ```ts
 * import { adapters, renderConversationHtml, verify } from "llm-anthology";
 * const convs = adapters.claude.parseExport(JSON.parse(raw));
 * const html = renderConversationHtml(convs[0]);
 * console.log(verify(convs[0], html)); // { ok, missing_tokens, coverage }
 * ```
 */
export * as ir from "./ir.js";
export type { Block, Branch, Citation, Conversation, Turn } from "./ir.js";
export * as sanitize from "./sanitize.js";
export { renderConversationHtml } from "./render_html.js";
export { renderConversationMd } from "./render_md.js";
export { verify, proseTokens, htmlVisibleTokens } from "./verify.js";
export type { VerifyResult } from "./verify.js";
export { hiddenCharHits, auditTexts } from "./audit.js";
export { demoConversation } from "./demo.js";

import * as chatgpt from "./adapters/chatgpt.js";
import * as claude from "./adapters/claude.js";
import * as gemini from "./adapters/gemini.js";
export const adapters = { chatgpt, claude, gemini };
export { chatgpt, claude, gemini };
