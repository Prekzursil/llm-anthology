/**
 * CLI paths the end-to-end suite (cli.test.ts) does not reach: file names built from
 * degenerate titles, ChatGPT inputs that are a single object, carry a non-string id or are
 * not a regular file, and Gemini transcripts and harvests with missing fields.
 */
import { mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { main, safeName } from "../src/cli.js";

let root: string;
beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "llm_anthology-cli-edges-"));
  vi.spyOn(console, "log").mockImplementation(() => {});
  vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => vi.restoreAllMocks());

const write = (name: string, obj: unknown): string => {
  const p = join(root, name);
  writeFileSync(p, JSON.stringify(obj), "utf-8");
  return p;
};
const report = (out: string): Record<string, unknown> =>
  JSON.parse(readFileSync(join(out, "_fidelity-report.json"), "utf-8"));
const pages = (out: string): number => readdirSync(join(out, "html")).length;

const cgConv = (id: string) => ({
  title: id,
  conversation_id: id,
  current_node: "n1",
  mapping: {
    n1: {
      id: "n1",
      parent: null,
      children: [],
      message: {
        id: "n1",
        author: { role: "user" },
        create_time: 1,
        content: { content_type: "text", parts: ["hello"] },
      },
    },
  },
});

describe("safeName", () => {
  it("blanks a control character and falls back to 'untitled'", () => {
    expect(safeName("a\tb", 0)).toBe("000-a b");
    expect(safeName("", 1)).toBe("001-untitled");
    expect(safeName(undefined, 2)).toBe("002-untitled");
    expect(safeName("...", 3)).toBe("003-untitled");
  });
});

describe("chatgpt inputs", () => {
  it("reads a file holding one conversation object rather than a list", () => {
    const out = join(root, "out");
    expect(main(["chatgpt", write("one.json", cgConv("one")), out])).toBe(0);
    expect(pages(out)).toBe(1);
  });

  it("skips a conversation whose id is not a string", () => {
    const src = write("cg.json", [{ conversation_id: 42, title: "numeric id" }, cgConv("ok")]);
    const out = join(root, "out");
    expect(main(["chatgpt", src, out])).toBe(0);
    expect(pages(out)).toBe(1);
  });

  it.runIf(process.platform !== "win32")(
    "finds no export files in a path that is neither a file nor a directory",
    () => {
      const out = join(root, "out");
      expect(main(["chatgpt", "/dev/null", out])).toBe(0);
      expect(Number(report(out).conversations)).toBe(0);
    },
  );
});

describe("gemini inputs", () => {
  it("matches a harvest whose conversations and turns miss fields", () => {
    const src = write("t.json", [
      { verb: "Prompted", prompt: "alpha", response_md: "x" },
      { verb: "Prompted", response_md: "x" },
    ]);
    const harvest = write("h.json", [
      {
        turns: [
          { role: "user", text: "alpha" },
          { role: "user", text: "alpha" },
          { text: "alpha" },
          { role: "user", text: "no such prompt" },
        ],
      },
      { id: "nomatch", title: "N", turns: [{ role: "user", text: "zzz" }] },
      { id: "noturns", title: "E" },
    ]);
    const out = join(root, "out");
    expect(main(["gemini", src, out, "--harvest", harvest])).toBe(0);
    expect(report(out).harvest_matched_records).toBe(1);
    // the matched group + the unmatched-leftovers group
    expect(pages(out)).toBe(2);
  });

  it("groups records that carry no timestamp", () => {
    const src = write("t.json", [{ verb: "Prompted", prompt: "p", response_md: "r" }]);
    const out = join(root, "out");
    expect(main(["gemini", src, out])).toBe(0);
    expect(String(report(out).grouping_mode)).toContain("PROVISIONAL");
  });

  it("reports an empty transcript as one conversation with no turns (exit 3)", () => {
    const out = join(root, "out");
    expect(main(["gemini", write("t.json", []), out])).toBe(3);
    expect(Number(report(out).conversations)).toBe(1);
  });
});
