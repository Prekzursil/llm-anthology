# Architecture

`llm-anthology` (llm_anthology) is a **local, offline** renderer for exported AI chat sessions.
It parses the native export formats of Claude (claude.ai Data Export), Gemini (Google
Takeout "Gemini Apps" activity), and ChatGPT (`conversations.json`) into one
provider-agnostic intermediate representation (IR), then renders every conversation twice:
a browser-faithful **HTML** copy for viewing and a clean **Markdown** copy for keeping and
re-feeding to tools. Two independent cross-checks run on every conversation: a
**text-exact fidelity gate** (`llm_anthology/verify.py`) and a **hidden-character forensic audit**
(`llm_anthology/audit.py`).

**No network, no egress.** Nothing is fetched and nothing leaves the machine: the HTML is
self-contained with inlined CSS and a `default-src 'none'` Content-Security-Policy
(`llm_anthology/render_html.py:32-33`), remote markdown images are defanged into labelled links
instead of `<img>` loads (`llm_anthology/render_html.py:88-102`), and the build scripts state the
guarantee explicitly (`llm_anthology/build.py`, `llm_anthology/loaders.py`). Session content is
sensitive; every example in this document and in the code (`llm-anthology demo`,
`llm_anthology/build.py`) is **synthetic**.

Runtime dependency: `markdown-it-py` (MIT), imported at `llm_anthology/render_html.py:18`.
(Note: `pyproject.toml` currently carries only project metadata and pytest config —
`pyproject.toml:1-10` — it does not declare the dependency.)

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
(`IR_VERSION = 1`, `ir.py:10`; stamped on each `Conversation` via `ir_version`,
`ir.py:42`) so a future schema change is explicit.

- **`Conversation`** (`ir.py:32-42`): `id`, `title`, `provider` (`claude | chatgpt |
  gemini`), `turns`, `created_at`, `updated_at`, `account`, `meta` (provider extras such
  as the Claude summary or Gemini gem list), `ir_version`.
- **`Turn`** (`ir.py:22-29`): `role` (`human | assistant`), `blocks`, `uuid`,
  `timestamp`, and `branch` — `{"index": i, "total": n}` when the turn's parent had
  siblings, i.e. the message sits at a regeneration/edit point.
- **`Block`** (`ir.py:13-19`): `type`, `text` (display body or label), `data` (typed
  payload), `citations`.

### Block types

The block types actually produced and rendered across the codebase:

| type          | produced by                                                 | HTML rendering (`render_html.py`)                          | MD rendering (`render_md.py`) |
|---------------|-------------------------------------------------------------|------------------------------------------------------------|-------------------------------|
| `text`        | all adapters                                                | markdown-rendered body + citation pills (`:146-147`)       | body passes through untouched, sources appended (`:66-81`) |
| `thinking`    | claude (`claude.py:175-178`), chatgpt (`chatgpt.py:107-117`)| collapsed `<details>`; labelled "hidden in claude.ai" when withheld (`:148-153`) | blockquote with a brain marker (`:82-85`) |
| `tool_use`    | claude (`claude.py:179-183`)                                | tool card with JSON input (`:154-159`)                     | bold header + fenced JSON (`:86-91`) |
| `tool_result` | claude (`claude.py:184-190`), chatgpt `execution_output` (`chatgpt.py:123-126`) | result card, error-flagged (`:160-169`) | bold header + fence (`:92-99`) |
| `attachment`  | claude (`claude.py:158-162`), gemini (`gemini.py:59-67`)    | chip + collapsible extracted document text (`:170-178`)    | chip line + `<details>` (`:100-108`) |
| `file`        | claude (`claude.py:163-166`)                                | name chip, "(no bytes in export)" (`:179-181`)             | chip line (`:117-118`) |
| `code`        | chatgpt (`chatgpt.py:119-121`)                              | `<pre><code class="language-x">` (`:182-186`)              | fenced block (`:109-110`) |
| `event`       | gemini non-prompt verbs (`gemini.py:79-85`)                 | explicit event card — never a forged reply (`:187-190`)    | quoted event line (`:111-112`) |
| `media`       | gemini (`gemini.py:68-71`), chatgpt image pointers (`chatgpt.py:134-137`) | **local relative paths only**; anything with a scheme is shown, never fetched (`:191-198`) | image link to the local path (`:113-116`) |
| `unknown`     | claude (`claude.py:191-193`), chatgpt (`chatgpt.py:139-144`)| "unrendered X block" card **showing the payload** (`:199-208`) | tagged fence with the payload (`:119-128`) |

Drift note (honest): the comment at `ir.py:16` enumerates
`text|thinking|tool_use|tool_result|attachment|file|image|unknown` — it omits
`code`/`event`/`media`, and no adapter emits `image` today (`media` is the real type; an
`image` block would fall through to the markdown default at `render_html.py:209`).

### `x_raw` passthrough — nothing dropped at the IR layer

An unrecognised content item is never silently discarded. The Claude adapter wraps it as
`Block("unknown", data={"orig_type": t, "x_raw": item})` (`claude.py:191-193`); the
ChatGPT adapter does the same for unknown parts and content types
(`chatgpt.py:139-144`). Both renderers then **show the raw payload**, not just the type
name, because a future block type may carry real user text (`render_html.py:199-208`,
`render_md.py:119-128`). The only deliberate drop is Claude's `token_budget` content
item, which is hidden UI state, not content (`claude.py:22`, `claude.py:170-171`).

## Adapters — per-provider specifics

### Claude (`llm_anthology/adapters/claude.py`) — a message tree via `parent_message_uuid`

The export is an array of conversations, each with `chat_messages[]`; messages form a
**tree** through `parent_message_uuid` (schema probed from real exports on disk,
`claude.py:3-18`; 91 real branch points measured in the corpus, `claude.py:16-17`).

The adapter renders the **active path**:

- Children are indexed per parent (`claude.py:107-110`), and at each node the walk
  descends into the child whose **subtree contains the globally newest `created_at`**
  (`claude.py:129-132`), computed iteratively and cycle-safely by `_subtree_max_ts`
  (`claude.py:71-102`). Picking merely the newest *immediate* child is wrong: the locally
  newest child is often an abandoned regeneration while the live thread continues under
  an older sibling (`claude.py:74-77`).
- **Branch annotation:** when a message's parent had multiple children, the turn carries
  `branch={"index": i, "total": n}` with siblings ordered by timestamp
  (`claude.py:123-127`); the renderers surface it as an `i/n` badge
  (`render_html.py:216`) or an italic `_(branch i/n)_` header suffix (`render_md.py:53-54`).
- **Multiple roots:** a message whose parent uuid resolves to nothing starts a second
  root; every root is walked in chronological order because dropping extra roots loses
  real content (measured: 42 messages across 18 conversations, `claude.py:112-119`).
- **Orphan sweep:** nodes reachable from *no* root (e.g. an orphaned cycle) are appended
  at the end; branch siblings are deliberately *not* swept in, since that would splice
  abandoned regenerations into the transcript (`claude.py:134-152`).

Content mapping (`claude.py:155-194`): attachments and files render above the message
text as on claude.ai; `text` keeps its citations; `thinking` keeps its hidden flag and
summaries; `tool_use`/`tool_result` keep name, input/content, display text, error flag
and integration metadata. `display_content` is coerced to `str` defensively because the
export sometimes stores a structured list there (`claude.py:25-28`).

### Gemini (`llm_anthology/adapters/gemini.py`) — a flat Takeout log needing external grouping

Takeout is a **flat activity log**: each record is one exchange with **no conversation
id** (`gemini.py:3-6`). Grouping therefore comes from *outside* the adapter and is passed
in as `groups` (`gemini.py:21-29`); the adapter itself only turns records into IR turns.

- Schema probed from the real `transcript.json`, 1060 records (`gemini.py:8-11`). Verbs
  on disk: `Prompted` 967, `Used` 60, `Created Gemini Canvas` 26, `Gave feedback` 6,
  `Selected` 1 (`gemini.py:12-13`).
- Only `Prompted` is a real exchange and becomes a human turn + an assistant turn
  (`gemini.py:87-90`). Every other verb is a **feature event** rendered as a single
  explicit `event` turn — "never as a fabricated model reply" (`gemini.py:79-85`). That
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
  cycle-safely (`chatgpt.py:76-87`). Every other child of a branching node is an
  abandoned regeneration and must not be rendered, or discarded replies silently mix into
  the transcript (`chatgpt.py:10-13`).
- Hidden nodes: `system`-role messages and anything flagged
  `metadata.is_visually_hidden_from_conversation` are skipped; `tool`-role messages
  render on the assistant side (`chatgpt.py:23`, `chatgpt.py:90-99`).
- Consecutive assistant nodes merge into **one visual turn** (mirroring how the app
  displays a multi-node reply) (`chatgpt.py:42-45`).
- Content types handled: `text` / `multimodal_text` (including `image_asset_pointer` ->
  `media`), `code` (with language), `thoughts` / `reasoning_recap` -> `thinking`,
  `execution_output` -> `tool_result`; anything else passes through as `unknown` with
  `x_raw` (`chatgpt.py:102-144`). Unix-epoch `create_time` values are converted to ISO
  timestamps (`chatgpt.py:66-73`).
- **Status (honest):** "NOT YET VALIDATED against a real export — synthetic tests only;
  re-verify when a real conversations.json lands" (`chatgpt.py:18-19`).

## Shared sanitise + render core

### `llm_anthology/sanitize.py` — hidden-unicode neutraliser + link allowlist

The corpus was flagged for hidden zero-width / bidi / private-use / TAG-block
prompt-injection characters. The policy (`sanitize.py:1-19`):

- **Preserve the evidence but neutralise it.** In HTML, every flagged codepoint becomes a
  visible inert badge `<span class="cp-badge" data-cp="U+XXXX">` (`sanitize.py:80-88`) —
  never the raw invisible character — so a reader and a diff can see exactly what was
  there.
- **Raw HTML in bodies is escaped** (`html=False` markdown), matching how chatgpt.com /
  claude.ai / gemini treat message bodies as text, not live markup (`sanitize.py:8-10`).
- **Legitimate emoji survive.** The flag decision is position-aware (`_flagged_at`,
  `sanitize.py:65-77`): a ZWJ *between two pictographs* and a VS16 *directly after* a
  pictograph are kept; the same codepoints anywhere else are flagged.
- Flagged classes (`sanitize.py:42-62`): the TAG block `U+E0000-E007F`; **all variation
  selectors** `U+FE00-FE0F` and `U+E0100-E01EF` (the 256-value channel behind modern
  invisible-text smuggling — category `Mn`, so no category test catches them); invisible
  Hangul fillers (`sanitize.py:26-28`); `Cc` controls except tab/LF/CR; and categories
  `Cf`, `Co`, `Cn`, `Cs` (a lone surrogate survives `json.loads` but would abort a UTF-8
  write — see the `errors="replace"` writer at `llm_anthology/build.py`).
- **Callers must pass decoded strings**: Claude exports store invisibles as `\uXXXX` JSON
  escapes, so scanning raw file bytes reports a false "clean" (`sanitize.py:16-19`).

Surfaces exported (`sanitize.py:96-153`): `neutralize_html` (escape + badge),
`badge_invisibles` (badge only, for already-rendered fragments), `ncr_invisibles` (inert
numeric character references, for use *inside* tags/attributes where a badge `<span>`
would break the markup), `scan_invisibles` (audit list of `(index, U+XXXX)` hits),
`sanitize_for_copy` (strip entirely — the clipboard/agent-feed surface), and
`is_safe_url` (scheme allowlist: `http://`/`https://` only, killing `javascript:`,
`data:`, `file:` — `sanitize.py:150-153`).

### `llm_anthology/render_html.py` — IR -> browser-faithful static HTML (the "view" copy)

- Markdown via `markdown-it-py` with the `gfm-like` preset and `html=False`, falling back
  to `commonmark` on older library versions (`render_html.py:23-25`).
- **Theme pack:** `render_conversation_html(conv, theme="claude")` inlines
  `llm_anthology/themes/<theme>.css` into a `<style>` tag (`render_html.py:27`,
  `render_html.py:40-45`, `render_html.py:222-224`). One theme ships today —
  `claude.css`, a grounded claude.ai dark theme; a light theme is a documented follow-up
  (`llm_anthology/themes/claude.css:1-2`).
- **CSP:** `default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; ...`,
  no scripts anywhere (`render_html.py:32-33`).
- **Link hardening fails closed:** *every* anchor is rewritten; safe URLs get
  `rel="noopener noreferrer" target="_blank"`, anything unverifiable is defanged to
  `<a class="unsafe" data-unsafe-href="...">` inert text, and an anchor whose href can't
  be parsed is stripped to `<a>` (`render_html.py:48-71`).
- **Images are defanged:** a markdown image would emit a remote `<img src>`, which CSP
  blocks into a broken image that quietly contradicts the no-egress promise; instead it
  becomes a chip naming the alt text plus an explicit "(remote image — not fetched)"
  link (`render_html.py:88-102`). Only `media` blocks with **local relative paths** ever
  produce a real `<img>` (`render_html.py:191-198`).
- **Math is held out of the markdown pass** and restored verbatim (escaped): CommonMark
  would strip backslashes before punctuation and mutate TeX irrecoverably, invisible to a
  token gate. Unrendered-but-intact beats mutated; KaTeX rendering and server-side code
  highlighting are documented follow-ups (`render_html.py:35-37`, `render_html.py:105-123`).
- **Badging is text-node-only:** the rendered fragment is split on tags; text nodes get
  visible badges, but inside a tag (a link title, an alt, a code-fence info string) an
  inert numeric character reference is emitted instead, so evidence is preserved without
  corrupting markup (`render_html.py:74-85`).
- Known cosmetic limitation: the assistant label and avatar are hard-coded to
  "Claude"/"C" for every provider (`render_html.py:212-215`).

### `llm_anthology/render_md.py` — IR -> clean, portable Markdown (the "keep" copy)

- **Content-faithful pass-through:** provider bodies are already Markdown, so `text`
  passes through untouched (`render_md.py:1-7`, `render_md.py:66-81`).
- **Hidden unicode is STRIPPED here** (via `sanitize_for_copy`, `render_md.py:62-63`) —
  this file may be re-fed to a model, so no invisible injection payload survives. The
  HTML renderer *badges* the same characters for forensic viewing; the two outputs are
  deliberately different surfaces of the same policy.
- **Header-forgery defence:** single-line fields (titles, names, languages) have newlines
  collapsed so a value can never forge a `## Human` turn header inside the file
  (`render_md.py:19-22`); link text is bracket-escaped and URLs are percent-encoded so a
  `)` in a URL or a `]` in a title cannot forge a second live link
  (`render_md.py:14-16`, `render_md.py:26-38`, `render_md.py:75-78`).
- Document shape: `# title`, a `>` metadata line, then `## Human` / `## Assistant`
  headers (with `_(branch i/n)_` where applicable), blocks, and `---` separators
  (`render_md.py:41-59`).

## Cross-checks

### `llm_anthology/verify.py` — the text-exact fidelity gate

The achievable "faithful" contract: **pixel equality with the live page is impossible;
TEXT-exactness is the hard gate** — every prose word in the source IR must survive into
the rendered HTML (`verify.py:1-9`). This catches any rendering bug that silently drops
or garbles content.

- Source side: word tokens (`\w+`, lowercased) from `text` and `thinking` blocks only;
  tool/attachment payloads are structural and excluded from the *prose* gate
  (`verify.py:42-52`). Ordered-list markers are dropped as structural — `<ol>` renders
  the number via a CSS counter, so it is visible but absent from DOM text
  (`verify.py:28-31`).
- Rendered side: strip badge spans, `<style>` and `<head>` (so CSS/`<title>` can't mask a
  real drop), replace tags with spaces, then also harvest the attribute values that can
  legitimately hold source words — `href/src/alt/title` and `language-*` classes; `class`
  in general is deliberately excluded so shell class names can't mask a dropped body word
  (`verify.py:20-27`, `verify.py:55-61`).
- Comparison is a **multiset difference** of `Counter`s; `verify()` returns
  `{ok, missing_tokens, coverage}` and any non-empty `missing_tokens` fails the gate
  (`verify.py:64-74`).

### `llm_anthology/audit.py` — the hidden-character forensic audit

Complementary coverage: while the fidelity gate reads only prose, the audit scans
**every string a hidden codepoint could hide in** — the conversation title and account,
every block's text, `extracted_content`/`file_name`/`name`/`integration_name`, tool
`input`/`content` (JSON-dumped when structured), and citation titles/URLs
(`audit.py:15-40`). Scanning only `Block.text` under-reported by roughly 5x, because
injected payloads mostly sit in uploaded-document text, tool I/O, or titles
(`audit.py:1-6`). `hidden_char_hits` flattens `sanitize.scan_invisibles` hits per
conversation (`audit.py:43-48`).

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

- **Claude rail:** validated on 236 real conversations; fidelity gate passed 231/236.
- **Gemini rail:** 1060 real Takeout records -> 2027 turns (consistent with the verb
  census at `gemini.py:12-13`: 2·967 Prompted + 93 events). Grouping is **provisional**
  (gap heuristic) until a web-app harvest supplies true boundaries
  (`llm_anthology/loaders.py`).
- **ChatGPT rail:** adapter written, **UNVERIFIED** against a real export
  (`chatgpt.py:18-19`); no build driver yet.

Known limitations, all deliberate and documented in-code:

- The gate is **text-exact, not pixel-identical** (`verify.py:3-6`).
- Math is preserved verbatim, not rendered; code is not syntax-highlighted
  (`render_html.py:9-11`).
- One theme (`claude`); assistant label hard-coded "Claude" for all providers
  (`render_html.py:214`).
- `ir.py:16`'s block-type comment lags the real emitted set (see the table above).
- `markdown-it-py` is not declared as a dependency in `pyproject.toml`.

## Tests

101 tests under `tests/` (counted via `pytest --collect-only`): sanitize 22,
render_html 20, claude adapter 15, render_md 13, chatgpt adapter 10, gemini adapter 9,
verify 9, audit 3. `conftest.py` pins the project root onto `pythonpath` alongside
`pyproject.toml`'s pytest config (`pyproject.toml:7-10`).
