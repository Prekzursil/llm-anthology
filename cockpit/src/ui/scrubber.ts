/**
 * The TIME SCRUBBER: a timeline slider over the spawn tree's creation-event axis
 * (`graph.timeline`). Dragging the handle emits an `as_of_ms`; the consumer feeds that
 * to `graph.at` and re-renders the graph as it stood at that instant. A play control
 * animates the graph's GROWTH from the earliest birth (`min_ms`) to the latest
 * (`max_ms`). Nodes with no timestamp (dangling edge endpoints) never enter the axis, so
 * a disclosure line reports the `undated_count` that is shown at every T.
 *
 * The tick/scale MAPPING is pure and exhaustively unit-tested (see `scrubber.test.ts`);
 * the DOM class below is a thin shell around it (built + driven in a browser, so — like
 * `graph/canvas.ts` — it carries no unit tests under the node-env vitest runner).
 */

import type { Timeline } from "../ipc/types";
import { engineErrorText } from "./errors";

// -- pure tick/scale mapping ---------------------------------------------------------

/** A linear axis mapping between epoch-ms timestamps and a 0..1 track fraction. */
export interface TimeScale {
  /** Earliest dated event (the axis origin). */
  readonly min: number;
  /** Latest dated event (the axis end). */
  readonly max: number;
  /** Position of a timestamp along the track, clamped to [0, 1]. */
  toFraction(ms: number): number;
  /** Timestamp at a track fraction, clamped to [min, max]. */
  toMs(fraction: number): number;
}

/** One tick on the slider track: a birth event, its axis fraction, and its px offset. */
export interface TickMark {
  ms: number;
  fraction: number;
  x: number;
}

/** Inputs to one playback frame: where we are, the axis bounds, and the wall-clock step. */
export interface PlayState {
  /** Current playhead position (epoch ms). */
  currentMs: number;
  /** Axis origin (earliest birth). */
  min: number;
  /** Axis end (latest birth). */
  max: number;
  /** Wall-clock time elapsed since the previous frame (ms). */
  elapsedWallMs: number;
  /** Wall-clock duration of a full min -> max sweep (ms). */
  durationMs: number;
}

/** Result of one playback frame: the new playhead, and whether the sweep has finished. */
export interface PlayStep {
  ms: number;
  done: boolean;
}

/** Clamp a value to the unit interval. */
function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value));
}

/**
 * Position of `ms` along the [min, max] axis, as a fraction in [0, 1]. A zero or
 * negative span (a single distinct event, or a degenerate range) collapses to 0 — the
 * whole axis is one instant, so every timestamp sits at the origin.
 */
export function msToFraction(ms: number, min: number, max: number): number {
  const span = max - min;
  if (span <= 0) return 0;
  return clamp01((ms - min) / span);
}

/**
 * The inverse of {@link msToFraction}: the timestamp at `fraction` of the axis. The
 * fraction is clamped to [0, 1] first, so the result is always within [min, max]; a
 * degenerate span returns `min`.
 */
export function fractionToMs(fraction: number, min: number, max: number): number {
  return min + clamp01(fraction) * (max - min);
}

/**
 * Build a {@link TimeScale} from a timeline, or `null` when there is no dated axis
 * (`events` empty). The bounds are read straight off the sorted event list — `events[0]`
 * and the last entry — which the sidecar keeps identical to `min_ms`/`max_ms`, so the
 * scale needs no separate null check on those fields.
 */
export function makeScale(timeline: Timeline): TimeScale | null {
  const { events } = timeline;
  if (events.length === 0) return null;
  const min = events[0];
  const max = events[events.length - 1];
  return {
    min,
    max,
    toFraction: (ms) => msToFraction(ms, min, max),
    toMs: (fraction) => fractionToMs(fraction, min, max),
  };
}

/**
 * One {@link TickMark} per birth event, in event (ascending) order, positioned across a
 * track `trackWidth` px wide. Empty when the timeline has no dated axis.
 */
export function computeTicks(timeline: Timeline, trackWidth: number): TickMark[] {
  const scale = makeScale(timeline);
  if (scale === null) return [];
  return timeline.events.map((ms) => {
    const fraction = scale.toFraction(ms);
    return { ms, fraction, x: fraction * trackWidth };
  });
}

/**
 * The greatest event at or before `ms` — the "effective as-of" that dedupes redundant
 * `graph.at` calls, since the snapshot only changes when the playhead crosses a tick.
 * `null` when `ms` precedes every event (or there are none). `events` MUST be sorted
 * ascending (the sidecar's contract), which lets the scan stop at the first miss.
 */
export function floorEventMs(ms: number, events: number[]): number | null {
  let result: number | null = null;
  for (const event of events) {
    if (event <= ms) result = event;
    else break;
  }
  return result;
}

/**
 * Advance the playhead one frame: move it toward `max` in proportion to the wall time
 * elapsed over the full sweep `durationMs`, clamping to `max` and flagging `done` on
 * arrival. A non-positive `durationMs` degenerates to an instant jump to the end.
 */
export function advancePlayhead(state: PlayState): PlayStep {
  const { currentMs, min, max, elapsedWallMs, durationMs } = state;
  const span = max - min;
  const msPerWallMs = durationMs > 0 ? span / durationMs : span;
  const next = currentMs + msPerWallMs * elapsedWallMs;
  if (next >= max) return { ms: max, done: true };
  return { ms: next, done: false };
}

/**
 * Disclosure for the undated (dangling, row-less) nodes that float outside the axis and
 * are therefore shown at every scrub position. Empty string when none are undated.
 */
export function undatedLabel(count: number): string {
  if (count <= 0) return "";
  const noun = count === 1 ? "thread" : "threads";
  return `${count} undated ${noun} shown at all times`;
}

// -- thin DOM shell ------------------------------------------------------------------

/** Wall-clock duration of a full play sweep (min -> max), in ms. */
const PLAY_DURATION_MS = 6000;

/** The slice of the IPC surface the scrubber needs: just the timeline axis. */
export interface ScrubberIpc {
  graphTimeline?(): Promise<Timeline>;
}

/** Notified with the effective (event-floored) `as_of_ms` whenever the playhead moves. */
export type ScrubHandler = (asOfMs: number) => void;

/**
 * The timeline-slider component. Builds its own controls into `container`, loads the
 * axis via {@link ScrubberIpc.graphTimeline}, and reports each scrub/play position to
 * `onScrub`. Emissions are floored to birth events and de-duplicated, so the consumer
 * only re-queries `graph.at` when the visible graph actually changes.
 */
export class TimeScrubber {
  private readonly root: HTMLElement;
  private readonly range: HTMLInputElement;
  private readonly ticksEl: HTMLElement;
  private readonly playBtn: HTMLButtonElement;
  private readonly undatedEl: HTMLElement;
  private readonly onInputBound: () => void;

  private events: number[] = [];
  private scale: TimeScale | null = null;
  private lastEmitted: number | null = null;

  private playing = false;
  private rafId = 0;
  private lastFrameMs = 0;
  private playheadMs = 0;

  constructor(
    container: HTMLElement,
    private readonly ipc: ScrubberIpc,
    private readonly onScrub: ScrubHandler,
  ) {
    this.root = document.createElement("div");
    this.root.className = "scrubber";

    this.playBtn = document.createElement("button");
    this.playBtn.type = "button";
    this.playBtn.className = "scrubber-play";
    this.playBtn.textContent = "Play";
    this.playBtn.disabled = true;
    this.playBtn.addEventListener("click", () => this.togglePlay());

    const track = document.createElement("div");
    track.className = "scrubber-track";

    this.ticksEl = document.createElement("div");
    this.ticksEl.className = "scrubber-ticks";
    this.ticksEl.setAttribute("aria-hidden", "true");

    this.range = document.createElement("input");
    this.range.type = "range";
    this.range.className = "scrubber-range";
    this.range.disabled = true;
    this.range.setAttribute("aria-label", "Time scrubber");
    this.onInputBound = () => this.onInput();
    this.range.addEventListener("input", this.onInputBound);

    track.append(this.ticksEl, this.range);

    this.undatedEl = document.createElement("span");
    this.undatedEl.className = "scrubber-undated muted";

    this.root.append(this.playBtn, track, this.undatedEl);
    container.append(this.root);
  }

  /** Load the axis and configure the controls. Idempotent-safe to call once on boot. */
  async load(): Promise<void> {
    if (this.ipc.graphTimeline === undefined) {
      this.disable("time axis unavailable");
      return;
    }
    try {
      this.applyTimeline(await this.ipc.graphTimeline());
    } catch (err) {
      // Not-attached is the initial state, not a timeline fault — do not surface the
      // engine's internal "call open_corpus first" text on the scrubber.
      this.disable(engineErrorText(err, "timeline failed"));
    }
  }

  private applyTimeline(timeline: Timeline): void {
    this.events = timeline.events;
    this.scale = makeScale(timeline);
    this.undatedEl.textContent = undatedLabel(timeline.undated_count);

    if (this.scale === null) {
      // No dated axis: keep the undated disclosure, but there is nothing to scrub.
      this.disable(null);
      return;
    }
    this.range.min = String(this.scale.min);
    this.range.max = String(this.scale.max);
    this.range.step = "1";
    this.range.value = String(this.scale.max); // default handle = "now" (full graph)
    this.range.disabled = false;
    this.playBtn.disabled = false;
    this.renderTicks(timeline);
  }

  private renderTicks(timeline: Timeline): void {
    const frag = document.createDocumentFragment();
    // Width 1 -> `x` is the fraction; positioned as a percentage so it stays responsive.
    for (const tick of computeTicks(timeline, 1)) {
      const mark = document.createElement("span");
      mark.className = "scrubber-tick";
      mark.style.position = "absolute";
      mark.style.left = `${tick.fraction * 100}%`;
      frag.append(mark);
    }
    this.ticksEl.replaceChildren(frag);
  }

  private onInput(): void {
    if (this.playing) this.stop(); // a manual scrub cancels playback
    this.emit(Number(this.range.value));
  }

  /** Floor to the nearest birth event, de-dupe, and notify the consumer. */
  private emit(ms: number): void {
    const effective = floorEventMs(ms, this.events) ?? ms;
    if (effective === this.lastEmitted) return;
    this.lastEmitted = effective;
    this.onScrub(effective);
  }

  private togglePlay(): void {
    if (this.playing) {
      this.stop();
      return;
    }
    if (this.scale === null) return;
    this.playing = true;
    this.playBtn.textContent = "Pause";
    this.playheadMs = this.scale.min;
    this.lastFrameMs = performance.now();
    this.range.value = String(this.scale.min);
    this.emit(this.scale.min);
    this.rafId = requestAnimationFrame((t) => this.frame(t));
  }

  private frame(nowMs: number): void {
    if (!this.playing || this.scale === null) return;
    const step = advancePlayhead({
      currentMs: this.playheadMs,
      min: this.scale.min,
      max: this.scale.max,
      elapsedWallMs: nowMs - this.lastFrameMs,
      durationMs: PLAY_DURATION_MS,
    });
    this.lastFrameMs = nowMs;
    this.playheadMs = step.ms;
    this.range.value = String(step.ms);
    this.emit(step.ms);
    if (step.done) {
      this.stop();
      return;
    }
    this.rafId = requestAnimationFrame((t) => this.frame(t));
  }

  private stop(): void {
    this.playing = false;
    this.playBtn.textContent = "Play";
    if (this.rafId !== 0) {
      cancelAnimationFrame(this.rafId);
      this.rafId = 0;
    }
  }

  private disable(message: string | null): void {
    this.range.disabled = true;
    this.playBtn.disabled = true;
    if (message !== null) this.undatedEl.textContent = message;
  }

  /** Tear down: stop playback, drop listeners, remove the DOM. */
  destroy(): void {
    this.stop();
    this.range.removeEventListener("input", this.onInputBound);
    this.root.remove();
  }
}
