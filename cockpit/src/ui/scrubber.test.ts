import { describe, expect, it } from "vitest";

import type { Timeline } from "../ipc/types";
import {
  advancePlayhead,
  computeTicks,
  floorEventMs,
  fractionToMs,
  makeScale,
  msToFraction,
  undatedLabel,
} from "./scrubber";

/**
 * Build a {@link Timeline} the way the sidecar does: `events` sorted ascending +
 * distinct, `min_ms`/`max_ms` pinned to its ends (both null when empty).
 */
function timeline(events: number[], undated = 0): Timeline {
  return {
    events,
    min_ms: events.length > 0 ? events[0] : null,
    max_ms: events.length > 0 ? events[events.length - 1] : null,
    undated_count: undated,
  };
}

describe("msToFraction", () => {
  it("maps a timestamp to its 0..1 position across the span", () => {
    expect(msToFraction(50, 0, 100)).toBe(0.5);
    expect(msToFraction(0, 0, 100)).toBe(0);
    expect(msToFraction(100, 0, 100)).toBe(1);
  });

  it("maps a non-zero-based span", () => {
    expect(msToFraction(150, 100, 200)).toBe(0.5);
  });

  it("clamps out-of-range timestamps to [0, 1]", () => {
    expect(msToFraction(-40, 0, 100)).toBe(0);
    expect(msToFraction(250, 0, 100)).toBe(1);
  });

  it("returns 0 for a degenerate (zero or negative) span", () => {
    expect(msToFraction(50, 50, 50)).toBe(0); // single instant: min === max
    expect(msToFraction(999, 50, 50)).toBe(0); // any ms collapses to the start
    expect(msToFraction(50, 100, 0)).toBe(0); // max < min (negative span)
  });
});

describe("fractionToMs", () => {
  it("is the inverse of msToFraction across the span", () => {
    expect(fractionToMs(0.5, 0, 100)).toBe(50);
    expect(fractionToMs(0, 0, 100)).toBe(0);
    expect(fractionToMs(1, 0, 100)).toBe(100);
    expect(fractionToMs(0.5, 100, 200)).toBe(150);
  });

  it("clamps out-of-range fractions to [min, max]", () => {
    expect(fractionToMs(-2, 0, 100)).toBe(0);
    expect(fractionToMs(3, 0, 100)).toBe(100);
  });

  it("collapses to min for a degenerate span", () => {
    expect(fractionToMs(0.5, 50, 50)).toBe(50);
  });
});

describe("makeScale", () => {
  it("returns null for an empty timeline (no dated axis)", () => {
    expect(makeScale(timeline([]))).toBeNull();
  });

  it("pins min/max to the event ends and delegates the mapping", () => {
    const scale = makeScale(timeline([0, 50, 100]));
    expect(scale).not.toBeNull();
    expect(scale?.min).toBe(0);
    expect(scale?.max).toBe(100);
    expect(scale?.toFraction(50)).toBe(0.5);
    expect(scale?.toMs(0.5)).toBe(50);
  });

  it("collapses a single-event axis to a point", () => {
    const scale = makeScale(timeline([10]));
    expect(scale?.min).toBe(10);
    expect(scale?.max).toBe(10);
    expect(scale?.toFraction(10)).toBe(0);
    expect(scale?.toFraction(999)).toBe(0);
    expect(scale?.toMs(0.5)).toBe(10);
  });
});

describe("computeTicks", () => {
  it("returns no ticks for an empty timeline", () => {
    expect(computeTicks(timeline([]), 200)).toEqual([]);
  });

  it("emits one tick per birth event, positioned across the track width", () => {
    expect(computeTicks(timeline([0, 50, 100]), 200)).toEqual([
      { ms: 0, fraction: 0, x: 0 },
      { ms: 50, fraction: 0.5, x: 100 },
      { ms: 100, fraction: 1, x: 200 },
    ]);
  });

  it("positions ticks on a non-zero-based axis", () => {
    expect(computeTicks(timeline([100, 150, 200]), 100)).toEqual([
      { ms: 100, fraction: 0, x: 0 },
      { ms: 150, fraction: 0.5, x: 50 },
      { ms: 200, fraction: 1, x: 100 },
    ]);
  });

  it("places a lone tick at the start of the track", () => {
    expect(computeTicks(timeline([10]), 200)).toEqual([{ ms: 10, fraction: 0, x: 0 }]);
  });
});

describe("floorEventMs", () => {
  const events = [10, 20, 30];

  it("returns null when there are no events", () => {
    expect(floorEventMs(25, [])).toBeNull();
  });

  it("returns null when the time precedes the first event", () => {
    expect(floorEventMs(5, events)).toBeNull();
  });

  it("returns the greatest event at or before the time", () => {
    expect(floorEventMs(25, events)).toBe(20); // between ticks -> floor
    expect(floorEventMs(20, events)).toBe(20); // exactly on a tick
    expect(floorEventMs(100, events)).toBe(30); // past the last -> last
  });

  it("returns the first event exactly at its boundary", () => {
    expect(floorEventMs(10, events)).toBe(10);
  });
});

describe("advancePlayhead", () => {
  it("advances the playhead proportionally to wall time over the play duration", () => {
    expect(
      advancePlayhead({ currentMs: 0, min: 0, max: 1000, elapsedWallMs: 10, durationMs: 100 }),
    ).toEqual({ ms: 100, done: false });
  });

  it("clamps to max and reports done once it reaches the end", () => {
    expect(
      advancePlayhead({ currentMs: 950, min: 0, max: 1000, elapsedWallMs: 10, durationMs: 100 }),
    ).toEqual({ ms: 1000, done: true });
  });

  it("reports done at the exact boundary (>= max)", () => {
    expect(
      advancePlayhead({ currentMs: 900, min: 0, max: 1000, elapsedWallMs: 10, durationMs: 100 }),
    ).toEqual({ ms: 1000, done: true });
  });

  it("jumps straight to the end when the duration is non-positive", () => {
    expect(
      advancePlayhead({ currentMs: 0, min: 0, max: 1000, elapsedWallMs: 10, durationMs: 0 }),
    ).toEqual({ ms: 1000, done: true });
  });

  it("finishes immediately on a zero-span axis", () => {
    expect(
      advancePlayhead({ currentMs: 5, min: 5, max: 5, elapsedWallMs: 10, durationMs: 100 }),
    ).toEqual({ ms: 5, done: true });
  });
});

describe("undatedLabel", () => {
  it("is empty when nothing is undated", () => {
    expect(undatedLabel(0)).toBe("");
  });

  it("uses the singular for exactly one", () => {
    expect(undatedLabel(1)).toBe("1 undated thread shown at all times");
  });

  it("uses the plural for more than one", () => {
    expect(undatedLabel(3)).toBe("3 undated threads shown at all times");
  });
});
