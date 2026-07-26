#!/usr/bin/env node
/**
 * `llm-anthology` — render ChatGPT / Claude / Gemini session exports to faithful HTML + Markdown.
 *
 * Fully local and offline: this tool never opens a network connection. Mirrors the
 * Python `llm-anthology` CLI (llm_anthology/cli.py + loaders.py + build.py) so the two rails behave
 * identically. Nothing here is on the byte-for-byte-parity-tested renderer path; the
 * heavy lifting is delegated to the same ported modules the tests cover.
 */
import { mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";

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
  llm-anthology chatgpt  <conversations.json> <out_dir> [--projects FILE]
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
  const report: Report = {
    conversations: n,
    rendered: index.length,
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
    const outAbs = join(outDir);
    files = files.filter((f) => !f.startsWith(outAbs));
  }
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

function loadChatgpt(mainPath: string, projectsPath?: string): [Conversation[], LoadError[], Map<string, string>] {
  const errors: LoadError[] = [];
  const byId = new Map<string, Record<string, unknown>>();
  const projOf = new Map<string, string>();
  for (const path of [mainPath, projectsPath].filter((p): p is string => Boolean(p && statSync(p, { throwIfNoEntry: false })?.isFile()))) {
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

export function main(argv: string[]): number {
  const [cmd, ...rest] = argv;
  if (!cmd || cmd === "-h" || cmd === "--help") {
    console.log(USAGE);
    return cmd ? 0 : 2;
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
  return 0;
}

const invoked = process.argv[1] && /llm_anthology|cli\.js/.test(process.argv[1]);
if (invoked) process.exit(main(process.argv.slice(2)));
