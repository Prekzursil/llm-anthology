/**
 * The adapters and renderers take UNTYPED input: an export is whatever JSON a provider
 * shipped, and the npm API accepts objects a JavaScript caller built. These cases feed each
 * one the partial and malformed shapes its fallbacks exist for (a missing key, a
 * non-object where an object belongs, a non-string where a string belongs) and pin what
 * comes out, the same `x or default` handling the Python rail has.
 */
import { describe, expect, it } from "vitest";

import * as chatgpt from "../src/adapters/chatgpt.js";
import * as claude from "../src/adapters/claude.js";
import * as gemini from "../src/adapters/gemini.js";
import { auditTexts } from "../src/audit.js";
import { block, conversation, turn } from "../src/ir.js";
import type { Block, Conversation } from "../src/ir.js";
import { hardenLinks, renderConversationHtml } from "../src/render_html.js";
import { renderConversationMd } from "../src/render_md.js";
import { escapeHtml, isPictographic } from "../src/sanitize.js";
import { htmlVisibleTokens } from "../src/verify.js";

type Rec = Record<string, unknown>;

const one = (blocks: Block[], title = "t"): Conversation =>
  conversation("c", title, "claude", { turns: [turn("assistant", blocks)] });

/** A block whose `text` is missing, as a JavaScript caller can hand the renderers. */
const textless = (type: string, data: Rec = {}): Block =>
  ({ ...block(type, { data }), text: undefined }) as unknown as Block;

const both = (conv: Conversation): [string, string] => [
  renderConversationHtml(conv),
  renderConversationMd(conv),
];

describe("ir, audit, sanitize and verify defaults", () => {
  it("builds a conversation with no options", () => {
    const c = conversation("c", "t", "claude");
    expect(c.turns).toEqual([]);
    expect(c.created_at).toBe("");
    expect(c.updated_at).toBe("");
  });

  it("audits an empty title as an empty string", () => {
    expect([...auditTexts(conversation("c", "", "claude"))][0]).toBe("");
  });

  it("checks every pictographic range, the last one included", () => {
    expect(isPictographic(0x2049)).toBe(true);
    expect(isPictographic(0x41)).toBe(false);
    // above every pictographic range: the only kind of code point that reaches the
    // regional-indicator upper bound (that range sits inside the first one)
    expect(isPictographic(0x20000)).toBe(false);
  });

  it("escapes a non-string as the empty string", () => {
    expect(escapeHtml(undefined)).toBe("");
    expect(escapeHtml(42)).toBe("");
  });

  it("tokenizes a missing page as no tokens", () => {
    expect(htmlVisibleTokens(null as unknown as string)).toEqual([]);
  });
});

describe("renderers on partial blocks", () => {
  it("renders a text block with no text as empty, never as 'undefined'", () => {
    const [html, md] = both(one([textless("text")]));
    expect(html).not.toContain("undefined");
    expect(md).not.toContain("undefined");
  });

  it("renders a code block with no language and no text", () => {
    const [html, md] = both(one([textless("code")]));
    expect(html).toContain("<pre><code></code></pre>");
    expect(md).toContain("```\n\n```");
  });

  it("labels a hidden thinking block", () => {
    const html = renderConversationHtml(
      one([block("thinking", { text: "x", data: { hidden: true } })]),
    );
    expect(html).toContain("Thought process (hidden in claude.ai)");
  });

  it("names an unnamed tool result 'tool'", () => {
    const [html, md] = both(one([block("tool_result", { data: { content: "out" } })]));
    expect(html).toContain("↩️ tool");
    expect(md).toContain("(`tool`)");
  });

  it("renders a tool input JSON cannot serialize as an empty body", () => {
    const [html, md] = both(one([block("tool_use", { data: { name: "t", input: () => 1 } })]));
    expect(html).toContain("<pre></pre>");
    expect(md).toContain("```json\n\n```");
  });

  it("labels an event by its name, else as 'event'", () => {
    const [html, md] = both(one([block("event", { data: { name: "Used" } }), block("event")]));
    expect(html).toContain("✨ Used");
    expect(html).toContain("✨ event");
    expect(md).toContain("> ✨ **Used**");
    expect(md).toContain("> ✨ **event**");
  });

  it("renders citations with no url, no title, or neither", () => {
    const cites = [{ title: "T" }, { url: "https://e.example/x" }, {}];
    const [html, md] = both(one([block("text", { text: "a", citations: cites })]));
    expect(html).toContain("<span>T</span>");
    expect(html).toContain(">https://e.example/x</a>");
    expect(md).toContain("T ()");
    expect(md).toContain("[https://e.example/x](https://e.example/x)");
    expect(md).toContain("source ()");
  });

  it("titles an untitled conversation '(untitled)'", () => {
    expect(renderConversationHtml(conversation("c", "", "claude"))).toContain("(untitled)");
  });

  it("keeps a single-quoted link title", () => {
    expect(hardenLinks(`<a href="https://e.example/" title='T'>x</a>`)).toContain(' title="T"');
  });
});

describe("claude adapter on partial exports", () => {
  it("reads a non-object conversation as an empty untitled one", () => {
    const c = claude.parseConversation("not a conversation");
    expect(c.title).toBe("(untitled)");
    expect(c.turns).toEqual([]);
  });

  it("stringifies a non-string id", () => {
    expect(claude.parseConversation({ uuid: 42, name: "N", chat_messages: [] }).id).toBe("42");
  });

  it("defaults every missing message field", () => {
    const c = claude.parseConversation({
      uuid: "c",
      name: "N",
      chat_messages: [
        { uuid: "m1", sender: "human" },
        {
          uuid: "m2",
          parent_message_uuid: "m1",
          sender: "assistant",
          attachments: [{}],
          files: [{}],
          content: [{ type: "text", text: "hi" }, { type: "thinking", thinking: "hm" }, {}],
        },
      ],
    });
    expect(c.turns[0]!.blocks).toEqual([]);
    const blocks = c.turns[1]!.blocks;
    expect(blocks.map((b) => b.type)).toEqual([
      "attachment",
      "file",
      "text",
      "thinking",
      "unknown",
    ]);
    expect(blocks[0]!.text).toBe("attachment");
    expect(blocks[1]!.text).toBe("file");
    expect(blocks[2]!.citations).toEqual([]);
    expect(blocks[3]!.data["summaries"]).toEqual([]);
    expect(blocks[4]!.data["orig_type"]).toBeNull();
  });

  const msg = (uuid: string, parent: string | null, at: string, text: string) => ({
    uuid,
    parent_message_uuid: parent,
    sender: parent ? "assistant" : "human",
    created_at: at,
    content: [{ type: "text", text }],
  });
  const texts = (c: Conversation) => c.turns.map((t) => t.blocks[0]!.text);

  it("follows a child listed before its parent", () => {
    const c = claude.parseConversation({
      uuid: "c",
      chat_messages: [msg("m2", "m1", "2", "b"), msg("m1", null, "1", "a")],
    });
    expect(texts(c)).toEqual(["a", "b"]);
  });

  it("takes the newest sibling even when it is listed first", () => {
    const c = claude.parseConversation({
      uuid: "c",
      chat_messages: [msg("r", null, "1", "r"), msg("x", "r", "3", "x"), msg("y", "r", "2", "y")],
    });
    expect(texts(c)).toEqual(["r", "x"]);
    expect(c.turns[1]!.branch).toEqual({ index: 2, total: 2 });
  });

  it("keeps the first of two siblings with the same timestamp", () => {
    const c = claude.parseConversation({
      uuid: "c",
      chat_messages: [msg("r", null, "1", "r"), msg("x", "r", "2", "x"), msg("y", "r", "2", "y")],
    });
    expect(texts(c)).toEqual(["r", "x"]);
    expect(c.turns[1]!.branch).toEqual({ index: 1, total: 2 });
  });

  it("reads an empty design chat", () => {
    const c = claude.parseDesignChat({});
    expect(c.title).toBe("(untitled design chat)");
    expect(c.turns).toEqual([]);
  });

  it("drops design-chat messages with no text", () => {
    const c = claude.parseDesignChat({
      title: "D",
      messages: [
        { role: "user", content: "   " },
        { role: "assistant", content: {} },
      ],
    });
    expect(c.turns).toEqual([]);
  });
});

describe("chatgpt adapter on partial exports", () => {
  it("reads a conversation with no mapping as empty", () => {
    expect(chatgpt.parseConversation({ id: "x" }).turns).toEqual([]);
  });

  it("stops at a mapping entry that is null", () => {
    const c = chatgpt.parseConversation({ id: "y", current_node: "a", mapping: { a: null } });
    expect(c.turns).toEqual([]);
  });

  it("falls back to a tip with no children, no timestamp and no content", () => {
    const c = chatgpt.parseConversation({
      id: "z",
      mapping: { n1: { message: { author: { role: "user" } } } },
    });
    expect(c.turns).toEqual([]);
  });
});

describe("gemini adapter on partial records", () => {
  it("tolerates null and non-object records and an unnamed attachment", () => {
    const records = [null, "not a record", { attachments: [{}] }] as unknown as Rec[];
    const c = gemini.parseConversation(records, [0, 1, 2]);
    expect(c.title).toBe("(untitled)");
    const attachments = c.turns.flatMap((t) => t.blocks).filter((b) => b.type === "attachment");
    expect(attachments.map((b) => b.text)).toEqual(["attachment"]);
  });
});
