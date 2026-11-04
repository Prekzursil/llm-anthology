# Architecture

> **SCOPE OF THIS DOCUMENT — read this first.** It describes the **render pipeline** only:
> export → adapter → IR → HTML/Markdown, plus the two cross-checks. That was the whole
> product when this file was written and it no longer is. The corpus plane (SQLite/FTS5
> index, spawn graph, search, time travel, diff, rollup, metadata, dedup, maintenance,
> gated export) and the Tauri cockpit that reads it over stdio JSON-RPC are **not covered
> here** — their surface is documented in `README.md` and, method by method, in the module
> docstring at the top of `llm_anthology/sidecar.py`. Absence from this file is not evidence
> that a plane does not exist.

`llm-anthology` (llm_anthology) is a **local, offline** renderer for exported AI chat sessions.
Six adapters live under `llm_anthology/adapters/`: the three account-export formats this
document details — Claude (claude.ai Data Export), Gemini (Google Takeout "Gemini Apps"
activity) and ChatGPT (`conversations.json`) — plus a Codex task export (`codex.py`) and
three session-store readers (`codex_rollout.py` + `codex_state.py`, `grok.py`,
`claude_code.py`) that feed the corpus rather than the render path. All six produce the same
provider-agnostic intermediate representation (IR). The render path then writes every
conversation twice: a browser-faithful **HTML** copy for viewing and a clean **Markdown**
copy for keeping and re-feeding to tools. Two independent cross-checks run on every
conversation: a **text-exact fidelity gate** (`llm_anthology/verify.py`) and a
**hidden-character forensic audit** (`llm_anthology/audit.py`).

**No network, no egress.** Nothing is fetched and nothing leaves the machine: the HTML is
self-contained with inlined CSS and a `default-src 'none'` Content-Security-Policy
(`llm_anthology/render_html.py:33-36`), remote markdown images are defanged into labelled links
instead of `<img>` loads (`llm_anthology/render_html.py:103-121`), and the build scripts state the
guarantee explicitly (`llm_anthology/build.py`, `llm_anthology/loaders.py`). Session content is
sensitive; every example in this document and in the code (`llm-anthology demo`,
`llm_anthology/build.py`) is **synthetic**.

Runtime dependencies (all three declared in `pyproject.toml`, and all three required):
`markdown-it-py>=2.2,<5` (MIT) for the Markdown→HTML step, `zstandard>=0.22` because a live
Codex store compresses its rollouts, and `ijson>=3.1,<4` to stream a ChatGPT export too large
to load whole. The earlier note here — that `pyproject.toml` carried only metadata and pytest
config and declared no dependency — is **obsolete**; it is also the reason the version bounds
are now gate-pinned (`tests/test_dependency_rails.py`).

## Pipeline

```
native export             per-provider adapter           versioned IR (llm_anthology/ir.py)
-------------             --------------------           -------------------------

claude export.json  -->   adapters/claude.py    --+
 (message tree via         active-path walk +     |
  parent_message_uuid)     branch annotation      |     Conversation
                                                  |       Turn (role, ts, branch)
Takeout             -->   adapters/gemini.py    --+-->      Block (typed; data{},
 transcript.json           flat records -> turns, |                x_raw passthrough)
 (+ external groups)       events kept explicit   |
                                                  |
conversations.json  -->   adapters/chatgpt.py   --+
 (mapping /                current_node -> root
  current_node tree)       chain, reversed
                                      |
                                      v
                     shared sanitize + render core
                     sanitize.py (badge / strip / URL allowlist)
                          |                         |
                          v                         v
                   render_html.py             render_md.py
                   html/NNN-title.html        md/NNN-title.md
                   (theme CSS inlined,        (portable copy,
                    CSP, badged invisibles)    invisibles stripped)

        cross-checks, wired per conversation in build_*.py:
          verify.py   text-exact fidelity gate  -> _fidelity-report.json
          audit.py    hidden-char forensic scan -> _hidden-char-audit.json
```

Entry point: the `llm-anthology` console command (`llm_anthology/cli.py`), with subcommands `claude`,
`chatgpt`, `gemini` and `demo`. It delegates to `llm_anthology/loaders.py` (per-provider loading
and grouping) and `llm_anthology/build.py` (the shared render/verify/write pipeline). The ChatGPT
rail is wired (`llm-anthology chatgpt`) but has not been validated against a large real export
(`llm_anthology/adapters/chatgpt.py`).

## The IR (`llm_anthology/ir.py`)

Every adapter emits, and both renderers consume, exactly this shape. It is versioned
(`IR_VERSION = 1`, `ir.py:11`; stamped on each `Conversation` via `ir_version`,
`ir.py:47`) so a future schema change is explicit.

- **`Conversation`** (`ir.py:37-47`): `id`, `title`, `provider` (`claude | chatgpt |
  gemini`), `turns`, `created_at`, `updated_at`, `account`, `meta` (provider extras such
  as the Claude summary or Gemini gem list), `ir_version`.
- **`Turn`** (`ir.py:26-34`): `role` (`human | assistant`), `blocks`, `uuid`,
  `timestamp`, and `branch` — `{"index": i, "total": n}` when the turn's parent had
  siblings, i.e. the message sits at a regeneration/edit point.
- **`Block`** (`ir.py:14-23`): `type`, `text` (display body or label), `data` (typed
  payload), `citations`.

### Block types

The block types actually produced and rendered across the codebase:

| type          | produced by                                                 | HTML rendering (`render_html.py`)                          | MD rendering (`render_md.py`) |
|---------------|-------------------------------------------------------------|------------------------------------------------------------|-------------------------------|
| `text`        | all adapters                                                | markdown-rendered body + citation pills (`:146-147`)       | body passes through untouched, sources appended (`:66-81`) |
| `thinking`    | claude (`claude.py:199-202`), chatgpt (`chatgpt.py:111-120`)| collapsed `<details>`; labelled "hidden in claude.ai" when withheld (`:152-157`) | blockquote with a brain marker (`:86-89`) |
| `tool_use`    | claude (`claude.py:203-210`)                                | tool card with JSON input (`:178-183`)                     | bold header + fenced JSON (`:96-101`) |
| `tool_result` | claude (`claude.py:211-217`), chatgpt `execution_output` (`chatgpt.py:126-129`) | result card, error-flagged (`:171-180`) | bold header + fence (`:96-103`) |
| `attachment`  | claude (`claude.py:182-186`), gemini (`gemini.py:76-92`)    | chip + collapsible extracted document text (`:170-178`)    | chip line + `<details>` (`:100-108`) |
| `file`        | claude (`claude.py:187-190`)                                | name chip, "(no bytes in export)" (`:203-205`)             | chip line (`:140-142`) |
| `code`        | chatgpt (`chatgpt.py:122-124`)                              | `<pre><code class="language-x">` (`:192-196`)              | fenced block (`:113-114`) |
| `event`       | gemini non-prompt verbs (`gemini.py:104-114`)                 | explicit event card — never a forged reply (`:187-190`)    | quoted event line (`:111-112`) |
| `media`       | gemini (`gemini.py:93-96`), chatgpt image pointers (`chatgpt.py:137-140`) | **local relative paths only**; anything with a scheme is shown, never fetched (`:201-208`) | image link to the local path (`:117-120`) |
| `unknown`     | claude (`claude.py:218-220`), chatgpt (`chatgpt.py:142-147`)| "unrendered X block" card **showing the payload** (`:209-209`) | tagged fence with the payload (`:122-131`) |

Drift note (honest): the comment at `ir.py:18` enumerates
`text|thinking|tool_use|tool_result|attachment|file|image|unknown` — it omits
`code`/`event`/`media`, and no adapter emits `image` today (`media` is the real type; an
`image` block would fall through to the markdown default at `render_html.py:261`).

### `x_raw` passthrough — nothing dropped at the IR layer

An unrecognised content item is never silently discarded. The Claude adapter wraps it as
`Block("unknown", data={"orig_type": t, "x_raw": item})` (`claude.py:218-220`); the
ChatGPT adapter does the same for unknown parts and content types
(`chatgpt.py:142-147`). Both renderers then **show the raw payload**, not just the type
name, because a future block type may carry real user text (`render_html.py:249-260`,
`render_md.py:145-154`). The only deliberate drop is Claude's `token_budget` content
item, which is hidden UI state, not content (`claude.py:23`, `claude.py:194-195`).

## Adapters — per-provider specifics

### Claude (`llm_anthology/adapters/claude.py`) — a message tree via `parent_message_uuid`

The export is an array of conversations, each with `chat_messages[]`; messages form a
**tree** through `parent_message_uuid` (schema probed from real exports on disk,
`claude.py:3-18`; 91 real branch points measured in the corpus, `claude.py:16-17`).

The adapter renders the **active path**:

- Children are indexed per parent (`claude.py:129-132`), and at each node the walk
  descends into the child whose **subtree contains the globally newest `created_at`**
  (`claude.py:153-156`), computed iteratively and cycle-safely by `_subtree_max_ts`
  (`claude.py:81-121`). Picking merely the newest *immediate* child is wrong: the locally
  newest child is often an abandoned regeneration while the live thread continues under
  an older sibling (`claude.py:84-87`).
- **Branch annotation:** when a message's parent had multiple children, the turn carries
  `branch={"index": i, "total": n}` with siblings ordered by timestamp
  (`claude.py:147-151`); the renderers surface it as an `i/n` badge
  (`render_html.py:270`) or an italic `_(branch i/n)_` header suffix (`render_md.py:69-70`).
- **Multiple roots:** a message whose parent uuid resolves to nothing starts a second
  root; every root is walked in chronological order because dropping extra roots loses
  real content (measured: 42 messages across 18 conversations, `claude.py:134-143`).
- **Orphan sweep:** nodes reachable from *no* root (e.g. an orphaned cycle) are appended
  at the end; branch siblings are deliberately *not* swept in, since that would splice
  abandoned regenerations into the transcript (`claude.py:158-176`).

Content mapping (`claude.py:179-221`): attachments and files render above the message
text as on claude.ai; `text` keeps its citations; `thinking` keeps its hidden flag and
summaries; `tool_use`/`tool_result` keep name, input/content, display text, error flag
and integration metadata. `display_content` is coerced to `str` defensively because the
export sometimes stores a structured list there (`claude.py:26-29`).

### Gemini (`llm_anthology/adapters/gemini.py`) — a flat Takeout log needing external grouping

Takeout is a **flat activity log**: each record is one exchange with **no conversation
id** (`gemini.py:3-6`). Grouping therefore comes from *outside* the adapter and is passed
in as `groups` (`gemini.py:22-41`); the adapter itself only turns records into IR turns.

- Schema probed from the real `transcript.json`, 1060 records (`gemini.py:8-11`). Verbs
  on disk: `Prompted` 967, `Used` 60, `Created Gemini Canvas` 26, `Gave feedback` 6,
  `Selected` 1 (`gemini.py:12-13`).
- Only `Prompted` is a real exchange and becomes a human turn + an assistant turn
  (`gemini.py:116-119`). Every other verb is a **feature event** rendered as a single
  explicit `event` turn — "never as a fabricated model reply" (`gemini.py:104-114`). That
  arithmetic is why the real-corpus build reports 2·967 + 93 = **2027 turns**
  (`llm_anthology/loaders.py` prints exactly this expectation).
- Grouping strategies live in `llm_anthology/loaders.py`:
  - **Harvest (TRUE grouping):** with a `gemini_full_harvest.json` from the live web app
    (the only system that knows the real boundaries), each harvested user turn is joined
    to its Takeout record by exact normalised prompt text; unmatched records are reported
    in an `(unmatched Takeout activity)` group, never dropped (`llm_anthology/loaders.py`).
  - **Gap heuristic (PROVISIONAL):** without a harvest, split on a >30-minute gap or a
    Gem change, with every group explicitly titled "(provisional group N)" so a reader
    never mistakes it for ground truth (`llm_anthology/loaders.py`, `llm_anthology/loaders.py`).
  - The mode used is stamped into `_fidelity-report.json` as `grouping_mode`
    (`llm_anthology/loaders.py`).

### ChatGPT (`llm_anthology/adapters/chatgpt.py`) — a `mapping`/`current_node` tree

Unlike the other two, ChatGPT stores a **message tree keyed by node id**:
`conversation{title, create_time, current_node, mapping{node_id: {id, message, parent,
children}}}` (`chatgpt.py:1-8`).

- The rendered thread is `current_node -> parent -> ... -> root`, **reversed**, walked
  cycle-safely (`chatgpt.py:80-91`). Every other child of a branching node is an
  abandoned regeneration and must not be rendered, or discarded replies silently mix into
  the transcript (`chatgpt.py:10-13`).
- Hidden nodes: `system`-role messages and anything flagged
  `metadata.is_visually_hidden_from_conversation` are skipped; `tool`-role messages
  render on the assistant side (`chatgpt.py:23`, `chatgpt.py:94-103`).
- Consecutive assistant nodes merge into **one visual turn** (mirroring how the app
  displays a multi-node reply) (`chatgpt.py:43-46`).
- Content types handled: `text` / `multimodal_text` (including `image_asset_pointer` ->
  `media`), `code` (with language), `thoughts` / `reasoning_recap` -> `thinking`,
  `execution_output` -> `tool_result`; anything else passes through as `unknown` with
  `x_raw` (`chatgpt.py:106-147`). Unix-epoch `create_time` values are converted to ISO
  timestamps (`chatgpt.py:69-76`).
- **Status (honest):** "NOT YET VALIDATED against a real export — synthetic tests only;
  re-verify when a real conversations.json lands" (`chatgpt.py:18-19`).

## Shared sanitise + render core

### `llm_anthology/sanitize.py` — hidden-unicode neutraliser + link allowlist

The corpus was flagged for hidden zero-width / bidi / private-use / TAG-block
prompt-injection characters. The policy (`sanitize.py:1-19`):

- **Preserve the evidence but neutralise it.** In HTML, every flagged codepoint becomes a
  visible inert badge `<span class="cp-badge" data-cp="U+XXXX">` (`sanitize.py:81-91`) —
  never the raw invisible character — so a reader and a diff can see exactly what was
  there.
- **Raw HTML in bodies is escaped** (`html=False` markdown), matching how chatgpt.com /
  claude.ai / gemini treat message bodies as text, not live markup (`sanitize.py:8-10`).
- **Legitimate emoji survive.** The flag decision is position-aware (`_flagged_at`,
  `sanitize.py:66-78`): a ZWJ *between two pictographs* and a VS16 *directly after* a
  pictograph are kept; the same codepoints anywhere else are flagged.
- Flagged classes (`sanitize.py:43-63`): the TAG block `U+E0000-E007F`; **all variation
  selectors** `U+FE00-FE0F` and `U+E0100-E01EF` (the 256-value channel behind modern
  invisible-text smuggling — category `Mn`, so no category test catches them); invisible
  Hangul fillers (`sanitize.py:27-29`); `Cc` controls except tab/LF/CR; and categories
  `Cf`, `Co`, `Cn`, `Cs` (a lone surrogate survives `json.loads` but would abort a UTF-8
  write — see the `errors="replace"` writer at `llm_anthology/build.py`).
- **Callers must pass decoded strings**: Claude exports store invisibles as `\uXXXX` JSON
  escapes, so scanning raw file bytes reports a false "clean" (`sanitize.py:16-19`).

Surfaces exported (`sanitize.py:99-156`): `neutralize_html` (escape + badge),
`badge_invisibles` (badge only, for already-rendered fragments), `ncr_invisibles` (inert
numeric character references, for use *inside* tags/attributes where a badge `<span>`
would break the markup), `scan_invisibles` (audit list of `(index, U+XXXX)` hits),
`sanitize_for_copy` (strip entirely — the clipboard/agent-feed surface), and
`is_safe_url` (scheme allowlist: `http://`/`https://` only, killing `javascript:`,
`data:`, `file:` — `sanitize.py:153-156`).

### `llm_anthology/render_html.py` — IR -> browser-faithful static HTML (the "view" copy)

- Markdown via `markdown-it-py` with the `gfm-like` preset and `html=False`, falling back
  to `commonmark` on older library versions (`render_html.py:24-26`).
- **Theme pack:** `render_conversation_html(conv, theme="claude")` inlines
  `llm_anthology/themes/<theme>.css` into a `<style>` tag (`render_html.py:28`,
  `render_html.py:43-48`, `render_html.py:281-284`). One theme ships today —
  `claude.css`, a grounded claude.ai dark theme; a light theme is a documented follow-up
  (`llm_anthology/themes/claude.css:1-2`).
- **CSP:** `default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; ...`,
  no scripts anywhere (`render_html.py:33-36`).
- **Link hardening fails closed:** *every* anchor is rewritten; safe URLs get
  `rel="noopener noreferrer" target="_blank"`, anything unverifiable is defanged to
  `<a class="unsafe" data-unsafe-href="...">` inert text, and an anchor whose href can't
  be parsed is stripped to `<a>` (`render_html.py:51-84`).
- **Images are defanged:** a markdown image would emit a remote `<img src>`, which CSP
  blocks into a broken image that quietly contradicts the no-egress promise; instead it
  becomes a chip naming the alt text plus an explicit "(remote image — not fetched)"
  link (`render_html.py:103-121`). Only `media` blocks with **local relative paths** ever
  produce a real `<img>` (`render_html.py:241-248`).
- **Math is held out of the markdown pass** and restored verbatim (escaped): CommonMark
  would strip backslashes before punctuation and mutate TeX irrecoverably, invisible to a
  token gate. Unrendered-but-intact beats mutated; KaTeX rendering and server-side code
  highlighting are documented follow-ups (`render_html.py:38-40`, `render_html.py:124-142`).
- **Badging is text-node-only:** the rendered fragment is split on tags; text nodes get
  visible badges, but inside a tag (a link title, an alt, a code-fence info string) an
  inert numeric character reference is emitted instead, so evidence is preserved without
  corrupting markup (`render_html.py:87-100`).
- Known cosmetic limitation: the assistant label and avatar are hard-coded to
  "Claude"/"C" for every provider (`render_html.py:266-269`).

### `llm_anthology/render_md.py` — IR -> clean, portable Markdown (the "keep" copy)

- **Content-faithful pass-through:** provider bodies are already Markdown, so `text`
  passes through untouched (`render_md.py:1-7`, `render_md.py:82-100`).
- **Hidden unicode is STRIPPED here** (via `sanitize_for_copy`, `render_md.py:78-79`) —
  this file may be re-fed to a model, so no invisible injection payload survives. The
  HTML renderer *badges* the same characters for forensic viewing; the two outputs are
  deliberately different surfaces of the same policy.
- **Header-forgery defence:** single-line fields (titles, names, languages) have newlines
  collapsed so a value can never forge a `## Human` turn header inside the file
  (`render_md.py:31-34`); link text is bracket-escaped and URLs are percent-encoded so a
  `)` in a URL or a `]` in a title cannot forge a second live link
  (`render_md.py:15-28`, `render_md.py:38-50`, `render_md.py:91-97`).
- Document shape: `# title`, a `>` metadata line, then `## Human` / `## Assistant`
  headers (with `_(branch i/n)_` where applicable), blocks, and `---` separators
  (`render_md.py:53-75`).

## Cross-checks

### `llm_anthology/verify.py` — the text-exact fidelity gate

The achievable "faithful" contract: **pixel equality with the live page is impossible;
TEXT-exactness is the hard gate** — every prose word in the source IR must survive into
the rendered HTML (`verify.py:1-9`). This catches any rendering bug that silently drops
or garbles content.

- Source side: word tokens (`\w+`, lowercased) from `text` and `thinking` blocks only;
  tool/attachment payloads are structural and excluded from the *prose* gate
  (`verify.py:43-53`). Ordered-list markers are dropped as structural — `<ol>` renders
  the number via a CSS counter, so it is visible but absent from DOM text
  (`verify.py:29-32`).
- Rendered side: strip badge spans, `<style>` and `<head>` (so CSS/`<title>` can't mask a
  real drop), replace tags with spaces, then also harvest the attribute values that can
  legitimately hold source words — `href/src/alt/title` and `language-*` classes; `class`
  in general is deliberately excluded so shell class names can't mask a dropped body word
  (`verify.py:21-28`, `verify.py:56-62`).
- Comparison is a **multiset difference** of `Counter`s; `verify()` returns
  `{ok, missing_tokens, coverage}` and any non-empty `missing_tokens` fails the gate
  (`verify.py:65-75`).

### `llm_anthology/audit.py` — the hidden-character forensic audit

Complementary coverage: while the fidelity gate reads only prose, the audit scans
**every string a hidden codepoint could hide in** — the conversation title and account,
every block's text, `extracted_content`/`file_name`/`name`/`integration_name`, tool
`input`/`content` (JSON-dumped when structured), and citation titles/URLs
(`audit.py:16-41`). Scanning only `Block.text` under-reported by roughly 5x, because
injected payloads mostly sit in uploaded-document text, tool I/O, or titles
(`audit.py:1-6`). `hidden_char_hits` flattens `sanitize.scan_invisibles` hits per
conversation (`audit.py:44-49`).

## Build drivers

`llm_anthology/build.py` (`llm_anthology/build.py`) and `llm_anthology/loaders.py`
(`llm_anthology/loaders.py`) wire the pipeline per conversation and write:

- `<out>/html/NNN-title.html` and `<out>/md/NNN-title.md` (Windows-illegal filename
  characters scrubbed, names truncated — `llm_anthology/build.py`);
- `index.html` (Claude build, `llm_anthology/build.py`);
- `_hidden-char-audit.json` — per-conversation hit counts and codepoints;
- `_fidelity-report.json` — pass/fail per conversation with coverage and a
  missing-token sample (plus `grouping_mode` for Gemini).

Robustness decisions: each conversation renders inside its own `try/except` so one
malformed conversation can never truncate the corpus (`llm_anthology/build.py`,
`llm_anthology/build.py`); the input glob excludes the output directory so the tool never
ingests its own output (`llm_anthology/build.py`); writes use `errors="replace"` so an
unpaired surrogate that slipped through cannot abort a build mid-corpus
(`llm_anthology/build.py`); both builds end with terminal-state stdout markers
(`FIDELITY_GATE_PASSED`, `ERRORS`, ... — `llm_anthology/build.py`,
`llm_anthology/loaders.py`). `llm-anthology demo` renders a fully synthetic
conversation exercising every major block type, with no real content
(`llm_anthology/build.py`).

## Status and known gaps

Validation status (as reported from the real-corpus builds; the corpus itself is private
and not part of the repo):

- **Claude rail:** validated on real conversations, and the two figures in this repository
  **disagree**: 236 conversations / gate 231 here, against 212 / gate 207 in `README.md`.
  212 / 207 is the later pair and supersedes this one — the earlier count included
  non-conversation metadata artefacts as conversations. **UNVERIFIED:** the exact composition
  of the difference. Neither number is reproducible from the repository (the corpus is
  private), so the settling experiment is the owner re-running the Claude build over the
  current export and reading `_fidelity-report.json` — its per-conversation rows are the only
  authority, and whichever pair that produces should replace both.
- **Gemini rail:** 1060 real Takeout records -> 2027 turns (consistent with the verb
  census at `gemini.py:12-13`: 2·967 Prompted + 93 events). Grouping is **provisional**
  (gap heuristic) until a web-app harvest supplies true boundaries
  (`llm_anthology/loaders.py`).
- **ChatGPT rail:** **UNVALIDATED at scale** — a hardened adapter with synthetic tests, but
  no large real export has been run through it. The old "no build driver yet" half of this
  line is obsolete: `llm-anthology chatgpt <src> <out>` exists (`cli.py` -> the `chatgpt`
  subcommand -> `loaders.load_chatgpt`), and `llm-anthology index --chatgpt-export` puts the
  same export into the corpus.

Known limitations, all deliberate and documented in-code:

- The gate is **text-exact, not pixel-identical** (`verify.py:3-6`).
- Math is preserved verbatim, not rendered; code is not syntax-highlighted
  (`render_html.py:9-11`).
- One **conversation-page** theme: `llm_anthology/themes/` holds exactly `claude.css`, and
  `render_conversation_html` defaults to `theme="claude"`. (Not to be confused with
  `build.THEMES`, which does carry four per-provider colour sets — those style the generated
  `index.html` only, not the pages themselves.) The assistant label is hard-coded for every
  provider: `render_html.py:271` reads `who = "You" if role == "human" else "Claude"`, so a
  rendered Gemini or ChatGPT page attributes its replies to Claude. Cosmetic, but it is a
  wrong label rather than a missing one.
- `ir.py`'s block-type comment on `Block.type` lags the emitted set, and now measurably:
  the comment reads `text|thinking|tool_use|tool_result|attachment|file|image|unknown`, while
  the constructors across the adapters emit `attachment, code, event, file, media, text,
  thinking, tool_result, tool_use, unknown`. So it advertises `image`, which nothing emits,
  and omits `code`, `event` and `media`. Not fixed here — `ir.py` is source, not
  documentation, and this file only records the drift.

## Tests

**1677 tests** under `tests/` as of 2026-08-10, measured with
`python -m pytest --collect-only` and reading the `N tests collected` line. This number
drifts by design — recount it rather than trusting it, and do not restore the old per-module
breakdown (sanitize 22, render_html 20, ...) that stood here when the total was 101: a
hand-maintained census of a suite that grows every week is stale the day after it is written.
`conftest.py` pins the project root onto `pythonpath` alongside `pyproject.toml`'s pytest
config.

Do **not** add a second `-q`: `pyproject.toml`'s `addopts` already carries one, and `-qq`
suppresses the pass/fail summary line, which reads as a broken suite.
