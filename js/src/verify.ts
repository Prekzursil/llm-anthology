/**
 * Text-exact fidelity gate. 1:1 with llm_anthology/verify.py.
 *
 * Literal pixel equality with a live page is impossible, but TEXT-exactness is a
 * hard gate: every prose word present in the source IR must survive into the
 * rendered HTML. This catches any rendering bug that silently drops or garbles
 * content. A non-empty `missing_tokens` on any conversation fails the gate.
 *
 * Two things must match Python exactly or the gate drifts between rails:
 *  - Python's `\w` is UNICODE-aware (str.isalnum() or underscore) while JS's is
 *    ASCII-only, so tokenizing uses the generated WORD_RANGES table;
 *  - `html.unescape` decodes the full HTML5 named-entity set, so the `entities`
 *    package stands in rather than a hand-rolled five-entity map.
 */
import { decodeHTML } from "entities";

import { WORD_RANGES } from "./generated/unicode-data.js";
import type { Conversation } from "./ir.js";

const BADGE_SPAN = /<span class="cp-badge"[\s\S]*?<\/span>/gi;
const STYLE = /<style[\s\S]*?<\/style>/gi;
const HEAD = /<head[\s\S]*?<\/head>/gi;
const TAG = /<[^>]+>/g;
// Source words can legitimately land in an ATTRIBUTE rather than visible text: a
// markdown link's URL -> href, an image -> src/alt, a code fence's language ->
// class="language-x". Harvest those so the gate does not false-positive on them.
// `class` is deliberately EXCLUDED: shell class names (avatar, bubble, turn, wrap,
// md, role) would mask a genuinely dropped body word that happens to match one.
const ATTR_VALS = /(?:href|src|alt|title)="([^"]*)"/gi;
const LANG_CLASS = /class="[^"]*language-([\w+.-]+)/gi;
// Ordered-list markers ("1." "2)") are STRUCTURAL: <ol> renders the number via a CSS
// counter, so it is visible to a reader but absent from the DOM text. Drop them from
// the source side or the gate reports every numbered list as missing content.
const OL_MARKER = /^[ \t]{0,3}\d+[.)][ \t]+/gm;

function isWordCp(cp: number): boolean {
  let lo = 0;
  let hi = WORD_RANGES.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const r = WORD_RANGES[mid]!;
    if (cp < r[0]) hi = mid - 1;
    else if (cp >= r[0] + r[1]) lo = mid + 1;
    else return true;
  }
  return false;
}

/** Python's `re.findall(r"\w+", s.lower())` over Unicode word characters.
 *  Always called with a string (a block body or decoded HTML), matching the
 *  Python rail which likewise assumes a string. */
function tok(s: string): string[] {
  const lowered = s.toLowerCase();
  const out: string[] = [];
  let cur = "";
  for (const ch of lowered) {
    if (isWordCp(ch.codePointAt(0)!)) cur += ch;
    else if (cur) {
      out.push(cur);
      cur = "";
    }
  }
  if (cur) out.push(cur);
  return out;
}

/**
 * Word tokens a reader must see: text + thinking bodies. The \w+ tokenizer already
 * treats invisible/format chars as boundaries — matching how the HTML renderer
 * badges them — so a hidden char never joins or splits a real word here.
 * Tool/attachment payloads are structural and excluded from this prose gate.
 */
export function proseTokens(conv: Conversation): string[] {
  const toks: string[] = [];
  for (const t of conv.turns) {
    for (const b of t.blocks) {
      if (b.type === "text" || b.type === "thinking") {
        toks.push(...tok(b.text.replace(OL_MARKER, " ")));
      }
    }
  }
  return toks;
}

export function htmlVisibleTokens(h: string): string[] {
  let s = (h ?? "")
    .replace(BADGE_SPAN, " ") // badges replace invisibles — not content words
    .replace(STYLE, " ")
    .replace(HEAD, " "); // drop CSS + <title> so they cannot mask a real drop
  const attrs = [
    ...[...s.matchAll(ATTR_VALS)].map((m) => m[1]!),
    ...[...s.matchAll(LANG_CLASS)].map((m) => m[1]!),
  ].join(" ");
  const body = s.replace(TAG, " ");
  return tok(decodeHTML(body + " " + attrs));
}

function counter(items: string[]): Map<string, number> {
  const m = new Map<string, number>();
  for (const it of items) m.set(it, (m.get(it) ?? 0) + 1);
  return m;
}

/** Python sorts strings by CODE POINT; JS's default sort compares UTF-16 code
 *  units, which differs for astral characters. */
function byCodePoint(a: string, b: string): number {
  const ca = [...a];
  const cb = [...b];
  for (let i = 0; i < Math.min(ca.length, cb.length); i++) {
    const d = ca[i]!.codePointAt(0)! - cb[i]!.codePointAt(0)!;
    if (d !== 0) return d;
  }
  return ca.length - cb.length;
}

export interface VerifyResult {
  ok: boolean;
  missing_tokens: string[];
  coverage: number;
}

export function verify(conv: Conversation, renderedHtml: string): VerifyResult {
  const want = counter(proseTokens(conv));
  const got = counter(htmlVisibleTokens(renderedHtml));

  const missing: string[] = [];
  let total = 0;
  for (const [word, n] of want) {
    total += n;
    const deficit = n - (got.get(word) ?? 0); // multiset difference
    for (let i = 0; i < deficit; i++) missing.push(word);
  }
  missing.sort(byCodePoint);
  const covered = total - missing.length;
  return {
    ok: missing.length === 0,
    missing_tokens: missing,
    coverage: total ? covered / total : 1.0,
  };
}
