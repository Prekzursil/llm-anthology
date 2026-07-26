# JS/TypeScript Port Plan — llm-anthology

**Status: DESIGN ONLY. Nothing here is built.** This document maps the Python implementation
(read in full, cited by `file:line`) to a TypeScript port, module by module, with library
choices (all MIT/permissive), the tricky bits, and a test-parity strategy targeting
**byte-for-byte behaviour on the security-critical paths** (sanitize, render_md copy surface,
link hardening, fidelity gate).

Ground rules carried over from the Python tool, non-negotiable in the port:

- **Local-only, no egress.** No network at runtime; rendered HTML keeps
  `default-src 'none'` CSP (render_html.py:32-33), remote images stay defanged
  (render_html.py:88-102), links stay allowlisted to http/https (sanitize.py:150-153).
- **Synthetic content only** in every example, fixture, and doc. Real conversation text never
  enters the repo (the Python tests already follow this — e.g. tests/test_claude_adapter.py:1-2).
- **Honesty about fidelity:** the gate is TEXT-exact (verify.py:1-9), not pixel-identical.
  Nothing below claims pixel parity.

Accuracy of the inherited status (do not overstate): Claude rail validated on real exports
(gate 231/236); Gemini on 1060 real records with grouping provisional
(build_gemini.py:6-12); **ChatGPT adapter is NOT yet validated against a real export**
(chatgpt.py:18-19) — the port inherits that caveat verbatim.

---

## 0. Measured baseline (all numbers below were produced on this machine, 2026-07-19)

| Probe | Result |
|---|---|
| Python | 3.14.6, `unicodedata.unidata_version` = **16.0.0** |
| Node | v24.16.0 (V8 UCD is **newer** than 16.0.0 — see §2) |
| Python test suite | **101 tests** collected, all passing (22 sanitize · 9 verify · 20 render_html · 13 render_md · 15 claude · 10 chatgpt · 9 gemini · 3 audit) |
| `_is_flagged` bitmap over U+0000..U+10FFFF (Python) | 959,541 flagged codepoints, **741 contiguous ranges**, ~7.5 KB as JSON pairs; SHA-256 `7645d00be5bf8046510359a5599cdf04f5785e32efe3ba3d8bbdb7ac493ea570` |
| Same bitmap via native JS `\p{...}` on Node 24 | 954,738 flagged — **4,803 disagreements, ALL of them Python-side Cn** (codepoints unassigned in UCD 16.0.0 but assigned in V8's newer UCD). Zero JS-only flags. |
| Flagged codepoints that have a Unicode name (badge titles) | 430 (~14.5 KB as JSON map); every other flagged cp raises in `unicodedata.name` → `"unnamed"` (sanitize.py:82-85) |
| Python `\w` (verify tokenizer) | = `[\p{L}\p{Nd}\p{Nl}\p{No}_]` exactly — matches Lm/No/Nl, does NOT match Mn/Mc/Pc-other-than-underscore. 142,940 cps in **771 ranges** |
| Python `\d` | Unicode Nd (matches Arabic-Indic digits): 760 cps in 71 ranges |
| Python `\s` | exactly 29 cps: `09-0D 1C-1F 20 85 A0 1680 2000-200A 2028 2029 202F 205F 3000` — **excludes U+FEFF**; JS `\s` **includes U+FEFF** and excludes `85`, `1C-1F` |
| markdown-it-py 4.0.0 `gfm-like` vs markdown-it (JS) 14.3.0 | **20/20 byte-identical** renders on a battery covering tables, strikethrough, fences, links+title, images, raw-HTML escaping, backslash-escapes, ordered lists, entities, hard breaks — once the JS side is constructed as `markdownit({html:false, linkify:false, typographer:false, xhtmlOut:true})` (the py preset sets `xhtmlOut=True`; without it 18/20, diffs only `<br />`/`<img … />` void-tag style) |
| `[...str]` on `"a\ud800b"` in JS | 3 elements, middle = U+D800 — JS string spread iterates code points **and passes lone surrogates through**, matching Python's per-code-point `for ch in s` |
| `JSON.stringify` | reorders integer-like keys (`{"2","1"}` → `1,2`); escapes lone surrogates as `\ud800` (well-formed stringify); Python `json.dumps` preserves insertion order and emits raw lone surrogates |
| `String.replace(str, …)` | first occurrence only (Python `str.replace` = all) — every ported `.replace` must be `replaceAll` |
| `Buffer.from('\ud800','utf8')` | U+FFFD bytes; Python `errors="replace"` writes `?` (build_claude.py:92-95) |
| `unicode-properties@1.4.1` (npm) | **misclassifies**: returns `Cc` for U+088E and U+105C0, both `Lo` in UCD 16 — stale data; rejected (see §2) |

---

## 1. Target stack and licenses

Runtime: **Node ≥ 20, pure ESM, TypeScript strict**. No DOM types needed. Runtime
dependencies are deliberately two (one of which is already transitive):

| Package | Version (npm, verified) | License (npm, verified) | Role | URL |
|---|---|---|---|---|
| `markdown-it` | 14.3.0 — **pin exact** | MIT | render_html markdown pass (same lineage as markdown-it-py, byte-identity measured, §0) | https://github.com/markdown-it/markdown-it |
| `entities` | 8.0.0 | BSD-2-Clause | HTML entity decode in verify.ts (`html.unescape` stand-in); already a transitive dep of markdown-it | https://github.com/fb55/entities |
| `typescript` (dev) | 7.0.2 (5.x also fine) | Apache-2.0 | compiler | https://www.typescriptlang.org/ |
| `vitest` (dev) | 4.1.10 | MIT | test runner (node:test is the zero-dep fallback) | https://github.com/vitest-dev/vitest |
| *(rejected)* `unicode-properties` | 1.4.1 | MIT | — rejected on measured stale data (§0, §2) | https://github.com/foliojs/unicode-properties |
| *(alternative only)* `@unicode/unicode-16.0.0` | 1.6.17 | MIT | optional cross-check source for the generated table (dev-time only) | https://github.com/node-unicode/node-unicode-data |

markdown-it's own transitive deps (`argparse`, `entities`, `linkify-it`, `mdurl`,
`punycode.js`, `uc.micro`) are MIT/BSD. No other runtime deps: the CLI uses `node:fs`,
`node:path`, `node:process` only. Lockfile committed; installs run from the lockfile so the
build is reproducible and can be done offline from a warmed cache (the *tool itself* never
touches the network).

---

## 2. The hard part: `unicodedata.category` in JS (sanitize.ts)

### What the code actually needs (read before assuming)

`_is_flagged` (sanitize.py:42-62) needs category tests for **Cc, Cf, Co, Cn, Cs only**.
**Mn is never queried**: variation selectors are Mn, and precisely because "NO category test
catches them" (sanitize.py:52-53) they are matched by explicit ranges
(`FE00-FE0F`, `E0100-E01EF`), as are the TAG block (`E0000-E007F`, sanitize.py:48) and the
invisible Hangul fillers (category Lo, sanitize.py:26-28). `_badge` additionally needs
`unicodedata.name` (sanitize.py:82-85) for the tooltip only.

### Options researched

**Option A — native RegExp property escapes** (`/^\p{Cf}$/u` etc., ES2018):
works on Node 24 including `\p{Cs}` on a lone surrogate and `\p{Cn}` (probed, §0).
**Measured against CPython 3.14: 4,803 codepoints disagree**, every one a codepoint that
Python's UCD 16.0.0 calls Cn but V8's newer UCD has since assigned. The engine's Unicode
version is not pinnable and shifts under every Node upgrade → **rejected as the primary
mechanism** for a security predicate that must be byte-for-byte reproducible.
Ref: https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Regular_expressions/Unicode_character_class_escape

**Option B — `unicode-properties` (foliojs)**: probed and **rejected on evidence** — it
returns `Cc` for U+088E and U+105C0 (both assigned letters, Lo, in UCD 16; U+088E has been
assigned since Unicode 14). Stale data plus a wrong default makes it strictly worse than
Option A.

**Option C — `@unicode/unicode-16.0.0` range data**: correct UCD 16 data, MIT, but it is a
second source of truth generated independently of CPython, and a heavyweight dev dependency.
Viable, but inferior to:

**Option D (RECOMMENDED) — generate the table from CPython's own `unicodedata`.**
A ~60-line `tools/gen-unicode-data.py`, run under the same pinned Python as the Python rail,
emits `src/generated/unicode-data.ts` containing:

- `UNIDATA_VERSION = "16.0.0"` (stamped from `unicodedata.unidata_version`);
- `FLAGGED_RANGES`: the full `_is_flagged` bitmap encoded as 741 `[start, len]` pairs
  (~7.5 KB; delta-encoding halves it, optional). Encoding the *whole predicate* rather than
  per-category sets means sanitize.ts contains **zero category logic**: `isFlagged(cp)` is a
  binary search over ranges, and cannot drift from Python by construction;
- `FLAGGED_NAMES`: the 430 `{cp: name}` entries where `unicodedata.name` succeeds
  (~14.5 KB); lookup miss → `"unnamed"`, which is exactly Python's ValueError path
  (sanitize.py:82-85) since all other flagged cps are nameless;
- `WORD_RANGES` (771 ranges) and `ND_RANGES` (71 ranges) for verify.ts / render regexes (§5.8);
- `BITMAP_SHA256 = "7645d00b…"` — a generated self-test recomputes the bitmap from
  `FLAGGED_RANGES` and asserts this hash, so a corrupted or hand-edited table fails loudly.

**Version-skew policy:** when the Python rail moves to a CPython with a newer UCD, rerun the
generator with that Python; the version stamp changes and the parity CI (§7) re-locks both
rails. Native `\p{…}` is kept as a **secondary oracle test**: iterate all 0x110000 cps and
assert every table-vs-native disagreement satisfies "Python side is Cn" — the only
explanation consistent with pure UCD-version skew; any other disagreement fails the build.

### Position-aware logic ports verbatim

`_flagged_at` (sanitize.py:65-77), `_zwj_in_emoji` (sanitize.py:90-93) and
`_is_pictographic` (sanitize.py:31-39) port line-for-line. Two hard notes:

- `_is_pictographic` is a deliberate **approximation** of Extended_Pictographic
  (sanitize.py:32). Do **not** "upgrade" it to the real UTS-51 property in the port — that
  would change flag decisions relative to Python. If it is ever upgraded, upgrade both rails
  in lockstep.
- All indexing is per **code point** (`s[i]`, `s[i-1]`, `s[i+1]`). JS `s[i]` is a UTF-16
  code *unit* — the port must materialize `const cps = [...s]` once and index that array.
  Probed: JS string spread yields astral chars as single elements **and passes lone
  surrogates through** (§0), so Python semantics — including the poisoned-surrogate case
  guarded by tests/test_sanitize.py:132-138 — are preserved exactly.

---

## 3. Module map

Proposed layout:

```
llm_anthology-js/
  src/
    ir.ts              sanitize.ts        verify.ts         audit.ts
    render_html.ts     render_md.ts
    adapters/{claude,chatgpt,gemini}.ts
    pyshim.ts                      # escapeHtml, pyJsonDumps, isoformat, PY_WS, cp-sort…
    generated/unicode-data.ts      # emitted by tools/gen-unicode-data.py (§2)
    themes/claude.css              # copied byte-for-byte (3,476 B)
  bin/build-claude.ts  bin/build-gemini.ts
  tools/gen-unicode-data.py  tools/gen-parity-fixtures.py
  tests/…                          # 1:1 ports of the 101 tests + parity suites
```

### 3.1 `sanitize.py` → `src/sanitize.ts`

| Python | Port |
|---|---|
| `_WS_CC_OK`, `_ZWJ`, `_VS16`, `_INVISIBLE_LETTERS` (sanitize.py:23-28) | consts (values baked into `FLAGGED_RANGES` too; kept for `_flagged_at`) |
| `_is_flagged` (42-62) | `isFlaggedCp(cp)` = range binary search (§2) |
| `_flagged_at` (65-77) | verbatim over a code-point array |
| `_badge` (80-87) | `FLAGGED_NAMES` lookup; `U+%04X` → `cp.toString(16).toUpperCase().padStart(4,"0")` prefixed `U+` (Python `%04X` never truncates >4 hex digits — e.g. `U+E0041`; `padStart` matches that) |
| `neutralize_html` / `badge_invisibles` / `ncr_invisibles` / `scan_invisibles` / `sanitize_for_copy` (96-147) | same five functions over `[...s]`; non-string input → `""` guard kept (each has `s if isinstance(s, str) else ""`) |
| `is_safe_url` (150-153) | `.trim().toLowerCase().startsWith(…)` — JS `trim()` differs from Python `strip()` at U+FEFF/U+0085/U+1C-1F, but only leading/trailing whitespace before `http` is affected; replicate Python by trimming the measured PY_WS set (§0) to be exact |

**Tricky bits:** (a) `scan_invisibles` returns *code-point* indices in Python; keep that unit
in JS (document it — a naive UTF-16 index would silently shift every hit after an astral
char, and the audit sidecar format would diverge). (b) `html.escape` has no JS stdlib
equivalent — `pyshim.escapeHtml(s, quote)` must reproduce Python exactly, **including
`&#x27;` for `'`** when `quote=true` (probed: `&lt;&amp;&quot;&#x27;&gt;`) and quotes left
alone when `quote=false`; escape order `&` first.

**Test parity:** the 22 tests of tests/test_sanitize.py port 1:1 (including the VS-smuggling
payload at test_sanitize.py:106-114 and the lone-surrogate case at 132-138) **plus** the
full-range bitmap hash test (§7) which covers all 1,114,112 codepoints, not samples.

### 3.2 `ir.py` → `src/ir.ts`

Dataclasses (ir.py:13-42) become interfaces + factory functions
(`mkBlock/mkTurn/mkConversation`) that apply the same defaults (`data: {}`,
`citations: []`, `branch: null`, `ir_version: 1` from ir.py:10). Interfaces (not classes)
keep the IR JSON-shaped, which the adapters and renderers rely on. No tests exist for ir.py
alone; adapter tests cover it.

### 3.3 `adapters/claude.py` → `src/adapters/claude.ts`

The most algorithmic module. Direct port of `_subtree_max_ts` (claude.py:71-102, already
iterative and cycle-safe — no recursion-limit concern in JS either) and `_active_path`
(claude.py:105-152). Tricky bits, each a real footgun:

- **Use `Map`, never plain objects, for `by_id`/`children`/`memo`.** A uuid that *looks*
  numeric would be reordered by JS object key iteration (integer-like keys iterate first),
  and `children` is keyed by `parent_message_uuid` including `null`
  (claude.py:109-110) — `Map` handles both; objects break both silently.
- **`max(kids, key=lambda k: (submax…, _ts(k)))`** (claude.py:132): Python `max` returns the
  *first* maximum on ties and compares tuples lexicographically. Port as a manual reduce
  with strict `>` on `[submax, ts]` pairs (element-wise string compare) so tie-breaking is
  identical — a `>=` here silently changes which branch renders.
- **`sorted(roots, key=_ts)`** (claude.py:119): both languages' sorts are stable; comparator
  is plain string `<`/`>` on ISO timestamps (ASCII, so code-unit order == code-point order).
- Branch annotation `ordered.index(cur) + 1` (claude.py:126-127): `findIndex` on the sorted
  copy; note Python `list.index` compares by equality — here elements are the same object
  references, so `indexOf` identity match is equivalent.
- `_blocks_from_message` (claude.py:155-194) is mechanical; keep the `_s()` coercion
  (claude.py:25-28) — real exports carry structured `display_content` lists
  (tests/test_claude_adapter.py:159-168).

**Test parity:** all 15 tests port 1:1; the load-bearing ones are multi-root no-loss
(test_claude_adapter.py:89-99), live-thread-vs-newest-child (102-113), orphaned-cycle sweep
(116-125), and cycle termination (128-133).

### 3.4 `adapters/chatgpt.py` → `src/adapters/chatgpt.ts`

Mechanical walk of `mapping`/`current_node` (chatgpt.py:76-87) with role/visibility
filtering (chatgpt.py:90-99) and assistant-turn coalescing (chatgpt.py:43-47). One real
divergence to engineer around:

- **`_ts_top`** (chatgpt.py:66-73): Python emits `datetime.fromtimestamp(v, tz=utc)
  .isoformat()` → `2025-01-01T00:00:00+00:00`, with `.500000` microseconds only when
  fractional (probed). JS `Date.toISOString()` emits `2025-01-01T00:00:00.000Z` — different
  string, and it lands in rendered output (header meta, render_html.py:226). Write
  `pyshim.isoformatUtc(epochSeconds)` replicating Python's format (`+00:00`, microseconds
  6-digit only-when-nonzero, and Python's year 1..9999 bounds → out of range returns `""`
  matching the OverflowError/OSError/ValueError guard at chatgpt.py:70-72).

Inherited caveat, restated: **UNVERIFIED against a real export** (chatgpt.py:18-19) — the
port copies the synthetic-fixture tests (10) and the caveat; do not claim more.

### 3.5 `adapters/gemini.py` → `src/adapters/gemini.ts`

Simplest adapter (gemini.py:21-90): records → turns, `Prompted` vs event verbs
(gemini.py:74-90), attachments/media dict-or-string tolerance (gemini.py:57-71). No tricky
bits beyond `_s()` coercion. 9 tests port 1:1.

### 3.6 `render_html.py` → `src/render_html.ts`

- **markdown engine:** `markdownit({html: false, linkify: false, typographer: false,
  xhtmlOut: true})` — measured byte-identical to the Python configuration
  (§0; the `gfm-like` preset's table+strikethrough are active in the JS default preset, and
  `xhtmlOut:true` is what the py preset sets). **Pin markdown-it@14.3.0 exactly**; a version
  bump re-runs the render-parity corpus (§7) before it lands. The Python fallback to
  `commonmark` (render_html.py:24-25) is dropped — the JS dep is pinned, not ambient.
- **Math stash** (`_MATH`, `_MATH_PH`, render_html.py:36-37, 105-123): regex ports verbatim
  (JS needs `/g`); the restore loop `frag.replace(ph, escaped)` (render_html.py:121-122)
  **must be `replaceAll`** — probed: JS `replace(string, …)` substitutes only the first
  occurrence, which would leave sentinel text in any doc where markdown duplicates a
  placeholder. Same for `url.replace("&amp;", "&")` (render_html.py:66, 97).
- `_harden_links` (render_html.py:48-71): regexes port with `i` flag; group numbering
  identical; fail-closed property is contract-tested (tests/test_render_html.py:147-156).
- `_badge_text_nodes` (render_html.py:74-85): `re.split` with a capture group == JS
  `String.split(/(<[^>]*>)/)` — both keep captured separators; probeable one-liner in tests.
- `_defang_images` (render_html.py:88-102), `_pre` (126-127), `_citations_html` (130-142),
  `_block_html` (145-209), `_turn_html` (212-219), `render_conversation_html` (222-236):
  mechanical string assembly; JSON bodies via `pyshim.pyJsonDumps` (§5.4).
- Theme CSS (render_html.py:27, 40-45): copy `llm_anthology/themes/claude.css` (3,476 B) into the
  package; load with `fs.readFileSync(new URL("./themes/claude.css", import.meta.url))`
  inside try/catch → `""` on failure, matching `_load_theme`'s OSError → `""`.

**Test parity:** all 20 tests port 1:1, including the attribute-invisible NCR case
(test_render_html.py:129-137) and latex-backslash preservation (104-111). Because the
markdown pass is measured byte-identical, **whole-document byte parity with Python is an
achievable target** for render_html on the parity corpus (§7), not just token parity.

### 3.7 `render_md.py` → `src/render_md.ts`

The **copy surface** — security-critical (hidden unicode STRIPPED, render_md.py:4-6;
header-forging and link-breakout defenses at render_md.py:19-38, 66-99). Ports are
mechanical except:

- `_NEWLINES = \s*[\r\n]+\s*` (render_md.py:13): Python `\s` ≠ JS `\s` (§0). Reachability
  analysis: `_md_line` runs on `_clean()` output, and every divergent char (U+0085,
  U+001C-1F are Cc; U+FEFF is Cf) is *already stripped* by `_clean` — but do not rely on
  that subtlety: use the explicit PY_WS class from §0 so the regex is Python-equal by
  construction.
- `body.splitlines()` in the thinking-quote (render_md.py:84): Python splits on
  U+2028/U+2029 (Zl/Zp — **not flagged**, so they survive `_clean`; probed `cat_2028 = Zl`).
  A JS `split(/\r?\n/)` would leave U+2028 embedded and produce an *unquoted* line inside a
  blockquote — a real divergence on poisoned input. Port as
  `split(/\r\n|[\n\r\u2028\u2029]/)` (the full Python `str.splitlines` boundary set minus
  the chars `_clean` already removed: `\x0b \x0c \x1c-\x1e \x85` are all flagged-Cc).
- `_md_url` percent-encoding map (render_md.py:15-16, 36-38) and `_md_inline`/`_md_code_span`
  escapes (25-33): verbatim; iterate code points.

**Test parity:** all 13 tests port 1:1 — the forged-turn (test_render_md.py:85-91),
title-rides-in-citation (65-73), and `)`-breakout (76-82) tests are the contract. Target
**byte-for-byte** output equality with Python on the parity corpus: everything here is our
own string assembly, no library in the path.

### 3.8 `verify.py` → `src/verify.ts`

- `_WORD = \w+` (verify.py:15): JS `\w` is ASCII-only even with `/u` (probed). Use the
  generated `WORD_RANGES` (== Python `\w` at UCD 16.0.0, §0/§2) compiled into a RegExp
  character class or a per-code-point membership scan. Native `[\p{L}\p{N}_]` is the same
  *definition* but on the engine's UCD — fine as an oracle, not as the shipped tokenizer,
  for the same skew reason as §2. (Inside one rail the same tokenizer runs on both `want`
  and `got`, so skew cannot flip the gate; the generated table is about cross-rail
  reproducibility of `missing_tokens`/`coverage` output.)
- `_OL_MARKER` `\d` (verify.py:31) → `ND_RANGES` class (Python `\d` = Nd, probed on
  Arabic-Indic digits); `_LANG_CLASS` `[\w+.-]` (verify.py:27) → word-class + `+.-`.
- `_BADGE_SPAN`/`_STYLE`/`_HEAD` with DOTALL+IGNORECASE (verify.py:16-18) → `/…/gis`.
- `html.unescape` (verify.py:61) → `entities` `decodeHTML` (BSD-2). In practice the input is
  our own renderer's output (only `&amp; &lt; &gt; &quot; &#x27; &#xXXXX;` + markdown-it's
  entity normalization), a subset where both decoders are exactly equal; full HTML5
  named-entity edge parity vs Python is UNVERIFIED and does not need to be verified for this
  use.
- `Counter` multiset difference (verify.py:65-74) → `Map<string, number>`, subtract keeping
  positives; `sorted(missing.elements())` sorts by **code point** in Python — use a
  code-point comparator, not default `sort()` (UTF-16 order differs for astral-plane tokens,
  which are real: Gothic/math-alphanumeric letters are `\p{L}` and tokenize).
- `.lower()` → `toLowerCase()`: both full Unicode case mapping; probed equal on the Turkish
  İ edge (both yield 2 chars). Residual locale-independent differences are theoretically
  possible but none are known for tokens `\w` can produce; flagged as a watch item, not a
  blocker.

**Test parity:** 9 tests port 1:1 (the CSS-class-masking regression at
test_verify.py:62-68 is the subtle one).

### 3.9 `audit.py` → `src/audit.ts`

`audit_texts` (audit.py:15-41) is a generator — port as a generator function or array
builder. The one real decision: audit scans `json.dumps(v, ensure_ascii=False)` of tool
blobs (audit.py:33). `JSON.stringify` escapes lone surrogates to visible `\ud800` text
(probed), which would make a surrogate-poisoned tool input **invisible to the JS audit**.
Fix: scan `pyshim.pyJsonDumps(v)` (§5.4), which emits raw code points like Python. 3 tests
port 1:1.

### 3.10 `build_claude.py` / `build_gemini.py` → `bin/build-claude.ts` / `bin/build-gemini.ts`

CLI with `process.argv`; no CLI framework. Points of care:

- `_safe_name` (build_claude.py:21-27): `[:60]` is a code-point slice → `[...s].slice(0,60)`;
  `%03d` → `padStart(3,"0")`; the `_ILLEGAL` class is ASCII-only and ports as-is. (Neither
  rail handles Windows reserved device names — `CON`, `NUL` — a shared, documented gap.)
- Recursive glob + sort (build_claude.py:32-36): hand-rolled `fs.readdirSync` walk emitting
  the same `**/*.json` set, sorted with the code-point comparator; keep the
  "never ingest our own output" prefix filter (build_claude.py:35-36).
- Per-conversation try/catch isolation (build_claude.py:55-76) and the three reports
  (`index.html`, `_hidden-char-audit.json`, `_fidelity-report.json`) port verbatim;
  `round(coverage, 4)` (build_claude.py:70) — Python rounds half-to-even; use a
  `pyshim.round4` if report byte-parity is wanted, else document (cosmetic).
- `_write` with `errors="replace"` (build_claude.py:92-95): Node's UTF-8 encoder replaces
  unpaired surrogates with U+FFFD instead of `?` (probed). Post-sanitize, no surrogate can
  reach a write on the HTML path (badged) or MD path (stripped), so this differs only for a
  hypothetical bug — accepted divergence, documented.
- build_gemini `_norm` (build_gemini.py:28-29) runs on RAW prompt text → use the PY_WS class
  for `\s+` and `replaceAll(" "," ")`; `_ts`'s `datetime.fromisoformat`
  (build_gemini.py:38-44) → a small strict ISO parser (regex → `Date.UTC`), **not**
  `new Date(str)` (JS parses timezone-less date-times as local time and accepts garbage);
  only deltas are compared (GAP, build_gemini.py:23), so a consistent naive→UTC mapping
  reproduces the grouping exactly. Harvest join logic (46-73) and gap grouping (76-90) are
  mechanical.
- `make_demo` (build_claude.py:120-148) ports as the shared synthetic fixture — it is
  already synthetic and exercises every block type.

No Python tests cover the build scripts (a shared gap); the port adds smoke tests around
`_safe_name`, grouping, and report shapes, and the parity corpus (§7) covers end-to-end
output.

### 3.11 `pyshim.ts` (new, shared)

Single home for every Python-semantics shim so divergences live in one audited file:
`escapeHtml(s, quote)` (§3.1), `unescapeHtml` (entities), `pyJsonDumps(x)` — Python
`json.dumps(…, ensure_ascii=False, indent=2)` formatting: 2-space indent, `", "`/`": "`
separators (measured identical to `JSON.stringify(x,null,2)` for containers), control chars
escaped, **lone surrogates emitted raw**, insertion order (accepting the integer-like-key
caveat in §4) — `isoformatUtc`, `parseIsoNaive`, `PY_WS` regex fragment, `codePointCompare`,
`cpSlice`, `round4`.

---

## 4. Inevitable Python↔JS divergences (each: where it bites → disposition)

| # | Divergence | Where it bites | Disposition |
|---|---|---|---|
| 1 | Engine UCD ≠ CPython UCD (measured 4,803 cps) | any native `\p{…}`/category use | **Eliminated** by generated tables (§2); native kept as oracle only |
| 2 | `s[i]` = code unit; astral = 2 units | sanitize position logic, `[:60]` slices, scan indices | **Eliminated**: `[...s]` code-point arrays everywhere (probed incl. lone surrogates) |
| 3 | `String.replace` = first-only | math-placeholder restore (render_html.py:121-122), `&amp;` unescape (66, 97), NBSP in `_norm` | **Eliminated**: `replaceAll` + a lint ban on 2-arg string `.replace` |
| 4 | `JSON.stringify` reorders integer-like keys; `JSON.parse` already reorders them on ingest | rendered tool-input/unknown JSON bodies (render_html.py:157, 204; render_md.py:89, 96, 123) | **Accepted, cosmetic**: token multiset unchanged → fidelity gate unaffected; documented. (Restoring source order would require a custom JSON parser — out of scope) |
| 5 | `JSON.stringify` escapes lone surrogates | audit blob scan (audit.py:33), tool bodies | **Eliminated** via `pyJsonDumps` emitting raw code points |
| 6 | `toISOString` vs `isoformat` (`Z`/millis vs `+00:00`/micros) | chatgpt timestamps → visible meta line | **Eliminated** via `isoformatUtc` shim |
| 7 | JS `\s`, `\w`, `\d` ≠ Python's (measured, §0) | verify tokenizer, `_NEWLINES`, `_OL_MARKER`, `_norm` | **Eliminated** via PY_WS literal + generated WORD/ND ranges |
| 8 | `splitlines` extra boundaries (U+2028/29 reachable post-clean) | thinking blockquote (render_md.py:84) | **Eliminated** via explicit boundary class (§3.7) |
| 9 | UTF-8 write of unpaired surrogate: `?` vs U+FFFD | `_write` error path only; unreachable post-sanitize | **Accepted**, documented (build_claude.py:92-95) |
| 10 | `round()` half-to-even vs JS rounding | coverage figure in `_fidelity-report.json` | **Accepted** (or `round4` shim if byte-parity of reports is wanted) |
| 11 | Default `sort()` UTF-16 order vs Python code-point order | `sorted(missing.elements())`, file lists | **Eliminated** via `codePointCompare` |
| 12 | `dict` vs object key order for numeric-looking keys | claude `children`/`by_id` maps | **Eliminated**: `Map` mandated (§3.3) |
| 13 | `html.unescape` full-entity edges vs `entities` | verify on arbitrary (non-own) HTML | **Accepted**: input is always our own renderer output (subset-equal); UNVERIFIED beyond that, does not need to be |
| 14 | markdown-it JS vs markdown-it-py on inputs outside the measured battery | render_html bodies | **Mitigated**: version-pinned; whole-document parity corpus (§7) gates upgrades; 20/20 measured today; any residual diff is caught by that corpus, and the in-rail fidelity gate still guarantees no content loss |

---

## 5. Test-parity strategy

### 5.1 Port the 101 tests 1:1

Same file names, same test names, same synthetic fixtures, vitest:

| Python file | Tests | JS file |
|---|---|---|
| tests/test_sanitize.py | 22 | tests/sanitize.test.ts |
| tests/test_verify.py | 9 | tests/verify.test.ts |
| tests/test_render_html.py | 20 | tests/render_html.test.ts |
| tests/test_render_md.py | 13 | tests/render_md.test.ts |
| tests/test_claude_adapter.py | 15 | tests/claude_adapter.test.ts |
| tests/test_chatgpt_adapter.py | 10 | tests/chatgpt_adapter.test.ts |
| tests/test_gemini_adapter.py | 9 | tests/gemini_adapter.test.ts |
| tests/test_audit.py | 3 | tests/audit.test.ts |

Fixture strings containing invisibles must be written as `\u{…}` escapes in TS source (never
raw), so the port's own repo can't be flagged by an invisible-char scanner — the Python
tests embed raw invisibles; behaviour is identical either way after parsing.

### 5.2 Cross-language golden corpus (the byte-for-byte gate)

`tools/gen-parity-fixtures.py` runs the **Python** implementation and emits
`tests/parity/golden.json` (checked in, regenerated on demand):

- **sanitize**: for a battery of ~200 synthetic strings — every range boundary of every
  flagged block (first/last cp of TAG, VS1-16, VS17-256, fillers, Cc edges 08/09/0D/0E,
  2028/2029 non-flagged controls-adjacent, bare vs in-sequence ZWJ/VS16, lone surrogates,
  astral emoji) — the exact output of all of `neutralize_html`, `badge_invisibles`,
  `ncr_invisibles`, `sanitize_for_copy`, and `scan_invisibles`. JS asserts **string
  equality**.
- **render_md** and **render_html**: full documents for a set of synthetic IR conversations
  (including `make_demo` from build_claude.py:120-148 and adversarial ones from the test
  files). JS asserts **byte equality** (achievable: §0 measured markdown byte-identity;
  everything else is our own assembly).
- **verify**: `{missing_tokens, coverage, ok}` for matched and deliberately-broken pairs.
- **adapters**: IR JSON (canonicalized) for synthetic exports.

This corpus is the definition of "byte-for-byte on the security-critical paths": if any
golden entry mismatches, the port is wrong (or a deliberate, documented divergence from §4
needs a carve-out in the harness).

### 5.3 Full-range differential (total, not sampled)

The single highest-value test, already executed once during this design (§0): recompute the
`isFlagged` bitmap over all of U+0000..U+10FFFF from the shipped table and assert
SHA-256 == `7645d00b…` (the hash the generator stamped). Runs in <1 s. A second test runs
the native-`\p{}` oracle comparison with the "disagreement ⇒ Python-side Cn" invariant (§2).

### 5.4 CI parity job

A workflow job with **both** runtimes: regenerate `golden.json` with the pinned Python,
`git diff --exit-code` (proves the checked-in corpus is current), then run the JS suite.
Node pinned to a major; markdown-it pinned exact; a markdown-it or Node bump PR must show
the parity suite green before merge. (Windows + Linux runners — the repo's own
CRLF-vs-LF lesson: renderer output uses `\n` only and `_write` pins `newline="\n"`
(build_claude.py:94); Node `writeFileSync` never translates newlines, so parity holds, but
`.gitattributes` must mark `golden.json` and `.css` as `eol=lf` so checkout can't skew the
byte comparisons.)

---

## 6. Build order (when a build is approved — not part of this task)

1. **M0** `pyshim.ts` + `tools/gen-unicode-data.py` + generated table + bitmap-hash test.
2. **M1** `sanitize.ts` + 22 ported tests + sanitize golden battery. *(Gate: §5.3 green.)*
3. **M2** `ir.ts`, `verify.ts`, `audit.ts` + their 12 tests.
4. **M3** `render_md.ts` (13 tests, golden byte-parity), then `render_html.ts`
   (20 tests, golden byte-parity) — md first: no library in the path, isolates shim bugs.
5. **M4** adapters (34 tests) + golden IR fixtures.
6. **M5** CLIs + end-to-end smoke on synthetic corpora + CI parity job.

Each milestone lands with its Python-ported tests green **and** its golden slice
byte-identical; no milestone may weaken a §4 "Eliminated" row to "Accepted" without a
documented decision.

## 7. Open items / UNVERIFIED (inline flags collected)

- **UNVERIFIED:** ChatGPT adapter vs a real `conversations.json` — inherited from the Python
  rail (chatgpt.py:18-19); the port cannot fix this, only preserve the caveat.
- **UNVERIFIED:** markdown-it-py 4.0.0 ↔ markdown-it 14.3.0 equality *beyond* the 20-fixture
  battery measured here. The claimed upstream sync target of markdown-it-py 4.x was not
  independently confirmed; the whole-document golden corpus (§5.2) is the mechanism that
  settles it empirically, fixture by fixture, rather than by README claim.
- **UNVERIFIED:** `toLowerCase()` vs `str.lower()` full equality over all `\w` tokens
  (probed equal on the known edge; no exhaustive sweep run). A cheap exhaustive
  single-code-point sweep (142,940 word cps) can be added to §5.3 if wanted.
- Node's exact UCD version (newer than 16.0.0 — direction proven, version not pinned down);
  irrelevant once Option D ships, since the shipped table carries its own version stamp.
- Neither rail handles Windows reserved filenames (`CON`, `NUL`) in `_safe_name` — shared
  pre-existing gap, out of scope for parity.
