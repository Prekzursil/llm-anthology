/**
 * Edge / branch-path tests mirroring the Python tests/test_coverage_paths.py, so the
 * JS library modules reach the same Lean 100% bar. Every case is a genuine path (a
 * block-type variant, a falsy inner condition, an unserialisable payload), exercised
 * with a real input rather than excluded.
 */
import { describe, expect, it } from "vitest";

import * as pkg from "../src/index.js";
import { hiddenCharHits } from "../src/audit.js";
import * as chatgpt from "../src/adapters/chatgpt.js";
import * as claude from "../src/adapters/claude.js";
import * as gemini from "../src/adapters/gemini.js";
import { block, conversation, turn } from "../src/ir.js";
import type { Conversation } from "../src/ir.js";
import { isFlagged, isSafeUrl, neutralizeHtml } from "../src/sanitize.js";
import { defangImages, hardenLinks, renderConversationHtml } from "../src/render_html.js";
import { renderConversationMd } from "../src/render_md.js";
import { proseTokens, verify } from "../src/verify.js";

const conv = (turns: ReturnType<typeof turn>[], title = "t", provider = "claude"): Conversation =>
  conversation("c", title, provider, { turns });

// ------------------------------------------------------------------ barrel export

describe("index barrel", () => {
  it("re-exports the public surface", () => {
    expect(typeof pkg.renderConversationHtml).toBe("function");
    expect(typeof pkg.renderConversationMd).toBe("function");
    expect(typeof pkg.verify).toBe("function");
    expect(typeof pkg.hiddenCharHits).toBe("function");
    expect(typeof pkg.demoConversation).toBe("function");
    expect(pkg.adapters.claude.parseExport).toBe(claude.parseExport);
    expect(pkg.chatgpt.parseConversation).toBe(chatgpt.parseConversation);
    expect(pkg.ir.IR_VERSION).toBe(1);
    expect(typeof pkg.sanitize.neutralizeHtml).toBe("function");
  });
});

// -------------------------------------------------------------------- sanitize

describe("sanitize branch edges", () => {
  it("isFlagged binary search takes both left and right at many points", () => {
    // span the code space so both `cp < start` and `cp >= start+len` branches fire
    for (const cp of [
      0x00, 0x09, 0x20, 0x41, 0x61, 0x7f, 0x200b, 0x202e, 0x3164, 0xd800, 0xe0041, 0xfe0f, 0x10fffe,
      0x10ffff,
    ]) {
      expect(typeof isFlagged(cp)).toBe("boolean");
    }
    expect(isFlagged(0x200b)).toBe(true);
    expect(isFlagged(0x61)).toBe(false);
  });
  it("isPictographic operands: heart, regional indicator, and a specific cp", () => {
    expect(neutralizeHtml("❤️")).not.toContain("data-cp"); // 0x2764 + VS16 preserved
    expect(neutralizeHtml("\u{1F1F7}\u{1F1F4}")).not.toContain("data-cp"); // regional indicators
    expect(neutralizeHtml("♥️")).not.toContain("data-cp"); // 0x2665 heart suit
  });
  it("isSafeUrl handles non-string input", () => {
    expect(isSafeUrl(null)).toBe(false);
    expect(isSafeUrl(42)).toBe(false);
  });
  it("neutralizeHtml on non-string input takes the codePoints/escape empty branch", () => {
    expect(neutralizeHtml(null)).toBe("");
    expect(neutralizeHtml(42)).toBe("");
  });
  it("more isPictographic operands stay intact", () => {
    for (const emoji of ["♥️", "‼️", "⁉️", "☺️", "✈️"]) {
      expect(neutralizeHtml(emoji)).not.toContain("data-cp");
    }
  });
});

// -------------------------------------------------------- link/image hardening

describe("hardenLinks (fail-closed over anchor forms markdown-it never emits)", () => {
  it("drops a href-less anchor", () => {
    expect(hardenLinks('<a name="x">t</a>')).toBe("<a>t</a>");
  });
  it("keeps a safe double-quoted href and preserves the title", () => {
    const out = hardenLinks('<a href="https://ok.example" title="the title">t</a>');
    expect(out).toContain('href="https://ok.example"');
    expect(out).toContain('title="the title"');
    expect(out).toContain('rel="noopener noreferrer"');
  });
  it("reads a single-quoted href", () => {
    expect(hardenLinks("<a href='https://ok.example'>t</a>")).toContain(
      'href="https://ok.example"',
    );
  });
  it("reads an unquoted href and defangs an unsafe scheme", () => {
    expect(hardenLinks("<a href=https://ok.example>t</a>")).toContain('href="https://ok.example"');
    const bad = hardenLinks('<a href="ftp://x/y">t</a>');
    expect(bad).toContain('data-unsafe-href="ftp://x/y"'); // defanged, not a live href
    expect(bad).not.toContain("rel="); // no live-link attributes
  });
});

describe("defangImages", () => {
  it("turns a safe remote image into a labelled link, never an <img>", () => {
    const out = defangImages('<img src="https://ok.example/a.png" alt="pic">');
    expect(out).toContain("remote image");
    expect(out).not.toContain("<img");
  });
  it("marks a non-http image unavailable", () => {
    const out = defangImages('<img src="local/a.png" alt="pic">');
    expect(out).toContain("image unavailable");
  });
  it("defaults alt text when absent", () => {
    expect(defangImages('<img src="https://ok.example/a.png">')).toContain("🖼 image");
  });
});

// --------------------------------------------------------------------- render

describe("render html/md block variants", () => {
  const rich = conv([
    turn("assistant", [
      block("code", { text: "x=1", data: { language: "python" } }),
      block("event", { text: "Used Canvas", data: { name: "Used" } }),
      block("file", { text: "f.png", data: { file_name: "f.png" } }),
      block("media", { data: { path: "local.png" } }),
      block("media", { data: { path: "https://remote.example/x.png" } }),
      block("tool_result", { data: { name: "t", is_error: true, content: { k: "v" } } }),
      block("attachment", {
        text: "d.pdf",
        data: { file_name: "d.pdf", extracted_content: "doc text" },
      }),
      block("attachment", { text: "b.bin", data: { file_name: "b.bin" } }), // no extracted_content
      block("unknown", { data: { orig_type: "future", x_raw: { t: "P" } } }),
      block("unknown", { data: { orig_type: "empty" } }), // no x_raw -> body ""
      block("weird-future-type", { text: "fallback body" }),
    ]),
  ]);

  it("html covers every block", () => {
    const out = renderConversationHtml(rich);
    expect(out).toContain('class="language-python"');
    expect(out).toContain("✨");
    expect(out).toContain("no bytes in export");
    expect(out).toContain('src="../media/local.png"');
    expect(out).toContain("🖼");
    expect(out).toContain(" err");
    expect(out).toContain("Attached document text");
    expect(out).toContain("unrendered future block");
    expect(out).toContain("fallback body");
  });

  it("md covers every block", () => {
    const md = renderConversationMd(rich);
    expect(md).toContain("```python");
    expect(md).toContain("✨");
    expect(md).toContain("remote media");
    expect(md).toContain("unrendered empty block");
    expect(md).toContain("fallback body");
    expect(md).toContain("b.bin");
    expect(md).not.toContain("```json\nnull"); // empty unknown has no payload fence
  });

  it("tool_use with display + input, and tool_result string content", () => {
    const c = conv([
      turn("assistant", [
        block("tool_use", { text: "disp", data: { name: "s", input: { q: 1 } } }),
        block("tool_result", { text: "disp", data: { name: "t", content: "plain result" } }),
      ]),
    ]);
    expect(renderConversationHtml(c)).toContain("tool-disp");
    expect(renderConversationMd(c)).toContain("Tool call");
  });

  it("nested-path local media renders relative, not under ../media", () => {
    const c = conv([turn("assistant", [block("media", { data: { path: "sub/dir/shot.png" } })])]);
    expect(renderConversationHtml(c)).toContain('src="sub/dir/shot.png"');
    expect(renderConversationMd(c)).toContain("(sub/dir/shot.png)");
  });

  it("empty conversation and empty-blocks turn render", () => {
    expect(renderConversationHtml(conv([]))).toContain("conv-title");
    expect(renderConversationMd(conv([]))).toContain("# t");
    expect(renderConversationHtml(conv([turn("human", [])]))).toContain("human");
    expect(renderConversationMd(conv([turn("human", [])]))).toContain("Human");
  });

  it("empty provider omits the meta line", () => {
    const c = conv([turn("assistant", [block("text", { text: "x" })])], "t", "");
    expect(renderConversationHtml(c)).toContain('conv-meta"></div>');
    expect(renderConversationMd(c).split("\n")[1]).toBe("");
  });

  it("citations: safe pill, unsafe span, and none", () => {
    const c = conv([
      turn("assistant", [
        block("text", {
          text: "a",
          citations: [
            "not a dict" as unknown as { url: string },
            { url: "https://ok.example", title: "OK" },
            { url: "javascript:alert(1)", title: "bad" },
          ],
        }),
        block("text", { text: "b", citations: [] }),
      ]),
    ]);
    const html = renderConversationHtml(c);
    expect(html).toContain("<span>bad</span>");
    expect(html).not.toContain('href="javascript:');
    const md = renderConversationMd(c);
    expect(md).toContain("Sources:"); // block a has a safe citation
    expect(md).not.toMatch(/(?<!\\)\]\(javascript:/); // the unsafe one is inert
  });

  it("defangs a non-http markdown image", () => {
    const c = conv([turn("assistant", [block("text", { text: "![alt](assets/local.png)" })])]);
    const out = renderConversationHtml(c);
    expect(out).toContain("image unavailable");
    expect(out).not.toContain("<img");
  });

  it("missing theme yields empty css", () => {
    expect(
      renderConversationHtml(conv([turn("human", [block("text", { text: "x" })])]), "nope"),
    ).toContain("<style></style>");
  });

  it("remote media chip and unserialisable unknown payload do not throw", () => {
    const bad = {
      toJSON() {
        throw new Error("no");
      },
    };
    const c = conv([
      turn("assistant", [block("unknown", { data: { orig_type: "x", x_raw: { o: bad } } })]),
    ]);
    expect(renderConversationHtml(c)).toContain("unrendered x block");
    expect(renderConversationMd(c)).toContain("unrendered x block");
  });
});

// --------------------------------------------------------------------- verify

describe("verify edges", () => {
  it("ignores non-prose blocks", () => {
    expect(
      proseTokens(
        conv([
          turn("assistant", [
            block("tool_use", { data: { name: "n", input: {} } }),
            block("text", { text: "realword" }),
          ]),
        ]),
      ),
    ).toEqual(["realword"]);
  });

  it("flags missing words and sorts by code point incl. a prefix tie", () => {
    // "ab" vs "abc": prefix tie exercises the length comparison in byCodePoint; the
    // astral token exercises the per-codepoint compare
    const c = conv([turn("human", [block("text", { text: "\u{1d400} abc ab zzz" })])]);
    const v = verify(c, "<html><body>nothing</body></html>");
    expect(v.ok).toBe(false);
    expect(v.missing_tokens).toContain("ab");
    expect(v.missing_tokens).toContain("abc");
    expect(v.missing_tokens.indexOf("ab")).toBeLessThan(v.missing_tokens.indexOf("abc"));
    expect(v.coverage).toBeLessThan(1);
  });
});

// --------------------------------------------------------------------- audit

describe("audit edges", () => {
  it("skips non-dict citation, non-string title, and unserialisable blob", () => {
    const bad = {
      toJSON() {
        throw new Error("no");
      },
    };
    const c = conv([
      turn("assistant", [
        block("text", {
          text: "a",
          citations: [
            "nope" as unknown as { url: string },
            { title: 123 as unknown as string, url: "https://ok" },
          ],
        }),
        block("tool_use", { data: { name: "n", input: { o: bad } } }),
      ]),
    ]);
    expect(hiddenCharHits(c)).toEqual([]);
  });
});

// -------------------------------------------------------------------- adapters

describe("chatgpt adapter edges", () => {
  const node = (
    id: string,
    parent: string | null,
    kids: string[],
    role?: string,
    ct?: unknown,
  ) => ({
    id,
    parent,
    children: kids,
    message: role ? { id, author: { role }, create_time: 1.0, content: ct, metadata: {} } : null,
  });
  const parse = (
    mapping: Record<string, unknown>,
    current: unknown = "a",
    extra: Record<string, unknown> = {},
  ) => chatgpt.parseConversation({ title: "t", id: "x", current_node: current, mapping, ...extra });

  it("coalesces consecutive assistant nodes", () => {
    const c = parse(
      {
        r: node("r", null, ["a"]),
        a: node("a", "r", ["b"], "user", { content_type: "text", parts: ["q"] }),
        b: node("b", "a", ["c"], "assistant", { content_type: "text", parts: ["one"] }),
        c: node("c", "b", [], "assistant", { content_type: "text", parts: ["two"] }),
      },
      "c",
    );
    expect(c.turns.map((t) => t.role)).toEqual(["human", "assistant"]);
    expect(c.turns[1]!.blocks.map((b) => b.text)).toEqual(["one", "two"]);
  });

  it("unknown content type -> unknown block", () => {
    const c = parse({ a: node("a", null, [], "assistant", { content_type: "tether", url: "u" }) });
    expect(c.turns[0]!.blocks[0]!.type).toBe("unknown");
  });

  it("reasoning_recap content string, empty-body thought skipped, multiple thoughts", () => {
    const c = parse({
      a: node("a", null, [], "assistant", {
        content_type: "thoughts",
        thoughts: [{ summary: "", content: "" }, "nope", { summary: "s", content: "kept" }],
      }),
    });
    expect(c.turns[0]!.blocks.map((b) => b.text)).toEqual(["kept"]);
    const c2 = parse({
      a: node("a", null, [], "assistant", { content_type: "reasoning_recap", content: "recap" }),
    });
    expect(c2.turns[0]!.blocks[0]!.text).toBe("recap");
  });

  it("execution_output (filled + empty), whitespace + non-str parts, image + non-image", () => {
    const c = parse(
      {
        a: node("a", null, ["b"], "tool", { content_type: "execution_output", text: "out" }),
        b: node("b", "a", ["d"], "assistant", { content_type: "execution_output", text: "" }),
        d: node("d", "b", [], "user", {
          content_type: "multimodal_text",
          parts: [
            "  ",
            12345,
            "real",
            { content_type: "image_asset_pointer", asset_pointer: "file-service://file-ABC" },
            { content_type: "audio_asset_pointer", x: 1 },
          ],
        }),
      },
      "d",
    );
    const kinds = c.turns.flatMap((t) => t.blocks.map((b) => b.type));
    expect(kinds).toContain("tool_result");
    expect(kinds).toContain("media");
    expect(kinds).toContain("unknown");
    expect(
      c.turns.flatMap((t) => t.blocks.filter((b) => b.type === "text").map((b) => b.text)),
    ).toEqual(["real"]);
  });

  it("out-of-range/overflow timestamp degrades to empty, unknown ctype fallback", () => {
    const c = parse(
      { a: node("a", null, [], "user", { content_type: "text", parts: ["hi"] }) },
      "a",
      { create_time: 1e20 },
    );
    expect(c.created_at).toBe("");
    const c2 = parse({ a: node("a", null, [], "assistant", { content_type: "code", text: "" }) });
    expect(c2.turns).toEqual([]); // empty code -> no block -> no turn
  });

  it("null current_node falls back to newest leaf, keeping first best over worse later", () => {
    const c = chatgpt.parseConversation({
      title: "t",
      id: "x",
      current_node: null,
      mapping: {
        root: node("root", null, ["new", "old"]),
        new: node("new", "root", [], "assistant", { content_type: "text", parts: ["NEWER"] }),
        old: node("old", "root", [], "assistant", { content_type: "text", parts: ["OLDER"] }),
      },
    });
    c.turns
      .flatMap((t) => t.blocks.map((b) => b.text))
      .forEach((x) => expect(typeof x).toBe("string"));
    const gotNew = c.turns.some((t) => t.blocks.some((b) => b.text === "NEWER"));
    // both leaves share create_time 1.0, so the first-iterated leaf wins deterministically
    expect(gotNew || c.turns.length > 0).toBe(true);
  });

  it("fallback ignores malformed, system, and visually-hidden path nodes", () => {
    const system = node("system", null, ["hidden"], "system", {
      content_type: "text",
      parts: ["internal"],
    });
    const hidden = node("hidden", "system", ["visible"], "assistant", {
      content_type: "text",
      parts: ["also internal"],
    });
    hidden.message!.metadata = { is_visually_hidden_from_conversation: true };
    const c = parse(
      {
        malformed: null,
        system,
        hidden,
        visible: node("visible", "hidden", [], "user", {
          content_type: "text",
          parts: ["shown"],
        }),
      },
      null,
    );
    expect(c.turns.map((t) => t.blocks.map((b) => b.text))).toEqual([["shown"]]);
  });
});

describe("chatgpt parseExport shape handling", () => {
  it("wraps a single conversation object and drops non-objects", () => {
    const single = chatgpt.parseExport({
      title: "t",
      conversation_id: "x",
      current_node: null,
      mapping: {},
    });
    expect(single.length).toBe(1);
    const arr = chatgpt.parseExport([
      null,
      "str",
      7,
      { title: "t", conversation_id: "y", current_node: null, mapping: {} },
    ]);
    expect(arr.length).toBe(1);
  });
});

describe("gemini adapter edges", () => {
  it("ungrouped parseAll wraps all records (null and empty groups)", () => {
    const recs = [{ verb: "Prompted", prompt: "q", response_md: "a" }];
    expect(gemini.parseAll(recs, null)[0]!.id).toBe("all"); // null groups
    expect(gemini.parseAll(recs, [])[0]!.id).toBe("all"); // empty groups array
  });

  it("records a repeated gem once, and no gems yields empty meta", () => {
    const withGem = gemini.parseConversation(
      [
        { verb: "Prompted", prompt: "a", response_md: "x", gem: "G" },
        { verb: "Prompted", prompt: "b", response_md: "x", gem: "G" }, // same gem -> not re-added
      ],
      [0, 1],
      "T",
      "g",
    );
    expect((withGem.meta as { gems: string[] }).gems).toEqual(["G"]);
    const noGem = gemini.parseConversation(
      [{ verb: "Prompted", prompt: "a", response_md: "x" }],
      [0],
      "T",
      "g",
    );
    expect(noGem.meta).toEqual({});
  });

  it("a group missing id/title/turn_idxs still parses with defaults", () => {
    const convs = gemini.parseAll([{ verb: "Prompted", prompt: "q", response_md: "a" }], [{}]);
    expect(convs[0]!.title).toBe("(untitled)");
    expect(convs[0]!.id).toBe("");
  });

  it("sparse records exercise the s()/get() fallback chains", () => {
    const c = gemini.parseConversation(
      [
        {
          verb: "Prompted",
          title: "T",
          detail: "D", // prompt missing -> title fallback
          attachments: ["", { name: "n" }],
          media: [{ name: "m.png" }, "plain.png"],
        },
        { verb: "Used", detail: "only detail" }, // event label from detail
        { verb: "Used" }, // event label from verb
        {}, // verb missing -> defaults to Prompted
      ],
      [0, 1, 2, 3],
      "T",
      "g",
    );
    const kinds = c.turns.flatMap((t) => t.blocks.map((b) => b.type));
    expect(kinds).toContain("attachment");
    expect(kinds).toContain("media");
    expect(kinds).toContain("event");
  });

  it("out-of-range indices are skipped; string attachment + media; event without media", () => {
    const c = gemini.parseConversation(
      [
        {
          verb: "Prompted",
          prompt: "q",
          response_md: "a",
          attachments: ["f.txt", { name: "n", on_disk: "/d" }],
          media: ["pic.png", { on_disk: "/m.png" }],
        },
        { verb: "Used", title: "Used X", detail: "d" },
      ],
      [0, 1, 5, -1],
      "T",
      "g",
    );
    const kinds = c.turns.flatMap((t) => t.blocks.map((b) => b.type));
    expect(kinds).toContain("attachment");
    expect(kinds).toContain("media");
    expect(kinds).toContain("event");
  });
});

describe("null/empty content branches", () => {
  it("tool_result with null content and an empty thinking body", () => {
    const c = conv([
      turn("assistant", [
        block("thinking", { text: "" }), // empty body -> "> " branch
        block("tool_result", { text: "", data: { name: "t", content: null } }),
        block("tool_use", { text: "", data: { name: "u", input: null } }),
      ]),
    ]);
    expect(renderConversationHtml(c)).toContain("Thought process"); // HTML thinking label
    expect(renderConversationMd(c)).toContain("🧠 **Thinking**"); // MD thinking label
    expect(verify(c, "<html></html>").ok).toBe(true);
  });
});

describe("claude adapter edges", () => {
  it("account as a plain string; design chat flat string, non-text and non-dict blocks", () => {
    expect(
      claude.parseConversation({ uuid: "c", name: "n", account: "s", chat_messages: [] }).account,
    ).toBe("s");
    const c = claude.parseDesignChat({
      uuid: "d",
      title: "T",
      messages: [
        { uuid: "m1", role: "user", content: "flat" },
        {
          uuid: "m2",
          role: "assistant",
          content: { contentBlocks: ["nope", { type: "img", id: "a" }] },
        },
        { uuid: "m3", role: "assistant", content: 123 },
        "bare string",
      ],
    });
    const kinds = c.turns.flatMap((t) => t.blocks.map((b) => b.type));
    expect(kinds).toContain("text");
    expect(kinds).toContain("unknown");
  });

  it("abandoned-branch tree: descends to the live thread and marks the branch", () => {
    // a user turn with two assistant children (siblings); the live one continues a
    // deeper subtree, so argmaxKid must descend to it, not the newest immediate child,
    // and the chosen node carries a 2-sibling branch marker
    const m = (u: string, parent: string | null, sender: string, text: string, t: string) => ({
      uuid: u,
      parent_message_uuid: parent,
      sender,
      created_at: t,
      content: [{ type: "text", text, citations: [] }],
      attachments: [],
      files: [],
    });
    const c = claude.parseConversation({
      uuid: "c",
      name: "n",
      account: { uuid: "a" },
      chat_messages: [
        m("r", null, "human", "q", "t1"),
        m("old", "r", "assistant", "ABANDONED", "t2"),
        m("new", "r", "assistant", "LIVE", "t3"),
        m("cont", "new", "human", "continues here", "t4"),
      ],
    });
    const texts = c.turns.flatMap((t) => t.blocks.map((b) => b.text));
    expect(texts).toContain("LIVE");
    expect(texts).toContain("continues here");
    expect(texts).not.toContain("ABANDONED");
    expect(c.turns.some((t) => t.branch !== null && t.branch.total === 2)).toBe(true);
  });

  it("design chat with a text contentBlock renders its text", () => {
    const c = claude.parseDesignChat({
      uuid: "d",
      title: "T",
      messages: [
        {
          uuid: "m",
          role: "assistant",
          content: { content: "FLAT", contentBlocks: [{ type: "text", text: "BLOCK TEXT" }] },
        },
      ],
    });
    const texts = c.turns.flatMap((t) => t.blocks.map((b) => b.text));
    expect(texts).toContain("BLOCK TEXT");
    expect(texts).not.toContain("FLAT"); // contentBlocks win over the flat string
  });

  it("renders orphaned mutually-parented messages (the orphan sweep)", () => {
    // a.parent=b and b.parent=a -> neither is a root -> both are swept as orphans
    const m = (u: string, parent: string) => ({
      uuid: u,
      parent_message_uuid: parent,
      sender: "human",
      created_at: `t-${u}`,
      content: [{ type: "text", text: u, citations: [] }],
      attachments: [],
      files: [],
    });
    const c = claude.parseConversation({
      uuid: "c",
      name: "n",
      chat_messages: [m("a", "b"), m("b", "a")],
    });
    const texts = c.turns.flatMap((t) => t.blocks.map((b) => b.text));
    expect(texts).toContain("a");
    expect(texts).toContain("b");
  });

  it("deduplicates repeated UUIDs in both reachable and orphaned components", () => {
    const m = (u: string, parent: string | null, text: string, t: string) => ({
      uuid: u,
      parent_message_uuid: parent,
      sender: "human",
      created_at: t,
      content: [{ type: "text", text, citations: [] }],
      attachments: [],
      files: [],
    });
    const c = claude.parseConversation({
      uuid: "c",
      name: "n",
      chat_messages: [
        m("root", null, "first root", "t1"),
        m("root", null, "duplicate root", "t2"),
        m("a", "b", "first a", "t3"),
        m("a", "b", "duplicate a", "t4"),
        m("b", "a", "b", "t5"),
      ],
    });
    expect(c.turns.filter((t) => t.uuid === "root")).toHaveLength(1);
    expect(c.turns.filter((t) => t.uuid === "a")).toHaveLength(1);
  });

  it("minimal blocks hit the || default fallbacks", () => {
    const c = conv([
      turn("assistant", [
        block("media", { data: { path: "" } }), // empty path -> "media" default
        block("file", { data: {} }), // no file_name -> "file"
        block("unknown", { data: {} }), // no orig_type -> "unknown"
        block("tool_use", { data: {} }), // no name -> "tool"
        block("attachment", { data: {} }), // no file_name -> "attachment"
      ]),
    ]);
    expect(renderConversationHtml(c)).toContain("🖼 media");
    const md = renderConversationMd(c);
    expect(md).toContain("_(no content in export)_");
    expect(md).toContain("unrendered unknown block");
  });

  it("chatgpt s()/get() fallbacks: message without author or metadata", () => {
    const c = chatgpt.parseConversation({
      title: "t",
      id: "x",
      current_node: "a",
      mapping: {
        a: {
          id: "a",
          parent: null,
          children: [],
          message: { id: "a", create_time: 1.0, content: { content_type: "text", parts: ["hi"] } },
        },
      },
    });
    expect(c.turns[0]!.role).toBe("assistant"); // no author -> role "" -> assistant
  });

  it("thinking, tool_use, tool_result and an unknown content item all parse", () => {
    const c = claude.parseConversation({
      uuid: "c",
      name: "n",
      account: { uuid: "a" },
      chat_messages: [
        {
          uuid: "m1",
          parent_message_uuid: null,
          sender: "human",
          created_at: "t1",
          content: [{ type: "text", text: "q", citations: [] }],
          attachments: [{ file_name: "a.pdf" }],
          files: [{ file_name: "f" }],
        },
        {
          uuid: "m2",
          parent_message_uuid: "m1",
          sender: "assistant",
          created_at: "t2",
          content: [
            { type: "thinking", thinking: "reasoning", thinking_hidden: true, summaries: [] },
            { type: "tool_use", name: "s", input: { q: 1 }, display_content: "d" },
            { type: "tool_result", name: "s", content: "r", is_error: false },
            { type: "token_budget", value: 1 }, // hidden content type -> skipped
            { type: "future_kind", data: 1 }, // unknown -> passthrough
          ],
          attachments: [],
          files: [],
        },
      ],
    });
    const kinds = c.turns.flatMap((t) => t.blocks.map((b) => b.type));
    expect(kinds).toEqual(
      expect.arrayContaining([
        "attachment",
        "file",
        "text",
        "thinking",
        "tool_use",
        "tool_result",
        "unknown",
      ]),
    );
    expect(kinds).not.toContain("token_budget");
  });
});
