/**
 * End-to-end + edge-path tests for the `llm-anthology` CLI (cli.ts) — mirrors the Python
 * tests/test_cli.py and tests/test_coverage_paths.py loader cases so the two rails'
 * CLI behaviour matches and cli.ts reaches the Lean 100% gate.
 */
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve as resolvePath } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as chatgptAdapter from "../src/adapters/chatgpt.js";
import * as claudeAdapter from "../src/adapters/claude.js";
import { isEntryPoint, main } from "../src/cli.js";
import * as renderHtmlModule from "../src/render_html.js";
import * as verifyModule from "../src/verify.js";

let root: string;
beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "llm_anthology-cli-"));
  vi.spyOn(console, "log").mockImplementation(() => {});
  vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => vi.restoreAllMocks());

const write = (p: string, obj: unknown): string => {
  writeFileSync(p, typeof obj === "string" ? obj : JSON.stringify(obj), "utf-8");
  return p;
};
const report = (out: string): Record<string, unknown> =>
  JSON.parse(readFileSync(join(out, "_fidelity-report.json"), "utf-8"));

function claudeExport() {
  const msg = (u: string, parent: string | null, sender: string, text: string) => ({
    uuid: u, parent_message_uuid: parent, sender, created_at: `t-${u}`,
    content: [{ type: "text", text, citations: [] }], attachments: [], files: [], text: "",
  });
  return [{
    uuid: "c1", name: "Chat A", created_at: "2025-01-01T00:00:00Z", account: { uuid: "acc1" },
    chat_messages: [msg("m1", null, "human", "hello"), msg("m2", "m1", "assistant", "hi there")],
  }];
}
function chatgptExport() {
  return [{
    title: "CG A", conversation_id: "a", create_time: 1.0, current_node: "n2",
    mapping: {
      n0: { id: "n0", message: null, parent: null, children: ["n1"] },
      n1: { id: "n1", parent: "n0", children: ["n2"], message: { id: "n1", author: { role: "user" }, create_time: 1.0, content: { content_type: "text", parts: ["hello"] }, metadata: {} } },
      n2: { id: "n2", parent: "n1", children: [], message: { id: "n2", author: { role: "assistant" }, create_time: 2.0, content: { content_type: "text", parts: ["hi there"] }, metadata: {} } },
    },
  }];
}
const geminiRecords = () => [
  { verb: "Prompted", prompt: "hello", response_md: "hi there", timestamp_iso: "2026-01-01T10:00:00", gem: null, attachments: [], media: [], title: "", detail: "" },
];

describe("usage / demo", () => {
  it("no command returns exit 2", () => {
    expect(main([])).toBe(2);
  });
  it("--help returns 0", () => {
    expect(main(["--help"])).toBe(0);
  });
  it("demo without a path returns 2", () => {
    expect(main(["demo"])).toBe(2);
  });
  it("demo writes a self-contained HTML page", () => {
    const out = join(root, "sub", "demo.html");
    expect(main(["demo", out])).toBe(0);
    expect(readFileSync(out, "utf-8").toLowerCase()).toContain("<!doctype html");
  });
  it("a provider command needs both src and out", () => {
    expect(main(["claude", join(root, "x.json")])).toBe(2);
  });
  it("missing input is a clean error, not a throw", () => {
    expect(main(["claude", join(root, "nope.json"), join(root, "out")])).toBe(1);
  });
  it("an unknown command with no args falls through to usage", () => {
    expect(main(["bogus"])).toBe(2);
  });
  it("an unknown command WITH valid src/out reaches the dispatch else", () => {
    const src = write(join(root, "x.json"), []);
    expect(main(["bogus", src, join(root, "out")])).toBe(2);
  });
});

describe("claude", () => {
  it("renders a single export file", () => {
    const src = write(join(root, "claude.json"), claudeExport());
    const out = join(root, "out");
    expect(main(["claude", src, out])).toBe(0);
    expect(readdirSync(join(out, "html")).length).toBe(1);
    const md = readFileSync(join(out, "md", readdirSync(join(out, "md"))[0]!), "utf-8");
    expect(md).toContain("hello");
    expect(md).toContain("hi there");
  });

  it("skips metadata but keeps design_chats in a directory tree", () => {
    const acct = join(root, "acct");
    mkdirSync(join(acct, "projects"), { recursive: true });
    mkdirSync(join(acct, "design_chats"), { recursive: true });
    write(join(acct, "conversations.json"), claudeExport());
    write(join(acct, "users.json"), { uuid: "u" });
    write(join(acct, "projects", "p.json"), { uuid: "p", name: "proj" });
    write(join(acct, "design_chats", "d.json"), {
      uuid: "d", title: "A design chat",
      messages: [{ uuid: "m", role: "user", content: { content: "design me" } }],
    });
    const out = join(root, "out");
    expect(main(["claude", root, out])).toBe(0);
    expect(readdirSync(join(out, "html")).length).toBe(2);
  });

  it("falls back to any *.json when there is no conversations.json", () => {
    const d = join(root, "acct");
    mkdirSync(d, { recursive: true });
    write(join(d, "renamed-export.json"), claudeExport());
    const out = join(root, "out");
    expect(main(["claude", root, out])).toBe(0);
    expect(readdirSync(join(out, "html")).length).toBe(1);
  });

  it("reports malformed JSON rather than crashing", () => {
    const d = join(root, "acct");
    mkdirSync(d, { recursive: true });
    write(join(d, "conversations.json"), "{not json");
    const out = join(root, "out");
    expect(main(["claude", root, out])).toBe(0);
    expect((report(out).errors as Array<{ stage: string }>).some((e) => e.stage === "parse")).toBe(true);
  });

  it("reports a claude adapter failure", () => {
    const spy = vi.spyOn(claudeAdapter, "parseExport").mockImplementation(() => {
      throw new Error("synthetic adapt failure");
    });
    const src = write(join(root, "claude.json"), claudeExport());
    const out = join(root, "out");
    expect(main(["claude", src, out])).toBe(0);
    expect((report(out).errors as Array<{ stage: string }>).some((e) => e.stage === "adapt")).toBe(true);
    spy.mockRestore();
  });
});

describe("chatgpt", () => {
  it("renders and dedupes by id, surfaces project tag", () => {
    const data = [...chatgptExport(), ...chatgptExport()]; // same id twice
    (data[1] as Record<string, unknown>)["__project_id"] = "g-p-XYZ";
    const src = write(join(root, "cg.json"), data);
    const out = join(root, "out");
    expect(main(["chatgpt", src, out])).toBe(0);
    expect(readdirSync(join(out, "html")).length).toBe(1); // deduped
    expect(readFileSync(join(out, "index.html"), "utf-8")).toContain("g-p-XYZ");
  });

  it("merges a --projects file and skips junk records", () => {
    const src = write(join(root, "cg.json"), ["a string", 42, { no: "id" }, ...chatgptExport()]);
    const proj = write(join(root, "proj.json"), [{ conversation_id: "b", current_node: null, mapping: {}, __project_id: "g-p-2" }]);
    const out = join(root, "out");
    expect(main(["chatgpt", src, out, "--projects", proj])).toBe(0);
    expect(readdirSync(join(out, "html")).length).toBe(2);
  });

  it("reports malformed chatgpt json", () => {
    const src = write(join(root, "cg.json"), "{bad");
    const out = join(root, "out");
    expect(main(["chatgpt", src, out])).toBe(0);
    expect((report(out).errors as Array<{ stage: string }>).some((e) => e.stage === "parse")).toBe(true);
  });

  it("reports an adapter failure without aborting the corpus", () => {
    const spy = vi.spyOn(chatgptAdapter, "parseConversation").mockImplementation(() => {
      throw new Error("synthetic adapt failure");
    });
    const src = write(join(root, "cg.json"), chatgptExport());
    const out = join(root, "out");
    expect(main(["chatgpt", src, out])).toBe(0);
    expect((report(out).errors as Array<{ stage: string }>).some((e) => e.stage === "adapt")).toBe(true);
    spy.mockRestore();
  });
});

describe("gemini", () => {
  it("provisional grouping is labelled", () => {
    const src = write(join(root, "t.json"), geminiRecords());
    const out = join(root, "out");
    expect(main(["gemini", src, out])).toBe(0);
    expect(report(out).grouping_mode).toContain("PROVISIONAL");
  });

  it("harvest grouping is labelled TRUE and matches", () => {
    const src = write(join(root, "t.json"), [
      ...geminiRecords(),
      { verb: "Prompted", prompt: "second", response_md: "answer", timestamp_iso: "2026-01-01T10:01:00" },
    ]);
    // Reverse harvest order forces turn_idxs.sort(...) to restore source chronology.
    const harvest = write(join(root, "h.json"), [{ id: "g1", title: "Real", turns: [
      { role: "user", text: "second" },
      { role: "user", text: "hello" },
    ] }]);
    const out = join(root, "out");
    expect(main(["gemini", src, out, "--harvest", harvest])).toBe(0);
    const rep = report(out);
    expect(rep.grouping_mode).toContain("TRUE");
    expect(rep.harvest_matched_records).toBe(2);
    const md = readFileSync(join(out, "md", readdirSync(join(out, "md"))[0]!), "utf-8");
    expect(md.indexOf("hello")).toBeLessThan(md.indexOf("second"));
  });

  it("splits provisional groups on a >30min gap and a gem change", () => {
    const recs = [
      { verb: "Prompted", prompt: "a", response_md: "x", timestamp_iso: "2026-01-01T10:00:00", gem: null },
      { verb: "Prompted", prompt: "b", response_md: "x", timestamp_iso: "2026-01-01T10:05:00", gem: null },
      { verb: "Prompted", prompt: "c", response_md: "x", timestamp_iso: "2026-01-01T13:00:00", gem: null },
      { verb: "Prompted", prompt: "d", response_md: "x", timestamp_iso: "2026-01-01T13:01:00", gem: "G" },
      { verb: "Prompted", prompt: "e", response_md: "x", timestamp_iso: "not-a-date", gem: "G" },
    ];
    const src = write(join(root, "t.json"), recs);
    const out = join(root, "out");
    expect(main(["gemini", src, out])).toBe(0);
    expect(Number(report(out).rendered)).toBeGreaterThanOrEqual(3);
  });

  it("reports a malformed transcript and a malformed harvest", () => {
    write(join(root, "t.json"), "{bad");
    const out = join(root, "out");
    expect(main(["gemini", join(root, "t.json"), out])).toBe(0);
    expect((report(out).errors as Array<{ stage: string }>).some((e) => e.stage === "parse")).toBe(true);

    const src = write(join(root, "t2.json"), geminiRecords());
    write(join(root, "h.json"), "{bad");
    const out2 = join(root, "out2");
    expect(main(["gemini", src, out2, "--harvest", join(root, "h.json")])).toBe(0);
    expect((report(out2).errors as Array<{ stage: string }>).some((e) => e.stage === "parse")).toBe(true);
  });

  it("harvest grouping reports unmatched leftovers", () => {
    const src = write(join(root, "t.json"), [
      { verb: "Prompted", prompt: "matched", response_md: "x" },
      { verb: "Prompted", prompt: "orphan", response_md: "x" },
    ]);
    const harvest = write(join(root, "h.json"), [{ id: "g", title: "T", turns: [{ role: "assistant", text: "matched" }, { role: "user", text: "matched" }] }]);
    const out = join(root, "out");
    expect(main(["gemini", src, out, "--harvest", harvest])).toBe(0);
    // 2 conversations: the matched group + the unmatched-leftovers group
    expect(readdirSync(join(out, "html")).length).toBe(2);
  });
});

describe("fidelity report", () => {
  it("records a fidelity failure in the report", () => {
    const spy = vi.spyOn(verifyModule, "verify").mockReturnValue({
      ok: false, coverage: 0.5, missing_tokens: ["gone"],
    });
    const src = write(join(root, "claude.json"), claudeExport());
    const out = join(root, "out");
    expect(main(["claude", src, out])).toBe(0);
    const rep = report(out);
    expect(Number(rep.fidelity_passed)).toBe(0);
    expect((rep.failed as Array<{ coverage: number }>)[0]!.coverage).toBe(0.5);
    spy.mockRestore();
  });

  it("isolates a per-conversation render failure", () => {
    const spy = vi.spyOn(renderHtmlModule, "renderConversationHtml").mockImplementation(() => {
      throw new Error("synthetic render failure");
    });
    const src = write(join(root, "claude.json"), claudeExport());
    const out = join(root, "out");
    expect(main(["claude", src, out])).toBe(0);
    expect((report(out).errors as Array<{ stage: string }>).some((e) => e.stage === "render")).toBe(true);
    spy.mockRestore();
  });

  it("counts a hidden-char conversation and reports coverage", () => {
    const src = write(join(root, "claude.json"), (() => {
      const e = claudeExport();
      e[0]!.chat_messages[0]!.content[0]!.text = "a​b hidden";
      return e;
    })());
    const out = join(root, "out");
    expect(main(["claude", src, out])).toBe(0);
    expect(Number(report(out).hidden_char_conversations)).toBe(1);
    expect(existsSync(join(out, "_hidden-char-audit.json"))).toBe(true);
  });
});

describe("module entry point", () => {
  // The path shapes below are the whole point. `npm install` on Linux/macOS links a bin as
  // node_modules/.bin/<name> -> ../<pkg>/dist/cli.js, so process.argv[1] is the SYMLINK.
  // On Windows npm writes a .cmd shim that calls `node ...\dist\cli.js` instead, so argv[1]
  // is the real file. A check that pattern-matches the argv[1] STRING therefore passes on
  // Windows and silently does nothing on Linux — the CLI exits 0 having run no command.
  // These cases pin both shapes so that asymmetry cannot come back.
  const at = (p: string): string => resolvePath(p);
  const urlOf = (p: string): string => pathToFileURL(p).href;

  it("treats the npm bin SYMLINK as the entry point", () => {
    const real = at("/pkg/node_modules/llm-anthology/dist/cli.js");
    const link = at("/pkg/node_modules/.bin/llm-anthology");
    // the resolver is what makes a symlink and its target the same file
    const resolve = (p: string): string => (p === link ? real : p);
    expect(isEntryPoint(link, urlOf(real), resolve)).toBe(true);
  });

  it("treats a direct `node dist/cli.js` invocation as the entry point", () => {
    const real = at("/pkg/dist/cli.js");
    expect(isEntryPoint(real, urlOf(real), (p) => p)).toBe(true);
  });

  it("is NOT the entry point when imported as a library by another program", () => {
    const real = at("/pkg/dist/cli.js");
    expect(isEntryPoint(at("/usr/bin/vitest"), urlOf(real), (p) => p)).toBe(false);
  });

  it("is NOT the entry point when the process has no argv[1] at all", () => {
    expect(isEntryPoint(undefined, urlOf(at("/pkg/dist/cli.js")))).toBe(false);
  });

  it("does not throw when a path cannot be resolved on disk", () => {
    // The real resolver (no injection) must survive a path that does not exist — otherwise
    // merely importing the module could crash the importing program.
    expect(isEntryPoint(at("/definitely/not/here.js"), urlOf(at("/pkg/dist/cli.js"))))
      .toBe(false);
  });

  it("resolves REAL paths through the default resolver", () => {
    // No injected resolver: this drives realpathSync against files that actually exist,
    // which is the branch the injected-resolver cases above deliberately skip.
    const self = fileURLToPath(new URL("../src/cli.ts", import.meta.url));
    expect(isEntryPoint(self, urlOf(self))).toBe(true);
  });

  it("runs main and exits when the process entry point IS this module", async () => {
    const originalArgv = process.argv;
    const exit = vi.spyOn(process, "exit").mockImplementation(() => undefined as never);
    try {
      // the honest spelling: argv[1] is the module's own path, as it is in a real run
      process.argv = [
        originalArgv[0]!, fileURLToPath(new URL("../src/cli.ts", import.meta.url)), "--help",
      ];
      vi.resetModules();
      await import("../src/cli.js");
      expect(exit).toHaveBeenCalledWith(0);
    } finally {
      process.argv = originalArgv;
    }
  });
});
