/**
 * DEDUP: which of the owner's session files are redundant copies of each other.
 *
 * The engine side has worked for a long time with nothing able to reach it — `dedup.scan`
 * and `dedup.sessions` are both fully implemented (`llm_anthology/sidecar.py:1428-1455`)
 * and, until this panel, had no caller outside the Python tests.
 *
 * WHAT THIS PANEL DOES, and the boundary it does not cross: it REPORTS. `dedup` contains no
 * write, delete or move call, so nothing it can be asked to do removes one of the owner's
 * files (`sidecar.py:1400-1405`). The only destructive surface on this wire is
 * `maintenance.execute` (`cockpit/src/ipc/types.ts:986-992`), which is a separate plane with
 * a server-issued single-use handle and a typed confirmation. Acting on these results
 * therefore belongs to that plane, not here, and this file grows no delete button —
 * {@link REPORT_ONLY_NOTE} says so in the UI rather than leaving the absence to be read as
 * "not built yet".
 *
 * WHY A COUNT IS NOT AN ANSWER. "You have 12 duplicates" is unactionable and, worse, it is
 * an unverifiable claim about files the owner cannot replace. So every row carries what the
 * engine actually determined: the identity it matched on ({@link IDENTITY_BASIS_NOTE} — a
 * byte-equal session id and nothing else, `dedup.py:14-19`), which copy is being put
 * forward, and which copies merely also exist. Where the wire does not carry a fact, the row
 * says nothing rather than formatting a guess — a non-canonical copy travels as a PATH ONLY
 * (`sidecar.py:1423`), with no size, mtime or store kind, so no row may imply otherwise.
 *
 * THE EMPTY STATE IS FOUR STATES, not one, and conflating them is the defect this panel most
 * has to avoid. See {@link emptyReading}: a never-scanned panel, a scan that found no files
 * at all, a scan that found sessions but no redundancy, and a populated list are four
 * different facts, and exactly one of them ("sessions, no redundancy") is a clean bill of
 * health. On the synthetic fixture and on a fresh install the common case is one of the
 * first two, so they get their own words.
 *
 * THE SPLIT. Same shape as `ui/discoveryPanel` and `ui/corpusBar`: pure functions carry
 * every decision, a DOM-free controller sequences the engine calls, and a thin shell paints.
 * That is not stylistic — vitest runs `environment: "node"` here (`cockpit/vitest.config.ts`),
 * so a rule inside a method that also calls `document.createElement` cannot be asserted at
 * all and would sit at 0% coverage.
 *
 * FORMATTERS. `formatBytes` is IMPORTED from `ui/exportPanel` rather than re-written — that
 * module holds the repo's one byte formatter and depends on nothing but `ipc/types`, so the
 * import is cycle-free and cheap. `groupDigits`, `relativeAge` and `pathSummary` are
 * re-stated locally, and that IS duplication of three small functions `ui/discoveryPanel`
 * also exports. The reason is dependency direction, not ignorance: importing them would make
 * this panel's build depend on a sibling PANEL's exports. The honest fix once both settle is
 * to lift all four into a shared `ui/format` module and have every panel import that — not to
 * have one panel import another. Ownership meanwhile: `formatBytes`'s contract is
 * `exportPanel`'s, and `exportPanel.test.ts` is where it is specified.
 */

import { open } from "@tauri-apps/plugin-dialog";

import { engineErrorText } from "./errors";
import { formatBytes } from "./exportPanel";
import type { DedupScanResult, DedupSession, DiscoveryResult } from "../ipc/types";

// ---------------------------------------------------------------------------
// constants
// ---------------------------------------------------------------------------

/**
 * Duplicate groups shown before the rest collapse behind "+N more".
 *
 * 8 rather than discovery's 5 because a row here is the unit of the answer: a measured live
 * Codex store holds 2043 rollouts (`llm_anthology/discover.py`'s note on the same store), so
 * the list can be long, and a reader needs enough of it on screen to see whether the
 * duplication is a pattern (one mirror, every session) or a handful of stragglers.
 */
export const MAX_GROUP_ROWS = 8;

/** The one thing this panel will never do, stated where the user can read it. */
export const REPORT_ONLY_NOTE =
  "This is a report. Nothing here deletes, moves or removes a file — every copy listed is " +
  "still on disk exactly as it was.";

/** What a "duplicate" claim here actually rests on. */
export const IDENTITY_BASIS_NOTE =
  "Two files are the same session only when their session id matches byte for byte. " +
  "Nothing is matched on name, size, date or content similarity.";

/** Shown before any scan has run. Not a finding — the absence of one. */
export const NEVER_SCANNED_LABEL =
  "No Codex store has been scanned yet, so nothing is known about redundant copies. " +
  "Choose a Codex home below to scan it.";

/**
 * Why the list can hold more sessions than the last scan reported.
 *
 * `save_sessions` is INSERT-OR-REPLACE keyed on file path and never deletes a row
 * (`dedup.py:302-311`), while `load_sessions` re-consolidates every row in the table
 * (`dedup.py:313-317`). So the list is everything ever scanned into this index and the scan
 * tally is only the run that just finished. That divergence is correct behaviour, and
 * without saying so it reads as a miscount.
 */
export const ACCUMULATED_VIEW_NOTE =
  "This list includes sessions from an earlier scan of a different Codex home — the index " +
  "keeps what it has already scanned.";

// ---------------------------------------------------------------------------
// pure derivations — formatting
// ---------------------------------------------------------------------------

/**
 * Thousands-grouped digits.
 *
 * Hand-rolled rather than `toLocaleString()` because that reads the HOST locale, so the same
 * code emits "2,043" on one machine and "2 043" on another — a difference that would make
 * any assertion over this text pass or fail depending on whose box ran it.
 */
export function groupDigits(value: number): string {
  if (!Number.isFinite(value)) return String(value);
  const sign = value < 0 ? "-" : "";
  const digits = Math.abs(Math.trunc(value)).toString();
  return sign + digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

/**
 * "how recent" as text. `nowMs` is injected rather than read from the clock so the rule is
 * assertable; the shell passes `Date.now()`.
 *
 * null is REPORTED, never formatted: `PhysicalCopy.last_write_ms` is `Optional[int]`
 * (`dedup.py:118`) and `new Date(null)` is 1 January 1970, which reads as a real — and very
 * wrong — date rather than as "nothing datable was recorded".
 */
export function relativeAge(ms: number | null, nowMs: number): string {
  if (ms === null || !Number.isFinite(ms)) return "date unknown";
  const delta = nowMs - ms;
  if (delta < MINUTE_MS) return "just now";
  if (delta < HOUR_MS) return `${plural(Math.floor(delta / MINUTE_MS), "minute")} ago`;
  if (delta < DAY_MS) return `${plural(Math.floor(delta / HOUR_MS), "hour")} ago`;
  const days = Math.floor(delta / DAY_MS);
  if (days < 30) return `${plural(days, "day")} ago`;
  if (days < 365) return `${plural(Math.floor(days / 30), "month")} ago`;
  return `${plural(Math.floor(days / 365), "year")} ago`;
}

/** A path split into its final segment and everything above it, for either separator. */
export interface PathSummary {
  name: string;
  parent: string;
}

/**
 * Split `path` for display. Both separators are handled because these are real Windows paths
 * off the wire while the fixtures and tests use POSIX ones.
 *
 * The parent carries most of the signal here: duplicate copies of one session are the SAME
 * filename in two different store roots (`sessions` vs `sessions_backup`), so the name alone
 * distinguishes none of them.
 */
export function pathSummary(path: string): PathSummary {
  const trimmed = path.replace(/[\\/]+$/, "");
  const cut = Math.max(trimmed.lastIndexOf("/"), trimmed.lastIndexOf("\\"));
  if (cut === -1) return { name: trimmed, parent: "" };
  return { name: trimmed.slice(cut + 1), parent: trimmed.slice(0, cut) };
}

// ---------------------------------------------------------------------------
// pure derivations — where `codex_home` comes from
// ---------------------------------------------------------------------------

/**
 * Whether a path may be sent as `codex_home`, and if not, why — in the user's terms.
 *
 * Checked HERE as well as in the engine, not instead of it. The engine's refusal is a
 * JSON-RPC -32602 with internal wording (`tests/test_sidecar_dedup.py:90-96`), and for the
 * UNC case the point of refusing is to refuse BEFORE any filesystem access: a UNC root makes
 * the scan emit outbound SMB/NTLM, the Windows hash-leak class (`sidecar.py:487-497`). A
 * client-side check is a courtesy, never the guarantee — the engine's is the one that counts.
 *
 * The absoluteness rule is deliberately PERMISSIVE where this side cannot be certain: it
 * mirrors, rather than re-derives, Python's `os.path.isabs`, whose answer is
 * platform-dependent. A false rejection here would block a home the engine would have
 * accepted, so only the two forms that are certainly absolute are accepted and only the
 * clearly-relative are refused. UNVERIFIED: the exact set of paths where this and
 * `os.path.isabs` disagree has not been enumerated; the settling check is running both over
 * a shared table of spellings. The failure mode is benign — a path this lets through and the
 * engine rejects surfaces the engine's own message.
 */
export type CodexHomeVerdict = { ok: true } | { ok: false; reason: string };

export function checkCodexHome(path: string): CodexHomeVerdict {
  const trimmed = path.trim();
  if (trimmed === "") {
    return { ok: false, reason: "No folder was named, and this app will not guess one." };
  }
  if (trimmed.replace(/\//g, "\\").startsWith("\\\\")) {
    return {
      ok: false,
      reason: "That is a network (UNC) path. Only a folder on this computer can be scanned.",
    };
  }
  const driveAbsolute = /^[A-Za-z]:[\\/]/.test(trimmed);
  const posixAbsolute = trimmed.startsWith("/");
  if (!driveAbsolute && !posixAbsolute) {
    return { ok: false, reason: "That is not a full path — name the folder from the drive down." };
  }
  return { ok: true };
}

/** A Codex home the panel may honestly offer to scan. */
export interface CodexHomeCandidate {
  /** The value passed to `dedup.scan` as `codex_home`. */
  path: string;
  name: string;
  parent: string;
  /** "2,043 sessions · 15 minutes ago · high confidence". */
  summary: string;
}

/** A `detail` entry as a non-empty trimmed string, or "" when absent/blank/not a string. */
function detailString(detail: Record<string, unknown>, key: string): string {
  const value = detail[key];
  return typeof value === "string" ? value.trim() : "";
}

/** Windows paths are case-insensitive and may differ only by a trailing separator. */
function samePath(a: string, b: string): boolean {
  const norm = (p: string): string => p.replace(/[\\/]+$/, "").replace(/\//g, "\\").toLowerCase();
  return norm(a) === norm(b);
}

/**
 * The Codex homes a discovery scan found — the panel's answer to "where does `codex_home`
 * come from" when the user should not have to know the path.
 *
 * THE PROVIDER CHECK IS LOAD-BEARING, not tidiness. `dedup.known_store_roots` builds exactly
 * `<home>/sessions` and `<home>/sessions_backup` (`dedup.py:105-106`) — the Codex layout and
 * no other. Pointed at a Grok or Claude Code home, those two directories do not exist, the
 * scan returns a clean empty result (`tests/test_sidecar_dedup.py:101-107` proves a missing
 * root is empty, not an error), and the panel would report "no redundant copies" about a
 * store it never looked inside. A false clean bill of health on the owner's irreplaceable
 * data is exactly the claim this module must not make, so a non-Codex store is not offered.
 *
 * THE SHAPE CHECK. `discover.py:298-302` gives the Codex `StoreSpec` `report="base"`, so its
 * finding's `path` is the BASE — the codex home — and `detail.items_root` is the sessions
 * tree beneath it (`discover.py:578`). That is the same fact `discoveryPanel.deriveBuildParams`
 * relies on to fill `corpus.build`'s `codex_home`. A finding whose `items_root` is missing or
 * equal to `path` named the item tree ITSELF, so nothing in it identifies a home and
 * offering it would be a guess. KNOWN LIMIT: this reads shape plus provider name because the
 * finding carries no capability field; the settling change is for the engine to report which
 * providers its dedup layout covers.
 *
 * Nothing here scans, stats or opens anything: `known_store_roots` is pure path arithmetic,
 * so naming a home never reads the owner's sessions (`dedup.py:101-106`). The scan itself
 * still only happens when the user picks one.
 */
export function codexHomeCandidates(result: DiscoveryResult, nowMs: number): CodexHomeCandidate[] {
  const out: { candidate: CodexHomeCandidate; mtime: number }[] = [];
  for (const finding of result.findings) {
    if (finding.kind !== "session_store" || finding.provider !== "codex") continue;
    const itemsRoot = detailString(finding.detail, "items_root");
    if (itemsRoot === "" || samePath(itemsRoot, finding.path)) continue;
    if (!checkCodexHome(finding.path).ok) continue;
    const { name, parent } = pathSummary(finding.path);
    // `newest_mtime` is UNIX SECONDS here — discovery is the one surface on this wire that
    // reports seconds — and 0 means nothing datable was seen (`discover.py:552`).
    const ms = finding.newest_mtime > 0 ? Math.round(finding.newest_mtime * 1000) : null;
    out.push({
      mtime: finding.newest_mtime,
      candidate: {
        path: finding.path,
        name,
        parent,
        summary: `${groupDigits(finding.count)} ${finding.count === 1 ? "session" : "sessions"} · ${relativeAge(ms, nowMs)} · ${finding.confidence} confidence`,
      },
    });
  }
  return out
    .sort((a, b) => b.mtime - a.mtime || (a.candidate.path < b.candidate.path ? -1 : 1))
    .map((entry) => entry.candidate);
}

// ---------------------------------------------------------------------------
// pure derivations — the four empty readings
// ---------------------------------------------------------------------------

export type DedupEmptyKind = "never-scanned" | "no-files" | "no-duplicates" | "none";

export interface DedupEmptyReading {
  kind: DedupEmptyKind;
  /** Empty only when `kind` is "none". */
  label: string;
}

/**
 * Which of the four readings the panel is in. The whole point of this function is that three
 * of them are EMPTY LISTS that mean different things, and only one is good news.
 *
 *   `never-scanned`  — no scan has run. `dedup.sessions` returns `[]` here
 *     (`tests/test_sidecar_dedup.py:170-171`), and that is the absence of a measurement.
 *     Reporting it as "no duplicates" would assert something about the owner's files that
 *     nothing has looked at.
 *
 *   `no-files`       — a scan ran and found zero session files. A MISSING home produces
 *     EXACTLY this tally — `session_count: 0`, `copy_count: 0`, `errors: []`
 *     (`tests/test_sidecar_dedup.py:101-107`) — so it is indistinguishable from a real but
 *     empty store, and the likelier cause by far is the wrong folder. It names the folder it
 *     looked in so the user can see whether it is the one they meant.
 *
 *   `no-duplicates`  — sessions were found and none of them has a second copy. The only
 *     clean bill of health available, and the only one entitled to that wording.
 *
 *   `none`           — there is a list to render.
 *
 * `session_count` is checked before `duplicate_count` because a zero-session scan cannot
 * have meaningful duplicate arithmetic, whatever the other counters say.
 */
export function emptyReading(scan: DedupScanResult | null, home: string): DedupEmptyReading {
  if (scan === null) return { kind: "never-scanned", label: NEVER_SCANNED_LABEL };
  if (scan.session_count === 0) {
    return {
      kind: "no-files",
      label:
        `No Codex session files were found under ${home}. That is what an empty folder and ` +
        "the wrong folder both look like — check this is the Codex home you meant. A Codex " +
        "home contains a “sessions” folder.",
    };
  }
  if (scan.duplicate_count === 0) {
    return {
      kind: "no-duplicates",
      label:
        `${groupDigits(scan.session_count)} ${scan.session_count === 1 ? "session" : "sessions"} ` +
        "found, each stored in exactly one file. There are no redundant copies to report.",
    };
  }
  return { kind: "none", label: "" };
}

// ---------------------------------------------------------------------------
// pure derivations — what a row is allowed to claim
// ---------------------------------------------------------------------------

/** One physical file in a row, and everything honestly known about it. */
export interface DedupCopyLine {
  path: string;
  name: string;
  parent: string;
  /** `kept` is the copy the view puts forward; `other` files also exist, untouched. */
  role: "kept" | "other";
  /**
   * "live store · 403.2 KB · 5 minutes ago" for the kept copy; ALWAYS "" for the others.
   *
   * Not an oversight — the wire carries `store_kind`, `size_bytes` and `last_write_ms` for
   * the CANONICAL copy only (`sidecar.py:1416-1426`); `duplicate_paths` is a list of strings
   * (`:1423`). Filling this in for a non-canonical copy would mean inventing it.
   */
  detail: string;
}

/** One logical session as the panel presents it. */
export interface DedupRow {
  /** The wire record, unmodified, for anything a caller needs beyond the derived text. */
  session: DedupSession;
  /** "3 copies of one session" — or, for an unidentified file, what it actually is. */
  headline: string;
  /** WHY these files are called the same session. Never omitted. */
  basis: string;
  kept: DedupCopyLine;
  /** The non-canonical copies. Empty for a singleton. */
  others: DedupCopyLine[];
  /** {@link truncationWarning} for this row; "" when it does not apply. */
  warning: string;
}

/**
 * The truncation warning, or "" — the one condition where the view would otherwise hide
 * something the owner has.
 *
 * `has_larger_copy` means the canonical copy is SMALLER than one it demoted
 * (`dedup.py:149-164`): store rank outranks size, correctly, so a crash-truncated LIVE
 * rollout legitimately beats a complete backup, and the conversation shown is a prefix of
 * the one on disk. Reported rather than resolved, because reversing the canonical rule would
 * let a stale mirror outrank the authoritative store.
 *
 * WHETHER THE FULLER COPY CAN BE NAMED depends on the arithmetic, and this is the line
 * between a deduction and a guess. `has_larger_copy` says "some copy is larger" without
 * saying which, and no size travels for a non-canonical copy. With exactly one other copy,
 * that copy IS the larger one and naming it is sound. With two or more, it is not, so the
 * warning stays unnamed and lists them all instead.
 */
export function truncationWarning(session: DedupSession): string {
  if (!session.has_larger_copy) return "";
  const shown = `The copy shown is ${formatBytes(session.size_bytes)} and is a shorter, truncated version of this conversation.`;
  if (session.duplicate_paths.length === 1) {
    return `${shown} The complete one is ${session.duplicate_paths[0]}.`;
  }
  // The engine reports THAT a larger copy exists, not which; saying more would be invented.
  return `${shown} One of the other copies listed below is longer — the engine reports that a larger copy exists but not which one.`;
}

function copyLine(path: string, role: "kept" | "other", detail: string): DedupCopyLine {
  const { name, parent } = pathSummary(path);
  return { path, name, parent, role, detail };
}

function toRow(session: DedupSession, nowMs: number): DedupRow {
  const kept = copyLine(
    session.canonical_path,
    "kept",
    `${session.store_kind} store · ${formatBytes(session.size_bytes)} · ${relativeAge(session.last_write_ms, nowMs)}`,
  );
  const others = session.duplicate_paths.map((path) => copyLine(path, "other", ""));

  if (!session.is_identified) {
    return {
      session,
      headline: "One file, no session id",
      // Not a duplicate claim at all, so the basis explains why it is here instead.
      basis:
        "No session id could be read from this file or its name, so it was kept on its own " +
        "and never merged with anything. It may or may not duplicate another file — this " +
        "scan cannot tell.",
      kept,
      others,
      warning: truncationWarning(session),
    };
  }
  return {
    session,
    // Always plural, not a ternary: {@link partitionSessions} routes an identified session
    // with one copy to `singleCopyCount` and never to a row, so a row here always has two or
    // more. A `copy_count === 1` arm would be unreachable — and an unreachable arm is a
    // branch no test can cover and no reader can trust.
    headline: `${groupDigits(session.copy_count)} copies of one session`,
    basis: `Same session id ${session.session_id}, matched byte for byte.`,
    kept,
    others,
    warning: truncationWarning(session),
  };
}

export interface PartitionOptions {
  nowMs: number;
  /** Show every duplicate row rather than the first {@link MAX_GROUP_ROWS}. */
  expanded?: boolean;
  /** Rows before collapsing. Defaults to {@link MAX_GROUP_ROWS}. */
  maxRows?: number;
}

/** The rendered answer, split by what each group of files actually is. */
export interface DedupPartition {
  /** Sessions backed by two or more files — the answer to "what is redundant". */
  duplicates: DedupRow[];
  /** Files with no recoverable session id. NOT duplicates; a separate data-integrity note. */
  unidentified: DedupRow[];
  /** Identified sessions stored exactly once. Counted, never listed — nothing to act on. */
  singleCopyCount: number;
  hiddenCount: number;
  expandLabel: string;
}

/**
 * Order: truncation-flagged first, then the widest redundancy, then the largest, then path
 * and id so the same input always renders identically.
 *
 * Flagged first because it is the only row that is actively costing the reader something —
 * the app is showing them a shorter conversation than the one they have. Copy count next
 * because that is the size of the redundancy. The last two keys exist only to make the order
 * total; without them two otherwise-identical rows could swap between renders.
 */
function byUrgency(a: DedupSession, b: DedupSession): number {
  const flag = Number(b.has_larger_copy) - Number(a.has_larger_copy);
  if (flag !== 0) return flag;
  if (a.copy_count !== b.copy_count) return b.copy_count - a.copy_count;
  if (a.size_bytes !== b.size_bytes) return b.size_bytes - a.size_bytes;
  const byPath = compareText(a.canonical_path, b.canonical_path);
  return byPath !== 0 ? byPath : compareText(a.session_id, b.session_id);
}

/**
 * The expand/collapse control's label, or "" when there is nothing to toggle.
 *
 * Every hidden row is present and one click away, which is what makes this a button. It is
 * NOT the same kind of fact as an engine-side cap — see `ui/discoveryPanel`'s note on its two
 * truncations — and `dedup.sessions` returns the whole persisted view with no cap of its own
 * (`sidecar.py:1451-1455`), so this UI's collapsing is the only truncation on this surface.
 */
export function expandLabel(hiddenCount: number, expanded: boolean): string {
  if (hiddenCount > 0) return `+${groupDigits(hiddenCount)} more`;
  return expanded ? "Show fewer" : "";
}

/**
 * The persisted dedup view -> the rows to render, partitioned by what each one IS.
 *
 * The three buckets are not a display preference. A single-copy session is not redundant, so
 * listing it among duplicates would pad the answer with non-answers; an unidentified file was
 * keyed by PATH precisely so it could never merge (`dedup.py:38-44`, `:174-180`), so calling
 * it a duplicate reports the one thing it is not.
 */
export function partitionSessions(
  sessions: readonly DedupSession[],
  options: PartitionOptions,
): DedupPartition {
  const maxRows = options.maxRows ?? MAX_GROUP_ROWS;
  const expanded = options.expanded ?? false;

  const duplicated: DedupSession[] = [];
  const unidentified: DedupSession[] = [];
  let singleCopyCount = 0;
  for (const session of sessions) {
    if (!session.is_identified) unidentified.push(session);
    else if (session.copy_count > 1) duplicated.push(session);
    else singleCopyCount += 1;
  }

  const ordered = [...duplicated].sort(byUrgency);
  const limit = expanded ? ordered.length : Math.min(maxRows, ordered.length);
  const shown = ordered.slice(0, limit);
  const hiddenCount = ordered.length - shown.length;

  return {
    duplicates: shown.map((session) => toRow(session, options.nowMs)),
    unidentified: [...unidentified]
      .sort((a, b) => compareText(a.canonical_path, b.canonical_path))
      .map((session) => toRow(session, options.nowMs)),
    singleCopyCount,
    hiddenCount,
    expandLabel: expandLabel(hiddenCount, expanded),
  };
}

/**
 * What the scan itself cost and every way it might not tell the whole story.
 *
 * Attributed to "this scan" throughout, because the tally and the LIST answer different
 * questions — see {@link ACCUMULATED_VIEW_NOTE}. Unreadable locations are counted, never
 * pasted: the engine's `errors` are raw strings like
 * `C:\x: [WinError 5] Access is denied`, and reproducing them under a report would bury the
 * findings under noise about directories the user never asked about. That some were skipped
 * is the actionable part; which ones is not.
 */
export function scanNotes(scan: DedupScanResult): string[] {
  const notes = [
    `This scan read ${groupDigits(scan.copy_count)} ${scan.copy_count === 1 ? "file" : "files"} ` +
      `holding ${groupDigits(scan.session_count)} ${scan.session_count === 1 ? "session" : "sessions"}, ` +
      `of which ${groupDigits(scan.duplicate_count)} ${scan.duplicate_count === 1 ? "file is a repeat" : "files are repeats"}.`,
  ];
  if (scan.flagged_truncated > 0) {
    notes.push(
      `${groupDigits(scan.flagged_truncated)} ${scan.flagged_truncated === 1 ? "session shows" : "sessions show"} ` +
        "a shorter copy than the one also on disk — see the warnings below.",
    );
  }
  if (scan.unidentified > 0) {
    notes.push(
      `${groupDigits(scan.unidentified)} ${scan.unidentified === 1 ? "file has" : "files have"} ` +
        "no readable session id and could not be compared with anything.",
    );
  }
  if (scan.errors.length > 0) {
    notes.push(
      `${groupDigits(scan.errors.length)} ${scan.errors.length === 1 ? "location was" : "locations were"} ` +
        "skipped (missing, or not readable).",
    );
  }
  return notes;
}

// ---------------------------------------------------------------------------
// controller
// ---------------------------------------------------------------------------

/**
 * The IPC this panel needs, narrowed from `IpcClient` (interface segregation) so a test
 * injects three methods rather than the whole data surface.
 *
 * `discoverSources` is here because it is the only honest route to a `codex_home` that does
 * not make the user type a path — see {@link codexHomeCandidates}.
 */
export interface DedupIpc {
  dedupScan(codexHome: string): Promise<DedupScanResult>;
  dedupSessions(): Promise<DedupSession[]>;
  discoverSources(): Promise<DiscoveryResult>;
}

/** The environment seams: the clock and the folder picker. */
export interface DedupDeps {
  now(): number;
  /**
   * Ask for a Codex home. Resolves null when the user dismisses, which is a no-op and not an
   * error — the same contract as the corpus bar's and the discovery panel's pickers.
   */
  chooseCodexHome(): Promise<string | null>;
  maxRows?: number;
}

export interface DedupView {
  phase: "idle" | "scanning" | "ready" | "error";
  /** The home the current results came from; "" before any scan. */
  home: string;
  candidates: CodexHomeCandidate[];
  duplicates: DedupRow[];
  unidentified: DedupRow[];
  singleCopyCount: number;
  hiddenCount: number;
  expandLabel: string;
  expanded: boolean;
  /** Which of the four readings applies. `kind: "none"` means render the list. */
  empty: DedupEmptyReading;
  notes: string[];
  /** One line of progress or refusal. Never a raw engine string. */
  status: string;
  busy: boolean;
}

export type DedupViewListener = (view: DedupView) => void;

function initialView(): DedupView {
  return {
    phase: "idle",
    home: "",
    candidates: [],
    duplicates: [],
    unidentified: [],
    singleCopyCount: 0,
    hiddenCount: 0,
    expandLabel: "",
    expanded: false,
    empty: emptyReading(null, ""),
    notes: [],
    status: "",
    busy: false,
  };
}

/**
 * Headless dedup controller: finds the Codex homes worth offering, scans the one the user
 * names, and emits a {@link DedupView} on every transition.
 *
 * Single-flight throughout, like the discovery panel's controller: a re-entrant call while
 * one is in flight is IGNORED rather than queued, so a double-click cannot run two scans.
 * That matters more than usual here — a scan writes the derived view into the attached index
 * (`sidecar.py:1440-1441`), so two overlapping scans of different homes would interleave
 * their writes.
 *
 * NO SCAN IS EVER AUTOMATIC. There is no boot-time scan and no default home: `dedup.scan`
 * requires `codex_home` and never defaults it, deliberately, because an automated probe
 * really did read the owner's live Codex sessions through a similar fallback
 * (`sidecar.py:1429-1434`). The user names the folder; this class only carries it.
 */
export class DedupPanelController {
  private view: DedupView = initialView();
  private busy = false;
  private disposed = false;
  /**
   * The last loaded view, kept so an expand re-derives without re-scanning.
   *
   * The scan TALLY is deliberately not cached beside it: {@link emit} merges patches, so the
   * `empty` reading derived once at scan time survives every later paint. Holding a second
   * copy of it would be a field that has to be kept in sync for no gain.
   *
   * (A field named `scan` was the first attempt and is a trap worth naming: a field
   * initializer assigns to the INSTANCE, shadowing the prototype method of the same name, so
   * `private scan = null` silently turned `controller.scan(...)` into "is not a function" —
   * and tsc does not flag the collision.)
   */
  private sessions: readonly DedupSession[] = [];
  private expanded = false;

  constructor(
    private readonly ipc: DedupIpc,
    private readonly deps: DedupDeps,
    private readonly onChange: DedupViewListener,
  ) {}

  get current(): DedupView {
    return this.view;
  }

  private emit(patch: Partial<DedupView>): void {
    if (this.disposed) return;
    this.view = { ...this.view, ...patch };
    this.onChange(this.view);
  }

  /** Re-derive the rendered rows from the last load (after a scan or an expand). */
  private repartition(): Partial<DedupView> {
    const part = partitionSessions(this.sessions, {
      nowMs: this.deps.now(),
      expanded: this.expanded,
      maxRows: this.deps.maxRows,
    });
    return {
      duplicates: part.duplicates,
      unidentified: part.unidentified,
      singleCopyCount: part.singleCopyCount,
      hiddenCount: part.hiddenCount,
      expandLabel: part.expandLabel,
      expanded: this.expanded,
    };
  }

  /**
   * Ask discovery which Codex homes exist, so the user picks a name rather than a path.
   *
   * A failure here is not fatal to the panel — the folder picker still works — so it reports
   * and leaves the phase alone rather than tearing the surface down.
   */
  async loadCandidates(): Promise<void> {
    if (this.busy || this.disposed) return;
    this.busy = true;
    this.emit({ status: "Looking for Codex session stores on this computer…", busy: true });
    try {
      const result = await this.ipc.discoverSources();
      if (this.disposed) return;
      const candidates = codexHomeCandidates(result, this.deps.now());
      this.emit({
        candidates,
        status:
          candidates.length === 0
            ? "No Codex session store was found automatically — choose a folder to scan it."
            : "",
        busy: false,
      });
    } catch (err) {
      this.emit({
        status: engineErrorText(err, "Could not look for Codex stores"),
        busy: false,
      });
    } finally {
      this.busy = false;
    }
  }

  /**
   * Scan `codexHome`, then load the view it produced.
   *
   * Two calls, in this order, because they answer different questions: `dedup.scan` returns
   * the tally for THIS run and `dedup.sessions` returns the whole persisted view
   * (`sidecar.py:1442-1455`). Both are needed — the tally is what distinguishes an empty
   * store from a clean one, and the list is what the user reads.
   */
  async scan(codexHome: string): Promise<void> {
    if (this.busy || this.disposed) return;
    const verdict = checkCodexHome(codexHome);
    if (!verdict.ok) {
      // Refused before the wire. Not a failed scan — a scan that was never started.
      this.emit({ phase: "error", status: verdict.reason, busy: false });
      return;
    }
    this.busy = true;
    this.emit({ phase: "scanning", home: codexHome, status: `Scanning ${codexHome}…`, busy: true });
    try {
      const scan = await this.ipc.dedupScan(codexHome);
      if (this.disposed) return;
      const sessions = await this.ipc.dedupSessions();
      if (this.disposed) return;
      this.sessions = sessions;
      this.expanded = false;
      const notes = scanNotes(scan);
      // The list can legitimately hold more than this run found — see the constant. Said
      // only when the divergence is actually on screen, so it is never idle boilerplate.
      if (sessions.length > scan.session_count) notes.push(ACCUMULATED_VIEW_NOTE);
      this.emit({
        phase: "ready",
        ...this.repartition(),
        empty: emptyReading(scan, codexHome),
        notes,
        status: "",
        busy: false,
      });
    } catch (err) {
      this.emit({ phase: "error", status: engineErrorText(err, "Scan failed"), busy: false });
    } finally {
      this.busy = false;
    }
  }

  /** Ask for a folder and scan it. A dismissed dialog changes nothing at all. */
  async pickHome(): Promise<void> {
    if (this.busy || this.disposed) return;
    this.busy = true;
    let picked: string | null;
    try {
      picked = await this.deps.chooseCodexHome();
    } catch (err) {
      this.emit({ status: engineErrorText(err, "Could not open the folder picker"), busy: false });
      return;
    } finally {
      this.busy = false;
    }
    if (this.disposed || picked === null) return;
    await this.scan(picked);
  }

  /** Show every duplicate row, or collapse back to the cap. Re-derives; never re-scans. */
  toggleExpanded(): void {
    if (this.disposed) return;
    this.expanded = !this.expanded;
    this.emit(this.repartition());
  }

  /** Drop the loaded view without touching the index. */
  destroy(): void {
    this.disposed = true;
  }
}

// ---------------------------------------------------------------------------
// DOM shell
// ---------------------------------------------------------------------------

/**
 * The panel's DOM binding: builds the skeleton once, repaints on every view, and writes
 * everything with `textContent`, never `innerHTML`.
 *
 * That last point is not boilerplate here. Every string this paints is either a local
 * filesystem path off the wire or an engine error, and paths are display-sensitive in their
 * own right: `canonical_path` embeds the owner's username and is absent from
 * `redact.MetadataView` (`sidecar.py:1403-1405`). It reaches the DOM as text and never as
 * markup, and it must not leave the app.
 */
export class DedupPanel {
  private readonly controller: DedupPanelController;
  private shell: {
    status: HTMLElement;
    candidates: HTMLElement;
    notes: HTMLElement;
    body: HTMLElement;
  } | null = null;

  constructor(
    ipc: DedupIpc,
    private readonly container: HTMLElement,
    deps: Partial<DedupDeps> = {},
  ) {
    this.controller = new DedupPanelController(
      ipc,
      {
        now: deps.now ?? ((): number => Date.now()),
        chooseCodexHome: deps.chooseCodexHome ?? defaultChooseCodexHome,
        maxRows: deps.maxRows,
      },
      (view) => this.paint(view),
    );
  }

  /** Populate the candidate list. Does NOT scan — a scan is always the user's click. */
  async load(): Promise<void> {
    await this.controller.loadCandidates();
  }

  destroy(): void {
    this.controller.destroy();
    this.container.replaceChildren();
    this.container.removeAttribute("role");
    this.container.removeAttribute("aria-labelledby");
    this.shell = null;
  }

  /**
   * Create the skeleton once. The status line is created ONCE and reused because it is a
   * `role="status"` live region: replacing the element on every paint would land each
   * message in a node that was not yet in the accessibility tree, so none would be announced.
   */
  private ensureShell(): NonNullable<DedupPanel["shell"]> {
    if (this.shell !== null) return this.shell;

    const heading = document.createElement("h2");
    heading.className = "dedup-title";
    heading.id = "dedup-title";
    heading.textContent = "Duplicate session copies";

    // The two standing facts, painted before any result: what this panel will not do, and
    // what a duplicate claim rests on. Both belong above the list, not in a footnote.
    const charter = document.createElement("p");
    charter.className = "dedup-charter muted";
    charter.textContent = `${REPORT_ONLY_NOTE} ${IDENTITY_BASIS_NOTE}`;

    const status = document.createElement("p");
    status.className = "dedup-status muted";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");

    const candidates = document.createElement("div");
    candidates.className = "dedup-candidates";

    const notes = document.createElement("p");
    notes.className = "dedup-notes muted";

    const body = document.createElement("div");
    body.className = "dedup-body";

    this.container.setAttribute("role", "group");
    this.container.setAttribute("aria-labelledby", "dedup-title");
    this.container.replaceChildren(heading, charter, status, candidates, notes, body);
    this.shell = { status, candidates, notes, body };
    return this.shell;
  }

  private paint(view: DedupView): void {
    const shell = this.ensureShell();
    shell.status.textContent = view.status;
    shell.notes.textContent = view.notes.join(" ");
    shell.candidates.replaceChildren(...this.renderCandidates(view));
    shell.body.replaceChildren(...this.renderBody(view));
  }

  /** The "where to scan" controls: each discovered home, plus the manual fallback. */
  private renderCandidates(view: DedupView): HTMLElement[] {
    const out: HTMLElement[] = [];
    for (const candidate of view.candidates) {
      const row = document.createElement("div");
      row.className = "dedup-candidate";

      const text = document.createElement("div");
      text.className = "dedup-candidate-text";
      const name = document.createElement("span");
      name.className = "dedup-candidate-name";
      name.textContent = candidate.path;
      const summary = document.createElement("span");
      summary.className = "dedup-candidate-summary muted";
      summary.textContent = candidate.summary;
      text.append(name, summary);

      const button = document.createElement("button");
      button.type = "button";
      button.className = "dedup-scan";
      button.textContent = "Scan for duplicates";
      button.disabled = view.busy;
      button.addEventListener("click", () => void this.controller.scan(candidate.path));

      row.append(text, button);
      out.push(row);
    }

    const pick = document.createElement("button");
    pick.type = "button";
    pick.className = "dedup-pick";
    pick.textContent = view.candidates.length === 0 ? "Choose Codex folder…" : "Scan another folder…";
    pick.disabled = view.busy;
    pick.addEventListener("click", () => void this.controller.pickHome());
    out.push(pick);
    return out;
  }

  private renderBody(view: DedupView): HTMLElement[] {
    const out: HTMLElement[] = [];
    if (view.empty.label !== "") {
      const el = document.createElement("p");
      // The three empty readings are distinct facts, so each carries its own class: a
      // never-scanned panel and a clean store must not be able to look identical.
      el.className = `dedup-empty dedup-empty-${view.empty.kind} muted`;
      el.textContent = view.empty.label;
      out.push(el);
    }
    for (const row of view.duplicates) out.push(this.renderRow(row, "dedup-row"));

    if (view.expandLabel !== "") {
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "dedup-more";
      toggle.textContent = view.expandLabel;
      toggle.disabled = view.busy;
      toggle.setAttribute("aria-expanded", String(view.expanded));
      toggle.addEventListener("click", () => this.controller.toggleExpanded());
      out.push(toggle);
    }
    if (view.singleCopyCount > 0) {
      const el = document.createElement("p");
      el.className = "dedup-singles muted";
      el.textContent =
        `${groupDigits(view.singleCopyCount)} other ` +
        `${view.singleCopyCount === 1 ? "session is" : "sessions are"} stored in a single file, ` +
        "with nothing redundant to report.";
      out.push(el);
    }
    if (view.unidentified.length > 0) {
      const heading = document.createElement("h3");
      heading.className = "dedup-subtitle";
      heading.textContent = "Files with no readable session id";
      out.push(heading);
      for (const row of view.unidentified) out.push(this.renderRow(row, "dedup-row dedup-row-unidentified"));
    }
    return out;
  }

  private renderRow(row: DedupRow, className: string): HTMLElement {
    const section = document.createElement("section");
    section.className = className;

    const headline = document.createElement("p");
    headline.className = "dedup-headline";
    headline.textContent = row.headline;
    section.appendChild(headline);

    const basis = document.createElement("p");
    basis.className = "dedup-basis muted";
    basis.textContent = row.basis;
    section.appendChild(basis);

    if (row.warning !== "") {
      const warning = document.createElement("p");
      warning.className = "dedup-warning";
      // A plain-text mark, not an emoji-presentation glyph: those render double-width in
      // some terminals and fonts and shove the line out of alignment.
      warning.textContent = `! ${row.warning}`;
      section.appendChild(warning);
    }

    section.appendChild(this.renderCopy(row.kept, "Kept in view"));
    for (const other of row.others) section.appendChild(this.renderCopy(other, "Also on disk"));
    return section;
  }

  private renderCopy(copy: DedupCopyLine, roleLabel: string): HTMLElement {
    const el = document.createElement("div");
    el.className = `dedup-copy dedup-copy-${copy.role}`;

    const role = document.createElement("span");
    role.className = "dedup-copy-role";
    role.textContent = roleLabel;

    const path = document.createElement("span");
    path.className = "dedup-copy-path";
    path.textContent = copy.path;
    path.title = copy.path;

    el.append(role, path);
    if (copy.detail !== "") {
      const detail = document.createElement("span");
      detail.className = "dedup-copy-detail muted";
      detail.textContent = copy.detail;
      el.appendChild(detail);
    }
    return el;
  }
}

/**
 * The default folder picker: the native directory dialog, for the same reason the corpus bar
 * uses the native file dialog — a webview `<input type="file">` yields no filesystem path
 * under Tauri v2.
 *
 * `multiple: false` narrows the plugin's `string | string[] | null` return to one path, but
 * the array case is handled anyway: a signature change there would otherwise turn into
 * `codex_home: "['C:\\...']"` on the wire.
 */
async function defaultChooseCodexHome(): Promise<string | null> {
  const picked = await open({
    multiple: false,
    directory: true,
    title: "Choose Codex home folder",
  });
  if (picked === null) return null;
  return Array.isArray(picked) ? (picked[0] ?? null) : picked;
}

// ---------------------------------------------------------------------------
// tiny helpers
// ---------------------------------------------------------------------------

function plural(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

/** Ordinal string compare, so a sort key is total and platform-independent. */
function compareText(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}
