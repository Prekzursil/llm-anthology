/**
 * How much of a transcript the reader puts in the DOM at once.
 *
 * MEASURED against this machine's real corpus (13,099 Claude Code sessions, parsed with the
 * engine's own `claude_code.parse_transcript_file`), which is why these numbers are what they
 * are:
 *
 *   * uniform random sample, n=400 — median 2 turns, p95 3, p99 8, max 107. Nothing over 200.
 *   * the 25 largest files — worst case 2,562 turns / 22,319 blocks / 25.0M characters, and a
 *     second at 2,544 turns / 27.6M characters.
 *
 * `reader.ts` built ALL of it in one `replaceChildren`: ~66,000 elements and 27.6 MB of text
 * for that worst case. So the tail is rare (0.25% of conversations exceed 100 turns) and
 * catastrophic when it lands — which is exactly the shape that never shows up in testing and
 * hangs on a real session.
 *
 * The property that matters most here is that the walk TERMINATES and COVERS. A window that
 * fails to advance is an infinite "show more" that renders nothing new — strictly worse than
 * the slow render it replaces, and invisible to any test that only checks the first step.
 */
import { describe, expect, it } from "vitest";

import {
  moreButtonLabel,
  READER_TURN_CHUNK,
  turnWindow,
  windowStatus,
} from "./readerWindow";

describe("turnWindow", () => {
  it("renders a whole short transcript in one step, with nothing held back", () => {
    // The common case by a wide margin: the measured median is 2 turns.
    expect(turnWindow(2, 0)).toEqual({ from: 0, end: 2, remaining: 0 });
  });

  it("reports an empty transcript as complete rather than as a first step", () => {
    expect(turnWindow(0, 0)).toEqual({ from: 0, end: 0, remaining: 0 });
  });

  it("stops the FIRST step at the chunk and says how much is left", () => {
    // The measured worst case. 2,562 turns is ~66,000 elements if rendered at once.
    expect(turnWindow(2562, 0)).toEqual({
      from: 0,
      end: READER_TURN_CHUNK,
      remaining: 2562 - READER_TURN_CHUNK,
    });
  });

  it("resumes from what is already shown rather than restarting", () => {
    // Load-bearing: the reader APPENDS. A window that returned from:0 each time would
    // re-render turns that are already in the DOM, which is the `replaceChildren` repaint
    // that `tools/probe_keyboard_reach.mjs` exists to catch.
    expect(turnWindow(2562, 200)).toEqual({ from: 200, end: 400, remaining: 2162 });
  });

  it("TERMINATES and covers every turn exactly once, with no step over the chunk", async () => {
    // The whole contract in one property. Driven over the measured worst case plus the
    // awkward sizes around the chunk boundary.
    for (const total of [0, 1, 199, 200, 201, 400, 401, 2562]) {
      let shown = 0;
      let steps = 0;
      const covered: number[] = [];
      for (;;) {
        const w = turnWindow(total, shown);
        expect(w.from).toBe(shown);                       // no gap, no re-render
        expect(w.end - w.from).toBeLessThanOrEqual(READER_TURN_CHUNK);
        for (let i = w.from; i < w.end; i++) covered.push(i);
        shown = w.end;
        if (w.remaining === 0) break;
        expect(w.end).toBeGreaterThan(w.from);            // it MUST advance
        // A cap that cannot be hit is not a test. Ten chunks covers 2,562 at 200; this
        // fires long before a non-advancing window could spin the suite.
        expect(++steps).toBeLessThan(50);
      }
      expect(covered).toEqual(Array.from({ length: total }, (_, i) => i));
    }
  });

  it("never asks for a zero-width step, whatever chunk it is handed", () => {
    // `Math.min(0, …)` would render nothing, never advance `shown`, and leave a "show more"
    // button that does nothing forever — a hang rather than an error, which is the worst way
    // for this to fail. Same defect the roots walk had to guard in `graph/forest.ts`.
    expect(turnWindow(10, 0, 0).end).toBeGreaterThan(0);
    expect(turnWindow(10, 0, -5).end).toBeGreaterThan(0);
    expect(turnWindow(10, 0, 2.7)).toEqual({ from: 0, end: 2, remaining: 8 });
  });

  it("clamps a nonsense `shown` instead of producing a negative slice", () => {
    // Defensive: `shown` is DOM state, and a reopened reader that failed to reset it would
    // otherwise ask for turns[-4..] or claim negative remaining.
    expect(turnWindow(5, 9)).toEqual({ from: 5, end: 5, remaining: 0 });
    expect(turnWindow(5, -4)).toEqual({ from: 0, end: 5, remaining: 0 });
  });
});

describe("windowStatus", () => {
  it("says nothing when the whole transcript is on screen", () => {
    // The subtitle already reports the turn count; a second line saying the same thing
    // reads as a warning about a transcript that is in fact complete.
    expect(windowStatus(2, 2)).toBe("");
    expect(windowStatus(0, 0)).toBe("");
  });

  it("DISCLOSES a partial transcript, grouped so the numbers are readable", () => {
    // The rule this repo keeps having to relearn: the user is never shown a truncated thing
    // that looks complete (cf. `rootsStatus`, the 1,000-root sidebar, "1,432 hits" over 200).
    expect(windowStatus(200, 2562)).toBe("showing 200 of 2,562 turns");
  });
});

describe("moreButtonLabel", () => {
  it("is absent when there is nothing more to show", () => {
    expect(moreButtonLabel(0)).toBeNull();
  });

  it("offers a full chunk while one remains", () => {
    expect(moreButtonLabel(2362)).toBe("Show 200 more");
  });

  it("names the REMAINDER on the last step, so the button is not a lie", () => {
    // "Show 200 more" with 62 left promises 138 turns that do not exist.
    expect(moreButtonLabel(62)).toBe("Show the last 62");
  });

  it("groups a four-digit remainder", () => {
    expect(moreButtonLabel(1500, 4000)).toBe("Show the last 1,500");
  });
});
