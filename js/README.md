# llm-anthology (JavaScript / TypeScript)

**Turn your ChatGPT / Claude / Gemini data exports into browser-faithful HTML and clean, portable Markdown — entirely offline. No network. No egress.**

This is the JS/TypeScript port of [`llm-anthology`](https://github.com/Prekzursil/llm-anthology) (also published to PyPI as a Python package). It is a faithful port, not a reimplementation: its output is validated **byte-for-byte** against the Python rail.

## Install

```bash
npm install llm-anthology
```

Node ≥ 20. The only runtime dependencies are [`markdown-it`](https://github.com/markdown-it/markdown-it) (pinned exactly to 14.3.0) and [`entities`](https://github.com/fb55/entities), both MIT/BSD.

## CLI

```bash
# a single Claude export file, or a directory tree of them (incl. design_chats/)
npx llm-anthology claude ./claude-export/ ./out/claude/

# ChatGPT conversations.json (optionally a second project-tagged file)
npx llm-anthology chatgpt ./conversations.json ./out/chatgpt/ --projects ./projects.json

# Gemini Takeout activity (optionally a web harvest for TRUE grouping)
npx llm-anthology gemini ./transcript.json ./out/gemini/ --harvest ./web_harvest.json

# a synthetic sample page, no real content
npx llm-anthology demo ./demo.html
```

Each run writes `<out>/html/NNN-title.html`, `<out>/md/NNN-title.md`, an `index.html`, and two reports: `_fidelity-report.json` (a text-exact gate, per conversation) and `_hidden-char-audit.json` (every flagged invisible codepoint).

## Library

```ts
import { adapters, renderConversationHtml, renderConversationMd, verify } from "llm-anthology";

const convs = adapters.claude.parseExport(JSON.parse(raw));
const html = renderConversationHtml(convs[0]);
const md = renderConversationMd(convs[0]);
console.log(verify(convs[0], html)); // { ok, missing_tokens, coverage }
```

## Security posture

Identical to the Python rail: no network I/O anywhere; every emitted page ships `Content-Security-Policy: default-src 'none'`, no scripts, inlined CSS, and defanged remote images. Hidden/invisible codepoints (zero-width, bidi, the Unicode TAG block, variation selectors — the 256-value smuggling channel — private-use, unassigned, lone surrogates, invisible Hangul fillers) are badged inertly in HTML and stripped in Markdown, position-aware so legitimate emoji survive.

The flag predicate is **generated from CPython's own `unicodedata`** (`tools/gen-unicode-data.py`) rather than JS's `\p{…}` escapes, because V8's Unicode version is not pinnable and drifts from the Python rail — so `isFlagged()` is a binary search over a table that cannot diverge from Python by construction.

## Parity with the Python rail

The two rails are held to byte-for-byte equivalence by generated fixtures replayed in the test suite (325 tests): the sanitizer over an adversarial battery (lone surrogates, variation-selector smuggling, multi-ZWJ emoji families), both renderers over every block type plus an injection battery, the fidelity gate and audit, and the three adapters over real + synthetic corpora.

**One known divergence.** `markdown-it` (JS) and `markdown-it-py` differ on a non-breaking space (U+00A0) immediately after a soft line break: JS preserves it, the Python `markdown-it-py` collapses it. Measured on a real 212-conversation corpus this affected a single file by a handful of characters, changed no words (both rails pass the fidelity gate), and the JS behaviour is marginally the more faithful of the two (the browser shows the character). It is documented rather than papered over.

## License

MIT.
