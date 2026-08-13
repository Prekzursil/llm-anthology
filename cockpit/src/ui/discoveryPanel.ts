/**
 * AUTO-DISCOVERY: the first-run surface.
 *
 * The app's one route to a corpus used to be naming a SQLite index in a file dialog — a
 * path the user has no reason to know. `ui/corpusBar` fixed the "there is no control at
 * all" half of that; this fixes the rest, by asking the engine what session data is
 * already on the machine (`sources.discover`) and offering it. The owner's requirement
 * was literal: "it needs to autodetect them around computer and so, no open them
 * manually."
 *
 * WHAT IT MAY HONESTLY OFFER, and why the three kinds differ:
 *
 *   * `built_index`   -> OPEN it. `open_corpus` takes exactly this path.
 *   * `session_store` -> IMPORT it, but ONLY when the finding actually supplies both
 *     parameters `corpus.build` requires. The engine defaults NEITHER `sessions_root` nor
 *     `codex_home` (`llm_anthology/sidecar.py:838-843` states the rule and names the
 *     measured reason; `:856-861` enforces that at least one source is named),
 *     deliberately, because defaulting the latter would read the user's live private store
 *     unasked. So a store whose finding names only one of them gets a stated reason, not a
 *     guessed default.
 *   * `export_file`   -> IMPORT it, naming the ONE `<provider>_export` parameter it maps to.
 *     There is one ingest verb, `corpus.build`, and it runs TWO ingests: `loaders.load_corpus`
 *     for the session stores AND `loaders.ingest_exports` for the downloaded exports
 *     (`llm_anthology/sidecar.py:1107-1110`, both call sites, back to back).
 *
 *     THIS BULLET USED TO SAY THE OPPOSITE, and it was the load-bearing half of a defect
 *     the owner reported as "it seems things are not yet functional". It read: there is no
 *     import RPC for a downloaded export, `corpus.build` "runs `loaders.load_corpus`, which
 *     is Codex rollout logs plus a Codex state DB and nothing else", so "offering an Import
 *     button here would be inventing a capability". Every clause of that is now false, and
 *     one of them was already false when written — `load_corpus` has taken `grok_root` and
 *     `claude_root` for longer than this comment has existed. The engine's own note at
 *     `sidecar.py:265-267` records the export seam being added *because* the app could not
 *     import an export; it was never propagated across this boundary, and this comment, a
 *     user-facing refusal string, the mock, the README and a GREEN TEST all went on
 *     asserting the absence. Withholding a capability the engine has is exactly as
 *     dishonest as inventing one it does not.
 *
 *     A `chatgpt_projects` / `gemini_harvest` companion path is NOT sent: discovery reports
 *     neither, and each ALONE is refused -32602 by the engine (measured), because `specs` is
 *     keyed on the primary `<provider>_export` (`sidecar.py:996-998`). Their eventual home is
 *     an opt-in per-row control, unset by default.
 *
 * THE SPLIT. Same shape as `ui/corpusBar` and `ui/exportPanel`: pure derivations carry
 * every DECISION (what is actionable, how findings group and rank, what each of the two
 * truncations says, when a poll stops), a DOM-free controller sequences the engine calls,
 * and a thin shell paints. That is not stylistic — vitest runs `environment: "node"`
 * here (`vitest.config.ts`), so anything welded to `document` cannot be asserted at all.
 *
 * DECISIONS THIS FILE MAKES THAT THE READER SHOULD KNOW ABOUT:
 *
 *   * It does NOT gate the import on `detail.ingestable`, because that counter is a
 *     HAND-MAINTAINED capability flag in the engine's spec table and it has already been
 *     wrong once. `ItemPattern.ingestable` defaults to True (`discover.py:225`) and
 *     `detail.ingestable` is just the sum over the patterns that carry it (`discover.py:572`),
 *     so today BOTH Codex rollout shapes count and the number equals the total. It read 0
 *     for months while `.zst` was flagged unreadable, and `discover.py:285-295` records both
 *     the flip and the damage: the shipped panel told users their entire Codex history could
 *     not be imported, of a store the reader had just been fixed to glob whole
 *     (`adapters/codex_rollout.py:416-419`, measured 2043 `.zst` against 0 plain `.jsonl` at
 *     `adapters/codex_rollout.py:357-359`). A UI that gates on the flag inherits that
 *     staleness and understates the product as confidently as a wrong flag overstates it, so
 *     the counter is displayed — generically, with the rest of `detail` — and never obeyed.
 *     (The mock's discovery fixture still hardcodes `ingestable: 0` for its Codex store,
 *     `ipc/mock.ts`, so a preview shows the historical value rather than the current one.)
 *   * `detail` is rendered key-agnostically. Its keys are provider-specific and adding a
 *     provider is a table edit in the engine (`discover.py:48-51`), so a renderer built
 *     around a fixed key set would silently drop whatever a new provider reports.
 *   * IMPORTING CREATES ITS INDEX AT A DEFAULT PATH AND ASKS NOBODY. The first line of this
 *     note says the app's one route to a corpus "used to be naming a SQLite index in a file
 *     dialog"; until this change that was still half true, because the FIRST import opened a
 *     native "Create corpus index" save dialog and made the user choose where `anthology.db`
 *     lives. The owner reported exactly that screen. `app_info` has resolved the DECISION
 *     G-10 default since CF-1 (`cockpit/src-tauri/src/lib.rs:49-62`, `:546-577`) and nothing
 *     on this side of the boundary read it — the same unwired-mechanism shape as the export
 *     seam above, one layer out. {@link DiscoveryPanelController.resolveDestination} now
 *     consumes it, and the dialog is DEMOTED rather than deleted: it is the fallback when
 *     resolution genuinely fails, and it is one press of {@link CHANGE_DESTINATION_LABEL}
 *     away when the user wants a different location.
 */

import { save } from "@tauri-apps/plugin-dialog";

import { engineErrorText } from "./errors";
import type {
  AppInfo,
  BuildParams,
  BuildStatus,
  CreateCorpusResult,
  DiscoveryFinding,
  DiscoveryResult,
  OpenCorpusResult,
} from "../ipc/types";

// ---------------------------------------------------------------------------
// constants
// ---------------------------------------------------------------------------

/**
 * Rows shown per group before the rest collapse behind "+N more".
 *
 * 5, because of the measured census rather than taste: a real scan of this machine
 * returned 25 ChatGPT exports against 7 Claude, 2 Codex and 1 Gemini. Rendering all 25
 * would bury the one `session_store` that is actually importable under a wall of
 * near-identical `conversations.json` rows — the useful thing must be reachable without
 * scrolling past the repetitive one.
 */
export const MAX_ROWS_PER_GROUP = 5;

/** Poll cadence and the point at which the UI stops watching an ingest. */
export const DEFAULT_POLL_LIMITS: PollLimits = {
  intervalMs: 750,
  // 30 minutes. An ingest of a 2000-rollout store is minutes of work, so a short ceiling
  // would abandon a healthy build; but the loop must have SOME ceiling, because a job
  // wedged in "running" would otherwise be polled until the process exits.
  maxElapsedMs: 30 * 60 * 1000,
};

/** Shown when a scan completed and found nothing at all. Names the manual fallback. */
export const NOTHING_FOUND_LABEL =
  "No AI session data found in the usual places. Use “Open corpus…” in the top bar to " +
  "choose a corpus index yourself.";

/** Why a source of an unrecognised kind is listed but offers no action. */
export const UNKNOWN_KIND_REASON = "Detected. This app has no action for this kind of source yet.";

/** The control that demotes the save dialog from compulsory to optional. */
export const CHANGE_DESTINATION_LABEL = "Change…";

// ---------------------------------------------------------------------------
// pure derivations — where a NEW index goes when nothing is attached
// ---------------------------------------------------------------------------

/**
 * The default corpus index out of an `app_info` reply, or null when there is no answer.
 *
 * Null is a real outcome, not a defensive stub. `locations` and `locations_error` are BOTH
 * always present with exactly one null (`cockpit/src-tauri/src/lib.rs:26-29`, `:49-53`), and
 * resolution genuinely fails in a stripped environment with neither `%LOCALAPPDATA%` nor
 * `%USERPROFILE%` (`lib.rs:604-610`). A blank string counts as no answer too: the field
 * crosses the wire through `to_string_lossy` (`lib.rs:443`), so "present" and "useful" are
 * different questions.
 *
 * MEASURED on this machine rather than assumed, because the whole zero-dialog behaviour
 * rests on this being answerable in practice and not merely in principle
 * (`.scratch/perfect/_probe_cbdialog_locations.ps1`): `%LOCALAPPDATA%` is set, survives
 * `reject_roaming`, and the derived
 * `%LOCALAPPDATA%\LLM Anthology\anthology.sqlite` is absolute, non-UNC and on a DRIVE_FIXED
 * volume whose parent directory already exists. UNVERIFIED on a domain profile whose
 * `%LOCALAPPDATA%` is redirected to a share — the settling experiment is to run that probe
 * there and read the drive type; the fallback below is what covers it either way.
 */
export function defaultIndexPath(info: AppInfo): string | null {
  const resolved = info.locations?.index_path;
  if (typeof resolved !== "string" || resolved.trim() === "") return null;
  return resolved;
}

/**
 * The sentence that replaces the dialog.
 *
 * Removing a prompt is only an improvement if the app then SAYS what it decided. A silent
 * default is worse than the dialog it replaced: the user would have a corpus index somewhere
 * they never chose and cannot name. So the path is stated in full — not shortened to its
 * basename the way a row's `detail` is — and the override control is named in the same
 * breath, because a default nobody can see how to change is a jail rather than a default.
 */
export function destinationNote(indexPath: string): string {
  return (
    `A new corpus index will be created at ${indexPath}. ` +
    `Press “${CHANGE_DESTINATION_LABEL}” to keep it somewhere else.`
  );
}

// ---------------------------------------------------------------------------
// pure derivations — time
// ---------------------------------------------------------------------------

/**
 * A finding's `newest_mtime` (UNIX **seconds**) as epoch ms, or null when it is unknown.
 *
 * Two conversions in one, and both have bitten this codebase's neighbours: discovery is
 * the only surface here that reports seconds while every other timestamp is `_ms`, and it
 * reports `0.0` — never null — when nothing datable was seen (`discover.py:583` for a store,
 * and `_mtime`'s own fallback at `discover.py:788`). Passing
 * that 0 to `new Date()` yields 1 January 1970, which reads as a real (and very wrong)
 * date rather than as "no date".
 */
export function mtimeToMs(seconds: number): number | null {
  if (!Number.isFinite(seconds) || seconds <= 0) return null;
  return Math.round(seconds * 1000);
}

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

/**
 * "how recent" as text. `nowMs` is injected rather than read from the clock so the rule is
 * assertable; the shell passes `Date.now()`.
 *
 * A future timestamp is reported as "just now" rather than "in 3 days": a file mtime ahead
 * of the clock means skew (or a copy that preserved a remote mtime), and a negative age is
 * noise the user cannot act on.
 */
export function relativeAge(ms: number | null, nowMs: number): string {
  if (ms === null) return "date unknown";
  const delta = nowMs - ms;
  if (delta < MINUTE_MS) return "just now";
  if (delta < HOUR_MS) return plural(Math.floor(delta / MINUTE_MS), "minute") + " ago";
  if (delta < DAY_MS) return plural(Math.floor(delta / HOUR_MS), "hour") + " ago";
  const days = Math.floor(delta / DAY_MS);
  if (days < 30) return plural(days, "day") + " ago";
  if (days < 365) return plural(Math.floor(days / 30), "month") + " ago";
  return plural(Math.floor(days / 365), "year") + " ago";
}

// ---------------------------------------------------------------------------
// pure derivations — paths, counts, detail
// ---------------------------------------------------------------------------

/** A path split into its final segment and everything above it, for either separator. */
export interface PathSummary {
  /** The final segment — the filename, or the directory's own name. */
  name: string;
  /** Everything above it; empty when the path has no separator. */
  parent: string;
}

/**
 * Split `path` for display. Both separators are handled because these are real Windows
 * paths off the wire while the fixtures and tests use POSIX ones.
 *
 * The parent matters as much as the name here, and that is not a general UI preference: a
 * measured scan returns 25 files ALL named `conversations.json`, so the name alone
 * distinguishes none of them and the containing directory is the only thing that does.
 */
export function pathSummary(path: string): PathSummary {
  const trimmed = path.replace(/[\\/]+$/, "");
  const cut = Math.max(trimmed.lastIndexOf("/"), trimmed.lastIndexOf("\\"));
  if (cut === -1) return { name: trimmed, parent: "" };
  return { name: trimmed.slice(cut + 1), parent: trimmed.slice(0, cut) };
}

/**
 * Thousands-grouped digits.
 *
 * Hand-rolled rather than `toLocaleString()` because that reads the HOST locale, so the
 * same code emits "2,043" on one machine and "2 043" on another — a difference that would
 * make any assertion over this text pass or fail depending on whose box ran it.
 */
export function groupDigits(value: number): string {
  if (!Number.isFinite(value)) return String(value);
  const sign = value < 0 ? "-" : "";
  const digits = Math.abs(Math.trunc(value)).toString();
  return sign + digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** The human noun for a kind's `count`, which means a different thing per kind. */
function countNoun(kind: string): string {
  if (kind === "built_index") return "conversation";
  if (kind === "session_store") return "session";
  return "file";
}

/** e.g. "2,043 sessions". `count` is items for a store, conversations for an index. */
export function countLabel(finding: DiscoveryFinding): string {
  return `${groupDigits(finding.count)} ${countNoun(finding.kind)}${finding.count === 1 ? "" : "s"}`;
}

/** A kind as a phrase for a group heading; an unrecognised kind shows itself verbatim. */
export function kindLabel(kind: string): string {
  if (kind === "built_index") return "corpus index";
  if (kind === "session_store") return "session store";
  if (kind === "export_file") return "downloaded export";
  return kind;
}

/**
 * One `detail` value as display text, or null to omit the key entirely.
 *
 * Omission is meaningful, not tidying: the engine writes `state_db: ""` for a marker file
 * it did NOT find (`discover.py:564`), so rendering the empty string would assert the
 * opposite of what it means. A path value is shortened to its final segment because the
 * row already carries the full location.
 */
export function formatDetailValue(value: unknown): string | null {
  if (typeof value === "number") return groupDigits(value);
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (Array.isArray(value)) {
    const parts = value.map((v) => (typeof v === "string" ? v : String(v)));
    return parts.length === 0 ? null : parts.join(", ");
  }
  if (typeof value === "string") {
    if (value === "") return null;
    return /[\\/]/.test(value) ? pathSummary(value).name : value;
  }
  // null, undefined, and any object shape a future provider might add: no honest one-line
  // rendering exists, so it is left out rather than stringified into "[object Object]".
  return null;
}

/**
 * `detail` -> a single "key value · key value" line, keys sorted for a stable render.
 *
 * Deliberately key-AGNOSTIC (see the module note): whatever a provider reports is shown.
 */
export function detailSummary(detail: Record<string, unknown>): string {
  return Object.keys(detail)
    .sort()
    .map((key) => {
      const text = formatDetailValue(detail[key]);
      return text === null ? null : `${key} ${text}`;
    })
    .filter((part): part is string => part !== null)
    .join(" · ");
}

// ---------------------------------------------------------------------------
// pure derivations — what a finding lets you DO
// ---------------------------------------------------------------------------

export type DiscoveryActionKind = "open" | "import" | "none";

/** What a row offers, and — when it offers nothing — the reason, in the user's words. */
export interface DiscoveryAction {
  kind: DiscoveryActionKind;
  /** Button text. Empty when `kind` is "none". */
  label: string;
  /** False when there is no button to press, or one that cannot be pressed yet. */
  enabled: boolean;
  /** Why not. Empty when `enabled`. */
  reason: string;
  /** The derived `corpus.build` parameters; null unless `kind` is "import" and enabled. */
  build: BuildParams | null;
}

/** Windows paths are case-insensitive and may differ only by a trailing separator. */
function samePath(a: string, b: string): boolean {
  const norm = (p: string): string => p.replace(/[\\/]+$/, "").replace(/\//g, "\\").toLowerCase();
  return norm(a) === norm(b);
}

/** A `detail` entry as a non-empty string, or "" when absent/blank/not a string. */
function detailString(detail: Record<string, unknown>, key: string): string {
  const value = detail[key];
  return typeof value === "string" ? value.trim() : "";
}

/**
 * A session store -> the two parameters `corpus.build` needs, or a stated reason it cannot
 * have them.
 *
 * The derivation is STRUCTURAL rather than a provider name-check, because the structure is
 * exactly what encodes the difference. `discover.py:582` names a store finding's `path`
 * from its spec's `report` field, and `:578` carries the item tree alongside it: a
 * `report="base"` store (Codex) names its BASE, with the item tree separate in
 * `detail.items_root` — so `path` is the Codex home and
 * `items_root` is the sessions root, and both parameters exist. A `report="subdir"` store
 * (Claude Code) names the item tree ITSELF, so `path` and `items_root` are the same
 * directory and no home is named anywhere in the finding.
 *
 * That second case is not a gap worth papering over with a default: `corpus.build` would
 * then be pointed at a Claude Code transcript directory, where `ingest_sessions` globs
 * `rollout-*.jsonl` (`adapters/codex_rollout.py:416-419`) and matches nothing — a build
 * that reports complete success having imported zero conversations — the exact outcome the
 * engine's own docstring calls out, having added an existing-directory check to stop it
 * (`sidecar.py:845-847`).
 *
 * KNOWN LIMIT, stated rather than hidden: this reads a base-reporting store as
 * Codex-shaped. That is true of every store in the shipped table (`discover.py:271-333`
 * has exactly one of each), but a future base-reporting NON-Codex store would be offered an
 * import it cannot satisfy. The settling check is a new `StoreSpec` row: if one is ever
 * added with `report="base"`, this rule needs a capability field from the engine instead.
 */
/**
 * The one provider this rule name-checks, and why the structural rule is no longer enough.
 *
 * The derivation below reads a store's SHAPE rather than its name, because shape encoded the
 * capability: a base-reporting store (Codex) names a home and an item tree separately, a
 * subdir-reporting one names only the tree. That held while Codex was the sole importable
 * store. It does not any more — Grok and Claude Code are BOTH subdir-reporting, so shape
 * cannot decide it.
 *
 * The reason it cannot has CHANGED, which is worth recording rather than overwriting. This
 * said "only Grok has an ingest path; nothing calls the Claude Code adapter yet", and that
 * was true: the adapter existed and `loaders.load_corpus` never called it. The engine now
 * takes `claude_root`, so BOTH are importable and shape still cannot decide it — two
 * identical shapes that happen to agree today is not the same as shape carrying the answer,
 * and a third subdir store would land here refused by default.
 *
 * This is a stopgap and should be replaced: the ENGINE knows which providers it can ingest and
 * the UI is guessing. The settling change is a capability field on the discovery finding —
 * then this constant and the name-check both disappear.
 */
const GROK_PROVIDER = "grok";
const CLAUDE_CODE_PROVIDER = "claude-code";

/**
 * The subdir-reporting stores the ENGINE can actually ingest, each mapped to the
 * `corpus.build` parameter that names it. Both take the finding's `path` verbatim, because
 * `discover.py:644` sets `path = scan_root` for `report="subdir"` and that IS the root the
 * adapter's `ingest_sessions` takes — so neither needs derivation beyond being named.
 *
 * This is the stopgap the block above describes, and it is now visibly one: what was a
 * single name-check is a two-entry table, and the comment's proposed settling change — a
 * capability field on the discovery finding, sent by the engine that actually knows — stops
 * being a nice-to-have at the third provider. Every entry here is a fact about the ENGINE
 * duplicated in the UI, and duplicated facts drift. The Claude Code entry exists because
 * that drift already happened once: the adapter shipped, `loaders.load_corpus` did not call
 * it, and this file correctly refused the import for months — then the engine gained
 * `claude_root` and the refusal became the stale half.
 */
const SUBDIR_IMPORTABLE: ReadonlyArray<readonly [string, keyof BuildParams]> = [
  [GROK_PROVIDER, "grok_root"],
  [CLAUDE_CODE_PROVIDER, "claude_root"],
];

/**
 * The DOWNLOADED-EXPORT providers, each mapped to the one `corpus.build` parameter that names
 * it. The sibling of {@link SUBDIR_IMPORTABLE}, and a better-founded table than that one.
 *
 * The mapping is not a guess and not maintained by hand: it is mechanically
 * `<provider>_export`, which is how the engine derives its own key set
 * (`_EXPORT_KEYS` from `_EXPORT_PARAMS`, `llm_anthology/sidecar.py:277-279`). The four
 * providers are 1:1 with `loaders.EXPORT_PROVIDERS`, and that pairing is ALREADY GATED on the
 * Python side by `tests/test_sidecar_build.py::test_the_export_params_match_the_loader_
 * vocabulary` — so a fifth export provider cannot be added to the engine without a test
 * naming it, which is the property `SUBDIR_IMPORTABLE` lacks.
 *
 * Written as a derivation rather than four literal pairs on purpose: a literal table would let
 * `["gemini", "chatgpt_export"]` typecheck, and a provider-to-wrong-key row is precisely the
 * silent mis-import this file's ambiguity handling exists to make visible.
 */
const EXPORT_PROVIDERS = ["chatgpt", "claude", "codex", "gemini"] as const;

type ExportProvider = (typeof EXPORT_PROVIDERS)[number];

const EXPORT_IMPORTABLE: ReadonlyArray<readonly [ExportProvider, keyof BuildParams]> =
  EXPORT_PROVIDERS.map((provider) => [provider, `${provider}_export`] as const);

/**
 * The sentence a row shows when discovery could not tell which provider a file belongs to;
 * empty when it could.
 *
 * `discover.py` emits ONE FINDING PER MATCHING SPEC and records the rivals in
 * `detail.ambiguous_with` (`discover.py:719-724`), so an ambiguous `conversations.json`
 * arrives as two findings that land in two different provider groups. Neither is wrong to
 * offer — the file IS one of them — but exactly one import will index anything, and the two
 * rows are otherwise indistinguishable on screen. Naming the rival is the cheapest honest fix:
 * it does not guess, and it tells the user what to try if the first pick indexes nothing.
 *
 * NOT a refusal. Refusing both would withhold the capability over a distinction the engine
 * makes cheaply and non-destructively (a wrong pick indexes 0 and changes nothing else), and
 * `buildOutcomeMessage` already reports a zero-count import as the non-success it is.
 */
export function ambiguityNote(finding: DiscoveryFinding): string {
  const rivals = finding.detail["ambiguous_with"];
  if (!Array.isArray(rivals) || rivals.length === 0) return "";
  const names = rivals.map((r) => (typeof r === "string" ? r : String(r))).join(", ");
  return (
    `This file also matches ${names}, so which provider wrote it is a guess. Importing it as ` +
    `${finding.provider} is safe to try — a wrong guess indexes no conversations and changes ` +
    "nothing else."
  );
}

/**
 * An EXPORT finding -> the one `<provider>_export` parameter, or a stated reason.
 *
 * Separate from the store derivation below, and reached BEFORE it, because an export finding
 * carries no `items_root` at all: its whole `detail` is `{size_bytes, ambiguous_with?}`
 * (`discover.py:719-724`). Running the store rule over it refuses every export for missing a
 * field its provider does not report — which is not a hypothetical, it is what the
 * `items_root === ""` early return did on the first attempt at this change.
 *
 * The path is passed VERBATIM. Not the parent directory "to pick up shards": the engine
 * already matches `conversations-*.json` (`discover.py:349-351`), `ingest_exports` is
 * idempotent per file, and a DIRECTORY handed to a claude export risks the recursive `.js`
 * fallback glob that `loaders.py:948-954` records as having once added ~30 junk conversations.
 * (Spelled in prose, not as the literal pattern: its `*` + `/` sequence closes a block comment
 * and the first draft of this docstring broke the build.)
 */
function deriveExportParams(
  finding: DiscoveryFinding,
): { build: BuildParams; missing: "" } | { build: null; missing: string } {
  const importable = EXPORT_IMPORTABLE.find(([provider]) => finding.provider === provider);
  if (importable === undefined) {
    // A fifth export provider in `discover.PROVIDERS` with no engine parameter behind it.
    // Naming it beats sending `${provider}_export` and letting the engine refuse: the RPC
    // would fail with a -32602 the user cannot act on, whereas this says which provider.
    return {
      build: null,
      missing:
        `this app cannot import a ${finding.provider} export yet — it reads ChatGPT, Claude, ` +
        "Codex and Gemini exports",
    };
  }
  return { build: { [importable[1]]: finding.path }, missing: "" };
}

export function deriveBuildParams(
  finding: DiscoveryFinding,
): { build: BuildParams; missing: "" } | { build: null; missing: string } {
  // KIND FIRST. The store rules below all read `detail.items_root`, which only a store has.
  if (finding.kind === "export_file") return deriveExportParams(finding);
  const itemsRoot = detailString(finding.detail, "items_root");
  if (itemsRoot === "") {
    return {
      build: null,
      missing: "the scan reported no item root for this store, so there is nothing to point an import at",
    };
  }

  // A GROK or CLAUDE CODE store is importable on its own. `corpus.build` takes its root and
  // needs nothing else: `codex_home` is optional and omitting it means "no Codex state graph
  // to merge", which is exactly true here. NO codex_home is invented — passing one merges a
  // graph the import does not have, and passing the LIVE default is the behaviour that once
  // let an automated probe read the owner's real private sessions.
  const importable = SUBDIR_IMPORTABLE.find(([provider]) => finding.provider === provider);
  if (importable) {
    return { build: { [importable[1]]: finding.path }, missing: "" };
  }

  if (samePath(finding.path, itemsRoot)) {
    // A subdir-reporting store the engine has no ingest path for. There is no live example
    // today — Claude Code was the last one and gained `claude_root` — so this branch now
    // guards a provider that does not exist yet rather than one that does. It stays because
    // `discover.py` can report a store this app cannot build from, and claiming otherwise
    // would offer an import that fails at the RPC instead of here, where the reason can be
    // shown.
    return {
      build: null,
      missing: `this app cannot import a ${finding.provider} store yet — it reads Codex, Grok and Claude Code session stores`,
    };
  }
  return { build: { sessions_root: itemsRoot, codex_home: finding.path }, missing: "" };
}

/** Context the action depends on beyond the finding itself. */
export interface ActionContext {
  /** True when a corpus index is already attached, so an import needs no new one. */
  corpusAttached: boolean;
}

/**
 * The import button's text.
 *
 * `corpusAttached` changes it because the two flows differ in what they do to the user's data:
 * with nothing attached the app must first CREATE an index, so the label ends in an ellipsis to
 * signal it will ask where; with a corpus already open the import lands in that one, and saying
 * so prevents an import silently joining a corpus the user forgot was attached.
 *
 * `qualifier` names the PROVIDER, and only when the finding is ambiguous. Two findings over one
 * ambiguous file render in two groups, and a bare "Import…" on both gives the user two
 * identical buttons over the same path with nothing to choose by; "Import as chatgpt…" beside
 * "Import as claude…" is the same information the engine has, on screen. An unambiguous row
 * keeps the short label — qualifying every button would spend the distinction on the 99% of
 * rows that do not need it.
 */
function importLabel(ctx: ActionContext, qualifier: string): string {
  const verb = qualifier === "" ? "Import" : `Import as ${qualifier}`;
  return ctx.corpusAttached ? `${verb} into open corpus` : `${verb}…`;
}

/**
 * A finding -> the one thing the app may honestly offer for it.
 */
export function deriveAction(finding: DiscoveryFinding, ctx: ActionContext): DiscoveryAction {
  if (finding.kind === "built_index") {
    return { kind: "open", label: "Open", enabled: true, reason: "", build: null };
  }
  // A store and an export share this arm now. They differ only in how the parameters are
  // derived — which is `deriveBuildParams`'s job, not this one's — and folding them together is
  // what stops a THIRD ingestable kind arriving as a copy of the refusal branch again.
  if (finding.kind === "session_store" || finding.kind === "export_file") {
    const derived = deriveBuildParams(finding);
    if (derived.build === null) {
      return { kind: "none", label: "", enabled: false, reason: `Detected, but ${derived.missing}.`, build: null };
    }
    return {
      kind: "import",
      label: importLabel(ctx, ambiguityNote(finding) === "" ? "" : finding.provider),
      enabled: true,
      reason: "",
      build: derived.build,
    };
  }
  return { kind: "none", label: "", enabled: false, reason: UNKNOWN_KIND_REASON, build: null };
}

// ---------------------------------------------------------------------------
// pure derivations — grouping, ranking, and the TWO truncations
// ---------------------------------------------------------------------------

/**
 * Presentation rank by kind: openable now, then importable, then merely present.
 *
 * Mirrors the engine's own `_KIND_RANK` (`discover.py:77`) — but it is re-stated here
 * rather than relied upon, because the engine sorts survivors by `(kind, provider, path)`
 * (`discover.py:819-820`) whereas a person wants the NEWEST first within a group. The two
 * orderings genuinely differ, so the UI re-sorts instead of rendering wire order.
 */
function kindRank(kind: string): number {
  if (kind === "built_index") return 0;
  if (kind === "session_store") return 1;
  if (kind === "export_file") return 2;
  return 9;
}

/** One rendered finding. */
export interface DiscoveryRow {
  finding: DiscoveryFinding;
  /** The final path segment — the filename, or the store directory's name. */
  name: string;
  /** Everything above it; the only thing distinguishing 25 same-named exports. */
  parent: string;
  /** "2,043 sessions · 15 minutes ago · high confidence". */
  summary: string;
  /** The generically-rendered `detail` line; empty when nothing renders. */
  detail: string;
  /**
   * A sentence about THIS row that the generic `detail` line cannot express; empty for almost
   * every row. Today it carries exactly one thing: {@link ambiguityNote}.
   *
   * Separate from `detail` because `detail` is deliberately key-AGNOSTIC (see the module note)
   * and therefore renders `ambiguous_with claude` — the raw engine key, which states the fact
   * without saying what to do about it. A row whose action is a coin-flip needs prose, and prose
   * about a specific key is exactly what the generic renderer must not start doing.
   */
  note: string;
  action: DiscoveryAction;
}

/** One provider+kind group, with BOTH truncations accounted for separately. */
export interface DiscoveryGroup {
  /** `"<provider>/<kind>"` — the same key the engine uses in `stats.truncated_groups`. */
  key: string;
  provider: string;
  kind: string;
  heading: string;
  /** The rows actually rendered (capped unless the group is expanded). */
  rows: DiscoveryRow[];
  /** Findings the scan returned for this group. */
  totalCount: number;
  /** Findings this UI is holding back behind "+N more". */
  hiddenCount: number;
  /** True when the ENGINE capped this group before the UI ever saw it. */
  backendTruncated: boolean;
  /** True when the user has expanded this group past the cap. */
  expanded: boolean;
  /** Label for the expand/collapse CONTROL; empty when there is nothing to toggle. */
  expandLabel: string;
  /** The engine-cap SENTENCE; empty unless the engine capped this group. */
  capNote: string;
}

/*
 * THE TWO TRUNCATIONS ARE TWO FIELDS, and they are rendered as two different things,
 * because they are not the same fact and no single control can express both.
 *
 *   `expandLabel` -> a BUTTON. This UI is holding rows back; every one of them is present
 *   and one click away.
 *
 *   `capNote` -> a SENTENCE, never a button. The ENGINE capped the group at
 *   `DEFAULT_MAX_PER_GROUP` before the UI saw anything (`discover.py:106`, `:785`), so
 *   items exist on disk that are in NO list here and that no click can reveal. Putting
 *   that text on a control would promise a reveal that cannot happen; folding it into
 *   "+20 more" would report an unknown shortfall as a known one.
 */

/** The expand/collapse control's label, or "" when the group has nothing to toggle. */
export function expandLabel(hiddenCount: number, expanded: boolean): string {
  if (hiddenCount > 0) return `+${groupDigits(hiddenCount)} more`;
  // Expanded with nothing hidden: everything is shown, so the only move left is back.
  return expanded ? "Show fewer" : "";
}

/**
 * The engine-cap sentence.
 *
 * The cap is quoted from the data rather than restated as a literal 25: when a group was
 * capped, the count that survived IS the cap, so this cannot drift if the engine's
 * `DEFAULT_MAX_PER_GROUP` ever changes.
 */
export function capNote(totalCount: number): string {
  return `The scan listed only the newest ${groupDigits(totalCount)} of this group; older ones on disk are not shown at all.`;
}

/** Everything grouping needs beyond the scan itself. */
export interface GroupOptions extends ActionContext {
  /** For the relative ages. */
  nowMs: number;
  /** Rows before collapsing. Defaults to {@link MAX_ROWS_PER_GROUP}. */
  maxRows?: number;
  /** Group keys the user expanded; those render every row. */
  expanded?: ReadonlySet<string>;
}

/** Newest first, then by path so a re-render over the same data is byte-identical. */
function byNewest(a: DiscoveryFinding, b: DiscoveryFinding): number {
  if (a.newest_mtime !== b.newest_mtime) return b.newest_mtime - a.newest_mtime;
  return a.path < b.path ? -1 : a.path > b.path ? 1 : 0;
}

/**
 * A scan -> the ordered, capped groups to render.
 *
 * Ordering is by how actionable a kind is first (so the openable index outranks the
 * 25-strong export tail no matter how recent that tail is), then by recency between groups
 * of equal rank, then by provider for stability.
 */
export function groupFindings(result: DiscoveryResult, options: GroupOptions): DiscoveryGroup[] {
  const maxRows = options.maxRows ?? MAX_ROWS_PER_GROUP;
  const expanded = options.expanded ?? new Set<string>();
  const truncated = new Set(result.stats.truncated_groups);

  const buckets = new Map<string, DiscoveryFinding[]>();
  for (const finding of result.findings) {
    const key = `${finding.provider}/${finding.kind}`;
    const bucket = buckets.get(key);
    if (bucket === undefined) buckets.set(key, [finding]);
    else bucket.push(finding);
  }

  const groups: DiscoveryGroup[] = [];
  for (const [key, findings] of buckets) {
    const sorted = [...findings].sort(byNewest);
    const isExpanded = expanded.has(key);
    const limit = isExpanded ? sorted.length : Math.min(maxRows, sorted.length);
    const shown = sorted.slice(0, limit);
    const hiddenCount = sorted.length - shown.length;
    const first = sorted[0];
    const backendTruncated = truncated.has(key);
    groups.push({
      key,
      provider: first.provider,
      kind: first.kind,
      heading: `${first.provider} · ${kindLabel(first.kind)}`,
      rows: shown.map((finding) => toRow(finding, options)),
      totalCount: sorted.length,
      hiddenCount,
      backendTruncated,
      expanded: isExpanded,
      expandLabel: expandLabel(hiddenCount, isExpanded),
      capNote: backendTruncated ? capNote(sorted.length) : "",
    });
  }

  return groups.sort((a, b) => {
    const rank = kindRank(a.kind) - kindRank(b.kind);
    if (rank !== 0) return rank;
    const recency = newestOf(b) - newestOf(a);
    if (recency !== 0) return recency;
    return a.key < b.key ? -1 : a.key > b.key ? 1 : 0;
  });
}

/** A group's newest mtime — its rows are already newest-first, so that is the first one. */
function newestOf(group: DiscoveryGroup): number {
  return group.rows.length === 0 ? 0 : group.rows[0].finding.newest_mtime;
}

function toRow(finding: DiscoveryFinding, options: GroupOptions): DiscoveryRow {
  const { name, parent } = pathSummary(finding.path);
  const age = relativeAge(mtimeToMs(finding.newest_mtime), options.nowMs);
  return {
    finding,
    name,
    parent,
    summary: `${countLabel(finding)} · ${age} · ${finding.confidence} confidence`,
    detail: detailSummary(finding.detail),
    note: ambiguityNote(finding),
    action: deriveAction(finding, { corpusAttached: options.corpusAttached }),
  };
}

/**
 * Scan-level notes: what the scan cost, and every way it might NOT have seen everything.
 *
 * The skipped locations are counted, never listed. A real scan of this machine produced 7
 * of them — permission denials and absent directories — and pasting seven raw
 * `<path>: [WinError 5] Access is denied` strings under a first-run panel would bury the
 * findings under noise about directories the user never expected to be searched. Saying
 * that some were skipped is the part that is actionable; which ones is not.
 */
export function scanNotes(result: DiscoveryResult): string[] {
  const stats = result.stats;
  const notes = [
    `Scanned ${groupDigits(stats.roots_scanned)} locations in ${stats.elapsed_seconds.toFixed(1)}s.`,
  ];
  if (stats.budget_exhausted) {
    notes.push("The scan reached its file limit before finishing, so this list may be incomplete.");
  }
  if (stats.errors.length > 0) {
    notes.push(
      `${groupDigits(stats.errors.length)} ${stats.errors.length === 1 ? "location was" : "locations were"} skipped (missing, or not readable).`,
    );
  }
  return notes;
}

// ---------------------------------------------------------------------------
// pure derivations — the ingest poll
// ---------------------------------------------------------------------------

export interface PollLimits {
  /** Delay between polls. */
  intervalMs: number;
  /** Stop watching after this long, whatever the job is doing. */
  maxElapsedMs: number;
}

/** Whether to poll again, and if not, why the loop ended. */
export type PollOutcome = "continue" | "terminal" | "timeout";

/**
 * The whole stopping rule, extracted so "does this loop terminate?" is a unit test rather
 * than a promise.
 *
 * Anything that is not `running` is terminal — `done` and `failed` obviously, but also
 * `idle`, which `corpus.build_status` reports when it holds no job at all
 * (`llm_anthology/sidecar.py:973-975`). Treating `idle` as "keep waiting" would spin forever
 * against an engine that has nothing to report.
 */
export function pollOutcome(state: string, elapsedMs: number, limits: PollLimits): PollOutcome {
  if (state !== "running") return "terminal";
  if (elapsedMs >= limits.maxElapsedMs) return "timeout";
  return "continue";
}

/** Live progress text for a running ingest. `indexed_conversations` is a real count. */
export function buildProgressLabel(status: BuildStatus): string {
  return `Importing… ${groupDigits(status.indexed_conversations)} conversations indexed so far.`;
}

/**
 * A terminal status -> what to tell the user.
 *
 * `failed` carries the engine's own text through {@link engineErrorText}; per-file parse
 * errors are counted rather than listed, and are reported even on SUCCESS, because a build
 * that skipped 40 unreadable rollouts and called itself done would otherwise look like a
 * clean import of everything.
 */
export function buildOutcomeMessage(status: BuildStatus): string {
  const skipped =
    status.errors.length > 0
      ? ` ${groupDigits(status.errors.length)} ${status.errors.length === 1 ? "file" : "files"} could not be read and ${status.errors.length === 1 ? "was" : "were"} skipped.`
      : "";
  if (status.state === "done" && status.indexed_conversations === 0) {
    // A CLEAN COMPLETION THAT IMPORTED NOTHING is its own outcome, and it must not read as
    // success. It is what a wrong-provider pick over an ambiguous export produces — the
    // adapter reads the file, recognises none of it, and the job finishes with `state: "done"`
    // and an EMPTY error list (MEASURED in-process: a valid-JSON non-ChatGPT file under
    // `chatgpt_export` gave exactly that). So neither `state` nor `errors` distinguishes it,
    // and the old wording — "Import finished — 0 conversations in the corpus." — was
    // technically true while reading as a completed import, which is the one thing this line
    // must never do.
    //
    // Deliberately shares NO PREFIX with the success sentence. "Import finished but added no
    // conversations" was the first wording and it fails its own test: `not.toContain("Import
    // finished")` is the invariant worth having, because a UI that truncates, or a reader
    // skimming, sees the first two words and reads a completed import. Disjoint strings make
    // that impossible rather than unlikely.
    //
    // Says what to try, because both live causes are actionable: the wrong side of an
    // ambiguous file, or a source that no longer holds what it did.
    return (
      "Nothing was imported — the engine read the source and recognised no conversations in " +
      `it.${skipped} If this file matched more than one provider, try importing it as the ` +
      "other one."
    );
  }
  if (status.state === "done") {
    return `Import finished — ${groupDigits(status.indexed_conversations)} conversations in the corpus.${skipped}`;
  }
  if (status.state === "failed") {
    return `${engineErrorText(status.error ?? "the engine reported no reason", "Import failed")}${skipped}`;
  }
  // Neither running nor a known terminal state: report what was actually seen rather than
  // guessing which of success or failure it meant.
  return `Import stopped — the engine reports state “${status.state}”.${skipped}`;
}

// ---------------------------------------------------------------------------
// controller
// ---------------------------------------------------------------------------

/**
 * The IPC the panel needs, narrowed from `IpcClient` (interface segregation) so a test
 * injects five methods rather than the whole data surface.
 */
export interface DiscoveryIpc {
  discoverSources(): Promise<DiscoveryResult>;
  openCorpus(indexPath: string): Promise<OpenCorpusResult>;
  createCorpus(indexPath: string): Promise<CreateCorpusResult>;
  corpusBuild(params: BuildParams): Promise<{ job_id: string }>;
  corpusBuildStatus(jobId?: string): Promise<BuildStatus>;
  /**
   * OPTIONAL, unlike the five above, and the asymmetry is the one `ui/corpusBar`'s
   * `CorpusIpc.appInfo` already carries. The panel cannot do its job without discovery and
   * build; it works — degraded, via the save dialog — without knowing where the app would
   * put an index of its own. Keeping it optional also means every narrowed fake that
   * predates this still satisfies the interface, so the fallback arm is exercised by the
   * whole existing suite rather than by one bespoke test.
   *
   * The real `IpcClient` declares it NON-optional (`ipc/types.ts:1201`), so the shipped app
   * always takes the zero-dialog path.
   */
  appInfo?: () => Promise<AppInfo>;
}

/** The environment seams: the clock, the timer, and the destination picker. */
export interface DiscoveryDeps {
  now(): number;
  sleep(ms: number): Promise<void>;
  /**
   * Ask where to create a new corpus index. Resolves null when the user dismisses, which
   * is a no-op and not an error — the same contract as the corpus bar's picker.
   */
  chooseDestination(): Promise<string | null>;
  limits?: PollLimits;
  maxRows?: number;
}

/**
 * The panel's render-state. Flat rather than a discriminated union (the shape
 * `ui/exportPanel` uses) because the findings list persists ACROSS phases here — it stays
 * on screen while an import runs — so a union would repeat `groups` in most of its arms.
 */
export interface DiscoveryView {
  phase: "idle" | "scanning" | "ready" | "working" | "building" | "done" | "error";
  groups: DiscoveryGroup[];
  notes: string[];
  /** Non-empty only when a scan completed and found nothing. */
  emptyLabel: string;
  /** One line of progress, outcome, or refusal. Never a raw engine string. */
  status: string;
  /**
   * Where an import would CREATE its index, or "" when there is nothing to say — either
   * because a corpus is already attached (the import lands in that one and creates nothing)
   * or because no default could be resolved (the save dialog will ask instead).
   *
   * DERIVED, never patched: {@link DiscoveryPanelController.emit} recomputes it on every
   * transition from the attach state and the resolved default. A field a caller had to
   * remember to include would go stale in exactly the arm that matters — the first paint
   * after a corpus attaches.
   */
  destination: string;
  /** True while an engine call is in flight; the shell disables its controls. */
  busy: boolean;
  /**
   * True when `status` reports something the user CANNOT learn anywhere else, so the panel
   * must stay on screen even in a phase it would otherwise collapse from.
   *
   * This exists because of a measured defect. The panel collapses on `done` — correctly, so
   * an attached corpus gets the whole graph pane back — but that collapse happened BEFORE
   * the status line was written, so a build that skipped unreadable files composed
   * {@link buildOutcomeMessage} and then threw it away. A partial import was
   * indistinguishable from a complete one, and `buildOutcomeMessage` is the only place in
   * the cockpit that formats `BuildStatus.errors` at all, so the skipped count was lost
   * app-wide rather than merely here.
   *
   * A ONE-SHOT, not a mode: it describes the state being emitted, so {@link
   * DiscoveryPanelController.emit} clears it on every transition. A clean import sets it
   * false and still collapses silently — the fix must not make a successful import noisy.
   */
  needsAttention: boolean;
}

export type DiscoveryViewListener = (view: DiscoveryView) => void;
/** Called with the index path whenever a corpus becomes attached or its contents change. */
export type CorpusReadyListener = (indexPath: string) => void;

function initialView(): DiscoveryView {
  return {
    phase: "idle",
    groups: [],
    notes: [],
    emptyLabel: "",
    status: "",
    destination: "",
    busy: false,
    needsAttention: false,
  };
}

/**
 * Headless discovery controller: runs the scan, sequences open/create/import against the
 * injected {@link DiscoveryIpc}, watches an ingest to a terminal state, and emits a
 * {@link DiscoveryView} on every transition.
 *
 * Single-flight throughout, like {@link import("./corpusBar").CorpusBarController}: a
 * re-entrant call while one is in flight is ignored rather than queued, so a double-click
 * on "Import" cannot start two builds — which the engine would refuse anyway, with a
 * BUILD_IN_PROGRESS naming a job id the user never knew existed
 * (`llm_anthology/sidecar.py:892-896`).
 */
export class DiscoveryPanelController {
  private view: DiscoveryView = initialView();
  private busy = false;
  private disposed = false;
  /** The last scan, kept so a corpus-attach or an expand re-derives without re-scanning. */
  private result: DiscoveryResult | null = null;
  private attached: string | null = null;
  /**
   * Where a new index would go: the `app_info` default, or a destination the user picked
   * explicitly. Null until resolved, and null FOREVER once {@link destinationAsked} is set
   * and the answer was "there isn't one".
   */
  private destination: string | null = null;
  /** Whether `app_info` has been consulted, so a shell that cannot answer is asked once. */
  private destinationAsked = false;
  private readonly expanded = new Set<string>();
  private readonly limits: PollLimits;

  constructor(
    private readonly ipc: DiscoveryIpc,
    private readonly deps: DiscoveryDeps,
    private readonly onChange: DiscoveryViewListener,
    private readonly onCorpusReady: CorpusReadyListener,
  ) {
    this.limits = deps.limits ?? DEFAULT_POLL_LIMITS;
  }

  get current(): DiscoveryView {
    return this.view;
  }

  private emit(patch: Partial<DiscoveryView>): void {
    // `needsAttention` is reset BEFORE the patch is applied, so an explicit value in the
    // patch still wins. Without the reset the merge would carry a previous run's unread-
    // outcome marker forward and pin the panel open forever: import 2 skipped files, then
    // open a different corpus, and that unrelated success would inherit the marker.
    //
    // `destination` is RECOMPUTED here for the opposite reason: it is derived from state no
    // patch carries (the attach, and whether the default has resolved yet), so deriving it
    // once at the single exit point is what keeps it from going stale in the arm nobody
    // remembers — the paint immediately after a corpus attaches.
    this.view = {
      ...this.view,
      needsAttention: false,
      destination: this.destinationText(),
      ...patch,
    };
    this.onChange(this.view);
  }

  /** The destination to DISPLAY: nothing while a corpus is attached, nothing until resolved. */
  private destinationText(): string {
    if (this.attached !== null) return "";
    return this.destination ?? "";
  }

  /**
   * The `app_info` default, resolved at most once.
   *
   * FAIL-OPEN AT EVERY STEP, like `corpusBar.hintDefaultIndex`: the method may be absent (a
   * narrowed fake, or a shell older than the command), the call may reject, and resolution
   * itself may legitimately fail. None of those is worth a visible error, because each one
   * lands on a working fallback — the save dialog this change demoted, not deleted.
   *
   * Memoised on `destinationAsked` rather than on the value, so a failed resolution is not
   * retried on every import. An explicit override sets `destination` directly and therefore
   * short-circuits here on the first line.
   */
  private async ensureDestination(): Promise<string | null> {
    if (this.destination !== null) return this.destination;
    if (this.destinationAsked) return null;
    this.destinationAsked = true;
    const ask = this.ipc.appInfo;
    if (ask === undefined) return null;
    try {
      this.destination = defaultIndexPath(await ask.call(this.ipc));
    } catch {
      this.destination = null;
    }
    return this.destination;
  }

  /**
   * Where this import's new index goes — WITHOUT a dialog whenever that is answerable.
   *
   * This is the whole of the owner's C-B complaint, in five lines. The panel used to open a
   * native "Create corpus index" save dialog on every first import and make the user choose
   * where `anthology.db` lives — a decision they have no basis to make, about a file whose
   * location the app already knew. `app_info` has resolved the DECISION G-10 default since
   * CF-1 and nothing on this side of the boundary read it.
   *
   * The dialog remains the fallback rather than being removed, because "no default" is a
   * state the engine can genuinely report and a guessed path is worse than a prompt.
   */
  private async resolveDestination(): Promise<string | null> {
    const known = await this.ensureDestination();
    if (known !== null) return known;
    this.emit({ phase: "working", status: "Choose where to keep the new corpus index…", busy: true });
    return this.deps.chooseDestination();
  }

  /**
   * Open the picker on purpose, and adopt whatever it returns as the destination.
   *
   * The override half of the contract: a default the user cannot see how to change is not a
   * default. Single-flight like every other entry point here — a picker opened mid-import
   * would be choosing a destination for a build that has already been started elsewhere —
   * and a dismissal is a no-op, so the previous default survives it.
   *
   * It deliberately does NOT repaint `busy` while the picker is open, unlike
   * {@link resolveDestination}'s fallback arm. Two reasons, and the second is the load-bearing
   * one: the guard above already makes a second press a no-op, and an emit here would have to
   * carry `status` too — which would silently erase a failure line the user is still reading
   * (a create-then-decline leaves `phase: "error"` with nothing attached, i.e. with this
   * control visible). Cosmetically the button stays enabled behind a native modal for the
   * duration; UNVERIFIED whether Tauri's `save()` blocks webview input on every Windows
   * shell, and the settling experiment is a click-through attempt against the running app.
   */
  async changeDestination(): Promise<void> {
    if (this.busy || this.disposed) return;
    this.busy = true;
    try {
      const chosen = await this.deps.chooseDestination();
      if (this.disposed || chosen === null) return;
      this.destination = chosen;
      // An explicit choice supersedes the unasked default, so the engine is never consulted
      // afterwards — otherwise a later `ensureDestination` could overwrite the user's pick.
      this.destinationAsked = true;
      this.emitRegroup();
    } finally {
      this.busy = false;
    }
  }

  /** Re-derive the rendered groups from the last scan (after an attach or an expand). */
  private regroup(): DiscoveryGroup[] {
    if (this.result === null) return [];
    return groupFindings(this.result, {
      corpusAttached: this.attached !== null,
      nowMs: this.deps.now(),
      maxRows: this.deps.maxRows,
      expanded: this.expanded,
    });
  }

  /**
   * Emit a re-derivation: new `groups`, everything else — including an unread outcome —
   * left exactly as it was.
   *
   * {@link emit}'s blanket `needsAttention: false` is right for a TRANSITION, where a new
   * phase and status supersede the previous outcome. It is wrong here, and the difference is
   * not cosmetic: a re-derivation patches only `groups`, so `phase` stays `done` and clearing
   * the marker sends the very next paint down the collapse branch. Expanding a group would
   * delete the skipped-file report and the panel with it, which is the opposite of the
   * delivery guarantee {@link DiscoveryPanelController.acknowledge} exists to provide.
   *
   * The current value is COPIED rather than forced true, so a clean import stays unmarked and
   * still collapses.
   */
  private emitRegroup(): void {
    this.emit({ groups: this.regroup(), needsAttention: this.view.needsAttention });
  }

  /**
   * Tell the panel a corpus is (or is no longer) attached. The app calls this when the
   * corpus bar attaches one, because that changes what an import means — it would land in
   * the open corpus rather than a new one — and the row labels have to follow.
   */
  setCorpusAttached(indexPath: string | null): void {
    this.attached = indexPath;
    if (this.result !== null) this.emitRegroup();
  }

  /** Show every row of `key`, or collapse it back to the cap. */
  toggleGroup(key: string): void {
    if (this.expanded.has(key)) this.expanded.delete(key);
    else this.expanded.add(key);
    this.emitRegroup();
  }

  /**
   * Run a scan and present what it found. Warm ~1.8s / cold ~7.5s on the measured machine,
   * so the pending phase is emitted BEFORE the await — a panel that painted only on
   * completion would be a blank rectangle for those seconds, indistinguishable from a
   * broken one.
   */
  async scan(): Promise<void> {
    if (this.busy || this.disposed) return;
    this.busy = true;
    this.emit({ phase: "scanning", status: "Looking for AI session data on this computer…", busy: true });
    try {
      const result = await this.ipc.discoverSources();
      if (this.disposed) return;
      this.result = result;
      // Resolved HERE and not lazily at import time, so the destination is on screen before
      // the user presses anything. A default that only appears after the decision has been
      // made is not a default the user got to see.
      await this.ensureDestination();
      if (this.disposed) return;
      const groups = this.regroup();
      this.emit({
        phase: "ready",
        groups,
        notes: scanNotes(result),
        emptyLabel: result.findings.length === 0 ? NOTHING_FOUND_LABEL : "",
        status: "",
        busy: false,
      });
    } catch (err) {
      this.emit({
        phase: "error",
        status: engineErrorText(err, "Could not scan for session data"),
        busy: false,
      });
    } finally {
      this.busy = false;
    }
  }

  /** Do whatever `finding` honestly allows. A finding that allows nothing says why. */
  async activate(finding: DiscoveryFinding): Promise<void> {
    if (this.busy || this.disposed) return;
    const action = deriveAction(finding, { corpusAttached: this.attached !== null });
    if (action.kind === "open") {
      await this.openFinding(finding);
      return;
    }
    if (action.kind === "import" && action.build !== null) {
      await this.importStore(action.build);
      return;
    }
    // Nothing to do. Surface the reason instead of failing silently or, worse, calling an
    // engine method that cannot serve this kind.
    this.emit({ status: action.reason });
  }

  private async openFinding(finding: DiscoveryFinding): Promise<void> {
    this.busy = true;
    const { name } = pathSummary(finding.path);
    this.emit({ phase: "working", status: `Opening ${name}…`, busy: true });
    try {
      const result = await this.ipc.openCorpus(finding.path);
      if (!result.ok) throw new Error(`the engine did not attach ${finding.path}`);
      this.attached = result.index;
      this.emit({ phase: "done", status: `Opened ${name}.`, busy: false });
      this.onCorpusReady(result.index);
    } catch (err) {
      this.emit({
        phase: "error",
        status: engineErrorText(err, "Could not open that corpus"),
        busy: false,
      });
    } finally {
      this.busy = false;
    }
  }

  /**
   * Import a session store, creating and attaching an index first when nothing is open.
   *
   * That first step is not optional: `corpus.build` is forwarded only when a corpus is
   * already attached (`cockpit/src-tauri/src/lib.rs:35-45`), the engine re-checks that
   * itself (`sidecar.py:848` calling `_require_corpus`, `sidecar.py:701-703`), and it
   * additionally refuses an index it cannot reopen from disk (`sidecar.py:887-889`).
   * Firing the build anyway
   * on a fresh install would surface the engine's internal "no corpus attached: call
   * open_corpus first" — the exact leak `ui/errors` exists to stop.
   */
  private async importStore(build: BuildParams): Promise<void> {
    this.busy = true;
    try {
      if (this.attached === null) {
        const dest = await this.resolveDestination();
        if (this.disposed) return;
        if (dest === null) {
          // Dismissed. A no-op, not a failure — nothing was created and nothing changed.
          // Only reachable through the FALLBACK dialog: a resolved default never returns
          // null, which is why the zero-dialog path cannot end here.
          this.emit({ phase: "ready", status: "", busy: false });
          return;
        }
        const { name } = pathSummary(dest);
        // `phase` and `busy` are named rather than inherited: with a resolved default there
        // was no preceding "Choose where…" emit to inherit them from.
        this.emit({ phase: "working", status: `Creating ${name}…`, busy: true });
        await this.ipc.createCorpus(dest);
        const opened = await this.ipc.openCorpus(dest);
        if (!opened.ok) throw new Error(`the engine did not attach ${dest}`);
        this.attached = opened.index;
        // Announce the empty corpus straight away: the rest of the app can attach to it and
        // show its (empty) panes while the import fills it, rather than staying dead for
        // the length of the build.
        this.onCorpusReady(opened.index);
      }
      this.emit({ phase: "building", status: "Starting import…", busy: true });
      const handle = await this.ipc.corpusBuild(build);
      await this.watchBuild(handle.job_id);
    } catch (err) {
      this.emit({ phase: "error", status: engineErrorText(err, "Import failed"), busy: false });
    } finally {
      this.busy = false;
    }
  }

  /**
   * Poll `corpus.build_status` until the job leaves "running", the ceiling is reached, or
   * the panel is destroyed. The first poll happens immediately — a build that was already
   * finished should not cost a full interval of "starting…" before it says so.
   */
  private async watchBuild(jobId: string): Promise<void> {
    const started = this.deps.now();
    for (;;) {
      if (this.disposed) return;
      const status = await this.ipc.corpusBuildStatus(jobId);
      if (this.disposed) return;
      const outcome = pollOutcome(status.state, this.deps.now() - started, this.limits);
      if (outcome === "terminal") {
        const ok = status.state === "done";
        this.emit({
          phase: ok ? "done" : "error",
          status: buildOutcomeMessage(status),
          // A skipped file is the one thing here the user can learn NOWHERE else, so a
          // non-clean import holds the panel open until acknowledged. Keyed on the errors
          // list rather than on `ok`, because a FAILED build already keeps the panel open
          // (the error phase never collapses) — it is success that was silently discarding
          // its own report.
          needsAttention: status.errors.length > 0,
          busy: false,
        });
        // Reload on BOTH outcomes: the graph is committed before the long conversation
        // ingest, so even a failed build can have changed the index and the live view has
        // to follow it (`llm_anthology/sidecar.py`, `_run_build`'s `needs_reload` note).
        if (this.attached !== null) this.onCorpusReady(this.attached);
        return;
      }
      if (outcome === "timeout") {
        // The job is still running ENGINE-side; only the watching stopped. Saying
        // "finished" or "failed" here would both be false.
        this.emit({
          phase: "error",
          status:
            "Still importing after 30 minutes — this panel stopped watching. The import continues; reopen the app to see the result.",
          busy: false,
        });
        return;
      }
      this.emit({ phase: "building", status: buildProgressLabel(status), busy: true });
      await this.deps.sleep(this.limits.intervalMs);
    }
  }

  /**
   * The user has READ an outcome the panel was being held open for.
   *
   * Clearing the marker is what lets the panel finally collapse, so the acknowledgement —
   * not the emit — is the delivery guarantee: an import that skipped files cannot vanish
   * before someone dismissed the report of it. Re-entrant and safe when nothing is pending.
   */
  acknowledge(): void {
    if (!this.view.needsAttention) return;
    this.emit({ needsAttention: false });
  }

  /** Stop any in-flight poll loop. Safe to call more than once. */
  destroy(): void {
    this.disposed = true;
  }
}

// ---------------------------------------------------------------------------
// DOM shell
// ---------------------------------------------------------------------------

/**
 * The panel's DOM binding: builds the skeleton on first use, repaints groups and status,
 * and tears the whole thing down when there is nothing to show.
 *
 * The container is left EMPTY in the idle and done phases on purpose — `#discovery:empty`
 * collapses it in CSS, the same trick `#scrubber:empty` already uses — so a first run
 * shows the panel and an attached corpus gets the full graph pane back with no toggle.
 *
 * Text is written with `textContent`, never `innerHTML`: a path, a provider name and an
 * engine error are all attacker-influenceable text bound for the UI.
 */
export class DiscoveryPanel {
  private readonly controller: DiscoveryPanelController;
  /** The stable skeleton, or null while the container is collapsed. */
  private shell: {
    status: HTMLElement;
    dismiss: HTMLButtonElement;
    destination: HTMLElement;
    change: HTMLButtonElement;
    notes: HTMLElement;
    groups: HTMLElement;
  } | null = null;

  constructor(
    ipc: DiscoveryIpc,
    private readonly container: HTMLElement,
    onCorpusReady: CorpusReadyListener,
    deps: Partial<DiscoveryDeps> = {},
  ) {
    this.controller = new DiscoveryPanelController(
      ipc,
      {
        now: deps.now ?? ((): number => Date.now()),
        sleep: deps.sleep ?? ((ms): Promise<void> => new Promise((r) => setTimeout(r, ms))),
        chooseDestination: deps.chooseDestination ?? defaultChooseDestination,
        limits: deps.limits,
        maxRows: deps.maxRows,
      },
      (view) => this.paint(view),
      onCorpusReady,
    );
  }

  /** Run a scan and present it. The app calls this on boot when no corpus is attached. */
  async scan(): Promise<void> {
    await this.controller.scan();
  }

  setCorpusAttached(indexPath: string | null): void {
    this.controller.setCorpusAttached(indexPath);
  }

  destroy(): void {
    this.controller.destroy();
    this.teardown();
  }

  private teardown(): void {
    this.container.replaceChildren();
    // The role and label go with the content. Leaving them behind would park an empty
    // `role="group"` in the accessibility tree pointing `aria-labelledby` at a heading id
    // that no longer exists — a labelled group announcing nothing.
    this.container.removeAttribute("role");
    this.container.removeAttribute("aria-labelledby");
    this.shell = null;
  }

  /**
   * Create the skeleton once per visible run. The status line is created ONCE and reused
   * because it is a `role="status"` live region: replacing the element on every paint
   * would land each new message in a node that was not yet in the accessibility tree, and
   * none of them would be announced.
   */
  private ensureShell(): NonNullable<DiscoveryPanel["shell"]> {
    if (this.shell !== null) return this.shell;
    const heading = document.createElement("h2");
    heading.className = "discovery-title";
    heading.id = "discovery-title";
    heading.textContent = "Found on this computer";

    const status = document.createElement("p");
    status.className = "discovery-status muted";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");

    // Sits immediately after the status line it acknowledges, so it is both adjacent to the
    // outcome and the first focusable thing after it. Hidden unless the panel is actually
    // being held open — an always-present "Dismiss" would invite closing the panel during a
    // scan or an import, which is not what it does.
    //
    // `.discovery-action` is reused purely for its existing button STYLE, which makes that
    // class shared rather than row-specific: anything selecting a ROW's action must scope
    // through `.discovery-row .discovery-action`, because this button is created first and a
    // bare selector finds it instead. `.discovery-dismiss` is the identifying hook.
    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "discovery-action discovery-dismiss";
    dismiss.textContent = "Dismiss";
    dismiss.hidden = true;
    dismiss.addEventListener("click", () => this.controller.acknowledge());

    // WHERE a new index would go, and the control that changes it. Created AFTER `dismiss`
    // on purpose: this button reuses `.discovery-action` for its STYLE exactly as Dismiss
    // does, and a bare `.discovery-action` selector finds whichever came first — a fact the
    // suite pins ("scopes a row's action away from the Dismiss button"). Inserting ahead of
    // Dismiss would silently steal that selector.
    //
    // Both are toggled with `hidden` rather than created and destroyed, so the destination
    // line keeps a stable node across repaints for the same reason the status line does.
    const destination = document.createElement("p");
    destination.className = "discovery-destination muted";
    destination.hidden = true;

    const change = document.createElement("button");
    change.type = "button";
    change.className = "discovery-action discovery-change-destination";
    change.textContent = CHANGE_DESTINATION_LABEL;
    change.hidden = true;
    change.addEventListener("click", () => void this.controller.changeDestination());

    const notes = document.createElement("p");
    notes.className = "discovery-notes muted";

    const groups = document.createElement("div");
    groups.className = "discovery-groups";

    this.container.setAttribute("role", "group");
    this.container.setAttribute("aria-labelledby", "discovery-title");
    this.container.replaceChildren(heading, status, dismiss, destination, change, notes, groups);
    this.shell = { status, dismiss, destination, change, notes, groups };
    return this.shell;
  }

  private paint(view: DiscoveryView): void {
    // `needsAttention` is what keeps a terminal phase on screen. Everything else about the
    // collapse is unchanged: an idle panel and a CLEAN import still hand the pane straight
    // back to the graph, with no toggle for the user to find.
    if ((view.phase === "idle" || view.phase === "done") && !view.needsAttention) {
      // Its job is finished: collapse and hand the pane back to the graph.
      this.teardown();
      return;
    }
    const shell = this.ensureShell();
    shell.status.textContent = view.status;
    // Offered ONLY while the panel is held open, because dismissing is then the only way
    // out — nothing else will collapse a panel whose outcome has not been read.
    shell.dismiss.hidden = !view.needsAttention;
    // "" means there is nothing honest to say — a corpus is attached, so no index will be
    // created, or no default resolved, so the dialog will ask. Both hide the pair.
    const named = view.destination !== "";
    shell.destination.hidden = !named;
    shell.destination.textContent = named ? destinationNote(view.destination) : "";
    shell.change.hidden = !named;
    shell.change.disabled = view.busy;
    shell.notes.textContent = view.notes.join(" ");
    shell.groups.replaceChildren(
      ...(view.emptyLabel !== "" ? [this.renderEmpty(view.emptyLabel)] : []),
      ...view.groups.map((group) => this.renderGroup(group, view.busy)),
    );
  }

  private renderEmpty(label: string): HTMLElement {
    const el = document.createElement("p");
    el.className = "discovery-empty muted";
    el.textContent = label;
    return el;
  }

  private renderGroup(group: DiscoveryGroup, busy: boolean): HTMLElement {
    const section = document.createElement("section");
    section.className = "discovery-group";
    section.setAttribute("role", "group");
    section.setAttribute("aria-label", group.heading);

    const heading = document.createElement("h3");
    heading.className = "discovery-group-title";
    heading.textContent = `${group.heading} (${groupDigits(group.totalCount)})`;
    section.appendChild(heading);

    for (const row of group.rows) section.appendChild(this.renderRow(row, busy));

    // The UI's own collapsing: a real control, because every hidden row is one click away.
    if (group.expandLabel !== "") {
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "discovery-more";
      toggle.textContent = group.expandLabel;
      toggle.disabled = busy;
      toggle.setAttribute("aria-expanded", String(group.expanded));
      toggle.addEventListener("click", () => this.controller.toggleGroup(group.key));
      section.appendChild(toggle);
    }
    // The ENGINE's cap: never a control. Rendered independently of the expansion, because
    // expanding reveals what this UI held back and can never reveal what the scan did not
    // return — so this sentence has to survive the expansion.
    if (group.capNote !== "") {
      const note = document.createElement("p");
      note.className = "discovery-note muted";
      note.textContent = group.capNote;
      section.appendChild(note);
    }
    return section;
  }

  private renderRow(row: DiscoveryRow, busy: boolean): HTMLElement {
    const el = document.createElement("div");
    el.className = "discovery-row";

    const text = document.createElement("div");
    text.className = "discovery-row-text";

    const name = document.createElement("span");
    name.className = "discovery-name";
    name.textContent = row.name;
    name.title = row.finding.path;

    const where = document.createElement("span");
    where.className = "discovery-parent muted";
    where.textContent = row.parent;
    where.title = row.finding.path;

    const summary = document.createElement("span");
    summary.className = "discovery-summary muted";
    summary.textContent = row.detail === "" ? row.summary : `${row.summary} · ${row.detail}`;

    text.append(name, where, summary);
    // Appended only when there IS one, so `.discovery-row-note` being absent is itself the
    // assertion that this row is unambiguous — an always-present empty span would make the
    // two cases indistinguishable to a test and to a screen reader alike.
    if (row.note !== "") {
      const note = document.createElement("span");
      note.className = "discovery-row-note muted";
      note.textContent = row.note;
      text.appendChild(note);
    }
    el.appendChild(text);

    if (row.action.kind === "none") {
      const reason = document.createElement("span");
      reason.className = "discovery-reason muted";
      reason.textContent = row.action.reason;
      el.appendChild(reason);
    } else {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "discovery-action";
      button.textContent = row.action.label;
      button.disabled = busy || !row.action.enabled;
      if (!row.action.enabled && row.action.reason !== "") button.title = row.action.reason;
      button.addEventListener("click", () => void this.controller.activate(row.finding));
      el.appendChild(button);
    }
    return el;
  }
}

/**
 * The default destination picker: the native save dialog, as the corpus bar uses the
 * native open dialog and for the same reason — a webview `<input type="file">` yields no
 * filesystem path under Tauri v2.
 *
 * Statically imported, like `ui/corpusBar`'s `open`. A lazy `import()` was tried first, on
 * the theory that the module would not load in the DOM-less test environment; that theory
 * is false — `corpusBar.test.ts` imports a module that statically imports this same
 * package and passes — and the lazy form bought nothing while adding a rollup warning,
 * because the static import elsewhere keeps the package in the main chunk regardless.
 */
function defaultChooseDestination(): Promise<string | null> {
  return save({
    title: "Create corpus index",
    defaultPath: "anthology.db",
    filters: [{ name: "Corpus index", extensions: ["db", "sqlite3", "sqlite"] }],
  });
}

// ---------------------------------------------------------------------------
// tiny helpers
// ---------------------------------------------------------------------------

function plural(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}
