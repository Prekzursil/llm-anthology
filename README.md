# llm-anthology

**Turn your ChatGPT / Claude / Gemini data exports into browser-faithful HTML and clean, portable Markdown — entirely on your own machine. No network. No egress.**

Your AI conversations are part of your working history, but a provider "data export" is a pile of raw JSON that no human wants to read. `llm-anthology` converts those exports into two artifacts per conversation:

- a **view copy** — a self-contained static HTML page that reads like the original web app, safe to open in any browser, and
- a **keep copy** — clean Markdown that survives any future tooling and is safe to re-feed to a model.

Everything runs offline. The tool makes zero network requests, and the pages it emits are locked down so *they* can't make any either.

## Why

- **Own your AI history.** Conversations you had with an assistant are your notes, your decisions, your research trail. They should be readable and greppable on your disk, not trapped in a vendor UI or an unreadable JSON blob.
- **Browser-faithful and portable at the same time.** HTML for reading the way the conversation actually looked (roles, thinking blocks, tool calls, branches, citations); Markdown for grep, diff, static-site pipelines, and archival.
- **Fully local, built for sensitive content.** Chat exports contain private material and — increasingly — *hostile* material (hidden-unicode prompt-injection payloads travel inside conversations). This tool treats every export as sensitive **and** untrusted: nothing leaves the machine, and invisible characters are surfaced or stripped rather than silently passed through.

## Features

- **Three providers, one pipeline.** Each adapter parses its native export into a small provider-agnostic IR (`llm_anthology/ir.py`); the HTML and Markdown renderers consume only the IR.
  - **Claude** (claude.ai account data export): full content model — text, thinking (including thinking the UI withheld, labelled as such), tool calls/results, attachments with extracted document text, citations, file chips.
  - **ChatGPT** (`conversations.json`): message-tree walk from `current_node`, thoughts/reasoning recaps, code and execution-output blocks, image asset pointers. *Adapter written, not yet validated against a real export — see status table.*
  - **Gemini** (Google Takeout "Gemini Apps" activity): prompt/response exchanges plus honest rendering of feature events ("Used", "Created Gemini Canvas", …) as events — never as fabricated model replies.
- **Tree & branch handling.** Claude exports are a message *tree* (regenerations create siblings). The adapter walks the **active path** — descending toward the subtree holding the globally newest message, not merely the newest immediate child, which would abandon live threads (`llm_anthology/adapters/claude.py:105`) — annotates branch points ("2/2"), walks every root so orphaned subtrees aren't lost, and sweeps in unreachable messages rather than dropping them. ChatGPT's abandoned regenerations are excluded by following `current_node → root`, so discarded replies never leak into the transcript (`llm_anthology/adapters/chatgpt.py:76`).
- **Hidden-unicode neutralisation** (`llm_anthology/sanitize.py`). Zero-width and bidi format characters, the Unicode TAG block, variation selectors (the 256-value invisible-text smuggling channel), private-use, unassigned, lone surrogates, and invisible Hangul fillers are all flagged — position-aware, so legitimate emoji sequences (ZWJ between pictographs, VS16 after a pictograph) stay intact. Two deliberate surfaces:
  - **HTML (forensic):** each flagged codepoint becomes a *visible inert badge* (`⚑` with the `U+XXXX` name in a tooltip) — the evidence is preserved, never rendered invisibly.
  - **Markdown / filenames (safe copy):** flagged codepoints are *stripped*, so re-pasting an archived message into your next model session can't re-inject a payload.
- **Text-exact fidelity gate** (`llm_anthology/verify.py`). Every prose word token in the parsed source must survive into the rendered HTML (multiset comparison, attribute-aware). Any conversation with missing tokens is reported in `_fidelity-report.json`. This is the hard guarantee that no rendering bug silently drops or garbles content.
- **Forensic audit sidecar** (`llm_anthology/audit.py`). Hidden-character scanning covers *every* text surface — titles, tool inputs/outputs, attachment extracted text, citation titles — not just message bodies (scanning bodies alone under-reported by roughly 5x on real data). Results land in `_hidden-char-audit.json`.
- **Hardened, self-contained HTML** (`llm_anthology/render_html.py`). CSP `default-src 'none'`, no scripts, theme CSS inlined, links allowlisted to http/https with a fail-closed anchor rewrite, remote markdown images defanged into labelled links instead of `<img>` loads.
- **Corpus resilience.** Each conversation renders inside its own try/except; one malformed conversation is reported, never fatal to the rest of the corpus (`llm_anthology/build.py`). Loading errors (unreadable file, malformed JSON) are collected and reported the same way rather than raised.

## How it works

```
export JSON ──▶ adapter (claude | chatgpt | gemini) ──▶ provider-agnostic IR (llm_anthology/ir.py)
                                                             │
                       ┌─────────────────────────────────────┼──────────────────────┐
                       ▼                                     ▼                      ▼
              llm_anthology/render_html.py                   llm_anthology/render_md.py          llm_anthology/audit.py
              "view" copy (HTML,                    "keep" copy (MD,           hidden-char
              invisibles badged)                    invisibles stripped)       forensics
                       │
                       ▼
              llm_anthology/verify.py — text-exact fidelity gate (hard, per conversation)
```

## Install

Requires **Python >= 3.9**. The only third-party runtime dependency is [markdown-it-py](https://github.com/executablebooks/markdown-it-py) (MIT).

```bash
pip install llm-anthology
```

That installs the `llm-anthology` command. To work from a clone instead:

```bash
git clone https://github.com/Prekzursil/LLM_Anthology
cd llm-anthology
pip install -e ".[dev]"
python -m pytest          # 136 tests, all synthetic fixtures
```

## Quickstart

All paths below are synthetic examples — point them at your own export.

**Claude** — a single export JSON, or a directory that contains export JSON files:

```bash
llm-anthology claude ~/exports/claude/conversations.json out/claude/
llm-anthology claude ~/exports/claude/                   out/claude/
```

A real Claude export directory also contains `users.json`, `memories.json`, `projects/` and `design_chats/`. Only actual conversations are ingested — `conversations.json` files plus `design_chats/*.json`. That second one matters: design chats use a **different message shape** (`messages[]` with `role` + a content *dict*, rather than `chat_messages[]` with `sender` + a content *list*), so feeding one through the normal parser yields a silently **empty** conversation. On a real corpus that was 544 KB of conversation content rendering as blank pages.

No export handy? Render the built-in **synthetic demo** (exercises every block type, contains no real content):

```bash
llm-anthology demo out/demo.html
```

**Gemini** — a Takeout-derived activity transcript, optionally joined with a web-app harvest for true conversation grouping:

```bash
# provisional grouping (30-minute-gap heuristic, clearly labelled as such)
llm-anthology gemini ~/exports/gemini/transcript.json out/gemini/

# true grouping, joined on exact normalised prompt text
llm-anthology gemini ~/exports/gemini/transcript.json out/gemini/ --harvest ~/exports/gemini/web_harvest.json
```

The grouping mode is written into `_fidelity-report.json` as `grouping_mode`, so a heuristic can never be mistaken for ground truth.

**ChatGPT** — `conversations.json` from the data export, or any JSON array of conversation objects. Conversations appearing in both files are rendered once (deduped by id); records carrying a `__project_id` get their project shown in the index:

```bash
llm-anthology chatgpt ~/exports/chatgpt/conversations.json out/chatgpt/
llm-anthology chatgpt ~/exports/chatgpt/conversations.json out/chatgpt/ --projects ~/exports/chatgpt/projects.json
```

The ChatGPT adapter is **not yet validated against a large real export** — see the status table.

### Output layout

```
out/claude/
├── index.html                # linked list of all conversations
├── html/001-<title>.html     # one self-contained page per conversation
├── md/001-<title>.md         # matching portable Markdown
├── _fidelity-report.json     # per-conversation gate results + isolated errors
└── _hidden-char-audit.json   # every flagged invisible codepoint, per conversation
```

## The fidelity contract (read this before trusting it)

Being honest about what "faithful" means here:

- **Hard gate — text-exact (enforced).** Every prose word in the parsed source (message text and thinking bodies) must appear in the rendered HTML. The build runs `llm_anthology/verify.py` on every conversation and writes failures to `_fidelity-report.json`. Math spans are held out of the markdown pass entirely and restored verbatim, specifically so CommonMark's backslash stripping can't silently mutate TeX (`llm_anthology/render_html.py:105`) — unrendered-but-intact beats mutated.
- **Advisory — visual resemblance (not enforced).** The bundled theme approximates the native web-app look (bubbles, roles, collapsible thinking, tool panels). There is **no automated visual check** in this repo; visual fidelity is best-effort.
- **Explicitly NOT claimed — pixel-identical.** Literal pixel equality with a live, scripted web app is impossible for a static offline page and is not a goal. Math is preserved verbatim rather than typeset, and code blocks are not syntax-highlighted (both are documented follow-ups).

Latest full runs on real (private, not-in-repo) corpora: **Claude** — 212 conversations rendered (204 from `conversations.json` + 8 design chats), text-exact gate passed on 207/212, 0 errors; **Gemini** — 1,060 activity records → 2,027 turns rendered, with conversation grouping still provisional pending a web-app harvest.

That Claude figure was previously reported as 236. It was wrong, and the way it was wrong is worth recording: a `**/*.json` glob had been sweeping up the export's *metadata* files (`users.json`, `memories.json`, `projects/*.json`) — each of which the adapter happily wrapped as a single **empty** conversation — plus, when the output directory sat outside the source tree, the tool's own previous reports (`_hidden-char-audit.json` contributed 29 phantom "conversations", one per audit row). So 236 = 204 real + 32 empty artefacts. Counting rendered files is not the same as counting conversations; the count is now taken only from real conversation sources.

## Security & privacy posture

- **Local-only, no egress — by construction.** The code performs no network I/O anywhere (standard library + `markdown-it-py` only; no HTTP client, no sockets). Your conversations never leave your machine.
- **The output can't phone home either.** Every rendered page ships `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; base-uri 'none'; form-action 'none'`, contains no scripts, inlines its CSS, and never embeds a remote `<img>` — markdown images are defanged into labelled links that are only followed if *you* click them.
- **Exports are treated as untrusted input.**
  - Raw HTML inside message bodies is escaped, matching what chatgpt.com / claude.ai / gemini themselves do (`markdown-it` with `html=False`).
  - Every anchor is rewritten with an http/https allowlist that **fails closed**: an href the rewriter can't verify is dropped, and `javascript:` / `data:` / `file:` URLs are defanged to inert text (`llm_anthology/render_html.py:48`, `llm_anthology/sanitize.py:150`).
  - Hidden/invisible codepoints are badged in HTML, stripped in Markdown and filenames (see Features), and inventoried in the audit sidecar.
  - The Markdown writer hardens against markdown injection: newlines are stripped from single-line fields so a value can never forge a `## Human` turn header, and link titles/URLs are escaped and percent-encoded so a `)` or `]` in the data can't forge a second live link (`llm_anthology/render_md.py:19`).
  - Only local, scheme-less relative paths ever become `<img src>`; anything with a URL scheme is displayed as text, never fetched.
- **Repo hygiene.** `.gitignore` blocks rendered output and real conversation data from ever being committed. All examples and tests in this repo use synthetic content only.

## Provider status

| Provider | Input | Command | Status |
|---|---|---|---|
| **Claude** | claude.ai account data export (JSON file or directory tree) | `llm-anthology claude` | **Validated** on 212 real conversations; text-exact gate 207/212, 0 errors. Includes `design_chats/*.json` (separate message shape) |
| **Gemini** | Takeout "Gemini Apps" activity as a normalised `transcript.json` (+ optional web harvest) | `llm-anthology gemini` | **Validated** on 1,060 real records (2,027 turns rendered); conversation **grouping is PROVISIONAL** (time-gap heuristic) unless a web-app harvest supplies true boundaries |
| **ChatGPT** | `conversations.json` (message tree) | `llm-anthology chatgpt` | **UNVALIDATED at scale** — synthetic tests + a hardened adapter, but a large real export has not yet been run through it (`llm_anthology/adapters/chatgpt.py`) |

## Known limitations

- The HTML turn header currently labels every assistant "Claude", regardless of provider (`llm_anthology/render_html.py:214`).
- Math is preserved verbatim (`$...$` / `$$...$$` shown as-is), not typeset; no syntax highlighting; one bundled theme.
- Claude exports contain no bytes for uploaded `files` — they render as a named chip ("no bytes in export"). Attachment *text* (extracted document content) is preserved.
- Gemini: Takeout's activity log has no conversation IDs; the Takeout → `transcript.json` normalisation step is not part of this repo yet.

## Development

```bash
pip install -e ".[dev]"
python -m pytest    # 136 tests
```

Tests are synthetic-only (no real conversation content) and cover the adapters, both renderers, the sanitizer, the audit, and the fidelity gate.

## License

MIT.
