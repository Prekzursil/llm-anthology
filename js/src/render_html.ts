/**
 * IR -> browser-faithful, self-contained static HTML (the "view" copy).
 * 1:1 with llm_anthology/render_html.py.
 *
 * - Text bodies -> HTML via markdown-it with html:false (raw HTML in a message is
 *   escaped, exactly as chatgpt.com / claude.ai / gemini render it).
 * - Links hardened: http/https only get a live href (+ rel=noopener); anything else
 *   is defanged to inert text.
 * - Hidden unicode -> visible inert badges (forensic), never the raw invisible.
 * - Zero remote fetch: theme CSS inlined, CSP default-src 'none', no scripts, and
 *   markdown images are defanged into labelled links rather than <img> loads.
 *
 * markdown-it is pinned to 14.3.0 EXACTLY and constructed with xhtmlOut:true,
 * because that is what markdown-it-py's "gfm-like" preset sets; without it the two
 * rails disagree on void-tag style (`<br />` vs `<br>`). A version bump must be
 * re-measured against the parity corpus before it lands.
 */
import MarkdownIt from "markdown-it";

import { loadTheme } from "./generated/themes.js";
import type { Block, Citation, Conversation, Turn } from "./ir.js";
import {
  badgeInvisibles,
  escapeHtml,
  isSafeUrl,
  ncrInvisibles,
  neutralizeHtml,
  sanitizeForCopy,
  isLocalMediaPath,
} from "./sanitize.js";

const MD = new MarkdownIt({
  html: false,
  linkify: false,
  typographer: false,
  xhtmlOut: true,
});

const A_ANY = /<a\s+([^>]*)>/gi;
const IMG = /<img\s+[^>]*src="([^"]*)"[^>]*>/gi;
const TAG_SPLIT = /(<[^>]*>)/;

const CSP =
  "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; " +
  "font-src 'self' data:; base-uri 'none'; form-action 'none'";

/** $$display$$ or $inline$ on a single line; held out of markdown escaping entirely */
const MATH = /\$\$[^\n]+?\$\$|\$[^\n$]+?\$/g;
const MATH_PH = "zMaThSpAnZ"; // plain-word sentinel: markdown leaves it untouched

/** Python's str(): a missing value stringifies to "None", not "undefined". */
function pyStr(v: unknown): string {
  if (v === null || v === undefined) return "None";
  return String(v);
}

function jsonDumps(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? "";
}

/**
 * Rewrite EVERY anchor, failing CLOSED.
 *
 * Matching only `<a href="…"` would leave an anchor with attributes in another
 * order, single-quoted, or unquoted with its raw href untouched. Nothing emits
 * those today, but a markdown-it upgrade or an attrs plugin would silently disable
 * the only href control. Match any anchor and drop what we cannot verify.
 */
export function hardenLinks(frag: string): string {
  return frag.replace(A_ANY, (_m, attrs: string) => {
    const href = /href\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))/i.exec(attrs);
    if (!href) return "<a>";
    const url = href[2] ?? href[3] ?? href[4] ?? "";
    // preserve the link title (sanitised) — dropping it would lose real content
    const tm = /title\s*=\s*("([^"]*)"|'([^']*)')/i.exec(attrs);
    const title = tm ? (tm[2] ?? tm[3] ?? "") : "";
    const titleAttr = title ? ` title="${ncrInvisibles(escapeHtml(title, true))}"` : "";
    if (isSafeUrl(url.replace(/&amp;/g, "&"))) {
      return (
        `<a href="${ncrInvisibles(escapeHtml(url, true))}"${titleAttr}` +
        ` rel="noopener noreferrer" target="_blank">`
      );
    }
    return `<a class="unsafe" data-unsafe-href="${escapeHtml(url, true)}"${titleAttr}>`;
  });
}

/**
 * Badge invisibles in TEXT nodes only.
 *
 * Badging the whole fragment injected a <span> INSIDE tags when an invisible sat in
 * an attribute (a link title, an image alt, a code-fence info string), corrupting
 * the markup and destroying the evidence at exactly that spot. Inside a tag, emit an
 * inert numeric character reference instead.
 */
function badgeTextNodes(frag: string): string {
  return frag
    .split(TAG_SPLIT)
    .map((part) =>
      part.startsWith("<") && part.endsWith(">") ? ncrInvisibles(part) : badgeInvisibles(part),
    )
    .join("");
}

/**
 * A markdown image would emit a REMOTE <img src>. CSP blocks the load, so it renders
 * as a broken image and quietly contradicts the no-egress promise. Replace it with a
 * chip naming the alt text plus an explicit link.
 */
export function defangImages(frag: string): string {
  return frag.replace(IMG, (tag: string, src: string) => {
    const altM = /alt="([^"]*)"/i.exec(tag);
    const alt = escapeHtml(altM ? altM[1]! : "image", false);
    if (isSafeUrl(src.replace(/&amp;/g, "&"))) {
      return (
        `<span class="chip">🖼 ${alt} <a href="${escapeHtml(src, true)}" ` +
        `rel="noopener noreferrer" target="_blank">(remote image — not fetched)</a></span>`
      );
    }
    return `<span class="chip">🖼 ${alt} (image unavailable)</span>`;
  });
}

/**
 * Markdown -> HTML, with math spans held OUT of the markdown pass.
 *
 * CommonMark strips a backslash before ASCII punctuation, so `$\{a\,b\}$` would
 * silently become `${a,b}$` — a mutation of the TeX that is unrecoverable and that a
 * token-based fidelity gate cannot detect. Stash math first, restore it verbatim
 * (escaped) afterwards. Unrendered-but-intact beats mutated.
 */
function mdToHtml(text: unknown): string {
  const src = typeof text === "string" ? text : "";
  const spans: string[] = [];
  const stashed = src.replace(MATH, (m) => {
    spans.push(m);
    return `${MATH_PH}${spans.length - 1}${MATH_PH}`;
  });

  let frag = MD.render(stashed);
  for (let i = 0; i < spans.length; i++) {
    const escaped = escapeHtml(spans[i]!, false);
    // A literal-string replacement would (a) only replace the FIRST hit and (b) let
    // `$&`/`$1` inside the TeX act as substitution patterns. Both are silent
    // corruption, so replace globally with a FUNCTION.
    frag = frag.replace(new RegExp(`${MATH_PH}${i}${MATH_PH}`, "g"), () => escaped);
  }
  return badgeTextNodes(hardenLinks(defangImages(frag)));
}

function pre(text: unknown): string {
  return `<pre>${badgeInvisibles(escapeHtml(typeof text === "string" ? text : "", false))}</pre>`;
}

function citationsHtml(cites: Citation[]): string {
  const pills: string[] = [];
  for (const c of cites) {
    if (c === null || typeof c !== "object") continue;
    const url = typeof c.url === "string" ? c.url : "";
    const label = neutralizeHtml(c.title || url);
    if (isSafeUrl(url)) {
      // href is an ATTRIBUTE: neutralise invisibles as inert numeric refs, never as
      // a badge span (that would break out of the attribute)
      pills.push(
        `<a href="${ncrInvisibles(escapeHtml(url, true))}" rel="noopener noreferrer" ` +
          `target="_blank">${label}</a>`,
      );
    } else {
      pills.push(`<span>${label}</span>`);
    }
  }
  return pills.length ? `<div class="cites">${pills.join("")}</div>` : "";
}

function blockHtml(b: Block): string {
  const d = b.data;
  const str = (k: string): string => (typeof d[k] === "string" ? (d[k] as string) : "");

  if (b.type === "text") {
    return `<div class="md">${mdToHtml(b.text)}</div>${citationsHtml(b.citations)}`;
  }

  if (b.type === "thinking") {
    // claude.ai may WITHHOLD this from the reader; surfacing it silently would
    // misrepresent what the conversation actually showed. Mark it.
    const label = d["hidden"] ? "Thought process (hidden in claude.ai)" : "Thought process";
    return (
      `<details class="thinking"><summary>${label}</summary>` +
      `<div class="md">${mdToHtml(b.text)}</div></details>`
    );
  }

  if (b.type === "tool_use") {
    const name = neutralizeHtml(str("name") || "tool");
    const inp = d["input"];
    const body = inp !== undefined && inp !== null ? jsonDumps(inp) : "";
    const disp = b.text ? `<div class="tool-disp">${neutralizeHtml(b.text)}</div>` : "";
    return `<div class="tool"><div class="tool-head">🔧 ${name}</div>${disp}${pre(body)}</div>`;
  }

  if (b.type === "tool_result") {
    const name = neutralizeHtml(str("name") || "tool");
    const content = d["content"];
    const body =
      typeof content === "string"
        ? content
        : content !== undefined && content !== null
          ? jsonDumps(content)
          : "";
    const err = Boolean(d["is_error"]);
    const head = err ? `⚠️ ${name}` : `↩️ ${name}`;
    const disp = b.text ? `<div class="tool-disp">${neutralizeHtml(b.text)}</div>` : "";
    return (
      `<div class="tool${err ? " err" : ""}"><div class="tool-head">${head}</div>` +
      `${disp}${pre(body)}</div>`
    );
  }

  if (b.type === "attachment") {
    let chip = `<span class="chip">📎 ${neutralizeHtml(str("file_name") || "attachment")}</span>`;
    // the uploaded document's TEXT is real content — it must not be dropped from the
    // faithful copy just because the bytes aren't in the export
    const extracted = d["extracted_content"];
    if (typeof extracted === "string" && extracted.trim()) {
      chip +=
        `<details class="thinking"><summary>Attached document text</summary>` +
        `${pre(extracted)}</details>`;
    }
    return chip;
  }

  if (b.type === "file") {
    return (
      `<span class="chip">📎 ${neutralizeHtml(str("file_name") || "file")} ` +
      `<em>(no bytes in export)</em></span>`
    );
  }

  if (b.type === "code") {
    const lang = str("language");
    const cls = lang ? ` class="language-${escapeHtml(lang, true)}"` : "";
    return `<pre><code${cls}>${badgeInvisibles(escapeHtml(b.text ?? "", false))}</code></pre>`;
  }

  if (b.type === "event") {
    // a Gemini feature event (Used / Canvas / feedback) — never a fabricated reply
    return (
      `<div class="tool"><div class="tool-head">✨ ` +
      `${neutralizeHtml(b.text || str("name") || "event")}</div></div>`
    );
  }

  if (b.type === "media") {
    const path = str("path");
    // LOCAL relative media only; anything else is a chip, never fetched. The check
    // lives in sanitize.isLocalMediaPath because the obvious inline form
    // (!includes("://") && !startsWith("//")) is BYPASSABLE -- browsers normalise
    // "\" to "/" after it runs, so /\host/x.png fetched remotely.
    if (isLocalMediaPath(path)) {
      const rel = path.includes("/") ? path : "../media/" + path;
      return (
        `<figure class="media"><img src="${escapeHtml(rel, true)}" ` +
        `alt="${escapeHtml(path, true)}" loading="lazy"></figure>`
      );
    }
    return `<span class="chip">🖼 ${neutralizeHtml(path || "media")}</span>`;
  }

  if (b.type === "unknown") {
    // show the payload, not just the type name — a future block type may carry real
    // user text; printing only the type name would silently hide it
    const raw = d["x_raw"];
    let body = "";
    try {
      body = raw !== undefined && raw !== null ? jsonDumps(raw) : "";
    } catch {
      body = String(raw);
    }
    return (
      `<div class="tool"><div class="tool-head">unrendered ` +
      `${neutralizeHtml(pyStr(d["orig_type"]))} block</div>${pre(body)}</div>`
    );
  }

  return `<div class="md">${mdToHtml(b.text)}</div>`;
}

function turnHtml(t: Turn): string {
  const role = t.role === "human" ? "human" : "assistant";
  const who = role === "human" ? "You" : "Claude";
  const av = role === "human" ? "Y" : "C";
  const branch = t.branch ? `<span class="branch">${t.branch.index}/${t.branch.total}</span>` : "";
  const blocks = t.blocks.map(blockHtml).join("");
  return (
    `<div class="turn ${role}"><div class="role"><span class="avatar">${av}</span>` +
    `${who}${branch}</div><div class="bubble">${blocks}</div></div>`
  );
}

export function renderConversationHtml(conv: Conversation, theme = "claude"): string {
  const css = loadTheme(theme);
  const titlePlain = escapeHtml(sanitizeForCopy(conv.title || "(untitled)"), true);
  const titleDisp = neutralizeHtml(conv.title || "(untitled)");
  const meta = neutralizeHtml(
    [conv.provider, conv.account, conv.created_at].filter((x) => x).join(" · "),
  );
  const turns = conv.turns.map(turnHtml).join("");
  return (
    '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">' +
    `<meta http-equiv="Content-Security-Policy" content="${CSP}">` +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    `<title>${titlePlain}</title><style>${css}</style></head>` +
    `<body><main class="wrap"><h1 class="conv-title">${titleDisp}</h1>` +
    `<div class="conv-meta">${meta}</div>${turns}</main></body></html>`
  );
}
