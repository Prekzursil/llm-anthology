# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the version numbers follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

A release is three artifacts built from one commit, and they share this version number:
the `llm-anthology` PyPI package (engine + CLI), the `llm-anthology` npm package (the
TypeScript renderer port), and the Windows desktop installer attached to the GitHub
Release. See [RELEASING.md](RELEASING.md).

## [Unreleased]

Nothing yet.

## [0.1.0] — 2026-08-11

First public release. There is no earlier version, so rather than a diff against
nothing, this entry says what you get — and then, because the run-up to this release was
mostly a hardening pass, exactly which failure modes were found and closed before it
shipped. If you were tracking `main`, the second list is the one worth reading.

### What you can do with it

- **Read your conversations.** Open a session and read the actual transcript — the
  reader is windowed for very long threads and tells you what it is not currently
  showing rather than silently truncating.
- **Bring in six providers.** ChatGPT, Claude web exports, Claude Code, Gemini, Codex
  and Grok Build sessions all land in one corpus with one model behind them. Every
  source is opt-in, so a Grok-only or Claude-Code-only import is a normal thing to ask
  for.

  Counted as six rather than five — folding Claude Code into "Claude" reads naturally
  but contradicts the code, which treats them as distinct providers throughout:
  `discover.PROVIDERS` carries separate `claude` and `claude-code` entries, they get
  separate colours in the graph palette, and they take different roots because one is a
  downloaded export and the other a live session store on disk.
- **Let the app find your data.** First run scans for AI session stores already on the
  machine and shows you what it found, instead of asking you to hunt for paths. You can
  build a corpus from inside the app; the CLI equivalent is `llm-anthology index`.
- **Search the whole corpus.** FTS5 full-text search across every ingested session, with
  provider filters, dates on the hits, and an honest marker when results were cut off.
- **See the spawn tree.** The differentiator: which session spawned which, drawn as a
  forest. It renders at real-corpus scale, and when it declines to draw something it says
  so instead of showing you an empty canvas.
- **Export with a fidelity gate that can actually fail.** HTML and Markdown, with a
  verification pass that reports a mismatch rather than quietly shipping a lossy render.
- **Manage the corpus.** Deduplication, metadata repair and maintenance are reachable
  from the app, not just from the engine.
- **Run it offline.** No network, no egress, no localhost port — the engine is a stdio
  JSON-RPC server, so nothing else on the machine can talk to it. The desktop app runs
  under a strict Content-Security-Policy that is asserted against a real build.
- **Install one thing.** The Windows installer carries its own relocatable CPython with
  the engine installed into it, so an installed app needs neither Python nor this package
  on the machine.

### Fixed before the first release

Each of these was a real defect on `main`, found and closed during the pre-release pass.
They are listed because several of them silently produced *wrong or missing data* rather
than an error, which is the class most worth knowing about.

**Data that was silently being dropped**

- Compressed Codex rollouts were not read at all. A measured live store held 2043
  `rollout-*.jsonl.zst` files and zero plain `.jsonl` — so that entire history imported
  as zero conversations, and reported zero errors doing it. `zstandard` is now a required
  dependency, not an undeclared one.
- A conversation was parsed with whichever provider's parser happened to be first, not
  its own. Mixed corpora came out mangled.
- A blank `thread_id` dropped the session from the corpus entirely.
- A `%` anywhere in a path made an existing, valid index invisible to discovery.
- 140 spawn-tree roots were being dropped, and roots were duplicated across page
  boundaries in the forest view.
- The spawn graph was not persisted alongside the conversations, so it had to be
  recomputed and could disagree.
- A session that grew after being indexed stayed frozen at its old content instead of
  being re-indexed.
- A successful import threw away the record of what it had skipped, so you could not see
  what did not make it in.

**Crashes**

- The engine died outright on non-ANSI text.
- Ordinary typing could crash the search box.
- A relative `rollout_path` was rejected instead of resolved; a UNC path spelling was
  resolved before being checked.

**Privacy and safety**

- Three paths through the maintenance plane could still reach the live session store.
  Closed.
- `codex_home` is now optional, and omitting it no longer falls back to reading your
  live store.
- The desktop app now runs under a strict CSP, proven against a real Tauri build rather
  than asserted in config.

**Correctness of what you were shown**

- The adapter name was being written into the model-vendor field, so provider attribution
  was wrong. `provider` now means one thing throughout the engine and the app.
- `size_bytes` echoed whatever the caller claimed instead of measuring the file.
- The sidebar showed the oldest threads instead of the newest, and opening a thread from
  it left the detail pane empty.
- Search results had no total order, so equal-ranked hits could reorder between runs.
- Providers with no assigned tint all rendered as the same "unknown" grey.
- Focus jumped to a different control whenever a list repainted.
- Internal JSON-RPC method names were surfaced to users as if they were messages.
- Graph layout needed a round-trip per node; it now does one.
- The engine's stderr was not drained and response lines were unbounded, which could
  wedge the app.

### Infrastructure

- The desktop app is now built and tested in CI, on a Windows + Linux matrix. Before
  this, 365 cockpit tests, 18 Rust tests, the cockpit typecheck and the Tauri build had
  never run in CI on any platform — the code the release ships had never been compiled by
  a gate.
- Python coverage is gated at 100% branch coverage on three operating systems.
- A version-sync test fails the build if the five declared version strings disagree
  (`tests/test_release_version_sync.py`) — including `llm_anthology.__version__`, which
  ships inside the wheel and is what the engine reports to the app, so a missed bump
  there would have produced a correctly-named package containing an engine that
  announces the previous version.

[Unreleased]: https://github.com/Prekzursil/llm-anthology/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Prekzursil/llm-anthology/releases/tag/v0.1.0
