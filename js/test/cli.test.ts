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

// ---------------------------------------------------------- chatgpt: shard selection
//
// A real ChatGPT Data Export ships the corpus SHARDED as conversations-000.json ...
// conversations-NNN.json (17 shards / 1613 conversations in the observed export), so a
// DIRECTORY argument must contribute every shard. This rail used to filter both
// positionals through `statSync(p).isFile()`, which DROPPED a directory outright: the
// measured symptom was `chatgpt <dir>` -> CONVERSATIONS_RENDERED 0 of 0, ERRORS 0,
// exit 0 against the python rail's 2 of 2 on the same input, with `chatgpt <one shard>`
// -> 1 of 1 as the control proving the loader itself was fine.
//
// These mirror tests/test_coverage_paths.py:55-78 one-for-one so the two rails'
// file-selection semantics are pinned to the same cases, and add the two paths python
// reaches through the SAME helper that no python test names: the --projects positional
// (`_chatgpt_files` is applied to both) and a directory holding no export at all.
// a real 2-turn conversation, id-keyed. NOT the zero-turn `{mapping:{}}` shape the python
// test uses: this rail exits 3 on "conversations in, zero turns out", so a zero-turn fixture
// would assert exit 0 against a legitimate exit 3 and hide the shard bug behind it.
const cgConv = (id: string) => ({ ...chatgptExport()[0], conversation_id: id, title: id });

describe("chatgpt shard selection (loaders.py:120-135 parity)", () => {
  // the rendered filename is `NNN-<title>`, and these fixtures set title === id, so the
  // output directory listing IS the set of conversation ids that survived loading
  const ids = (out: string): string[] =>
    readdirSync(join(out, "html"))
      .map((f) => f.replace(/^\d+-/, "").replace(/\.html$/, ""))
      .sort();

  it("reads every shard in an export directory, ignoring non-export json", () => {
    const dir = join(root, "export");
    mkdirSync(dir, { recursive: true });
    write(join(dir, "conversations-000.json"), [cgConv("a")]);
    write(join(dir, "conversations-001.json"), [cgConv("b")]);
    write(join(dir, "not-a-conversation.json"), [cgConv("zz")]);
    const out = join(root, "out");
    expect(main(["chatgpt", dir, out])).toBe(0);
    const rep = report(out);
    expect(ids(out)).toEqual(["a", "b"]);
    expect(rep.errors).toEqual([]);
  });

  it("a directory also takes a plain conversations.json (older/renamed export)", () => {
    const dir = join(root, "export");
    mkdirSync(dir, { recursive: true });
    write(join(dir, "conversations.json"), [cgConv("solo")]);
    const out = join(root, "out");
    expect(main(["chatgpt", dir, out])).toBe(0);
    expect(ids(out)).toEqual(["solo"]);
    expect(report(out).errors).toEqual([]);
  });

  it("dedupes a conversation present in two shards", () => {
    const dir = join(root, "export");
    mkdirSync(dir, { recursive: true });
    write(join(dir, "conversations-000.json"), [cgConv("dup")]);
    write(join(dir, "conversations-001.json"), [cgConv("dup")]);
    const out = join(root, "out");
    expect(main(["chatgpt", dir, out])).toBe(0);
    expect(ids(out)).toEqual(["dup"]);
  });

  it("--projects may itself be a sharded directory", () => {
    const src = write(join(root, "cg.json"), [cgConv("main")]);
    const proj = join(root, "projects");
    mkdirSync(proj, { recursive: true });
    write(join(proj, "conversations-000.json"), [{ ...cgConv("p1"), __project_id: "g-p-1" }]);
    const out = join(root, "out");
    expect(main(["chatgpt", src, out, "--projects", proj])).toBe(0);
    expect(ids(out)).toEqual(["main", "p1"]);
    expect(readFileSync(join(out, "index.html"), "utf-8")).toContain("g-p-1");
  });

  it("a --projects path that does not exist is skipped, not a crash", () => {
    // python's _chatgpt_files returns [] for a path that is neither a dir nor a file, so a
    // typo'd --projects must degrade to "main export only" on both rails
    const src = write(join(root, "cg.json"), [cgConv("main")]);
    const out = join(root, "out");
    expect(main(["chatgpt", src, out, "--projects", join(root, "nope.json")])).toBe(0);
    expect(ids(out)).toEqual(["main"]);
    expect(report(out).errors).toEqual([]);
  });

  it("a directory holding no export contributes nothing and does not throw", () => {
    const dir = join(root, "empty");
    mkdirSync(dir, { recursive: true });
    write(join(dir, "user.json"), { id: "not-a-conversation" });
    const out = join(root, "out");
    expect(main(["chatgpt", dir, out])).toBe(0);
    expect(Number(report(out).conversations)).toBe(0);
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

describe("out_dir is never ingested", () => {
  // loaders.py:63-66 drops any input file under out_dir with
  //     out_abs = os.path.abspath(out_dir) + os.sep
  //     [f for f in files if not os.path.abspath(f).startswith(out_abs)]
  // and BOTH halves of that expression carry weight, so both are pinned below:
  //   * `+ os.sep` — without it a SIBLING directory whose name merely begins with
  //     out_dir's name ("export/site-backup" against out_dir "export/site") is dropped,
  //     and a real export silently renders as nothing at all;
  //   * `abspath` — without it the test compares two raw SPELLINGS, so an absolute src
  //     with a relative out_dir makes the filter a no-op and the rail re-ingests the
  //     `_fidelity-report.json` its own previous run wrote.
  // The filter also sits AFTER python's isfile/isdir split, so it covers a src that names
  // one file inside out_dir; cli.ts had it inside the directory branch only.
  const exportTree = (): string => {
    const exp = join(root, "export");
    mkdirSync(join(exp, "site-backup"), { recursive: true });
    write(join(exp, "site-backup", "conversations.json"), claudeExport());
    return exp;
  };

  it("keeps a real export whose directory merely PREFIXES out_dir", () => {
    const exp = exportTree();
    const out = join(exp, "site"); // …/export/site is a string prefix of …/export/site-backup
    expect(main(["claude", exp, out])).toBe(0);
    expect(Number(report(out).rendered)).toBe(1);
  });

  it("CONTROL: the same export with a non-prefixing out_dir", () => {
    const exp = exportTree();
    const out = join(root, "rendered");
    expect(main(["claude", exp, out])).toBe(0);
    expect(Number(report(out).rendered)).toBe(1);
  });

  it("never re-ingests its own output, however src and out_dir are spelled", () => {
    const exp = join(root, "export");
    mkdirSync(exp, { recursive: true });
    write(join(exp, "notes.json"), claudeExport()); // a renamed export → the *.json fallback
    const cwd = process.cwd();
    try {
      process.chdir(root);
      // src ABSOLUTE, out_dir RELATIVE — the mixed spelling is the whole point. Built from
      // process.cwd() post-chdir so both sides use the spelling the OS itself reports
      // (a Windows temp dir can be handed out in 8.3 short form, which neither
      // os.path.abspath nor path.resolve expands).
      const srcAbs = join(process.cwd(), "export");
      const outRel = join("export", "site");
      const outAbs = join(srcAbs, "site");
      expect(main(["claude", srcAbs, outRel])).toBe(0);
      expect(Number(report(outAbs).rendered)).toBe(1);
      // run 1 left _fidelity-report.json inside out_dir; run 2 must not read it back in
      expect(main(["claude", srcAbs, outRel])).toBe(0);
      expect(Number(report(outAbs).rendered)).toBe(1);
    } finally {
      process.chdir(cwd);
    }
  });

  it("drops a single-file src that lives inside out_dir", () => {
    const out = join(root, "site");
    mkdirSync(out, { recursive: true });
    const src = write(join(out, "conversations.json"), claudeExport());
    expect(main(["claude", src, out])).toBe(0);
    expect(Number(report(out).rendered)).toBe(0);
  });
});

describe("exit 3 — loaded, but produced nothing usable", () => {
  // cli.py:194-195 `if report["conversations"] and not report["turns"]: return 3`, over the
  // turns/empty_conversations counts build.py:117-118 puts in the report. Without them a
  // wrong-provider or drifted export renders blank pages and every scripted caller sees 0.
  it("returns 3 when a conversation loads but carries no turns", () => {
    // users.json is a file a REAL claude export directory ships (loaders.py:44-52); the
    // adapter wraps any object as one conversation, so this is the ordinary pipeline.
    const src = write(join(root, "users.json"), { uuid: "u", name: "users" });
    const out = join(root, "out");
    expect(main(["claude", src, out])).toBe(3);
    const rep = report(out);
    expect(Number(rep.conversations)).toBe(1);
    expect(Number(rep.turns)).toBe(0);
    expect(Number(rep.empty_conversations)).toBe(1);
  });

  it("a genuinely empty input is exit 0 — there was nothing to lose", () => {
    const src = write(join(root, "claude.json"), []);
    const out = join(root, "out");
    expect(main(["claude", src, out])).toBe(0);
    expect(Number(report(out).conversations)).toBe(0);
  });

  it("a PARTIALLY empty corpus still exits 0 and counts the empties", () => {
    const src = write(join(root, "cg.json"), chatgptExport());
    const proj = write(join(root, "proj.json"), [
      { conversation_id: "b", current_node: null, mapping: {} }, // parses, zero turns
    ]);
    const out = join(root, "out");
    expect(main(["chatgpt", src, out, "--projects", proj])).toBe(0);
    const rep = report(out);
    expect(Number(rep.turns)).toBe(2);
    expect(Number(rep.empty_conversations)).toBe(1);
  });

  it("prints TURNS_RENDERED always and EMPTY_CONVERSATIONS only when there are empties", () => {
    const lines = (): string =>
      vi.mocked(console.log).mock.calls.map((c) => c.join(" ")).join("\n");

    const good = write(join(root, "claude.json"), claudeExport());
    expect(main(["claude", good, join(root, "out")])).toBe(0);
    expect(lines()).toContain("TURNS_RENDERED 2");
    expect(lines()).not.toContain("EMPTY_CONVERSATIONS");

    vi.mocked(console.log).mockClear();
    const empty = write(join(root, "users.json"), { uuid: "u" });
    expect(main(["claude", empty, join(root, "out2")])).toBe(3);
    expect(lines()).toContain("TURNS_RENDERED 0");
    expect(lines()).toContain("EMPTY_CONVERSATIONS 1");
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
    // 3, not 0: the conversation loaded and nothing came out of it, which is exactly the
    // state cli.py:194-195 reserves exit 3 for. `errors` is populated but that is not what
    // decides the code — n counts the conversation, index stays empty, so turns is 0. This
    // expectation used to read 0 and that WAS the divergence: measured against the python
    // rail with render_conversation_html monkeypatched to raise, cli.main returns 3 with
    // conversations=1 rendered=0 turns=0 errors=1. Isolation itself is unchanged — one bad
    // record still costs only itself, which is what the errors assertion below pins.
    expect(main(["claude", src, out])).toBe(3);
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

describe("--version", () => {
  it("prints the package version and exits 0, matching the python rail", () => {
    // 0.1.0 shipped on both registries without this flag, so `llm-anthology --version`
    // printed usage instead of a version on BOTH rails. Parity is the point of this
    // file, so the two must agree on the exact output shape: `llm-anthology <semver>`.
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    try {
      expect(main(["--version"])).toBe(0);
      const printed = log.mock.calls.map((c) => String(c[0])).join("\n");
      const pkg = JSON.parse(
        readFileSync(resolvePath(fileURLToPath(import.meta.url), "..", "..", "package.json"), "utf8"),
      ) as { version: string };
      expect(printed.trim()).toBe(`llm-anthology ${pkg.version}`);
    } finally {
      log.mockRestore();
    }
  });

  it("wins over a following subcommand rather than trying to run it", () => {
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    try {
      expect(main(["--version", "claude", "nope", "nope"])).toBe(0);
      expect(log.mock.calls.map((c) => String(c[0])).join("\n")).toContain("llm-anthology ");
    } finally {
      log.mockRestore();
    }
  });
});
