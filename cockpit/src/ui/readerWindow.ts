/**
 * HOW MUCH of a transcript the reader puts in the DOM, and what it says about the rest.
 *
 * Same seam as `readerPresent` and `emptyStateLabel`: vitest runs `environment: "node"` here,
 * so a rule living inside a method that also calls `document.createElement` cannot be tested
 * at all. The decisions are here; `reader.ts` only paints them.
 *
 * WHY THIS EXISTS — measured, not assumed. `reader.ts` built every turn of a conversation in
 * one `replaceChildren`. Over this machine's real corpus (13,099 Claude Code sessions, parsed
 * with the engine's own `llm_anthology/adapters/claude_code.py` `parse_transcript_file`):
 *
 *   * a uniform random sample (n=400) is TINY — median 2 turns, p95 3, p99 8, max 107;
 *   * the 25 largest sessions are not — worst case 2,562 turns / 22,319 blocks / 25.0M
 *     characters of body text, and a 616 MB session file yielding 2,544 turns / 27.6M
 *     characters. At the 4-elements-per-turn-plus-2-to-3-per-block that `reader.ts` builds,
 *     that is ~66,000 elements and ~27 MB of text in ONE synchronous call.
 *
 * So the distribution is the point: 99.75% of conversations never come near this, which is
 * precisely why it survived to here — the tail is rare AND catastrophic, the shape that no
 * casual test produces and a real session eventually does.
 *
 * WHY PROGRESSIVE DISCLOSURE RATHER THAN `VirtualList`. The obvious reuse does not fit, for a
 * reason visible in its own source: `virtualList.ts` is FIXED-row-height — it positions every
 * row at `top = i * itemHeight` (`virtualList.ts:144`) and forces `height = itemHeight`
 * (`:147`). Transcript turns are the opposite of uniform; the measured corpus puts a one-line
 * "ok" next to a single 475,569-character tool result. Forcing those onto a uniform grid
 * either clips the long one or leaves an enormous gap after the short one, and a
 * variable-height rewrite would have to MEASURE each turn, which means building it — the very
 * cost being avoided.
 *
 * It is also the safer half of the trade. `VirtualList.paint()` repaints by
 * `sizer.replaceChildren()`, which detaches the focused row and drops focus to `<body>`;
 * that is the documented defect `tools/probe_keyboard_reach.mjs` and
 * `tools/probe_focus_survives_repaint.mjs` were written to catch, and both drive
 * `virtualList.ts` directly (`probe_keyboard_reach.mjs:30`, `probe_focus_survives_repaint.mjs:37`).
 * A window that only ever APPENDS never rebuilds a node that already exists, so it cannot
 * lose focus or scroll position that way at all — the failure mode is absent by construction
 * rather than defended against.
 */

/**
 * Turns per step.
 *
 * Above the measured p99 (8) and above the largest conversation in the uniform sample (107),
 * so the overwhelming majority of transcripts render complete on first paint and never show
 * the control at all. It still bounds the pathological case to ~200 turns / ~5,000 elements
 * per step, and reaches the end of the 2,562-turn worst case in 13 steps.
 */
export const READER_TURN_CHUNK = 200;

/** Which slice of the turns the next paint adds, and what is left after it. */
export interface TurnWindow {
  /** First turn index this step renders (inclusive). Equals what is already shown. */
  from: number;
  /** One past the last turn this step renders. */
  end: number;
  /** Turns still not in the DOM once this step has painted. */
  remaining: number;
}

/** Thousands-grouped, matching `rootsStatus` — a four-digit count is unreadable bare. */
function grouped(n: number): string {
  return n.toLocaleString("en-US");
}

/**
 * The next slice to append, given how many turns are already on screen.
 *
 * Returns `from` rather than assuming 0 because the reader APPENDS: re-rendering turns that
 * are already in the DOM would reintroduce the detach-the-focused-node repaint this design
 * exists to avoid.
 *
 * `chunk` is floored at 1 for the same reason `loadAllRoots` floors its page size: a
 * zero-width step renders nothing, never advances `shown`, and leaves a "show more" button
 * that does nothing forever. That is a hang, not an error — the worst way for this to fail,
 * and one no first-step test would catch.
 */
export function turnWindow(
  total: number,
  shown: number,
  chunk: number = READER_TURN_CHUNK,
): TurnWindow {
  const size = Math.max(1, Math.floor(chunk));
  const count = Math.max(0, total);
  const from = Math.min(Math.max(0, shown), count);
  const end = Math.min(count, from + size);
  return { from, end, remaining: count - end };
}

/**
 * The line that admits the transcript on screen is not all of it.
 *
 * Silent when everything is shown: the subtitle already reports the turn count, and a second
 * line repeating it reads as a warning about a transcript that is in fact complete — the
 * mirror of the mistake this function exists to prevent.
 */
export function windowStatus(shown: number, total: number): string {
  if (shown >= total) return "";
  return `showing ${grouped(shown)} of ${grouped(total)} turns`;
}

/**
 * What the reveal control says, or `null` when there is nothing left to reveal.
 *
 * The last step names the actual remainder. "Show 200 more" with 62 turns left promises 138
 * turns that do not exist, which is a small lie of exactly the kind this surface keeps
 * having to stop telling.
 */
export function moreButtonLabel(
  remaining: number,
  chunk: number = READER_TURN_CHUNK,
): string | null {
  if (remaining <= 0) return null;
  const size = Math.max(1, Math.floor(chunk));
  if (remaining <= size) return `Show the last ${grouped(remaining)}`;
  return `Show ${grouped(size)} more`;
}
