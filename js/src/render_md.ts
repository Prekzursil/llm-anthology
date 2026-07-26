/**
 * IR -> clean, portable Markdown (the "keep" copy). 1:1 with llm_anthology/render_md.py.
 *
 * Content-faithful: Claude/ChatGPT/Gemini bodies are already Markdown, so text
 * passes through untouched. Hidden unicode is STRIPPED (this file is a portable
 * copy that may be re-fed to a model — no invisible injection payload survives).
 * The HTML renderer instead BADGES the same chars for forensic viewing.
 *
 * Injection hardening that must not regress: newlines are stripped from every
 * single-line field so a value can never forge a `## Human` turn header, and link
 * titles/URLs are escaped and percent-encoded so a `)` or `]` in the data cannot
 * forge a second, live link.
 */
import type { Block, Conversation } from "./ir.js";
import { isSafeUrl, sanitizeForCopy } from "./sanitize.js";

const NEWLINES = /\s*[\r\n]+\s*/g;

/** Characters that let a value break OUT of markdown link syntax. */
const URL_UNSAFE: Record<string, string> = {
  "(": "%28",
  ")": "%29",
  " ": "%20",
  "<": "%3C",
  ">": "%3E",
  '"': "%22",
  "[": "%5B",
  "]": "%5D",
  "\t": "%09",
  "\n": "",
  "\r": "",
};

function clean(s: unknown): string {
  return sanitizeForCopy(s ?? "");
}

/** A single-line field. Newlines are stripped so a value can never forge a turn
 *  header inside the file we may re-feed to a model. */
function mdLine(s: unknown): string {
  return clean(s).replace(NEWLINES, " ").trim();
}

/** Inline text that sits inside markdown structure (e.g. link text). */
function mdInline(s: unknown): string {
  return mdLine(s).replace(/\\/g, "\\\\").replace(/\[/g, "\\[").replace(/\]/g, "\\]");
}

/** Content of a `...` span: no backticks, no newlines. */
function mdCodeSpan(s: unknown): string {
  return mdLine(s).replace(/`/g, "'");
}

/** Link destination: percent-encode what would terminate the destination. */
function mdUrl(u: unknown): string {
  return [...clean(u)].map((ch) => URL_UNSAFE[ch] ?? ch).join("");
}

function jsonDumps(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? "";
}

export function renderConversationMd(conv: Conversation): string {
  const out: string[] = [`# ${mdLine(conv.title)}`, ""];
  const meta = [
    conv.provider,
    conv.account ? `account ${conv.account}` : "",
    conv.created_at ? `created ${conv.created_at}` : "",
  ]
    .filter((x) => x)
    .join(" · ");
  if (meta) out.push(`> ${meta}`, "");

  for (const t of conv.turns) {
    const who = t.role === "human" ? "Human" : "Assistant";
    let hdr = `## ${who}`;
    if (t.branch) hdr += `  _(branch ${t.branch.index}/${t.branch.total})_`;
    out.push(hdr, "");
    for (const b of t.blocks) out.push(blockMd(b), "");
    out.push("---", "");
  }
  return out.join("\n").replace(/\s+$/, "") + "\n";
}

function blockMd(b: Block): string {
  const d = b.data;
  const str = (k: string): string => (typeof d[k] === "string" ? (d[k] as string) : "");

  if (b.type === "text") {
    let body = clean(b.text);
    const parts: string[] = [];
    for (const c of b.citations) {
      if (c === null || typeof c !== "object") continue;
      const url = typeof c.url === "string" ? c.url : "";
      const title = mdInline(c.title || url || "source");
      // isSafeUrl gates the SCHEME; the value must still be ENCODED, or a ')' in
      // the url (or a ']' in the title) forges a second, live link
      parts.push(isSafeUrl(url) ? `[${title}](${mdUrl(url)})` : `${title} (${mdInline(url)})`);
    }
    if (parts.length) body += "\n\nSources: " + parts.join(" · ");
    return body;
  }

  if (b.type === "thinking") {
    const body = clean(b.text);
    const quoted = body
      ? body.split("\n").map((ln) => "> " + ln).join("\n")
      : "> ";
    return `> 🧠 **Thinking**\n>\n${quoted}`;
  }

  if (b.type === "tool_use") {
    const name = mdCodeSpan(str("name") || "tool");
    const inp = d["input"];
    const inpS = inp !== undefined && inp !== null ? jsonDumps(inp) : "";
    const disp = b.text ? "\n\n" + clean(b.text) : ""; // display_content
    return `**🔧 Tool call: \`${name}\`**${disp}\n\n\`\`\`json\n${clean(inpS)}\n\`\`\``;
  }

  if (b.type === "tool_result") {
    const name = mdCodeSpan(str("name") || "tool");
    const content = d["content"];
    const cs =
      typeof content === "string"
        ? content
        : content !== undefined && content !== null
          ? jsonDumps(content)
          : "";
    const tag = d["is_error"] ? "⚠️ error" : "result";
    const disp = b.text ? "\n\n" + clean(b.text) : "";
    return `**↩️ Tool ${tag} (\`${name}\`)**${disp}\n\n\`\`\`\n${clean(cs)}\n\`\`\``;
  }

  if (b.type === "attachment") {
    let line = `📎 **${mdLine(str("file_name") || "attachment")}** (${
      mdLine(str("file_type") || "?")
    }, ${d["file_size"] ?? "?"} bytes)`;
    const ex = d["extracted_content"];
    if (ex) {
      line += `\n\n<details><summary>extracted content</summary>\n\n${clean(ex)}\n\n</details>`;
    }
    return line;
  }

  if (b.type === "code") {
    return `\`\`\`${mdLine(str("language"))}\n${clean(b.text)}\n\`\`\``;
  }

  if (b.type === "event") {
    return `> ✨ **${mdLine(b.text || str("name") || "event")}**`;
  }

  if (b.type === "media") {
    const path = str("path");
    // a REMOTE media url must not become a fetchable image in the portable copy
    // (it would beacon when this .md is rendered elsewhere) — mirror the HTML defang
    if (path.includes("://") || path.startsWith("//")) {
      return `🖼 ${mdInline(path)} _(remote media — not embedded)_`;
    }
    const rel = path.includes("/") ? path : "../media/" + path;
    return `![${mdInline(path || "media")}](${mdUrl(rel)})`;
  }

  if (b.type === "file") {
    return `📎 ${mdLine(str("file_name") || "file")} _(no content in export)_`;
  }

  if (b.type === "unknown") {
    let out = `_[unrendered ${mdLine(String(d["orig_type"] ?? "unknown"))} block]_`;
    const raw = d["x_raw"];
    let body = "";
    try {
      body = raw !== undefined && raw !== null ? jsonDumps(raw) : "";
    } catch {
      body = String(raw);
    }
    if (body) out += `\n\n\`\`\`json\n${clean(body)}\n\`\`\``; // never hide a payload
    return out;
  }

  return clean(b.text);
}
