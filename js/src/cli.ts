#!/usr/bin/env node
/**
 * `llm-anthology` — render ChatGPT / Claude / Gemini session exports to faithful HTML + Markdown.
 *
 * Fully local and offline: this tool never opens a network connection. Ported from the
 * Python `llm-anthology` CLI (llm_anthology/cli.py + loaders.py + build.py).
 *
 * WHAT PARITY ACTUALLY MEANS HERE. For the four commands this rail implements — claude,
 * chatgpt, gemini, demo — the loader, report and exit-code behaviour matches the python
 * rail and is pinned by test/cli.test.ts. It is NOT true that the two CLIs behave
 * identically overall, and an earlier version of this comment said so; these are the
 * measured residuals, none of which this file closes:
 *
 *   * python has two subcommands this rail does not: `codex` (a third export shape, whose
 *     absence matters because feeding codex.json to `chatgpt` yields zero conversations
 *     silently) and `index` (the SQLite corpus the cockpit reads). cli.py:47,62.
 *
 * `chatgpt <dir>` USED to be on that list — a real sharded Data Export directory loaded
 * NOTHING here and still exited 0, against the python rail's 2 of 2 on the same input.
 * `chatgptFiles` below now mirrors loaders.py:120-135, and test/cli.test.ts pins the same
 * cases as tests/test_coverage_paths.py:55-78.
 *
 * Nothing here is on the byte-for-byte-parity-tested renderer path; the heavy lifting is
 * delegated to the same ported modules the tests cover.
 */
import { mkdirSync, readdirSync, readFileSync, realpathSync, statSync, writeFileSync } from "node:fs";
import { basename, dirname, join, resolve as resolvePath, sep } from "node:path";
import { fileURLToPath } from "node:url";

import * as chatgpt from "./adapters/chatgpt.js";
import * as claude from "./adapters/claude.js";
import { demoConversation } from "./demo.js";
import * as gemini from "./adapters/gemini.js";
import { hiddenCharHits } from "./audit.js";
import type { Conversation } from "./ir.js";
import { renderConversationHtml } from "./render_html.js";
import { renderConversationMd } from "./render_md.js";
import { escapeHtml, neutralizeHtml, sanitizeForCopy } from "./sanitize.js";
import { verify } from "./verify.js";

const USAGE = `llm_anthology — render ChatGPT / Claude / Gemini session exports to HTML + Markdown (offline).

  llm-anthology claude   <export.json | dir>  <out_dir>
  llm-anthology chatgpt  <conversations.json | export dir> <out_dir> [--projects FILE|dir]
  llm-anthology gemini   <transcript.json>    <out_dir> [--harvest FILE]
  llm-anthology demo     <out.html>
`;

const ILLEGAL = /[<>:"/\\|?*\x00-\x1f]/g;
const WS = /\s+/g;

const THEMES: Record<string, { title: string; bg: string; fg: string; link: string; muted: string }> = {
  claude: { title: "Claude sessions", bg: "#1f1e1b", fg: "#f4f3ee", link: "#d97757", muted: "#b4b0a4" },
  chatgpt: { title: "ChatGPT sessions", bg: "#212121", fg: "#ececec", link: "#7ab7ff", muted: "#a0a0a0" },
  gemini: { title: "Gemini sessions", bg: "#1e1f20", fg: "#e3e3e3", link: "#8ab4f8", muted: "#9aa0a6" },
};
const FALLBACK_THEME = { title: "Sessions", bg: "#1b1b1b", fg: "#ededed", link: "#7ab7ff", muted: "#999999" };

function safeName(title: string | undefined, idx: number): string {
  let base = sanitizeForCopy(title || "untitled").replace(ILLEGAL, " ");
  base = base.replace(WS, " ").trim().slice(0, 60).replace(/[. ]+$/, "") || "untitled";
  return `${String(idx).padStart(3, "0")}-${base}`;
}

function writeText(path: string, text: string): void {
  writeFileSync(path, text, { encoding: "utf-8" });
}

function loadJson(path: string): unknown {
  return JSON.parse(readFileSync(path, "utf-8"));
}

interface LoadError {
  file: string;
  stage: string;
  error: string;
}

interface Report {
  conversations: number;
  rendered: number;
  turns: number;
  empty_conversations: number;
  fidelity_passed: number;
  failed: unknown[];
  errors: LoadError[];
  hidden_char_conversations: number;
  out_dir: string;
  [k: string]: unknown;
}

function renderCorpus(
  convs: Conversation[],
  outDir: string,
  provider: string,
  metaOf: (c: Conversation) => string,
  loadErrors: LoadError[],
  extra: Record<string, unknown> = {},
): Report {
  const theme = THEMES[provider] ?? FALLBACK_THEME;
  const htmlDir = join(outDir, "html");
  const mdDir = join(outDir, "md");
  mkdirSync(htmlDir, { recursive: true });
  mkdirSync(mdDir, { recursive: true });

  const index: Array<[string, string, number, string]> = [];
  const auditRows: unknown[] = [];
  const failed: unknown[] = [];
  const errors = [...loadErrors];
  let n = 0;
  for (const conv of convs) {
    n += 1;
    const name = safeName(conv.title, n);
    try {
      const hits = hiddenCharHits(conv);
      if (hits.length) {
        auditRows.push({
          file: name + ".html",
          title: conv.title,
          hidden_char_count: hits.length,
          codepoints: [...new Set(hits)].sort(),
        });
      }
      const html = renderConversationHtml(conv);
      const v = verify(conv, html);
      if (!v.ok) {
        failed.push({
          file: name + ".html",
          coverage: Math.round(v.coverage * 1e4) / 1e4,
          missing_sample: v.missing_tokens.slice(0, 20),
        });
      }
      writeText(join(htmlDir, name + ".html"), html);
      writeText(join(mdDir, name + ".md"), renderConversationMd(conv));
      index.push([name, conv.title, conv.turns.length, metaOf(conv)]);
    } catch (e) {
      errors.push({ file: name, stage: "render", error: String(e) });
    }
  }

  writeIndex(outDir, index, theme);
  writeText(join(outDir, "_hidden-char-audit.json"), JSON.stringify(auditRows, null, 2));
  // A conversation that parses cleanly but carries NO turns is a silent false-success:
  // feeding a Codex-shaped export to the ChatGPT loader yields 1 conversation / 0 turns /
  // 0 errors, so real content vanishes while the caller sees a clean load. Count turns so
  // the caller — and main()'s exit code — can tell "rendered" from "rendered something".
  // Mirrors build.py:112-118, field order included.
  const turns = index.reduce((sum, row) => sum + row[2], 0);
  const empty = index.filter((row) => row[2] === 0).length;
  const report: Report = {
    conversations: n,
    rendered: index.length,
    turns,
    empty_conversations: empty,
    fidelity_passed: index.length - failed.length,
    failed,
    errors,
    hidden_char_conversations: auditRows.length,
    out_dir: outDir,
    ...extra,
  };
  writeText(join(outDir, "_fidelity-report.json"), JSON.stringify(report, null, 2));
  return report;
}

function writeIndex(
  outDir: string,
  index: Array<[string, string, number, string]>,
  theme: { title: string; bg: string; fg: string; link: string; muted: string },
): void {
  const rows = index
    .map(
      ([name, title, turns, meta]) =>
        `<li><a href="html/${escapeHtml(name, true)}.html">${neutralizeHtml(title)}</a> ` +
        `<span class="muted">· ${turns} turns${meta ? " · " + escapeHtml(String(meta), true) : ""}</span></li>`,
    )
    .join("");
  const doc =
    '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
    `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">` +
    `<title>${escapeHtml(theme.title, true)}</title><style>` +
    `body{background:${theme.bg};color:${theme.fg};font-family:-apple-system,Segoe UI,sans-serif;max-width:820px;margin:0 auto;padding:32px 20px}` +
    `a{color:${theme.link};text-decoration:none}li{margin:6px 0;line-height:1.5}.muted{color:${theme.muted};font-size:.85em}` +
    `</style></head><body><h1>${escapeHtml(theme.title, true)} (${index.length})</h1><ul>${rows}</ul></body></html>`;
  writeText(join(outDir, "index.html"), doc);
}

function printReport(r: Report): void {
  console.log("CONVERSATIONS_RENDERED", r.rendered, "of", r.conversations);
  console.log("TURNS_RENDERED", r.turns);
  if (r.empty_conversations) {
    // loud, because the usual cause is a wrong-provider or drifted export -- a case that
    // otherwise renders blank pages and reports success
    console.log(
      "EMPTY_CONVERSATIONS",
      r.empty_conversations,
      "(no turns parsed - wrong provider, or the export format changed)",
    );
  }
  console.log("FIDELITY_GATE_PASSED", r.fidelity_passed, "of", r.rendered);
  console.log("HIDDEN_CHAR_CONVERSATIONS", r.hidden_char_conversations);
  console.log("ERRORS", r.errors.length);
  console.log("OUT_DIR", r.out_dir);
}

// ------------------------------------------------------------------ loaders

function loadClaude(src: string, outDir: string): [Conversation[], LoadError[]] {
  const convs: Conversation[] = [];
  const errors: LoadError[] = [];
  let files: string[];
  if (statSync(src).isFile()) {
    files = [src];
  } else {
    // readdirSync(recursive) rather than fs.globSync: the latter only stabilised in
    // Node 22, so it crashes on the Node 20 we support. Sorted the same way Python's
    // sorted(glob(...)) is, so the conversation numbering matches across the two rails.
    const all = readdirSync(src, { recursive: true })
      .filter((p): p is string => typeof p === "string" && p.endsWith(".json"))
      .map((p) => join(src, p))
      .sort();
    const conv = all.filter((f) => basename(f) === "conversations.json");
    const design = all.filter((f) => basename(dirname(f)) === "design_chats");
    files = conv.length || design.length ? [...conv, ...design] : all;
  }
  // Never ingest our own output — the site dir often lives inside the source dir. 1:1 with
  // loaders.py:63-66, and every part of that line is load-bearing:
  //
  //   * `resolvePath` on BOTH sides (python's os.path.abspath). Comparing the two argv
  //     spellings raw makes the filter a no-op whenever they differ: `claude <abs>/export
  //     export/site` left an absolute file list against a relative prefix, nothing matched,
  //     and the second run happily re-ingested the `_fidelity-report.json` the first wrote.
  //   * `+ sep`. Without the trailing separator the prefix test also matches a SIBLING whose
  //     name merely begins with out_dir's: out_dir `export/site` swallowed a real
  //     `export/site-backup/conversations.json`, so the corpus rendered 0 of 0, ERRORS 0,
  //     exit 0 — a whole export lost with nothing on stdout to say so.
  //   * placement AFTER the isFile/else split, not inside the else. Python filters both
  //     branches, so naming one file inside out_dir drops it there; doing it in the
  //     directory branch only re-rendered that file on this rail.
  //
  // No `if (outDir)` guard, unlike python's: outDir cannot be empty here — main() returns 2
  // before calling this when either positional is missing.
  const outAbs = resolvePath(outDir) + sep;
  files = files.filter((f) => !resolvePath(f).startsWith(outAbs));
  for (const f of files) {
    let data: unknown;
    try {
      data = loadJson(f);
    } catch (e) {
      errors.push({ file: basename(f), stage: "parse", error: String(e) });
      continue;
    }
    try {
      if (claude.isDesignChat(data)) convs.push(claude.parseDesignChat(data as Record<string, unknown>));
      else convs.push(...claude.parseExport(data));
    } catch (e) {
      errors.push({ file: basename(f), stage: "adapt", error: String(e) });
    }
  }
  return [convs, errors];
}

function cid(c: Record<string, unknown>): string {
  const v = c["conversation_id"] ?? c["id"] ?? "";
  return typeof v === "string" ? v : "";
}

/** fnmatch `conversations-*.json`, anchored — `*` never crosses a path separator. */
const CHATGPT_SHARD = /^conversations-.*\.json$/;

/**
 * Resolve ONE cli argument to the export files it stands for. 1:1 with
 * `_chatgpt_files` (loaders.py:120-135), and every part of that mapping is load-bearing:
 *
 *   * A real ChatGPT Data Export ships the corpus SHARDED as conversations-000.json …
 *     conversations-NNN.json (17 shards / 1613 conversations in the observed export), so
 *     a DIRECTORY must contribute every shard. The previous code filtered both positionals
 *     through `statSync(p).isFile()`, which dropped a directory silently: measured,
 *     `chatgpt <dir>` printed CONVERSATIONS_RENDERED 0 of 0 / ERRORS 0 and exited 0 while
 *     the python rail rendered 2 of 2 from the same directory. Nothing on stdout said the
 *     whole export had been discarded.
 *   * Shards FIRST (sorted), then `conversations.json`. Order decides dedup: load_chatgpt
 *     keeps the FIRST record for an id, so swapping these swaps which copy of a duplicated
 *     conversation wins.
 *   * NOT recursive, unlike loadClaude's `**` globs — python uses a single-segment
 *     glob.glob here, so a shard one level down is deliberately NOT picked up.
 *   * No file/directory test on the matched names, also matching python: glob.glob returns
 *     a DIRECTORY named `conversations-x.json` too, and both rails then fail it as a
 *     `parse` error rather than skipping it silently.
 *
 * Residual divergence, INLINE because it is real: python's glob matches through fnmatch,
 * which normcases on Windows, so a shard spelled `Conversations-000.JSON` would be picked
 * up by the python rail on Windows and by neither rail on Linux. This regex is
 * case-SENSITIVE on every platform. UNVERIFIED whether any export ships a non-lowercase
 * shard name — the observed export is all-lowercase; the settling experiment is a
 * directory listing of a real ChatGPT Data Export.
 */
function chatgptFiles(p: string): string[] {
  const st = statSync(p, { throwIfNoEntry: false });
  if (!st) return [];
  if (!st.isDirectory()) return st.isFile() ? [p] : [];
  // readdirSync + a name filter rather than fs.globSync: the latter only stabilised in
  // Node 22 and crashes on the Node 20 this package supports. Sorting names is equivalent
  // to python's sorted(glob(...)) over full paths — the directory prefix is common to all.
  const files = readdirSync(p)
    .filter((n) => CHATGPT_SHARD.test(n))
    .sort()
    .map((n) => join(p, n));
  const single = join(p, "conversations.json");
  if (statSync(single, { throwIfNoEntry: false })?.isFile()) files.push(single);
  return files;
}

function loadChatgpt(mainPath: string, projectsPath?: string): [Conversation[], LoadError[], Map<string, string>] {
  const errors: LoadError[] = [];
  const byId = new Map<string, Record<string, unknown>>();
  const projOf = new Map<string, string>();
  const paths = [mainPath, projectsPath].flatMap((p) => (p ? chatgptFiles(p) : []));
  for (const path of paths) {
    let raw: unknown;
    try {
      raw = loadJson(path);
    } catch (e) {
      errors.push({ file: basename(path), stage: "parse", error: String(e) });
      continue;
    }
    const list = Array.isArray(raw) ? raw : [raw];
    for (const c of list) {
      if (c === null || typeof c !== "object") continue;
      const rec = c as Record<string, unknown>;
      const id = cid(rec);
      if (!id) continue;
      if (!byId.has(id)) byId.set(id, rec);
      if (rec["__project_id"]) projOf.set(id, String(rec["__project_id"]));
    }
  }
  const convs: Conversation[] = [];
  for (const rec of byId.values()) {
    try {
      convs.push(chatgpt.parseConversation(rec));
    } catch (e) {
      errors.push({ file: cid(rec), stage: "adapt", error: String(e) });
    }
  }
  return [convs, errors, projOf];
}

const GAP_MS = 30 * 60 * 1000;

function gnorm(s: unknown): string {
  return (typeof s === "string" ? s : "").replace(/ /g, " ").replace(WS, " ").trim().toLowerCase();
}

function geminiGroupsFromHarvest(records: Record<string, unknown>[], harvest: unknown[]): [gemini.Group[], number] {
  const byPrompt = new Map<string, number[]>();
  records.forEach((r, i) => {
    const key = gnorm(r["prompt"]);
    if (key) (byPrompt.get(key) ?? byPrompt.set(key, []).get(key)!).push(i);
  });
  const groups: gemini.Group[] = [];
  const claimed = new Set<number>();
  for (const conv of harvest) {
    const idxs: number[] = [];
    for (const t of (((conv as Record<string, unknown>)["turns"] as unknown[]) ?? [])) {
      const role = String((t as Record<string, unknown>)["role"] ?? "").toLowerCase();
      if (role !== "user" && role !== "human") continue;
      for (const i of byPrompt.get(gnorm((t as Record<string, unknown>)["text"])) ?? []) {
        if (!claimed.has(i)) {
          claimed.add(i);
          idxs.push(i);
          break;
        }
      }
    }
    if (idxs.length) {
      const c = conv as Record<string, unknown>;
      groups.push({ id: String(c["id"] ?? ""), title: String(c["title"] ?? "(untitled)"), turn_idxs: idxs.sort((a, b) => a - b) });
    }
  }
  const leftovers = records.map((_, i) => i).filter((i) => !claimed.has(i));
  if (leftovers.length) groups.push({ id: "unmatched", title: "(unmatched Takeout activity)", turn_idxs: leftovers });
  return [groups, claimed.size];
}

function geminiGroupsFromGaps(records: Record<string, unknown>[]): gemini.Group[] {
  const groups: number[][] = [];
  let cur: number[] = [];
  let prevTs: number | null = null;
  let prevGem: unknown = null;
  records.forEach((r, i) => {
    const t = Date.parse(String(r["timestamp_iso"] ?? ""));
    const ts = Number.isNaN(t) ? null : t;
    const gem = r["gem"] ?? null;
    if (cur.length && ((prevTs !== null && ts !== null && ts - prevTs > GAP_MS) || gem !== prevGem)) {
      groups.push(cur);
      cur = [];
    }
    cur.push(i);
    prevTs = ts ?? prevTs;
    prevGem = gem;
  });
  if (cur.length) groups.push(cur);
  return groups.map((g, n) => ({ id: `grp${String(n + 1).padStart(3, "0")}`, title: `(provisional group ${n + 1})`, turn_idxs: g }));
}

function loadGemini(transcriptPath: string, harvestPath?: string): [Conversation[], LoadError[], Record<string, unknown>] {
  let records: Record<string, unknown>[];
  try {
    records = loadJson(transcriptPath) as Record<string, unknown>[];
  } catch (e) {
    return [[], [{ file: basename(transcriptPath), stage: "parse", error: String(e) }], {}];
  }
  let mode = "gap-heuristic (PROVISIONAL)";
  let matched = 0;
  let groups: gemini.Group[];
  if (harvestPath && statSync(harvestPath, { throwIfNoEntry: false })?.isFile()) {
    try {
      [groups, matched] = geminiGroupsFromHarvest(records, loadJson(harvestPath) as unknown[]);
      mode = "harvest (TRUE grouping)";
    } catch (e) {
      return [[], [{ file: basename(harvestPath), stage: "parse", error: String(e) }], {}];
    }
  } else {
    groups = geminiGroupsFromGaps(records);
  }
  return [gemini.parseAll(records, groups), [], { grouping_mode: mode, harvest_matched_records: matched, source_records: records.length }];
}

// ------------------------------------------------------------------ main

function flag(args: string[], name: string): string | undefined {
  const i = args.indexOf(name);
  return i >= 0 && i + 1 < args.length ? args[i + 1] : undefined;
}

/**
 * The version this build reports, read from the package.json that ships beside it.
 *
 * Resolved relative to THIS module rather than to cwd, so it is the version of the
 * installed package and not of whatever directory the user happens to be standing in.
 * The path works in both layouts: from `dist/cli.js` it resolves to the tarball root,
 * and from `src/cli.ts` under vitest it resolves to `js/package.json`. Reading the file
 * directly rather than importing it is deliberate — `exports` does not expose
 * `./package.json`, so a bare import would throw ERR_PACKAGE_PATH_NOT_EXPORTED.
 */
export function packageVersion(): string {
  const here = dirname(fileURLToPath(import.meta.url));
  const pkg = JSON.parse(readFileSync(join(here, "..", "package.json"), "utf8")) as { version: string };
  return pkg.version;
}

export function main(argv: string[]): number {
  const [cmd, ...rest] = argv;
  if (!cmd || cmd === "-h" || cmd === "--help") {
    console.log(USAGE);
    return cmd ? 0 : 2;
  }

  // Matches the python rail's `--version` exactly, output shape included, because
  // parity between the two CLIs is the contract this file is tested against. Python
  // uses argparse `action="version"`, which also wins before any subcommand is
  // resolved — hence this check sits above the command dispatch rather than inside it.
  if (cmd === "--version") {
    console.log(`llm-anthology ${packageVersion()}`);
    return 0;
  }

  if (cmd === "demo") {
    const out = rest[0];
    if (!out) {
      console.log(USAGE);
      return 2;
    }
    mkdirSync(dirname(out) || ".", { recursive: true });
    writeText(out, renderConversationHtml(demoConversation()));
    console.log("DEMO_WRITTEN", out);
    return 0;
  }

  const src = rest[0];
  const outDir = rest[1];
  if (!src || !outDir) {
    console.log(USAGE);
    return 2;
  }
  if (!statSync(src, { throwIfNoEntry: false })) {
    console.error(`ERROR: no such file or directory: ${src}`);
    return 1;
  }

  let report: Report;
  if (cmd === "claude") {
    const [convs, errors] = loadClaude(src, outDir);
    report = renderCorpus(convs, outDir, "claude", (c) => c.account || "", errors);
  } else if (cmd === "chatgpt") {
    const [convs, errors, projOf] = loadChatgpt(src, flag(rest, "--projects"));
    report = renderCorpus(convs, outDir, "chatgpt", (c) => projOf.get(c.id) ?? "", errors);
  } else if (cmd === "gemini") {
    const [convs, errors, extra] = loadGemini(src, flag(rest, "--harvest"));
    report = renderCorpus(convs, outDir, "gemini", () => "", errors, extra);
  } else {
    console.log(USAGE);
    return 2;
  }
  printReport(report);
  // Exit 3 = "loaded, but produced nothing usable" (cli.py:194-195). Returning 0 here made a
  // wrong-provider or drifted export indistinguishable from a good run: the corpus rendered
  // blank pages and every automated caller saw success. A genuinely empty input (0
  // conversations) is not an error -- there was nothing to lose. Content that went in and
  // did not come out is.
  if (report.conversations > 0 && report.turns === 0) return 3;
  return 0;
}

/**
 * A path in the form two `import.meta.url`-vs-`argv[1]` comparisons can agree on.
 *
 * `realpathSync.native` rather than `realpathSync`: on Windows only the native form expands
 * an 8.3 short name (`PREKZU~1`), and `os.tmpdir()` hands out short paths — so the plain
 * form can leave two spellings of one file looking different. A path that does not exist
 * cannot be canonicalised at all (that is not an error here: `argv[1]` may be anything),
 * so it falls back to a lexical resolve and simply fails to match.
 */
function canonicalPath(p: string): string {
  try {
    return realpathSync.native(p);
  } catch {
    return resolvePath(p);
  }
}

/**
 * Is this module the program the user actually started?
 *
 * This MUST compare resolved paths, not path spellings. The previous check was
 * `/llm_anthology|cli\.js/.test(process.argv[1])`, and it silently broke the published
 * package on every non-Windows machine:
 *
 *   * `npm install` on Linux/macOS links a bin as `node_modules/.bin/llm-anthology ->
 *     ../llm-anthology/dist/cli.js`, so `argv[1]` is the SYMLINK — a string containing
 *     neither `cli.js` nor `llm_anthology` (note the underscore: that is the PYTHON
 *     package's name, never the npm bin's). The test returned false, `main()` never ran,
 *     and the CLI exited 0 having done nothing at all — no output, no error, no file.
 *   * On Windows npm writes a `.cmd` shim that runs `node ...\dist\cli.js`, so `argv[1]`
 *     is the real file and the pattern matched. The bug was therefore invisible to every
 *     local Windows check and only ever showed up in Linux CI.
 *
 * `resolve` is injected so the decision is testable against both installation shapes
 * without creating real symlinks (which need elevation on Windows).
 */
export function isEntryPoint(
  argv1: string | undefined,
  moduleUrl: string,
  resolve: (p: string) => string = canonicalPath,
): boolean {
  if (!argv1) return false;
  return resolve(argv1) === resolve(fileURLToPath(moduleUrl));
}

if (isEntryPoint(process.argv[1], import.meta.url)) process.exit(main(process.argv.slice(2)));
