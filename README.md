# LLM Anthology

**Every AI coding session you've ever had — browsable, searchable, connected, exportable, and manageable. Entirely on your own machine. No network. No egress.**

Your conversations with ChatGPT, Claude, Gemini and Codex are your working history: your decisions, your research trail, your debugging. They currently live scattered across four vendor formats, in raw JSON nobody wants to read, in stores that quietly keep three copies of the same session. LLM Anthology turns that pile into one curated, private archive you actually own.

It is two things in one binary:

- an **engine** (Python) that ingests every provider into one shared model, indexes it for full-text search, and renders faithful HTML + Markdown, and
- a **cockpit** (Tauri desktop app) that shows the whole corpus as a **cross-provider spawn tree** — which agent session spawned which — with time-travel, diffing, and fidelity-gated export.

Everything runs offline. The engine makes zero network requests, and the pages it emits are locked down so *they* can't either. The desktop app additionally confines its engine to a Job Object so no engine can outlive the app (see [Privacy and security](#privacy-and-security) for exactly what is and is not enforced).

---

## Why this exists

- **Own your AI history.** It should be readable and greppable on your disk, not trapped in a vendor UI or an unreadable export blob.
- **See the shape of your work.** Modern agent sessions *spawn* other sessions. A flat list hides that entirely. The spawn tree is the thing no other tool in this space draws — and it is cross-provider, so a Claude session that spawned a Codex run appears as one connected graph.
- **Built for genuinely sensitive content.** Chat exports contain private material and, increasingly, *hostile* material — hidden-unicode prompt-injection payloads travel inside conversations. Every export is treated as both sensitive **and** untrusted.

---

## What it does

### Ingest — six providers, one model
Each adapter parses its native export into a small provider-agnostic IR (`llm_anthology/ir.py`); everything downstream consumes only the IR.

There are two shapes here and the difference is not cosmetic. The first four are **account exports** — one file or directory you download, rendered to HTML/MD by `llm_anthology <provider> <src> <out>`. The last two are **live session stores** on your own disk, which feed the searchable corpus index instead (`llm_anthology index`, or the cockpit's import). Codex is the only one that is both.

Every session store is **opt-in and never defaulted**: omit its flag and nothing under that path is read. That is deliberate rather than cautious — these directories hold whatever you have said to the tool, and an earlier default-to-the-live-store is how an automated probe once read the owner's real sessions.

| Provider | Input | Status |
|---|---|---|
| **Claude** | claude.ai account data export (file or directory tree) | **Validated** on 212 real conversations; text-exact gate 207/212, 0 errors. Includes `design_chats/*.json`, which use a different message shape |
| **Gemini** | Takeout "Gemini Apps" activity (+ optional web harvest) | **Validated** on 1,060 real records → 2,027 turns. Conversation **grouping is PROVISIONAL** (time-gap heuristic) unless a harvest supplies true boundaries |
| **Codex** | `rollout-*.jsonl` session files + `state_5` store | Powers the spawn tree and dedup |
| **ChatGPT** | `conversations.json` (message tree) | **UNVALIDATED at scale** — hardened adapter + synthetic tests, but no large real export has been run through it |
| **Grok** | a Grok Build session store (`<enc-cwd>/<session-id>/`) | Session store, `--grok-root`. Brings its own spawn edges from `subagents/` metas |
| **Claude Code** | the `projects/` tree under a Claude home | Session store, `--claude-root`. **UNVALIDATED at scale** — 875 lines of synthetic tests, and deliberately never run against the real store, which is private |

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
 EXPORTS                          SESSION STORES
 (claude · chatgpt ·              (codex rollouts + state DB · grok)
  gemini · codex tasks)                   │
            │                             │
            ▼                             ▼
      adapters ──▶ provider-agnostic IR ──▶ corpus + SQLite/FTS5 index
            │            (render only)            │
            │   the CLI renders an export to HTML/Markdown; only the SESSION
            │   STORES on the right are ingested into the corpus the cockpit
            │   reads. Claude Code has an adapter that is not yet wired in.
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

There are three ways in, and they are three separate artifacts of the same release. Pick
the one that matches what you want.

> **The first release is not published yet.** `llm-anthology` is unregistered on both
> [PyPI](https://pypi.org/project/llm-anthology/) and
> [npm](https://www.npmjs.com/package/llm-anthology), and this repository has no version
> tag, so the two package-manager commands below do not resolve *yet* and there is no
> installer to download. Until then, use **[From source](#from-source)**. The release
> runbook is
> [RELEASING.md](https://github.com/Prekzursil/ai-sessions-render/blob/main/RELEASING.md);
> delete this note when 0.1.0 ships.

### The desktop app — Windows

This is the product. Download `LLM Anthology_<version>_x64-setup.exe` from the
[latest release](https://github.com/Prekzursil/ai-sessions-render/releases/latest) and run
it.

It is **self-contained**: the installer carries a relocatable CPython
(python-build-standalone, staged by `cockpit/scripts/stage-engine.ps1`) with the engine
installed into it, so it needs **neither Python nor any of the packages below** on the
machine. Roughly a 14 MB download, ~60 MB installed. Each release also carries a
`.sha256` file if you want to check the download.

Two things to know before you click:

- **Windows only for now.** `bundle.targets` is `["nsis"]` and the bundled interpreter is
  a Windows build. The engine and CLI below are cross-platform; the desktop app is not
  yet.
  [RELEASING.md](https://github.com/Prekzursil/ai-sessions-render/blob/main/RELEASING.md)
  records what adding macOS/Linux would take.
- **It is not code-signed.** SmartScreen will warn on download and first run until the
  binary earns reputation. Verify the `.sha256` if that matters to you.

### The engine and CLI — Python, any OS

Requires **Python ≥ 3.9**. Two third-party runtime dependencies:
[markdown-it-py](https://github.com/executablebooks/markdown-it-py) (MIT) and
[zstandard](https://github.com/indygreg/python-zstandard) (BSD) — the latter is not
optional, because a live Codex store compresses its rollouts and without it that history
reads as zero conversations.

```bash
pip install llm-anthology
```

That gives you the `llm-anthology` command (short alias: `anth`) and the importable
`llm_anthology` package, which is also what the desktop app talks to over stdio.

### The renderer — npm, any OS

A standalone TypeScript port of the render path (no Python), for scripting an export or
embedding the renderer:

```bash
npm install -g llm-anthology     # the CLI
npm install llm-anthology        # as a library
```

Requires **Node ≥ 20**.

### From source

```bash
git clone https://github.com/Prekzursil/ai-sessions-render
cd ai-sessions-render
pip install -e ".[dev]"
python -m pytest
```

And for the desktop app:

```bash
cd cockpit
npm install
npm run tauri dev     # develop
npm run tauri build   # -> src-tauri/target/release/bundle/nsis/*.exe
```

A dev build with nothing staged into `src-tauri/resources/engine` falls back to `python`
on `PATH`, so you do not need to stage an interpreter to work on the app — run
`./scripts/stage-engine.ps1` only when you want to test the self-contained installer. See
[`cockpit/src-tauri/binaries/README.md`](cockpit/src-tauri/binaries/README.md).

The cockpit UI can also be opened in a plain browser (`npm run dev`) — outside Tauri it automatically serves an in-memory mock corpus, so the interface can be previewed, screenshotted and design-reviewed without a Rust build.

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
- **OS-level engine confinement.** The cockpit spawns its engine through one raw `CreateProcessW` with a **Job Object** carrying `KILL_ON_JOB_CLOSE` (no orphaned engine outlives the app) and `CREATE_NO_WINDOW` (no console flash). Both are active in the shipped binary.
- **AppContainer network isolation is BUILT BUT NOT ENABLED.** An AppContainer membrane with no `internetClient` capability is implemented and unit-tested (`cockpit/src-tauri/src/sidecar.rs`), but production spawns with `SpawnOpts::job_only()` — so the engine runs with your normal user network access. It does not *use* the network (see the point above), but nothing at the OS level currently stops it. Enabling the membrane is a code change, not a setting: the export destinations have to be granted to the package SID first.
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
