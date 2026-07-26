/**
 * Hidden-unicode neutralizer + HTML escaping + link-scheme allowlist.
 *
 * A 1:1 port of llm_anthology/sanitize.py. Policy:
 *
 * - PRESERVE the evidence but NEUTRALIZE it: every flagged codepoint becomes a
 *   VISIBLE, inert badge, never the raw invisible char, so a reader (and a diff)
 *   can see exactly what was there.
 * - Escape raw HTML in bodies (what chatgpt.com / claude.ai / gemini all do).
 * - Do NOT break legitimate emoji: VS16 and ZWJ *inside* an emoji sequence are
 *   preserved; a BARE zero-width joiner is badged.
 * - `sanitizeForCopy` STRIPS the flagged chars so re-pasting an archived message
 *   into the next model cannot re-inject.
 *
 * The flag predicate itself contains ZERO category logic here: it is a binary
 * search over a table generated from CPython's own `unicodedata` (see
 * tools/gen-unicode-data.py). Native `\p{Cf}`-style escapes were rejected because
 * V8's Unicode version is not pinnable and drifts from the Python rail.
 *
 * IMPORTANT: callers must pass DECODED strings. Claude exports store invisibles
 * as \\uXXXX JSON escapes, so scanning raw file bytes reports a false "clean".
 */
import { FLAGGED_NAMES, FLAGGED_RANGES } from "./generated/unicode-data.js";

const ZWJ = 0x200d;
const VS16 = 0xfe0f;

/**
 * True for invisible/dangerous codepoints, IGNORING position.
 *
 * Position-sensitive cases (ZWJ inside an emoji sequence, VS16 straight after a
 * pictograph) are decided by `flaggedAt`, which is what callers must use.
 */
export function isFlagged(cp: number): boolean {
  let lo = 0;
  let hi = FLAGGED_RANGES.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const range = FLAGGED_RANGES[mid]!;
    if (cp < range[0]) hi = mid - 1;
    else if (cp >= range[0] + range[1]) lo = mid + 1;
    else return true;
  }
  return false;
}

/** Approximate Extended_Pictographic - enough to keep in-sequence ZWJ intact.
 *  Deliberately an approximation; do NOT "upgrade" it without changing the
 *  Python rail in lockstep, or the two rails will disagree on flag decisions. */
export function isPictographic(cp: number): boolean {
  return (
    (cp >= 0x1f000 && cp <= 0x1faff) ||
    (cp >= 0x2600 && cp <= 0x27bf) ||
    (cp >= 0x2b00 && cp <= 0x2bff) ||
    (cp >= 0x1f1e6 && cp <= 0x1f1ff) || // regional indicators
    cp === 0x2764 ||
    cp === 0x2665 ||
    cp === 0x203c ||
    cp === 0x2049
  );
}

/** Code points of a string. JS indexing is per UTF-16 code UNIT, so every
 *  position-aware routine must work over this array to match Python's `s[i]`.
 *  Spread also passes lone surrogates through unchanged, which the poisoned-
 *  surrogate case depends on. */
export function codePoints(s: unknown): string[] {
  return [...(typeof s === "string" ? s : "")];
}

function cpAt(cps: string[], i: number): number {
  return cps[i]!.codePointAt(0)!;
}

function zwjInEmoji(cps: string[], i: number): boolean {
  const prev = i > 0 ? cps[i - 1]! : "";
  const next = i + 1 < cps.length ? cps[i + 1]! : "";
  return (
    prev !== "" &&
    next !== "" &&
    isPictographic(prev.codePointAt(0)!) &&
    isPictographic(next.codePointAt(0)!)
  );
}

/** Position-aware flag decision - the ONLY predicate callers should use. */
export function flaggedAt(cps: string[], i: number): boolean {
  const cp = cpAt(cps, i);
  if (cp === ZWJ) return !zwjInEmoji(cps, i);
  if (cp === VS16) return !(i > 0 && isPictographic(cpAt(cps, i - 1)));
  return isFlagged(cp);
}

/** Python's html.escape. quote=true also escapes " and '. */
export function escapeHtml(s: unknown, quote = true): string {
  let out = (typeof s === "string" ? s : "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  if (quote) out = out.replace(/"/g, "&quot;").replace(/'/g, "&#x27;");
  return out;
}

function hex4(cp: number): string {
  return cp.toString(16).toUpperCase().padStart(4, "0");
}

function badge(cp: number): string {
  const name = FLAGGED_NAMES[cp] ?? "unnamed";
  return (
    `<span class="cp-badge" data-cp="U+${hex4(cp)}" ` +
    `title="${escapeHtml(name, true)} (hidden)">⚑</span>`
  );
}

/** DECODED text -> HTML-safe string: raw HTML escaped, flagged invisibles
 *  replaced with visible inert badges, legitimate emoji preserved. */
export function neutralizeHtml(s: unknown): string {
  const cps = codePoints(s);
  let out = "";
  for (let i = 0; i < cps.length; i++) {
    out += flaggedAt(cps, i) ? badge(cpAt(cps, i)) : escapeHtml(cps[i]!, false);
  }
  return out;
}

/** Replace flagged invisibles with badges WITHOUT escaping other characters -
 *  for post-processing an already-rendered HTML fragment. */
export function badgeInvisibles(s: unknown): string {
  const cps = codePoints(s);
  let out = "";
  for (let i = 0; i < cps.length; i++) {
    out += flaggedAt(cps, i) ? badge(cpAt(cps, i)) : cps[i]!;
  }
  return out;
}

/** Replace flagged invisibles with INERT numeric character references, for use
 *  inside a tag/attribute where a badge <span> would break out of the markup. */
export function ncrInvisibles(s: unknown): string {
  const cps = codePoints(s);
  let out = "";
  for (let i = 0; i < cps.length; i++) {
    out += flaggedAt(cps, i) ? `&#x${hex4(cpAt(cps, i))};` : cps[i]!;
  }
  return out;
}

/** [[index, 'U+XXXX'], ...] for each flagged codepoint (audit sidecar). */
export function scanInvisibles(s: unknown): Array<[number, string]> {
  const cps = codePoints(s);
  const hits: Array<[number, string]> = [];
  for (let i = 0; i < cps.length; i++) {
    if (flaggedAt(cps, i)) hits.push([i, `U+${hex4(cpAt(cps, i))}`]);
  }
  return hits;
}

/** Strip flagged invisibles entirely (clipboard / agent-feed surface). */
export function sanitizeForCopy(s: unknown): string {
  const cps = codePoints(s);
  let out = "";
  for (let i = 0; i < cps.length; i++) {
    if (!flaggedAt(cps, i)) out += cps[i]!;
  }
  return out;
}

/** Allowlist http/https only (kills javascript:, data:, file:, ...). */
export function isSafeUrl(u: unknown): boolean {
  const s = (typeof u === "string" ? u : "").trim().toLowerCase();
  return s.startsWith("http://") || s.startsWith("https://");
}

/**
 * True only for a genuinely LOCAL, relative media path.
 *
 * Guarding with `!path.includes("://") && !path.startsWith("//")` was bypassable:
 * a browser normalises backslashes to forward slashes AFTER any such check, so
 * `/\host/x.png` and `\/host/x.png` reached the DOM as protocol-relative URLs and
 * fetched remotely. `../../../etc/passwd` also passed straight into src=.
 * Measured 3/3 bypasses on the Python twin before this existed.
 *
 * Allowlist, not blocklist: normalise separators FIRST (the browser will), then
 * require a relative path with no scheme, root anchor, drive letter or traversal.
 * Mirrors llm_anthology/sanitize.py::is_local_media_path -- keep both rails in step.
 */
export function isLocalMediaPath(p: unknown): boolean {
  const s = typeof p === "string" ? p : "";
  if (!s) return false;
  const norm = s.replace(/\\/g, "/");                    // normalise BEFORE deciding
  if (norm.includes("://") || norm.startsWith("/")) return false;
  if (norm.startsWith("data:") || norm.split("/")[0].includes(":")) return false;
  return !norm.split("/").some((seg) => seg === "..");
}
