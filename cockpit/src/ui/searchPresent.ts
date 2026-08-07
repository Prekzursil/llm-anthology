/**
 * What a search result SAYS — the presentation decisions, split out from the DOM.
 *
 * Same reason as `emptyStateLabel`: vitest runs `environment: "node"` in this project, so a
 * rule living inside a method that also calls `document.createElement` has no way to be
 * tested. Everything here is a pure string decision; `search.ts` does nothing but apply it.
 */

import type { SearchHit } from "../ipc/types";

/** Thousands-grouped, so a five-digit count is readable at a glance. */
function grouped(n: number): string {
  return n.toLocaleString("en-US");
}

/**
 * The text of a result row.
 *
 * Never empty. The engine's `snippet` is the conversation title cleaned up, and a title is
 * genuinely optional — an untitled conversation produced a row with no text at all, which
 * renders as a blank clickable button that looks like a rendering fault. Falling back to
 * "untitled" alone would make every such hit identical, so the id comes with it.
 */
export function hitLabel(hit: SearchHit): string {
  // Collapse whitespace: a title carrying newlines would otherwise overflow the fixed row
  // height the virtualized list measures against.
  const text = (hit.snippet ?? "").replace(/\s+/g, " ").trim();
  return text === "" ? `untitled · ${hit.conversation_id}` : text;
}

const HOUR = 3_600_000;
const DAY = 86_400_000;
const WEEK = 7 * DAY;

/**
 * How long ago a hit happened, or "" when it carries no timestamp.
 *
 * `ts_ms` was already on every hit and thrown away, leaving results with no time context in
 * an app whose whole subject is sessions over time.
 *
 * Bucketed on the ELAPSED delta rather than on calendar fields, so the result does not
 * depend on the machine's time zone. Past roughly two months "43w ago" stops meaning
 * anything and an absolute date is more useful; that one is UTC, and labelled as such by
 * being written ISO-style rather than localised.
 *
 * An undated conversation is real — the legacy canonical DB has no `updated_at_ms` — so this
 * returns nothing for it rather than inventing a time.
 */
export function relativeWhen(tsMs: number | undefined, nowMs: number): string {
  if (tsMs === undefined) return "";
  // A FUTURE timestamp (clock skew, or a rollout written moments ahead of the query) gives a
  // negative delta, which falls into the first bucket and reads as "just now" — correct, and
  // the reason there is no clamp here. An earlier version had `Math.max(0, ...)`; mutation
  // testing showed removing it changed no output at all, because every branch below is an
  // upper bound and a negative number satisfies the first one.
  const delta = nowMs - tsMs;
  if (delta < HOUR) return "just now";
  if (delta < DAY) return `${Math.floor(delta / HOUR)}h ago`;
  if (delta < WEEK) return `${Math.floor(delta / DAY)}d ago`;
  if (delta < 8 * WEEK) return `${Math.floor(delta / WEEK)}w ago`;
  return new Date(tsMs).toISOString().slice(0, 10);
}

/** Everything the status line needs to be truthful about what is on screen. */
export interface StatusInput {
  /** Matches in the corpus. */
  total: number;
  /** Rows actually handed to the list. */
  shown: number;
  tookMs: number;
  /** The active provider filter, if any. */
  provider?: string;
}

/**
 * The line above the results.
 *
 * It used to print the true total over a list capped at 200 rows, with nothing saying which
 * of the two you were looking at — so a user who scrolled to the bottom of 200 concluded the
 * other 1,232 did not exist. That is the same silent-truncation class as the sidebar showing
 * its thousand oldest threads. When the list is partial, it says so first.
 */
export function resultStatus({ total, shown, tookMs, provider }: StatusInput): string {
  const scope = provider ? ` in ${provider}` : "";
  const took = ` · ${tookMs}ms`;
  if (total === 0) return `No matches${scope}${took}`;
  if (shown < total) return `showing ${grouped(shown)} of ${grouped(total)} hits${scope}${took}`;
  return `${grouped(total)} hit${total === 1 ? "" : "s"}${scope}${took}`;
}

/**
 * The params for one query.
 *
 * Exists as a function purely so the provider filter has a test. `SearchParams.provider` was
 * on the wire contract from the start and the panel never sent it — a capability nothing
 * exercises is indistinguishable from one that does not work, and inlining this in a method
 * that also touches `document` would leave the replacement equally unexercised.
 *
 * `provider` is OMITTED rather than sent empty: the engine rejects a non-string but treats
 * any string as a filter, so `provider: ""` would filter to the providers named "".
 */
export function searchParams(
  q: string,
  provider: string,
  limit: number,
): { q: string; limit: number; provider?: string } {
  return provider === "" ? { q, limit } : { q, limit, provider };
}

/**
 * Which filter choice to select after the options are rebuilt.
 *
 * Keeps the user's selection across a corpus reload when that provider still exists, and
 * falls back to "everything" when it does not — otherwise opening a codex-only corpus while
 * "grok" is selected leaves a filter pinned to a provider with no rows, and the resulting
 * empty result list looks like an empty corpus.
 */
export function nextFilterValue(options: ProviderOption[], previous: string): string {
  return options.some((o) => o.value === previous) ? previous : "";
}

/** One entry in the provider filter. */
export interface ProviderOption {
  /** The `provider` param to send; "" means no filter. */
  value: string;
  label: string;
}

/**
 * Filter choices, derived from `corpus.stats().providers` — the providers this corpus
 * actually holds, not every provider the app can ingest. Offering "gemini" against a
 * codex-only store is a filter that can only ever return nothing.
 *
 * Returns NOTHING for a corpus with fewer than two providers: a filter whose only real
 * choice is the entire corpus is noise, and the caller hides the control entirely.
 *
 * Ordered by size (the big store is the one people narrow to) and then by name, so the list
 * does not reshuffle between two renders of the same corpus.
 */
export function providerOptions(providers: Record<string, number>): ProviderOption[] {
  const present = Object.entries(providers).filter(([, n]) => n > 0);
  if (present.length < 2) return [];
  present.sort((a, b) => (b[1] - a[1]) || (a[0] < b[0] ? -1 : 1));
  const total = present.reduce((sum, [, n]) => sum + n, 0);
  return [
    { value: "", label: `All providers (${grouped(total)})` },
    ...present.map(([name, n]) => ({ value: name, label: `${name} (${grouped(n)})` })),
  ];
}
