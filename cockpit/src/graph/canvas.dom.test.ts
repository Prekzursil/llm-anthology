// @vitest-environment happy-dom
/**
 * {@link SpawnTreeCanvas} — the canvas-2D spawn-tree renderer, through real DOM events and a
 * RECORDING 2D context.
 *
 * WHY A RECORDING CONTEXT, NOT happy-dom'S OWN. happy-dom's `getContext("2d")` is a
 * no-op stub with no drawing API, so the renderer would be unverifiable. The context below
 * is a Proxy that logs every method call and property write; assertions read the log, so a
 * colour/line-width/dash decision in `drawNode`/`drawEdge` fails a test when it changes.
 * `measureText` answers width = 6px/char, which makes `clipText`'s ellipsis binary search
 * deterministic.
 *
 * GEOMETRY IS INJECTED, never measured — happy-dom does no layout (the vitest.config.ts
 * trap note), so `getBoundingClientRect` is defined per element to the box each case needs.
 * A `StubResizeObserver` (the pattern `ui/virtualList.test.ts` uses) replaces the native
 * one: happy-dom's never fires, and the canvas's resize path is worth driving.
 *
 * WHAT IS WORTH ASSERTING: the fit-to-view transform arithmetic, the zoom/pan interactions
 * (clamped, cursor-anchored, click-vs-drag disambiguated), the hit-test contract
 * (topmost node wins; empty space reports null), and the colour precedence rules
 * (base tint < failed edge < diff overlay; selection ring beats everything).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RollupTable } from "../ipc/types";
import { DIFF_STYLES, edgeKey, type DiffOverlay } from "./diffOverlay";
import { CROSS_EDGE_COLOR, EDGE_COLOR, providerTint } from "./palette";
import type { LayoutEdge, LayoutNode, PositionedGraph } from "./layout";
import { SpawnTreeCanvas } from "./canvas";

// ---------------------------------------------------------------------------
// recording context
// ---------------------------------------------------------------------------

interface RecordingCtx {
  ctx: CanvasRenderingContext2D;
  calls: string[];
}

function makeCtx(): RecordingCtx {
  const calls: string[] = [];
  const store: Record<string, unknown> = {
    // Deterministic metrics: every glyph is 6px wide, so text width == 6 × length.
    measureText: (t: string) => ({ width: t.length * 6 }),
  };
  const ctx = new Proxy(store, {
    get(target: Record<string, unknown>, prop: string | symbol) {
      if (typeof prop === "symbol") return Reflect.get(target, prop);
      if (prop in target) return target[prop];
      const fn = (...args: unknown[]) => {
        calls.push(`${prop}(${args.map((a) => String(a)).join(", ")})`);
      };
      target[prop] = fn;
      return fn;
    },
    set(target: Record<string, unknown>, prop: string | symbol, value: unknown) {
      Reflect.set(target, prop, value);
      if (typeof prop !== "symbol") calls.push(`SET ${prop} = ${String(value)}`);
      return true;
    },
  }) as unknown as CanvasRenderingContext2D;
  return { ctx, calls };
}

// ---------------------------------------------------------------------------
// resize observer the test can fire (mirrors ui/virtualList.test.ts)
// ---------------------------------------------------------------------------

class StubResizeObserver {
  static instances: StubResizeObserver[] = [];
  readonly observed: Element[] = [];
  disconnected = false;
  constructor(private readonly callback: ResizeObserverCallback) {
    StubResizeObserver.instances.push(this);
  }
  observe(target: Element): void {
    this.observed.push(target);
  }
  unobserve(): void {
    /* canvas only ever disconnects */
  }
  disconnect(): void {
    this.disconnected = true;
  }
  fire(): void {
    this.callback([], this as unknown as ResizeObserver);
  }

  static get latest(): StubResizeObserver {
    const { instances } = StubResizeObserver;
    const found = instances[instances.length - 1];
    if (found === undefined) throw new Error("no ResizeObserver was constructed");
    return found;
  }
}

// ---------------------------------------------------------------------------
// fixtures
// ---------------------------------------------------------------------------

function node(
  id: string,
  x: number,
  y: number,
  over: Partial<LayoutNode["node"]> = {},
): LayoutNode {
  return {
    id,
    x,
    y,
    width: 100,
    height: 40,
    node: {
      id,
      title: id,
      provider: "codex",
      model_provider: "",
      created_at_ms: 0,
      preview: "",
      child_count: 0,
      depth: 0,
      ...over,
    },
  };
}

function edge(over: Partial<LayoutEdge> = {}): LayoutEdge {
  return {
    parent: "p",
    child: "c",
    cross: false,
    points: [
      { x: 0, y: 0 },
      { x: 50, y: 0 },
    ],
    ...over,
  };
}

interface Rig {
  canvas: HTMLCanvasElement;
  recorder: RecordingCtx;
  view: SpawnTreeCanvas;
}

/** A canvas of 800×600 CSS px at (0,0), with its 2d context and pointer capture stubbed. */
function makeCanvas(rectWidth = 800, rectHeight = 600): Rig {
  const canvas = document.createElement("canvas");
  const recorder = makeCtx();
  canvas.getContext = (() => recorder.ctx) as unknown as typeof canvas.getContext;
  Object.defineProperty(canvas, "getBoundingClientRect", {
    configurable: true,
    value: () => ({
      width: rectWidth,
      height: rectHeight,
      top: 0,
      left: 0,
      right: rectWidth,
      bottom: rectHeight,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    }),
  });
  canvas.setPointerCapture = vi.fn();
  canvas.hasPointerCapture = vi.fn(() => true);
  canvas.releasePointerCapture = vi.fn();
  document.body.append(canvas);
  const view = new SpawnTreeCanvas(canvas);
  return { canvas, recorder, view };
}

/** Let every scheduled rAF render run before asserting on the draw log. */
function flushFrame(): Promise<void> {
  return new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
}

function pointerEvent(type: string, x: number, y: number, pointerId = 1): PointerEvent {
  // MouseEvent-based construction for both arms: happy-dom's PointerEvent init accepted
  // bubbles/clientX but MEASURED to drop `pointerId` passed in the same init bag, and the
  // handlers only read clientX/clientY/pointerId — so a MouseEvent with the id patched on
  // is the honest event in either case.
  const e = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y });
  Object.defineProperty(e, "pointerId", { value: pointerId });
  return e as PointerEvent;
}

function wheelEvent(deltaY: number, x = 0, y = 0): WheelEvent {
  // Same reasoning as pointerEvent: a MouseEvent named "wheel" with deltaY patched on —
  // happy-dom's WheelEvent constructor was measured to drop clientX/clientY from its init.
  const e = new MouseEvent("wheel", { bubbles: true, cancelable: true, clientX: x, clientY: y });
  Object.defineProperty(e, "deltaY", { value: deltaY });
  return e as WheelEvent;
}

/** Writes recorded as `SET <prop> = <value>`. */
function sets(rig: Rig, prop: string): string[] {
  return rig.recorder.calls.filter((c) => c.startsWith(`SET ${prop} = `));
}

beforeEach(() => {
  StubResizeObserver.instances = [];
  Reflect.set(globalThis, "ResizeObserver", StubResizeObserver);
});

afterEach(() => {
  document.body.replaceChildren();
});

describe("SpawnTreeCanvas construction", () => {
  it("throws when the browser cannot produce a 2D context", () => {
    const canvas = document.createElement("canvas");
    canvas.getContext = () => null;
    document.body.append(canvas);
    expect(() => new SpawnTreeCanvas(canvas)).toThrow("2D canvas context unavailable");
  });

  it("sizes the backing store from the CSS box × dpr on construction", () => {
    const rig = makeCanvas();
    expect(rig.canvas.width).toBe(800);
    expect(rig.canvas.height).toBe(600);
    expect(StubResizeObserver.latest.observed).toEqual([rig.canvas]);
  });

  it("re-sizes and repaints when the observer fires", async () => {
    const rig = makeCanvas(400, 300);
    // Re-DEFINE, not assign: makeCanvas installed the first box as a read-only own property.
    Object.defineProperty(rig.canvas, "getBoundingClientRect", {
      configurable: true,
      value: () =>
        ({
          width: 900,
          height: 500,
          top: 0,
          left: 0,
          right: 900,
          bottom: 500,
          x: 0,
          y: 0,
          toJSON: () => ({}),
        }) as DOMRect,
    });
    StubResizeObserver.latest.fire();
    await flushFrame();
    expect(rig.canvas.width).toBe(900);
    expect(rig.recorder.calls.some((c) => c.startsWith("clearRect("))).toBe(true);
  });

  it("a zero box degenerates to a 1px backing store, not 0", () => {
    const rig = makeCanvas(0, 0);
    expect(rig.canvas.width).toBe(1);
    expect(rig.canvas.height).toBe(1);
  });
});

describe("fitToView", () => {
  it("centres and scales the graph with the given padding", async () => {
    const rig = makeCanvas();
    rig.view.setGraph({ nodes: [], edges: [], width: 400, height: 200 });
    await flushFrame();
    // scale = min((800-96)/400, (600-96)/200) = 1.76; offsets centre the box.
    expect(rig.recorder.calls).toContain("scale(1.76, 1.76)");
    expect(rig.recorder.calls).toContain("translate(48, 124)");
  });

  it("clamps a huge graph at MIN_SCALE instead of vanishing", async () => {
    const rig = makeCanvas();
    rig.view.setGraph({ nodes: [], edges: [], width: 100000, height: 100000 });
    await flushFrame();
    expect(rig.recorder.calls).toContain("scale(0.1, 0.1)");
  });

  it("clamps a tiny graph at MAX_SCALE instead of exploding", async () => {
    const rig = makeCanvas();
    rig.view.setGraph({ nodes: [], edges: [], width: 10, height: 5 });
    await flushFrame();
    expect(rig.recorder.calls).toContain("scale(4, 4)");
  });

  it("degenerates to scale 1 + padding when there is nothing to fit", async () => {
    const rig = makeCanvas();
    rig.view.setGraph({ nodes: [], edges: [], width: 0, height: 0 });
    await flushFrame();
    expect(rig.recorder.calls).toContain("scale(1, 1)");
    expect(rig.recorder.calls).toContain("translate(48, 48)");
  });

  it("fit=false keeps the current transform and still paints", async () => {
    const rig = makeCanvas();
    rig.view.setGraph({ nodes: [node("n1", 10, 20)], edges: [], width: 400, height: 200 }, false);
    await flushFrame();
    expect(rig.recorder.calls).toContain("translate(0, 0)");
    expect(rig.recorder.calls).toContain("scale(1, 1)");
  });
});

describe("node drawing", () => {
  it("fills with the provider tint and draws the title label", async () => {
    const rig = makeCanvas();
    const n = node("titled", 10, 20);
    rig.view.setGraph({ nodes: [n], edges: [], width: 0, height: 0 }, false);
    await flushFrame();
    expect(sets(rig, "fillStyle")).toContain(`SET fillStyle = ${providerTint("codex").fill}`);
    expect(rig.recorder.calls.some((c) => c.startsWith("fillText(titled,"))).toBe(true);
  });

  it("truncates a long title with an ellipsis to the node budget", async () => {
    const rig = makeCanvas();
    const long = "a title far too long for a one-hundred-pixel box";
    const n = node("x", 0, 0, { title: long });
    rig.view.setGraph({ nodes: [n], edges: [], width: 0, height: 0 }, false);
    await flushFrame();
    const labels = rig.recorder.calls.filter((c) => c.startsWith("fillText("));
    const drawn = labels[labels.length - 1] ?? "";
    expect(drawn).toContain("…");
    expect(drawn).not.toContain(long);
  });

  it("falls back to the id when the title is empty", async () => {
    const rig = makeCanvas();
    rig.view.setGraph(
      { nodes: [node("id-only", 0, 0, { title: "" })], edges: [], width: 0, height: 0 },
      false,
    );
    await flushFrame();
    expect(rig.recorder.calls.some((c) => c.startsWith("fillText(id-only,"))).toBe(true);
  });

  it("hides labels (and badges) once zoomed out past the label scale", async () => {
    const rig = makeCanvas();
    const rollup: RollupTable = {
      b: {
        self_tokens: 0,
        subtree_tokens: 0,
        self_count: 1,
        subtree_count: 9,
        max_depth: 2,
        child_count: 8,
      },
    };
    rig.view.setRollup(rollup);
    rig.view.setGraph({ nodes: [node("b", 0, 0)], edges: [], width: 0, height: 0 }, false);
    // Zoom far out: deltaY > 0 shrinks; huge value clamps at MIN_SCALE (0.1 < 0.45).
    rig.canvas.dispatchEvent(wheelEvent(6000));
    await flushFrame();
    expect(rig.recorder.calls).toContain("scale(0.1, 0.1)");
    expect(rig.recorder.calls.some((c) => c.startsWith("fillText("))).toBe(false);
  });

  it("draws a Σ badge only for subtrees larger than one member", async () => {
    const rig = makeCanvas();
    const rollup: RollupTable = {
      big: {
        self_tokens: 0,
        subtree_tokens: 0,
        self_count: 1,
        subtree_count: 3,
        max_depth: 1,
        child_count: 2,
      },
      leaf: {
        self_tokens: 0,
        subtree_tokens: 0,
        self_count: 1,
        subtree_count: 1,
        max_depth: 0,
        child_count: 0,
      },
    };
    rig.view.setRollup(rollup);
    rig.view.setGraph(
      { nodes: [node("big", 0, 0), node("leaf", 200, 0)], edges: [], width: 0, height: 0 },
      false,
    );
    await flushFrame();
    const badgeFills = rig.recorder.calls.filter((c) => c.startsWith("fillText(Σ"));
    expect(badgeFills).toHaveLength(1);
    expect(badgeFills[0]?.startsWith("fillText(Σ3,")).toBe(true);
    // The badge is right-aligned and slightly faded.
    expect(sets(rig, "textAlign")).toContain("SET textAlign = right");
    expect(sets(rig, "globalAlpha")).toContain("SET globalAlpha = 0.85");
  });

  it("clearing the rollup removes badges", async () => {
    const rig = makeCanvas();
    rig.view.setRollup({
      b: {
        self_tokens: 0,
        subtree_tokens: 0,
        self_count: 1,
        subtree_count: 3,
        max_depth: 1,
        child_count: 2,
      },
    });
    rig.view.setGraph({ nodes: [node("b", 0, 0)], edges: [], width: 0, height: 0 }, false);
    rig.view.setRollup(null);
    await flushFrame();
    expect(rig.recorder.calls.some((c) => c.startsWith("fillText(Σ"))).toBe(false);
  });

  it("draws a selection ring that beats every other border decision", async () => {
    const rig = makeCanvas();
    rig.view.setGraph({ nodes: [node("s", 0, 0)], edges: [], width: 0, height: 0 }, false);
    rig.view.select("s");
    await flushFrame();
    expect(sets(rig, "strokeStyle")).toContain("SET strokeStyle = #ffd54a");
    expect(sets(rig, "lineWidth")).toContain("SET lineWidth = 3");
  });

  it("select() with an unknown id selects nothing", async () => {
    const rig = makeCanvas();
    rig.view.setGraph({ nodes: [node("s", 0, 0)], edges: [], width: 0, height: 0 }, false);
    rig.view.select("missing");
    await flushFrame();
    expect(sets(rig, "strokeStyle")).not.toContain("SET strokeStyle = #ffd54a");
  });

  it("a selection dropped by a new graph stops painting the ring", async () => {
    const rig = makeCanvas();
    rig.view.setGraph({ nodes: [node("s", 0, 0)], edges: [], width: 0, height: 0 }, false);
    rig.view.select("s");
    rig.view.setGraph({ nodes: [node("other", 0, 0)], edges: [], width: 0, height: 0 }, false);
    await flushFrame();
    expect(sets(rig, "strokeStyle")).not.toContain("SET strokeStyle = #ffd54a");
  });
});

describe("edge drawing", () => {
  function oneEdge(view: SpawnTreeCanvas, e: LayoutEdge): void {
    view.setGraph({ nodes: [], edges: [e], width: 0, height: 0 }, false);
  }

  it("draws a normal edge solid, thin, in the base colour, with an arrowhead", async () => {
    const rig = makeCanvas();
    oneEdge(rig.view, edge());
    await flushFrame();
    expect(sets(rig, "strokeStyle")).toContain(`SET strokeStyle = ${EDGE_COLOR}`);
    expect(sets(rig, "lineWidth")).toContain("SET lineWidth = 1.25");
    expect(rig.recorder.calls).toContain("setLineDash()");
    expect(rig.recorder.calls.some((c) => c.startsWith("fill("))).toBe(true);
  });

  it("draws a cross-provider edge heavier and dashed", async () => {
    const rig = makeCanvas();
    oneEdge(rig.view, edge({ cross: true }));
    await flushFrame();
    expect(sets(rig, "strokeStyle")).toContain(`SET strokeStyle = ${CROSS_EDGE_COLOR}`);
    expect(sets(rig, "lineWidth")).toContain("SET lineWidth = 2");
    expect(rig.recorder.calls).toContain("setLineDash(6,4)");
  });

  it("colours a failed edge regardless of its cross-ness", async () => {
    const rig = makeCanvas();
    oneEdge(rig.view, edge({ status: "failed" }));
    await flushFrame();
    expect(sets(rig, "strokeStyle")).toContain("SET strokeStyle = #c0392b");
  });

  it("skips an edge with fewer than two points", async () => {
    const rig = makeCanvas();
    oneEdge(rig.view, edge({ points: [{ x: 0, y: 0 }] }));
    await flushFrame();
    expect(rig.recorder.calls.some((c) => c.startsWith("stroke("))).toBe(false);
  });

  it("tints an edge by its diff class; a removed edge is a dashed ghost", async () => {
    const rig = makeCanvas();
    const overlay: DiffOverlay = {
      nodeClass: {},
      edgeClass: { [edgeKey("p", "c")]: "removed" },
      tooltips: {},
    };
    rig.view.setDiffOverlay(overlay);
    oneEdge(rig.view, edge());
    await flushFrame();
    expect(sets(rig, "strokeStyle")).toContain(`SET strokeStyle = ${DIFF_STYLES.removed.color}`);
    expect(sets(rig, "globalAlpha")).toContain("SET globalAlpha = 0.4");
    expect(rig.recorder.calls).toContain("setLineDash(4,3)");
    rig.view.setDiffOverlay(null);
    await flushFrame();
    expect(sets(rig, "strokeStyle")).toContain(`SET strokeStyle = ${EDGE_COLOR}`);
  });

  it("tints a changed node border without ghosting it", async () => {
    const rig = makeCanvas();
    rig.view.setDiffOverlay({ nodeClass: { n: "changed" }, edgeClass: {}, tooltips: {} });
    rig.view.setGraph({ nodes: [node("n", 0, 0)], edges: [], width: 0, height: 0 }, false);
    await flushFrame();
    expect(sets(rig, "strokeStyle")).toContain(`SET strokeStyle = ${DIFF_STYLES.changed.color}`);
    expect(sets(rig, "lineWidth")).toContain("SET lineWidth = 2.5");
    expect(sets(rig, "globalAlpha")).not.toContain("SET globalAlpha = 0.4");
  });

  it("ghosts a removed node: faded fill + dashed border", async () => {
    const rig = makeCanvas();
    rig.view.setDiffOverlay({ nodeClass: { n: "removed" }, edgeClass: {}, tooltips: {} });
    rig.view.setGraph({ nodes: [node("n", 0, 0)], edges: [], width: 0, height: 0 }, false);
    await flushFrame();
    expect(sets(rig, "globalAlpha")).toContain("SET globalAlpha = 0.4");
    expect(rig.recorder.calls).toContain("setLineDash(5,3)");
  });
});

describe("wheel zoom", () => {
  it("zooms about the cursor and prevents the page scroll", async () => {
    const rig = makeCanvas();
    rig.view.setGraph({ nodes: [], edges: [], width: 0, height: 0 }, false);
    // deltaY such that factor = e^(-deltaY·0.0016) = 2  →  deltaY = −ln2/0.0016 ≈ −433.22.
    const e = wheelEvent(-Math.LN2 / 0.0016, 400, 300);
    rig.canvas.dispatchEvent(e);
    await flushFrame();
    expect(e.defaultPrevented).toBe(true);
    expect(rig.recorder.calls).toContain("scale(2, 2)");
    // offsetX = 400 − (400 − 0)·2 = −400; offsetY = 300 − (300 − 0)·2 = −300.
    expect(rig.recorder.calls).toContain("translate(-400, -300)");
  });

  it("clamps at MAX_SCALE and MIN_SCALE", async () => {
    const rig = makeCanvas();
    rig.view.setGraph({ nodes: [], edges: [], width: 0, height: 0 }, false);
    rig.canvas.dispatchEvent(wheelEvent(-6000));
    await flushFrame();
    expect(rig.recorder.calls).toContain("scale(4, 4)");
    rig.canvas.dispatchEvent(wheelEvent(6000));
    await flushFrame();
    expect(rig.recorder.calls).toContain("scale(0.1, 0.1)");
  });
});

describe("pointer interaction", () => {
  function graphWith(...nodes: LayoutNode[]): PositionedGraph {
    return { nodes, edges: [], width: 0, height: 0 };
  }

  it("pans on drag (capturing the pointer) and does NOT select on release", async () => {
    const rig = makeCanvas();
    const handler = vi.fn();
    rig.view.setSelectHandler(handler);
    rig.view.setGraph(graphWith(node("n1", 10, 20)), false);
    rig.canvas.dispatchEvent(pointerEvent("pointerdown", 100, 100, 7));
    expect(rig.canvas.setPointerCapture).toHaveBeenCalledWith(7);
    rig.canvas.dispatchEvent(pointerEvent("pointermove", 160, 150, 7));
    rig.canvas.dispatchEvent(pointerEvent("pointerup", 160, 150, 7));
    expect(rig.canvas.releasePointerCapture).toHaveBeenCalledWith(7);
    await flushFrame();
    expect(rig.recorder.calls).toContain("translate(60, 50)");
    expect(handler).not.toHaveBeenCalled();
  });

  it("clicks a node: movement under the slop selects and reports it", async () => {
    const rig = makeCanvas();
    const handler = vi.fn();
    rig.view.setSelectHandler(handler);
    rig.view.setGraph(graphWith(node("n1", 10, 20), node("n2", 10, 20)), false);
    rig.canvas.dispatchEvent(pointerEvent("pointerdown", 50, 40, 2));
    rig.canvas.dispatchEvent(pointerEvent("pointermove", 52, 41, 2)); // 3px ≤ 4 slop
    rig.canvas.dispatchEvent(pointerEvent("pointerup", 52, 41, 2));
    await flushFrame();
    expect(handler).toHaveBeenCalledTimes(1);
    const [id, hit] = handler.mock.calls[0] as [string | null, LayoutNode | null];
    expect(id).toBe("n2"); // last drawn wins
    expect(hit?.id).toBe("n2");
    expect(sets(rig, "strokeStyle")).toContain("SET strokeStyle = #ffd54a");
  });

  it("clicks empty space: reports null and paints no ring", async () => {
    const rig = makeCanvas();
    const handler = vi.fn();
    rig.view.setSelectHandler(handler);
    rig.view.setGraph(graphWith(node("n1", 10, 20)), false);
    rig.canvas.dispatchEvent(pointerEvent("pointerdown", 700, 500, 3));
    rig.canvas.dispatchEvent(pointerEvent("pointerup", 700, 500, 3));
    await flushFrame();
    expect(handler).toHaveBeenCalledWith(null, null);
    expect(sets(rig, "strokeStyle")).not.toContain("SET strokeStyle = #ffd54a");
  });

  it("ignores pointermove/up when no drag is in progress", async () => {
    const rig = makeCanvas();
    const handler = vi.fn();
    rig.view.setSelectHandler(handler);
    rig.view.setGraph(graphWith(node("n1", 10, 20)), false);
    rig.canvas.dispatchEvent(pointerEvent("pointermove", 500, 500, 4));
    rig.canvas.dispatchEvent(pointerEvent("pointerup", 50, 40, 4));
    await flushFrame();
    expect(handler).not.toHaveBeenCalled();
    expect(rig.recorder.calls).toContain("translate(0, 0)");
  });

  it("pointerleave ends the drag: later moves do nothing", async () => {
    const rig = makeCanvas();
    rig.view.setGraph(graphWith(node("n1", 10, 20)), false);
    rig.canvas.dispatchEvent(pointerEvent("pointerdown", 100, 100, 5));
    rig.canvas.dispatchEvent(pointerEvent("pointerleave", 100, 100, 5));
    rig.canvas.dispatchEvent(pointerEvent("pointermove", 300, 300, 5));
    await flushFrame();
    expect(rig.recorder.calls).toContain("translate(0, 0)");
  });

  it("hit-testing works through a panned and zoomed transform", async () => {
    const rig = makeCanvas();
    const handler = vi.fn();
    rig.view.setSelectHandler(handler);
    // Node occupies world (100..200, 100..140). Zoom 2× about (0,0) then pan +50/+30:
    // world point w maps to screen 2w + (50,30). Node centre (150,120) → (350,270).
    rig.view.setGraph(graphWith(node("z", 100, 100)), false);
    rig.canvas.dispatchEvent(wheelEvent(-Math.LN2 / 0.0016, 0, 0)); // scale → 2, offsets stay (0,0)
    rig.canvas.dispatchEvent(pointerEvent("pointerdown", 350, 270, 6));
    rig.canvas.dispatchEvent(pointerEvent("pointerup", 350, 270, 6));
    await flushFrame();
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler.mock.calls[0]?.[0]).toBe("z");
  });

  it("setSelectHandler(null) silences reports", async () => {
    const rig = makeCanvas();
    const handler = vi.fn();
    rig.view.setSelectHandler(handler);
    rig.view.setSelectHandler(null);
    rig.view.setGraph(graphWith(node("n1", 10, 20)), false);
    rig.canvas.dispatchEvent(pointerEvent("pointerdown", 50, 40, 8));
    rig.canvas.dispatchEvent(pointerEvent("pointerup", 50, 40, 8));
    await flushFrame();
    expect(handler).not.toHaveBeenCalled();
  });
});

describe("teardown", () => {
  it("scales the backing store by devicePixelRatio, falling back to 1 when it is falsy", () => {
    const rig = makeCanvas();
    const setDpr = (v: number) =>
      Object.defineProperty(window, "devicePixelRatio", { configurable: true, value: v });
    setDpr(2);
    StubResizeObserver.latest.fire();
    expect(rig.canvas.width).toBe(1600); // 800 CSS px × dpr 2
    setDpr(0); // a window that reports a nonsensical ratio must not produce a 0px canvas
    StubResizeObserver.latest.fire();
    expect(rig.canvas.width).toBe(800); // `|| 1` fallback
    setDpr(1);
  });

  it("destroy with NO frame pending skips the cancel and still unlistens", async () => {
    const cancelSpy = vi.spyOn(globalThis, "cancelAnimationFrame");
    const rig = makeCanvas();
    rig.view.setGraph({ nodes: [], edges: [], width: 10, height: 10 });
    await flushFrame(); // let the scheduled render run out first
    rig.view.destroy();
    expect(cancelSpy).not.toHaveBeenCalled();
    const e = wheelEvent(-Math.LN2 / 0.0016, 0, 0);
    rig.canvas.dispatchEvent(e);
    expect(e.defaultPrevented).toBe(false);
    cancelSpy.mockRestore();
  });

  it("a release without pointer capture skips releasePointerCapture", async () => {
    const rig = makeCanvas();
    rig.view.setGraph({ nodes: [node("n1", 10, 20)], edges: [], width: 0, height: 0 }, false);
    rig.canvas.hasPointerCapture = vi.fn(() => false); // browser already dropped it
    rig.canvas.dispatchEvent(pointerEvent("pointerdown", 50, 40, 9));
    rig.canvas.dispatchEvent(pointerEvent("pointerup", 50, 40, 9));
    await flushFrame();
    expect(rig.canvas.releasePointerCapture).not.toHaveBeenCalled();
  });

  it("destroy disconnects, unlistens, and cancels a pending frame", async () => {
    const cancelSpy = vi.spyOn(globalThis, "cancelAnimationFrame");
    const rig = makeCanvas();
    rig.view.setGraph({ nodes: [], edges: [], width: 10, height: 10 }); // fit schedules a render
    rig.view.destroy();
    expect(StubResizeObserver.latest.disconnected).toBe(true);
    expect(cancelSpy).toHaveBeenCalled();
    const e = wheelEvent(-Math.LN2 / 0.0016, 0, 0);
    rig.canvas.dispatchEvent(e);
    expect(e.defaultPrevented).toBe(false); // listener removed
    cancelSpy.mockRestore();
  });
});
