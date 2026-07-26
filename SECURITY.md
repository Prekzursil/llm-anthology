# Security & Threat Model

`llm-anthology` (llm_anthology) is a **local, offline** tool that renders exported AI chat
sessions (Claude / ChatGPT / Gemini) into static HTML and portable Markdown. Its input is
**untrusted by definition**: the corpus it was built against was flagged for hidden
zero-width / bidi / private-use / TAG-block prompt-injection content
(`llm_anthology/sanitize.py:3-4`). This document describes what the code actually defends against,
how, and — just as importantly — what it does **not** cover. Every claim cites `file:line`
in this repository. All examples in this document are synthetic.

---

## 1. Security goals

1. **No egress.** The build and the rendered artifacts never touch the network
   (`llm_anthology/build.py` — "NOTHING leaves the machine"). Verified two ways: no
   network-capable module (`urllib`, `http`, `socket`, `requests`, …) is imported anywhere
   in the package — the `llm-anthology` package imports only stdlib (`html`, `json`, `os`, `re`,
   `unicodedata`, `dataclasses`, `typing`, `collections`) plus `markdown-it-py`
   (`llm_anthology/render_html.py:13-20`) — and the emitted HTML carries a CSP that blocks remote
   loads (§4).
2. **Render hostile content without executing it.** Message bodies, titles, tool
   payloads, and attachment text are treated as text, never as live markup.
3. **Preserve the evidence.** Hidden/injection codepoints are *neutralised*, not silently
   deleted, in the forensic HTML view (`llm_anthology/sanitize.py:6-8`).
4. **Produce one surface that is safe to re-feed to a model.** The Markdown "keep" copy
   strips the payload channel entirely (`llm_anthology/sanitize.py:13-14`, `llm_anthology/render_md.py:3-6`).
5. **Detect silent content loss.** A text-exact fidelity gate and a hidden-character audit
   run over every conversation (`llm_anthology/verify.py`, `llm_anthology/audit.py`).

## 2. Trust boundaries

| Zone | Contents | Trust |
|---|---|---|
| Export files | `conversations.json` / `transcript.json` bodies, titles, tool inputs/outputs, attachment `extracted_content`, citation titles/URLs, unknown-block payloads | **Untrusted.** Anyone who ever influenced a message (a summarised web page, a tool result, a pasted document) writes into this zone. |
| llm_anthology code + Python stdlib + `markdown-it-py` | The pipeline itself | Trusted (TCB). Parsing is `json.load` only — no `pickle`, no `eval` (`llm_anthology/build.py`). |
| Outputs | `html/`, `md/`, `index.html`, `_hidden-char-audit.json`, `_fidelity-report.json` (`llm_anthology/build.py`) | Derived; HTML is self-contained, CSP-pinned. |

**Parties at risk:** (a) the human opening the rendered HTML in a browser; (b) any
model/agent that is later fed the Markdown copy; (c) the build host's filesystem.

## 3. Threat: hidden-unicode smuggling (the primary channel)

Invisible codepoints can carry instructions that a human reviewer cannot see but a model
will read. The neutraliser (`llm_anthology/sanitize.py`) flags, per codepoint and **position-aware**
(`_flagged_at`, `llm_anthology/sanitize.py:65-77`):

| Class | Codepoints | Why | Where |
|---|---|---|---|
| TAG block | U+E0000–U+E007F | Encodes an invisible ASCII shadow-alphabet (emoji tag sequences / deprecated language tags) | `llm_anthology/sanitize.py:48-49` |
| **Variation selectors** | **U+FE00–U+FE0F (VS1–16), U+E0100–U+E01EF (VS17–256)** | Category **Mn** (nonspacing mark), so *no category test catches them*. 256 selectors = a byte-per-selector channel, invisible in every renderer — **the primary modern invisible-text smuggling technique** | `llm_anthology/sanitize.py:50-54` |
| Zero-width joiner | U+200D | Payload glue when *outside* an emoji sequence | `llm_anthology/sanitize.py:24,73-74,90-93` |
| Invisible letters | U+115F, U+1160, U+3164, U+FFA0 (Hangul fillers) | Render as nothing but are category **Lo** — again invisible to category tests | `llm_anthology/sanitize.py:26-28,55-56` |
| Control chars (Cc) | all except TAB/LF/CR | Non-printing controls | `llm_anthology/sanitize.py:23,57-59` |
| Format chars (Cf) | e.g. zero-width space, soft hyphen, **bidi controls** (Trojan-Source-style reordering) | Invisible or text-reordering | `llm_anthology/sanitize.py:62` |
| Private use (Co), unassigned (Cn) | — | Undefined rendering, covert channel | `llm_anthology/sanitize.py:62` |
| Lone surrogates (Cs) | — | Survive `json.loads` but abort a UTF-8 write; previously killed a whole build mid-corpus | `llm_anthology/sanitize.py:60-62` (plus `errors="replace"` write defence, `llm_anthology/build.py`) |

**Emoji are not broken:** a ZWJ *between two pictographs* and a VS16 *immediately after a
pictograph* are presentation, not payload, and are preserved (`llm_anthology/sanitize.py:31-39,
65-77,90-93`). The pictograph test is an approximation of `Extended_Pictographic`
(`llm_anthology/sanitize.py:31-39`) — see §7 for the residual.

**Decoded-string requirement:** Claude exports store invisibles as `\uXXXX` JSON escapes,
so scanning raw file bytes reports a false "clean". All scanning happens *after*
`json.load` decodes them (`llm_anthology/sanitize.py:16-18`, verified twice during development).

### Three treatments, by surface

1. **HTML text nodes — BADGE (forensic).** Each flagged codepoint becomes a visible inert
   marker: `<span class="cp-badge" data-cp="U+XXXX" title="NAME (hidden)">⚑</span>`
   (`llm_anthology/sanitize.py:80-87`), applied via `neutralize_html` / `badge_invisibles`
   (`llm_anthology/sanitize.py:96-114`). The reader — and a diff — sees exactly what was there;
   the invisible char itself never reaches the DOM text.
2. **HTML attributes — inert numeric character references.** Inside a tag (an `href`, a
   link `title`, an `alt`, a code-fence language) a badge `<span>` would break out of the
   markup, so flagged codepoints become `&#xXXXX;` instead — recorded in the source,
   never parsed as markup (`llm_anthology/sanitize.py:117-127`; routed by `_badge_text_nodes`,
   which splits the fragment on tags and NCR-encodes only inside them,
   `llm_anthology/render_html.py:74-85`; used for `href`/`title` at `llm_anthology/render_html.py:65-68,136-139`).
3. **Markdown keep-copy — STRIP.** `sanitize_for_copy` deletes flagged codepoints
   entirely (`llm_anthology/sanitize.py:140-147`); every Markdown body passes through it
   (`llm_anthology/render_md.py:62-63`). No invisible payload survives into the file you may
   re-feed to a model.

Additionally, an **audit sidecar** records every flagged codepoint across *every* text
surface — title, account, block text, tool `input`/`content` (JSON-serialised if
structured), `extracted_content`, file names, citation titles and URLs
(`llm_anthology/audit.py:15-40`; scanning only body text under-reported by roughly 5×,
`llm_anthology/audit.py:3-6`). The build writes the result to `_hidden-char-audit.json`
(`llm_anthology/build.py`).

## 4. Threat: markup/script injection and egress from the HTML view

- **Raw HTML in bodies is escaped, never parsed.** markdown-it-py runs with
  `html=False` in both the `gfm-like` and `commonmark` fallback configurations
  (`llm_anthology/render_html.py:22-25`) — matching how chatgpt.com / claude.ai / gemini render
  bodies as text. `linkify` and `typographer` are also off (`llm_anthology/render_html.py:23`).
  Non-markdown surfaces (tool payloads, code blocks, titles) go through `html.escape`
  plus badging (`llm_anthology/render_html.py:126-127,184-186`, `llm_anthology/sanitize.py:96-103`).
- **Link scheme allowlist, fail-closed.** `is_safe_url` accepts only `http://` / `https://`
  after strip+lowercase — `javascript:`, `data:`, `file:`, `vbscript:` etc. all fail
  (`llm_anthology/sanitize.py:150-153`). `_harden_links` rewrites **every** anchor, not just
  well-formed ones: an anchor whose `href` cannot be parsed is emitted as a bare `<a>`;
  a disallowed scheme is defanged to inert text carrying `data-unsafe-href` (evidence
  preserved, nothing clickable); allowed links get
  `rel="noopener noreferrer" target="_blank"` (`llm_anthology/render_html.py:48-71`). Citation
  pills apply the same gate (`llm_anthology/render_html.py:130-142`).
- **Images are defanged.** A markdown image would emit a remote `<img src>`; instead it is
  replaced by a labelled chip with an explicit *link* ("remote image — not fetched") or
  "(image unavailable)" for unsafe sources — no fetch ever happens
  (`llm_anthology/render_html.py:88-102`). `media` blocks render an `<img>` only for **local
  relative paths**; any path containing a scheme (`://`) or protocol-relative `//` prefix
  is shown as an inert chip (`llm_anthology/render_html.py:192-198`).
- **CSP pins the page shut.** Every conversation page embeds
  `default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; font-src 'self'
  data:; base-uri 'none'; form-action 'none'` (`llm_anthology/render_html.py:32-33,228-235`); the
  index page carries `default-src 'none'; style-src 'unsafe-inline'`
  (`llm_anthology/build.py`). No `script-src` is granted and no `<script>` is ever
  emitted (`llm_anthology/render_html.py:228-235`), so scripts are dead even if an escaping bug
  slipped markup through; `img-src 'self' data:` means even an un-defanged remote image
  could not load; `base-uri 'none'` / `form-action 'none'` block base-hijack and form
  exfiltration. `style-src 'unsafe-inline'` exists solely for the tool's own inlined
  theme `<style>` (`llm_anthology/render_html.py:40-45,232`).
- **Math is preserved verbatim, not rendered** — `$…$`/`$$…$$` spans are held out of the
  markdown pass and restored escaped (`llm_anthology/render_html.py:36-37,105-123`), so no math
  renderer is in the attack surface.

## 5. Threat: structure forgery in the Markdown keep-copy

The `.md` copy may be re-fed to a model, so untrusted values must not be able to forge
conversation *structure* there:

- **Role-header forgery:** newlines are stripped from single-line fields so a title or
  file name can never fabricate a `## Human` turn header (`llm_anthology/render_md.py:19-22`).
- **Link breakout:** link text escapes `\`, `[`, `]` (`llm_anthology/render_md.py:25-28`); link
  destinations percent-encode `( ) < > " [ ]`, space and TAB and drop CR/LF
  (`llm_anthology/render_md.py:15-16,36-38`). The scheme gate alone is not enough — a `)` in a URL
  or a `]` in a title would otherwise forge a second, live link
  (`llm_anthology/render_md.py:75-78`).
- **Code-span breakout:** backticks and newlines are removed from inline code spans
  (`llm_anthology/render_md.py:31-33`).
- Unsafe-scheme citations are rendered as plain text, not links (`llm_anthology/render_md.py:77-78`).

## 6. Threat: build-host effects

- **Output file names** are sanitised (illegal/`..`-forming characters `<>:"/\|?*` and
  C0 controls replaced, hidden chars stripped, length-capped, index-prefixed)
  (`llm_anthology/build.py`), so a hostile conversation title cannot traverse or collide
  paths.
- **One bad conversation cannot truncate the corpus:** each conversation renders inside
  its own try/except; failures are reported in `_fidelity-report.json`, not fatal
  (`llm_anthology/build.py`).
- **The build never ingests its own output** (self-recursion guard, `llm_anthology/build.py`).
- **Unpaired surrogates cannot abort the write** (`errors="replace"`,
  `llm_anthology/build.py`) — defence in depth behind the Cs flagging in §3.

## 7. What is covered vs NOT covered

### Covered
- Invisible/format/private-use/unassigned/surrogate/TAG codepoints and the
  variation-selector channel: badged (HTML), NCR-inert (attributes), stripped (Markdown),
  audited (sidecar) — §3.
- Script execution, remote loads, scheme-based link attacks, base/form abuse in the HTML
  view — §4.
- Markdown structure forgery in the keep-copy — §5.
- Text-exact fidelity: every `\w+` word token (case-folded, multiset-counted) from `text`
  and `thinking` bodies must reappear in the rendered HTML's visible text or harvested
  `href|src|alt|title`/`language-*` attributes (`llm_anthology/verify.py:20-27,42-74`). The build
  records failures per conversation in `_fidelity-report.json` (`llm_anthology/build.py`).

### NOT covered (explicit non-goals and blind spots)

1. **No pixel gate.** The fidelity contract is **text-exact, not pixel-identical**
   (`llm_anthology/verify.py:3-6`). Layout, styling, ordering-on-screen, and anything visual are
   not verified against the live products.
2. **Token-gate blind spots** (`llm_anthology/verify.py`):
   - The tokenizer is `\w+` (`llm_anthology/verify.py:15`): dropped **punctuation, emoji,
     symbols, or whitespace-only content** is invisible to the gate.
   - Tokens are **case-folded** (`llm_anthology/verify.py:39`) — a case-mangling bug passes.
   - The comparison is an **order-insensitive multiset** (`llm_anthology/verify.py:64-67`) — words
     moved between turns/blocks pass.
   - Attribute harvesting (`llm_anthology/verify.py:20-27,59`) means a body word that wrongly
     ended up inside an `href`/`alt`/`title` attribute still counts as "visible".
   - **Tool payloads and attachment text are excluded from the prose gate**
     (`llm_anthology/verify.py:46-51`) — they are rendered escaped (§4) and audited for hidden
     chars (§3), but a silent *drop* there would not fail `verify()`.
   - The gate **reports**; it does not delete or quarantine failing output — failing
     conversations are still written and counted (`llm_anthology/build.py`).
3. **Homoglyphs / confusables are out of scope by design.** A visible Cyrillic `а` in
   place of Latin `a` is faithful *content*; rewriting it would violate fidelity. The
   sanitizer targets invisibility, not visual similarity.
4. **User-initiated navigation is out of scope.** An allowlisted `http(s)` link is
   clickable (with `noopener noreferrer`); the no-egress guarantee covers *automatic*
   loads, not a user choosing to follow a hostile-but-well-formed URL.
5. **Markdown media references are scheme-gated only in HTML.** A `media` block whose
   path is a remote URL is defanged in HTML (`llm_anthology/render_html.py:192-198`) but the
   Markdown side emits `![alt](path)` with encoding-only hardening
   (`llm_anthology/render_md.py:113-116`); a Markdown *previewer* that auto-fetches remote images
   could egress. In the observed corpora media paths are local names
   (`llm_anthology/adapters/gemini.py:68-70`, `llm_anthology/adapters/chatgpt.py:134-137`); a hostile
   export could differ. UNVERIFIED that any real export carries remote media paths;
   known gap regardless.
6. **`_is_pictographic` is an approximation** (`llm_anthology/sanitize.py:31-39`): a VS16/ZWJ
   adjacent to characters inside its broad ranges is treated as emoji presentation and
   preserved. Residual covert capacity in that position is on the order of bits, not
   bytes; a bare VS anywhere else is flagged.
7. **CSP is delivered via `<meta http-equiv>`**, the only option for `file://`-opened
   static pages. Modern browsers honour it, but meta-CSP applies from parse time and
   cannot express `frame-ancestors`; behaviour in non-browser HTML viewers is UNVERIFIED.
8. **The markdown parser is in the TCB.** `markdown-it-py` parses untrusted text with
   `html=False`; a parser bug is a real attack surface, and `pyproject.toml` currently
   carries pytest configuration only — the dependency version is not pinned there.
9. **Validation status (honest):** the Claude rail is validated on 236 real
   conversations (fidelity gate passing 231/236); the Gemini adapter on 1060 real
   records / 2027 turns with provisional conversation grouping; the **ChatGPT adapter is
   written but NOT yet validated against a real export**. The test suite is 101 tests
   under `tests/`.

## 8. Guarantees for downstream consumers

- **Rendered HTML** is self-contained (inline theme CSS, no scripts, CSP-pinned) and safe
  to open in a modern browser without network access; hidden codepoints appear as ⚑
  badges you can inspect (`data-cp`, Unicode name in the tooltip).
- **Markdown copies** contain no flagged invisible codepoints and no forgeable structure
  from untrusted values, and are the intended surface for re-feeding content to a model.
  They are still *untrusted prose* — strip badges of trust, not of caution: visible
  injection text ("ignore previous instructions…") survives verbatim, by design.
- `_hidden-char-audit.json` tells you exactly which conversations carried hidden
  codepoints, and which ones (`llm_anthology/build.py`).

## 9. Responsible disclosure

This is a personal, local-only project, but sanitizer bypasses matter beyond it. If you
find any of the following, please report it privately:

- a codepoint (or sequence) that renders invisibly in mainstream renderers but is not
  flagged by `_flagged_at` (`llm_anthology/sanitize.py:65-77`);
- a URL that passes `is_safe_url` (`llm_anthology/sanitize.py:150-153`) yet executes or exfiltrates;
- markup breakout through the badge / NCR / escaping path (§3–§4);
- a way for rendered output to trigger a network request despite §4;
- Markdown structure forgery that survives §5.

**How to report — preferred:** open a private report through GitHub's **Security → Report
a vulnerability** ("Private vulnerability reporting" is enabled on this repository). This
keeps the report confidential and threaded until a fix ships. If you cannot use GitHub,
email the address on the maintainer's GitHub profile with subject `[llm_anthology security]`.

Please include a **synthetic** minimal reproduction (e.g. a crafted export JSON with
placeholder text) — **never include real conversation content**, which is sensitive by
definition here. Best-effort response; there is no bug bounty. Please allow a reasonable
window before public disclosure.

---
*This document uses only synthetic examples. The tool runs entirely locally; nothing
leaves the machine (`llm_anthology/build.py`, §1).*
