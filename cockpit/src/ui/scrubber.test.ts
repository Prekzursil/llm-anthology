// @vitest-environment happy-dom
/**
 * The pure tick/scale mapping AND the DOM shell that drives it.
 *
 * WHY THE DOCBLOCK. The suite default is `environment: "node"` and stays that way — a
 * global DOM flip breaks four unrelated test files (measurements in `vitest.config.ts`).
 * This file opts in per-file so `TimeScrubber` can be built and driven.
 *
 * WHAT IS STUBBED AND WHY. `requestAnimationFrame` and `performance.now` are replaced with
 * a hand-driven clock. Not for speed: happy-dom's rAF is timer-backed, so real frames
 * arrive at wall-clock times the test cannot name, and every playback assertion here is
 * about the exact ms the playhead lands on. The axis numbers are chosen so that
 * `span / PLAY_DURATION_MS` is exactly 1 ms of axis per ms of wall clock — otherwise the
 * playhead lands on 1999.9999999999998, floors to the PREVIOUS tick, and the test asserts
 * a de-duplication that is really a float artefact.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Timeline } from "../ipc/types";
import {
  advancePlayhead,
  computeTicks,
  floorEventMs,
  fractionToMs,
  makeScale,
  msToFraction,
  type ScrubberIpc,
  TimeScrubber,
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

// -- the DOM shell -------------------------------------------------------------------

/**
 * A hand-driven frame clock.
 *
 * `all` keeps every callback ever requested, including cancelled ones, so a test can
 * model the one race the component's `!this.playing` guard exists for: a frame the
 * browser has ALREADY dispatched cannot be un-dispatched by `cancelAnimationFrame`.
 */
class FrameClock {
  readonly all: FrameRequestCallback[] = [];
  private readonly queue = new Map<number, FrameRequestCallback>();
  private nextId = 1; // never 0 — the component treats 0 as "no frame pending"

  request = (callback: FrameRequestCallback): number => {
    const id = this.nextId++;
    this.queue.set(id, callback);
    this.all.push(callback);
    return id;
  };

  cancel = (id: number): void => {
    this.queue.delete(id);
  };

  get pending(): number {
    return this.queue.size;
  }

  /** Deliver one frame at wall-clock `nowMs` to everything currently queued. */
  tick(nowMs: number): void {
    const due = [...this.queue.values()];
    this.queue.clear();
    for (const callback of due) callback(nowMs);
  }
}

/**
 * An axis whose span is exactly PLAY_DURATION_MS (6000), so playback advances 1 ms of
 * axis per ms of wall clock and every expected playhead value is an exact integer.
 */
const AXIS = [1000, 2000, 4000, 7000];

interface Harness {
  scrubber: TimeScrubber;
  scrubs: number[];
  play: HTMLButtonElement;
  range: HTMLInputElement;
  ticks: HTMLElement;
  undated: HTMLElement;
  container: HTMLElement;
}

let clock: FrameClock;

function build(ipc: ScrubberIpc): Harness {
  const container = document.createElement("div");
  document.body.append(container);
  const scrubs: number[] = [];
  const scrubber = new TimeScrubber(container, ipc, (ms) => scrubs.push(ms));

  const pick = <T extends HTMLElement>(selector: string): T => {
    const found = container.querySelector<T>(selector);
    if (found === null) throw new Error(`the scrubber built no ${selector}`);
    return found;
  };

  return {
    scrubber,
    scrubs,
    container,
    play: pick<HTMLButtonElement>(".scrubber-play"),
    range: pick<HTMLInputElement>(".scrubber-range"),
    ticks: pick(".scrubber-ticks"),
    undated: pick(".scrubber-undated"),
  };
}

/** An IPC double serving `events` once per call. */
function ipcFor(events: number[], undated = 0): ScrubberIpc {
  return { graphTimeline: async () => timeline(events, undated) };
}

/** Move the handle the way a user drag does: set the value, then fire `input`. */
function drag(harness: Harness, ms: number): void {
  harness.range.value = String(ms);
  harness.range.dispatchEvent(new Event("input"));
}

beforeEach(() => {
  clock = new FrameClock();
  vi.stubGlobal("requestAnimationFrame", clock.request);
  vi.stubGlobal("cancelAnimationFrame", clock.cancel);
  vi.spyOn(performance, "now").mockReturnValue(0);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

describe("TimeScrubber construction", () => {
  it("builds the control tree and starts inert", () => {
    const { container, play, range, ticks, undated } = build(ipcFor(AXIS));

    expect(container.querySelector(".scrubber")).not.toBeNull();
    expect(container.querySelector(".scrubber-track")).not.toBeNull();
    expect(play.type).toBe("button");
    expect(play.textContent).toBe("Play");
    expect(range.type).toBe("range");
    expect(range.getAttribute("aria-label")).toBe("Time scrubber");
    // The ticks are decoration for a control that already announces its value.
    expect(ticks.getAttribute("aria-hidden")).toBe("true");
    expect(undated.textContent).toBe("");
    // Inert until `load()` resolves, so a click during boot cannot play a missing axis.
    expect(play.disabled).toBe(true);
    expect(range.disabled).toBe(true);
  });
});

describe("TimeScrubber load", () => {
  it("configures the range from the axis and parks the handle at now", async () => {
    const harness = build(ipcFor(AXIS));

    await harness.scrubber.load();

    expect(harness.range.min).toBe("1000");
    expect(harness.range.max).toBe("7000");
    expect(harness.range.step).toBe("1");
    expect(harness.range.value).toBe("7000"); // the full graph, not an empty one
    expect(harness.range.disabled).toBe(false);
    expect(harness.play.disabled).toBe(false);
  });

  it("renders one tick per birth event, positioned as a percentage of the track", async () => {
    const harness = build(ipcFor(AXIS));

    await harness.scrubber.load();

    const lefts = [...harness.ticks.children].map((tick) => (tick as HTMLElement).style.left);
    // 1000 / 2000 / 4000 / 7000 over a 6000 span.
    expect(lefts).toEqual(["0%", `${(1000 / 6000) * 100}%`, "50%", "100%"]);
  });

  it("discloses the undated threads that sit outside the axis", async () => {
    const harness = build(ipcFor(AXIS, 2));

    await harness.scrubber.load();

    expect(harness.undated.textContent).toBe("2 undated threads shown at all times");
  });

  it("reports an unavailable axis when the IPC cannot supply one", async () => {
    const harness = build({}); // no graphTimeline at all

    await harness.scrubber.load();

    expect(harness.undated.textContent).toBe("time axis unavailable");
    expect(harness.range.disabled).toBe(true);
    expect(harness.play.disabled).toBe(true);
  });

  it("keeps the undated disclosure when there is nothing dated to scrub", async () => {
    // `disable(null)` deliberately leaves the text alone: the undated count is still the
    // truth about this graph, and overwriting it with an error would be a lie.
    const harness = build(ipcFor([], 3));

    await harness.scrubber.load();

    expect(harness.undated.textContent).toBe("3 undated threads shown at all times");
    expect(harness.range.disabled).toBe(true);
    expect(harness.play.disabled).toBe(true);
  });

  it("surfaces a genuine timeline fault with its label intact", async () => {
    const harness = build({
      graphTimeline: async () => {
        throw new Error("index corrupt");
      },
    });

    await harness.scrubber.load();

    expect(harness.undated.textContent).toBe("timeline failed: Error: index corrupt");
  });

  it("stays silent when the engine merely has no corpus attached yet", async () => {
    // Not-attached is the app's initial state, not a timeline fault, and the corpus bar
    // already says so. Repeating it here made the top bar state it three times over.
    const harness = build({
      graphTimeline: async () => {
        throw new Error("no corpus attached: call open_corpus first");
      },
    });

    await harness.scrubber.load();

    expect(harness.undated.textContent).toBe("");
    expect(harness.range.disabled).toBe(true);
  });
});

describe("TimeScrubber scrubbing", () => {
  it("emits the event-FLOORED as-of, not the raw handle position", async () => {
    const harness = build(ipcFor(AXIS));
    await harness.scrubber.load();

    drag(harness, 3000); // between the 2000 and 4000 births

    expect(harness.scrubs).toEqual([2000]);
  });

  it("de-dupes two positions that floor to the same birth", async () => {
    // The consumer re-queries `graph.at` on every emission, so a drag across the gap
    // between two ticks must not fire a request per pixel.
    const harness = build(ipcFor(AXIS));
    await harness.scrubber.load();

    drag(harness, 3000);
    drag(harness, 3500);
    drag(harness, 2000);

    expect(harness.scrubs).toEqual([2000]);
  });

  it("emits again once the handle crosses into the next birth", async () => {
    const harness = build(ipcFor(AXIS));
    await harness.scrubber.load();

    drag(harness, 3000);
    drag(harness, 5000);

    expect(harness.scrubs).toEqual([2000, 4000]);
  });

  it("emits the raw time when no birth precedes it", async () => {
    // The `?? ms` fallback in `emit`. With no dated axis there is nothing to floor to,
    // and the contract is to pass the position through rather than emit null or NaN.
    //
    // HONEST SCOPE: a real browser delivers no `input` event to a DISABLED range, and an
    // empty axis always leaves it disabled — so this pins the fallback's BEHAVIOUR, not a
    // flow a user can reach today. It stops mattering the moment any caller emits below
    // the first birth.
    const harness = build(ipcFor([], 0));
    await harness.scrubber.load();

    drag(harness, 42);

    expect(harness.scrubs).toEqual([42]);
  });
});

describe("TimeScrubber playback", () => {
  it("sweeps the axis from the earliest birth to the latest, emitting each crossed tick", async () => {
    const harness = build(ipcFor(AXIS));
    await harness.scrubber.load();

    harness.play.click();
    expect(harness.play.textContent).toBe("Pause");
    expect(harness.range.value).toBe("1000"); // rewound to the beginning of the graph
    expect(harness.scrubs).toEqual([1000]);

    clock.tick(1500); // playhead 2500 -> floors to 2000
    clock.tick(3000); // playhead 4000 -> exactly on a birth
    clock.tick(4000); // playhead 5000 -> still floors to 4000, so no emission
    expect(harness.scrubs).toEqual([1000, 2000, 4000]);

    clock.tick(6000); // playhead reaches max

    expect(harness.scrubs).toEqual([1000, 2000, 4000, 7000]);
    expect(harness.range.value).toBe("7000");
    // Arrival stops the sweep and releases the frame loop.
    expect(harness.play.textContent).toBe("Play");
    expect(clock.pending).toBe(0);
  });

  it("pauses on a second click", async () => {
    const harness = build(ipcFor(AXIS));
    await harness.scrubber.load();

    harness.play.click();
    clock.tick(1500);
    harness.play.click();

    expect(harness.play.textContent).toBe("Play");
    expect(clock.pending).toBe(0);

    clock.tick(3000); // nothing is queued, so nothing advances
    expect(harness.scrubs).toEqual([1000, 2000]);
  });

  it("lets a manual scrub cancel playback", async () => {
    const harness = build(ipcFor(AXIS));
    await harness.scrubber.load();

    harness.play.click();
    drag(harness, 5000);

    expect(harness.play.textContent).toBe("Play");
    expect(clock.pending).toBe(0);
    expect(harness.scrubs).toEqual([1000, 4000]);
  });

  it("ignores an in-flight frame that lands after playback stopped", async () => {
    // `cancelAnimationFrame` cannot recall a frame the browser has already dispatched, so
    // `frame()` must tolerate arriving after `stop()`.
    const harness = build(ipcFor(AXIS));
    await harness.scrubber.load();
    harness.play.click();
    const inFlight = clock.all[0];

    harness.play.click(); // pause: cancels the queued id, but `inFlight` is already out
    inFlight(1500);

    expect(harness.scrubs).toEqual([1000]);
    expect(clock.pending).toBe(0);
  });

  it("stops cleanly when the axis disappears mid-sweep", async () => {
    // A second `load()` that comes back empty nulls the scale WITHOUT stopping playback,
    // so the next frame finds itself playing an axis that no longer exists.
    let call = 0;
    const harness = build({
      graphTimeline: async () => (call++ === 0 ? timeline(AXIS) : timeline([], 0)),
    });
    await harness.scrubber.load();
    harness.play.click();
    expect(clock.pending).toBe(1);

    await harness.scrubber.load(); // the axis is gone now
    clock.tick(1500);

    expect(harness.scrubs).toEqual([1000]); // the frame bailed out
    expect(clock.pending).toBe(0); // and did not queue another
  });

  it("refuses to play when there is no axis", async () => {
    // `dispatchEvent`, NOT `.click()`, and the difference is the whole test: happy-dom
    // (correctly) drops `.click()` on a disabled control, so the click-based version of
    // this test passed while never reaching the guard at all — it asserted that nothing
    // happened because nothing was ever delivered. Coverage is what exposed that.
    //
    // HONEST SCOPE: the play button IS disabled whenever the scale is null, so a real
    // browser delivers no click here either. This pins the guard against the enabled-state
    // and the scale drifting apart, not against a click a user can currently land.
    const harness = build(ipcFor([], 0));
    await harness.scrubber.load();
    expect(harness.play.disabled).toBe(true);

    harness.play.dispatchEvent(new Event("click"));

    expect(harness.scrubs).toEqual([]);
    expect(harness.play.textContent).toBe("Play");
    expect(clock.pending).toBe(0);
  });
});

describe("TimeScrubber destroy", () => {
  it("stops playback, drops the input listener and removes its DOM", async () => {
    const harness = build(ipcFor(AXIS));
    await harness.scrubber.load();
    harness.play.click();

    harness.scrubber.destroy();

    expect(clock.pending).toBe(0);
    expect(harness.container.querySelector(".scrubber")).toBeNull();

    drag(harness, 4000); // the listener is gone
    expect(harness.scrubs).toEqual([1000]);
  });

  it("has no frame to cancel when it was never played", async () => {
    const harness = build(ipcFor(AXIS));
    await harness.scrubber.load();

    expect(() => harness.scrubber.destroy()).not.toThrow();
    expect(harness.container.querySelector(".scrubber")).toBeNull();
  });
});
