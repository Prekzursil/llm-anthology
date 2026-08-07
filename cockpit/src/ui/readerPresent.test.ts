/**
 * The reader's content decisions.
 *
 * The invariant that matters most here is NEGATIVE: no block may render as nothing. The
 * engine emits ten block types (text, thinking, tool_use, tool_result, code, attachment,
 * file, media, event, unknown) and `Block.text` is the display text for every one of them —
 * the body for text/thinking, a label otherwise. A reader that handled only `type === "text"`
 * would silently hide most of a coding session and look like a complete transcript while
 * doing it. That is the same silent-omission class as the 1000-root sidebar and the
 * 200-of-1432 search list, and it is the one a reader can least afford.
 */
import { describe, expect, it } from "vitest";

import {
  BLOCK_KINDS,
  blockDisplay,
  readerSubtitle,
  readerTitle,
  roleLabel,
  stubExplanation,
  turnWhen,
} from "./readerPresent";
import type { ConversationBlock, ConversationStub } from "../ipc/types";

const block = (over: Partial<ConversationBlock> = {}): ConversationBlock => ({
  type: "text",
  text: "hello",
  data: {},
  citations: [],
  ...over,
});

describe("blockDisplay", () => {
  it("shows a text block as its body, with no decoration", () => {
    expect(blockDisplay(block())).toEqual({ kind: "text", label: "", body: "hello" });
  });

  it("labels every non-body type so the reader shows WHAT it is", () => {
    expect(blockDisplay(block({ type: "thinking", text: "hmm" })))
      .toEqual({ kind: "thinking", label: "thinking", body: "hmm" });
    expect(blockDisplay(block({ type: "tool_use", text: "Bash" })))
      .toEqual({ kind: "tool_use", label: "tool call", body: "Bash" });
    expect(blockDisplay(block({ type: "tool_result", text: "ok" })))
      .toEqual({ kind: "tool_result", label: "tool result", body: "ok" });
  });

  it("RENDERS SOMETHING for every block type the engine can emit", () => {
    // The whole point. Enumerated from `llm_anthology/ir.py` and the adapters, so adding a
    // type there without handling it here fails this rather than vanishing from the UI.
    for (const type of BLOCK_KINDS) {
      const out = blockDisplay(block({ type, text: "payload" }));
      expect(out.body, `${type} rendered no body`).not.toBe("");
      expect(out.kind, `${type} lost its kind`).toBe(type);
    }
  });

  it("renders an UNKNOWN type rather than dropping it", () => {
    // A provider gaining a block type must degrade to "shown but unlabelled", never to
    // "silently absent" — the reader cannot claim to be a transcript otherwise.
    const out = blockDisplay(block({ type: "brand-new-thing", text: "payload" }));
    expect(out.body).toBe("payload");
    expect(out.label).toBe("brand-new-thing");
  });

  it("falls back to the payload when a labelled block carries no text", () => {
    // `text` is a LABEL for tool blocks, and an adapter may leave it empty while the real
    // content sits in `data`. Rendering an empty bubble would look like a broken message.
    const out = blockDisplay(block({ type: "tool_use", text: "", data: { name: "Read" } }));
    expect(out.body).toContain("Read");
  });

  it("says a block is empty rather than rendering a blank bubble", () => {
    const out = blockDisplay(block({ type: "text", text: "", data: {}, citations: [] }));
    expect(out.body).toBe("(empty)");
  });
});

describe("roleLabel", () => {
  it("names the two roles the IR defines", () => {
    expect(roleLabel("human")).toBe("You");
    expect(roleLabel("assistant")).toBe("Assistant");
  });

  it("shows an unexpected role as itself instead of guessing", () => {
    expect(roleLabel("system")).toBe("system");
    expect(roleLabel("")).toBe("unknown");
  });
});

describe("turnWhen", () => {
  it("formats a timestamp for reading, not for sorting", () => {
    expect(turnWhen("2026-08-07T10:04:05.000Z")).toBe("2026-08-07 10:04");
  });

  it("returns nothing for an absent or unparseable timestamp", () => {
    // An undated turn is real. Inventing a time would be worse than an empty cell.
    expect(turnWhen("")).toBe("");
    expect(turnWhen("not a date")).toBe("");
  });
});

describe("readerTitle", () => {
  it("uses the conversation title", () => {
    expect(readerTitle({ id: "c1", title: "Fixing the layout" })).toBe("Fixing the layout");
  });

  it("falls back to the id rather than showing an empty header", () => {
    expect(readerTitle({ id: "c1", title: "" })).toBe("untitled · c1");
    expect(readerTitle({ id: "c1", title: "   " })).toBe("untitled · c1");
  });
});

describe("readerSubtitle", () => {
  it("states the provider and how many turns are shown", () => {
    expect(readerSubtitle({ provider: "codex", turns: 12, parseErrors: 0 }))
      .toBe("codex · 12 turns");
  });

  it("DISCLOSES parse errors instead of presenting a partial transcript as whole", () => {
    // `parse_errors` is counted by the engine and was never surfaced. A transcript that
    // dropped lines must say so, or it is a confident lie about what the session contained.
    expect(readerSubtitle({ provider: "codex", turns: 12, parseErrors: 3 }))
      .toBe("codex · 12 turns · 3 lines could not be parsed");
    expect(readerSubtitle({ provider: "grok", turns: 1, parseErrors: 1 }))
      .toBe("grok · 1 turn · 1 line could not be parsed");
  });
});

describe("stubExplanation", () => {
  const stub = (reason: string): ConversationStub =>
    ({ id: "c1", available: false, reason } as ConversationStub);

  it("explains a missing session file in plain language", () => {
    // "rollout unavailable" is accurate and tells a user nothing they can act on.
    const text = stubExplanation(stub("rollout unavailable"));
    expect(text.toLowerCase()).toContain("moved");
    expect(text.toLowerCase()).toContain("session file");
  });

  it("passes an UNRECOGNISED reason through verbatim rather than swallowing it", () => {
    // The known reasons get plain language; anything else must still reach the user, or a
    // new engine failure mode becomes invisible the moment it is introduced.
    expect(stubExplanation(stub("rollout unreadable: permission denied")))
      .toContain("rollout unreadable: permission denied");
  });

  it("explains an export-only provider in words a user can act on", () => {
    // "no reader for provider 'chatgpt'" is the engine being precise; on its own it reads
    // as a bug rather than as a known limit of that source format.
    const text = stubExplanation(stub("no reader for provider 'chatgpt'"));
    expect(text).toContain("chatgpt");
    expect(text.toLowerCase()).toContain("export");
  });

  it("never returns an empty explanation, even with no reason given", () => {
    expect(stubExplanation(stub("")).trim()).not.toBe("");
  });
});
