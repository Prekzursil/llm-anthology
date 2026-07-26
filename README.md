# LLM Anthology

**Every AI coding session you've ever had — browsable, searchable, connected, exportable, and manageable. Entirely on your own machine. No network. No egress.**

Your conversations with ChatGPT, Claude, Gemini and Codex are your working history: your decisions, your research trail, your debugging. They currently live scattered across four vendor formats, in raw JSON nobody wants to read, in stores that quietly keep three copies of the same session. LLM Anthology turns that pile into one curated, private archive you actually own.

It is two things in one binary:

- an **engine** (Python) that ingests every provider into one shared model, indexes it for full-text search, and renders faithful HTML + Markdown, and
- a **cockpit** (Tauri desktop app) that shows the whole corpus as a **cross-provider spawn tree** — which agent session spawned which — with time-travel, diffing, and fidelity-gated export.

Everything runs offline. The engine makes zero network requests, the pages it emits are locked down so *they* can't either, and the desktop app runs its engine behind an OS-level sandbox that has no network capability at all.

---

## Why this exists

- **Own your AI history.** It should be readable and greppable on your disk, not trapped in a vendor UI or an unreadable export blob.
- **See the shape of your work.** Modern agent sessions *spawn* other sessions. A flat list hides that entirely. The spawn tree is the thing no other tool in this space draws — and it is cross-provider, so a Claude session that spawned a Codex run appears as one connected graph.
- **Built for genuinely sensitive content.** Chat exports contain private material and, increasingly, *hostile* material — hidden-unicode prompt-injection payloads travel inside conversations. Every export is treated as both sensitive **and** untrusted.

---

## What it does

### Ingest — four providers, one model
Each adapter parses its native export into a small provider-agnostic IR (`llm_anthology/ir.py`); everything downstream consumes only the IR.

| Provider | Input | Status |
|---|---|---|
| **Claude** | claude.ai account data export (file or directory tree) | **Validated** on 212 real conversations; text-exact gate 207/212, 0 errors. Includes `design_chats/*.json`, which use a different message shape |
| **Gemini** | Takeout "Gemini Apps" activity (+ optional web harvest) | **Validated** on 1,060 real records → 2,027 turns. Conversation **grouping is PROVISIONAL** (time-gap heuristic) unless a harvest supplies true boundaries |
| **Codex** | `rollout-*.jsonl` session files + `state_5` store | Powers the spawn tree and dedup |
| **ChatGPT** | `conversations.json` (message tree) | **UNVALIDATED at scale** — hardened adapter + synthetic tests, but no large real export has been run through it |

### Render — a view copy and a keep copy
Two artifacts per conversation: a self-contained **HTML** page that reads like the original web app, and clean portable **Markdown** that survives any future tooling and is safe to re-feed to a model.

### Search — FTS5 over the whole corpus
A SQLite full-text index across every provider at once.

### Spawn tree — the differentiator
A canvas graph of which session spawned which, across providers, laid out with ELK in a worker. Plus:
- **time travel** — scrub the corpus to any past moment and see only what existed then (`timetravel.py`);
- **diff** — structural delta between two corpus snapshots, id-set based, not pixels (`diff.py`);
- **rollup** — per-subtree token/turn aggregates (`rollup.py`).

### Export — with a fidelity gate that can actually fail
Export a graph or a transcript, and the round-trip **diff is the oracle**: the exported artifact is re-parsed and compared against the source, so an export that silently drops a node fails instead of shipping (`export.py`, `verify.py`).

### Manage — absorbed from codex-session-manager
The three capabilities inherited when that app was retired (see [Lineage](#lineage)):

- **Metadata layer** (`metadata.py`) — pin an **alias**, **tags** and **notes** onto any session. App-owned and **non-mutating by construction**: nothing in the module opens a session file, so your originals stay byte-identical. Searchable by tag or free text.
- **Safe maintenance** (`maintenance.py`) — **archive / move / reconcile / delete** for session stores. The safety property is a hard **planner/executor split**: `plan_maintenance()` is pure and returns a preview with typed warnings; `execute_maintenance()` only runs a plan the planner produced, refuses without an exact typed confirmation, defaults to dry-run, writes a **checkpoint** first, and is reversible via `restore_checkpoint()`. Every path is confined to the resolved store root — traversal, absolute and UNC escapes are refused, not sanitized.
- **Codex dedup** (`dedup.py`) — Codex writes the same session to the live store, a backup mirror and wherever your own syncs left it. Dedup collapses those physical copies into one **logical session** while keeping every copy attached as evidence. It is a **view**: it never deletes anything.

---

## Architecture

```
 exports / session stores
   (claude · chatgpt · gemini · codex rollouts + state)
            │
            ▼
      adapters ──▶ provider-agnostic IR ──▶ corpus + SQLite/FTS5 index
            │                                    │
            │                    ┌───────────────┼────────────────┐
            ▼                    ▼               ▼                ▼
      render_html / render_md   search      spawn tree      metadata · dedup
      verify.py (fidelity gate)                             maintenance (gated)
            │
            ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  Tauri cockpit                                              │
   │    TS + canvas UI  ◀── Tauri commands ──▶  Rust core        │
   │                                              │              │
   │                              stdio NDJSON JSON-RPC 2.0      │
   │                                              ▼              │
   │                              python -m llm_anthology.sidecar│
   └─────────────────────────────────────────────────────────────┘
```

The engine is a **stdio** JSON-RPC server — deliberately not a localhost HTTP port, so nothing else on the machine can talk to it.

---

## Install

Requires **Python ≥ 3.9**. The only third-party runtime dependency is [markdown-it-py](https://github.com/executablebooks/markdown-it-py) (MIT).

```bash
pip install llm-anthology
```

That installs the `llm-anthology` command (short alias: `anth`). From a clone:

```bash
git clone https://github.com/Prekzursil/LLM_Anthology
cd LLM_Anthology
pip install -e ".[dev]"
python -m pytest
```

### Desktop cockpit

```bash
cd cockpit
npm install
npm run tauri dev     # develop
npm run tauri build   # -> src-tauri/target/release/bundle/nsis/*.exe
```

The cockpit UI can also be opened in a plain browser (`npm run dev`) — outside Tauri it automatically serves an in-memory mock corpus, so the interface can be previewed, screenshotted and design-reviewed without a Rust build.

> **Packaging caveat, stated plainly:** the installer does **not** yet bundle a Python runtime (`bundle.externalBin` is empty). An installed cockpit resolves `python -m llm_anthology.sidecar` from `PATH`, so the machine needs Python and this package. Bundling a relocatable CPython is the documented next step (`cockpit/src-tauri/binaries/README.md`).

---

## Quickstart

All paths below are synthetic examples.

```bash
# Claude — a single export JSON, or a directory of them
llm-anthology claude ~/exports/claude/conversations.json out/claude/

# Gemini — provisional grouping, or true grouping with a harvest
llm-anthology gemini ~/exports/gemini/transcript.json out/gemini/
llm-anthology gemini ~/exports/gemini/transcript.json out/gemini/ --harvest ~/exports/gemini/web_harvest.json

# ChatGPT
llm-anthology chatgpt ~/exports/chatgpt/conversations.json out/chatgpt/

# No export handy? Render the built-in synthetic demo
llm-anthology demo out/demo.html
```

### Output layout

```
out/claude/
├── index.html                # linked list of all conversations
├── html/001-<title>.html     # one self-contained page per conversation
├── md/001-<title>.md         # matching portable Markdown
├── _fidelity-report.json     # per-conversation gate results + isolated errors
└── _hidden-char-audit.json   # every flagged invisible codepoint
```

---

## The fidelity contract (read this before trusting it)

- **Hard gate — text-exact (enforced).** Every prose word token in the parsed source must appear in the rendered HTML (multiset comparison, attribute-aware). Failures land in `_fidelity-report.json`. Math spans are held out of the markdown pass and restored verbatim so CommonMark's backslash stripping cannot silently mutate TeX — unrendered-but-intact beats mutated.
- **Advisory — visual resemblance (not enforced).** The theme approximates the native web-app look. There is no automated visual check.
- **Explicitly NOT claimed — pixel-identical.** Impossible for a static offline page, and not a goal. Math is preserved verbatim rather than typeset; code is not syntax-highlighted.

---

## Security & privacy posture

- **Local-only, by construction.** No network I/O anywhere in the engine — standard library plus `markdown-it-py`. No HTTP client, no sockets.
- **The output can't phone home either.** Every rendered page ships `default-src 'none'`, contains no scripts, inlines its CSS, and defangs remote images into labelled links.
- **Exports are treated as untrusted input.** Raw HTML in message bodies is escaped; every anchor is rewritten against an http/https allowlist that **fails closed**; `javascript:`/`data:`/`file:` URLs are defanged to inert text.
- **Hidden-unicode neutralisation** (`sanitize.py`). Zero-width and bidi formats, the TAG block, **variation selectors** (the 256-value invisible-text smuggling channel), private-use, unassigned, lone surrogates and invisible Hangul fillers are all flagged — position-aware, so legitimate emoji survive. Badged visibly in HTML (forensic), stripped in Markdown and filenames (safe copy).
- **OS-level engine sandbox.** The cockpit spawns its engine through one raw `CreateProcessW` that does three things at once: an **AppContainer** with no `internetClient` capability (the engine literally cannot reach the network), a **Job Object** with `KILL_ON_JOB_CLOSE` (no orphaned engine outlives the app), and `CREATE_NO_WINDOW`.
- **Cloud research is metadata-only, and that is enforced.** The optional research plane may summarise your corpus via an LLM. What may cross to it is a **strict allowlist of structural metadata** — ids, provider, timestamps, turn/char counts — and nothing else. Free text, including conversation **titles**, is excluded *by construction*: a title is derived from raw message content (the Codex adapter builds it from the first user message), so letting titles cross would leak raw content. Both layers are mutation-pinned by `tools/privacy_mutation_check.py`, which fails if re-adding the leak keeps the suite green.
- **Repo hygiene.** `.gitignore` blocks rendered output and real conversation data. Every example and test uses synthetic content only.

---

## Development

```bash
pip install -e ".[dev]"
python -m pytest            # engine: 100% line + branch coverage, enforced

cd cockpit
npx vitest run              # UI logic
npx tsc --noEmit
cd src-tauri && cargo test  # Rust core, incl. real-sidecar e2e round-trips
```

The engine gate is a hard **100% line *and* branch** requirement (`--cov-fail-under=100`); the few genuinely unreachable defensive guards carry a justified `# pragma: no cover`. Tests are synthetic-only — no real conversation content.

---

## Lineage

LLM Anthology is the union of two of this author's earlier projects:

- **ai-sessions-render** (`aisr`) — the Python engine: adapters, IR, renderers, sanitizer, fidelity gate. Renamed to `llm_anthology`; the old package name is retired.
- **codex-session-manager** — a C#/.NET WPF app for managing Codex session history. **Retired.** It shared zero code with this stack, so its three unique capabilities — the metadata layer, gated maintenance, and Codex dedup — were re-implemented here against the C# tests as the behavioural spec, rather than merged.

"Anthology" because that is what this is: a curated collection of everything you and your agents have written.

## License

MIT.
