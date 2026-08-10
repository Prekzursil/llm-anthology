# LLM Anthology

**An offline archive of record for your AI conversations — six providers in one searchable corpus on your own disk, with agent lineage where it exists. No network. No egress.**

Your conversations with ChatGPT, Claude, Gemini, Codex, Grok and Claude Code are your working history: your decisions, your research trail, your debugging. Today they are scattered across six unrelated vendor formats — some a download you have to remember to keep, some a live store on your disk that quietly holds three copies of the same session — and none of it is greppable. LLM Anthology turns that pile into one archive you own, can search, and can still read in ten years.

**Archive first**, and the order is the point: what matters is that the record *exists*, is complete, and outlives the vendor UI. Everything else is built on top of a complete record — including the payoff. Because the corpus is cross-provider, it can show **which session spawned which**, across vendors, as one connected graph. That graph is a consequence of having the whole archive in one place, not the reason for building it.

It is two things in one binary:

- an **engine** (Python) that ingests every provider into one shared model, indexes it for full-text search, and renders faithful HTML + Markdown, and
- a **cockpit** (Tauri desktop app) that browses and searches the corpus, and draws it as a **cross-provider spawn tree** — with time-travel, diffing, and fidelity-gated export.

Everything runs offline. The engine makes zero network requests, and the pages it emits are locked down so *they* can't either. The desktop app additionally confines its engine to a Job Object so no engine can outlive the app (see [Security & privacy posture](#security--privacy-posture) for exactly what is and is not enforced).

---

## Why this exists

- **Own your AI history.** It should be readable and greppable on your disk, not trapped in a vendor UI or an unreadable export blob. A vendor can change its export format, retire a UI, or lose your account; a file on your disk with an index next to it survives all three.
- **One corpus, not six piles.** Every provider lands in the same IR and the same SQLite/FTS5 index, so one query crosses all of them. That is what makes "when did I first work on X" a question you can actually answer.
- **See the shape of your work.** Modern agent sessions *spawn* other sessions, and a flat list hides that entirely. The spawn tree is the payoff for having a complete cross-provider archive: a Claude session that spawned a Codex run appears as one connected graph.
- **Built for genuinely sensitive content.** Chat exports contain private material and, increasingly, *hostile* material — hidden-unicode prompt-injection payloads travel inside conversations. Every export is treated as both sensitive **and** untrusted.

### Prior art — and what this is not

Session-history tooling for **coding agents** already exists, and it is good at what it does: browsing, resuming, pruning and otherwise managing the on-disk session stores that Codex, Claude Code and friends write. If that is the whole of your problem, use one of those. This project does not claim to beat them at it — the author retired his own entry in that category rather than keep competing with it (see [Lineage](#lineage)), and re-implemented only the three capabilities that had no equivalent here.

The ground this one stands on is narrower:

- **Consumer chat *exports* as first-class input.** A `conversations.json` downloaded from claude.ai, a ChatGPT export with its project shards, a Google Takeout Gemini activity log — parsed faithfully, rendered, indexed and searchable *next to* your agent sessions. An agent-store browser does not read these at all, because they are not a session store; they are a one-shot archive dump with its own shape per vendor, and each one needs its own adapter.
- **Cross-provider lineage.** Agent-store tools see one vendor's tree because that is the store they read. The spawn graph here is assembled across vendors from whatever each one records — Codex's `state_5` store, Grok's `subagents/` metas — so a chain that crosses providers stays one chain.
- **A fidelity gate you can point at.** Rendering is checked, not asserted: every prose token in the parsed source must appear in the rendered output, and failures are written to a report rather than swallowed.

**This is positioning, not a benchmark.** No survey of named alternatives has been run for this repository, so read any "nothing else does this" here as the author's belief and not as a measurement. If you know of a tool that ingests consumer exports *and* draws cross-provider lineage, the honest response is to compare against it.

---

## What it does

### Ingest — six providers, one model
Each adapter parses its native export into a small provider-agnostic IR (`llm_anthology/ir.py`); everything downstream consumes only the IR.

There are two input *shapes* here and the difference is not cosmetic. An **account export** is one file or directory you downloaded (Claude, ChatGPT, Gemini Takeout, and a Codex task export). A **session store** is a live directory on your own disk that a tool keeps writing to (Codex rollouts + its `state_5` store, Grok Build, Claude Code).

Both shapes reach the corpus. `llm-anthology index [<sessions-root>] <out.sqlite>` takes the optional Codex sessions ROOT positional plus `--grok-root` / `--claude-root`, **and** the four downloaded exports via `--chatgpt-export` (with `--chatgpt-projects`), `--claude-export`, `--codex-export` and `--gemini-export` (with `--gemini-harvest`). Name as many as you like in one build; naming none is refused before anything is written. Each export file is reported individually, so a 17-shard export that silently contributed 16 is visible rather than rounded into one total.

Only the export shapes have a **render** path — `llm-anthology <provider> <src> <out>` writes the HTML + Markdown pair. A session store is indexed and read in the app; it is not rendered to a directory of pages.

**One asymmetry to know about:** the cockpit's own import (`corpus.build`) currently accepts only the three session ROOTS. To pull a downloaded export into the corpus, build the index with the CLI and point the app at it. That is a gap in the app, not in the engine.

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
One SQLite full-text query crosses every provider at once, and it narrows: `provider`, plus `since` / `until` dates given at whatever width you have (`2026`, `2026-03`, `2026-03-04`, inclusive at that width). Ask for a `histogram` and you get a hits-over-time roll-up of the whole filtered set, not just the page you are looking at, and the total for that whole set comes back with every page. So "March, in Codex only" is one query rather than a manual scan. Results are ordered by bm25 relevance with the conversation id as a tiebreaker, which is what makes paging total rather than lucky.

### Spawn tree — the payoff, not the premise
A canvas graph of which session spawned which, across providers, laid out with ELK in a worker. It is the thing the archive buys you: the edges come from whatever each vendor happened to record, so it only becomes one connected graph once every provider is in one corpus. Plus:
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
 ACCOUNT EXPORTS                        SESSION STORES
 claude · chatgpt · gemini ·            codex rollouts + state_5 DB ·
 codex task export                      grok build · claude code
 (a file or directory you               (a live tree on your own disk;
  downloaded)                            opt-in, never defaulted)
            │                                      │
            └──────────────────┬───────────────────┘
                               ▼
                            adapters
                               ▼
                    provider-agnostic IR   (llm_anthology/ir.py)
                               │
            ┌──────────────────┴────────────────────┐
            ▼                                       ▼
  render_html / render_md               corpus + SQLite/FTS5 index
  verify.py (fidelity gate)             (llm-anthology index — BOTH shapes)
  EXPORT SHAPES ONLY                                │
  -> out/html/*.html + out/md/*.md                  │
                            ┌───────────┬───────────┼───────────────┐
                            ▼           ▼           ▼               ▼
                         search    spawn tree   metadata ·      export +
                         FTS5 +    time travel  dedup ·         verify
                         facets    diff·rollup  maintenance     round-trip
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

Both input shapes reach the corpus, and only the export shapes are rendered to a directory of pages. The cockpit's own `corpus.build` is the one narrower door: it accepts the three session ROOTS, so exports are ingested with the CLI (see [Ingest](#ingest--six-providers-one-model)).

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

Requires **Python ≥ 3.9**. Three third-party runtime dependencies, none of them optional:

- [markdown-it-py](https://github.com/executablebooks/markdown-it-py) (MIT) — renders the
  Markdown that becomes the HTML you read.
- [zstandard](https://github.com/indygreg/python-zstandard) (BSD) — a live Codex store
  compresses its rollouts, and without it that history reads as zero conversations.
- [ijson](https://github.com/ICRAR/ijson) (BSD) — streams a multi-gigabyte ChatGPT export
  instead of loading it whole. Pinned `>=3.1` because `use_float=True` landed there, and
  without it every non-integer arrives as a `Decimal`, which measurably emptied every
  ChatGPT timestamp in the index.

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

The renders above are the *keep* copies. The archive itself is the index — one SQLite file
holding every provider, and the only thing the desktop app reads:

```bash
# Session stores and downloaded exports in ONE build. Every source is opt-in: a flag you
# omit is a path that is never opened, and --codex-home is what merges the spawn graph.
llm-anthology index ~/codex-sessions/ ~/anthology.sqlite \
  --codex-home ~/codex-home/ \
  --claude-export ~/exports/claude/ \
  --gemini-export ~/exports/gemini/transcript.json

# then point the app (or the engine's stdio server) at it
python -m llm_anthology.sidecar --index ~/anthology.sqlite
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
- **There is no "ask your corpus" feature, and nothing here has ever egressed.** The engine still carries `research.py`, a corpus-blind synthesis plane, and the sidecar still registers `research.synthesize` / `research.extract_entities`. Read that as dead weight pending removal, not as a capability: the cockpit deliberately binds **no** Tauri command for either method, so no button in the app can reach them, and both backends default to a no-network `MockBackend`. No cloud client is wired to anything, and there is no HTTP or socket import anywhere in the engine. While the plane is still in the tree its design holds — every public function takes only a structural-metadata projection (ids, provider, timestamps, turn/char counts), free text including conversation **titles** is excluded *by construction* because a title is derived from raw message content, and `tools/privacy_mutation_check.py` mutation-pins that allowlist so re-adding the leak cannot keep the suite green.
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
