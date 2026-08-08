// @vitest-environment happy-dom
/**
 * VirtualList: the windowing maths, the empty state, and the focus-across-repaint fix.
 *
 * WHY THE DOCBLOCK ABOVE. The suite default is `environment: "node"` and stays that way —
 * a global DOM flip breaks four unrelated test files (see `vitest.config.ts` for the
 * measurements). This file opts in per-file instead.
 *
 * WHY GEOMETRY IS INJECTED RATHER THAN STYLED. happy-dom does no layout: a div with
 * `style.height = "300px"` reports `clientHeight === 0`, and `getBoundingClientRect()` is
 * all zeros (jsdom is identical here — this is not a happy-dom shortcoming). `paint()`
 * reads `this.viewport.clientHeight || this.itemHeight`, so a test that merely sets a CSS
 * height would take the FALLBACK on every single assertion and a "virtualization works"
 * claim would prove only that the fallback exists. So `mount()` defines `clientHeight` as
 * an own property, and one test deliberately omits it to cover the fallback as the
 * distinct case it is.
 *
 * `scrollTop` needs no such help: happy-dom stores an assignment verbatim. It does NOT
 * dispatch a `scroll` event though (a real browser does), so `scrollTo` fires one by hand.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { emptyStateLabel, VirtualList } from "./virtualList";

/**
 * A ResizeObserver whose callback the test can fire.
 *
 * happy-dom HAS a global `ResizeObserver` (jsdom does not — it throws `ReferenceError`),
 * but with no layout engine it can never fire: measured 0 callbacks after appending a
 * 500px child to an observed element. So the native one is enough to CONSTRUCT a
 * VirtualList and not enough to exercise the resize path, which is the whole reason this
 * stub exists.
 */
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
    /* VirtualList only ever disconnects. */
  }

  disconnect(): void {
    this.disconnected = true;
  }

  /** What a real ResizeObserver does once the viewport's box changes. */
  fire(): void {
    this.callback([], this as unknown as ResizeObserver);
  }

  static get latest(): StubResizeObserver {
    // Not `.at(-1)`: tsconfig targets ES2020, where Array.prototype.at does not exist.
    const { instances } = StubResizeObserver;
    const found = instances[instances.length - 1];
    if (found === undefined) throw new Error("no ResizeObserver was constructed");
    return found;
  }
}

const ITEM_HEIGHT = 20;

interface MountOptions {
  count?: number;
  itemHeight?: number;
  overscan?: number;
  emptyLabel?: string;
  /** Injected viewport height. Omit to leave it at the un-laid-out 0. */
  viewportHeight?: number;
  renderRow?: (item: string, index: number) => HTMLElement;
}

interface Harness {
  viewport: HTMLElement;
  list: VirtualList<string>;
  sizer: HTMLElement;
  rows: () => HTMLElement[];
  /** `data-vl-index` of every rendered row, in document order. */
  indices: () => number[];
  renderedFor: string[];
  scrollTo: (px: number) => void;
  setViewportHeight: (px: number) => void;
  observer: () => StubResizeObserver;
}

/** One row holding a single button, the shape the roots list uses. */
function oneButtonRow(item: string): HTMLElement {
  const row = document.createElement("div");
  const button = document.createElement("button");
  button.textContent = item;
  row.append(button);
  return row;
}

function mount(options: MountOptions = {}): Harness {
  const {
    count = 0,
    itemHeight = ITEM_HEIGHT,
    overscan,
    emptyLabel,
    viewportHeight,
    renderRow = oneButtonRow,
  } = options;

  const viewport = document.createElement("div");
  // Attached, because `focus()` moves `document.activeElement` only for an element that
  // is in the document — a detached one silently does not take focus.
  document.body.append(viewport);

  const setViewportHeight = (px: number): void => {
    Object.defineProperty(viewport, "clientHeight", { value: px, configurable: true });
  };
  if (viewportHeight !== undefined) setViewportHeight(viewportHeight);

  const renderedFor: string[] = [];
  const list = new VirtualList<string>(viewport, {
    itemHeight,
    overscan,
    emptyLabel,
    renderRow: (item, index) => {
      renderedFor.push(item);
      return renderRow(item, index);
    },
  });

  list.setItems(Array.from({ length: count }, (_, i) => `item-${i}`));

  const sizer = viewport.firstElementChild as HTMLElement;
  const rows = (): HTMLElement[] => [...sizer.children] as HTMLElement[];

  return {
    viewport,
    list,
    sizer,
    rows,
    indices: () => rows().map((row) => Number(row.dataset.vlIndex)),
    renderedFor,
    // happy-dom does not dispatch `scroll` on a scrollTop assignment, so do it by hand.
    scrollTo: (px: number) => {
      viewport.scrollTop = px;
      viewport.dispatchEvent(new Event("scroll"));
    },
    setViewportHeight,
    observer: () => StubResizeObserver.latest,
  };
}

beforeEach(() => {
  StubResizeObserver.instances = [];
  Reflect.set(globalThis, "ResizeObserver", StubResizeObserver);
});

afterEach(() => {
  document.body.replaceChildren();
});

describe("emptyStateLabel", () => {
  it("returns the label when the list holds nothing", () => {
    expect(emptyStateLabel(0, "No threads yet.")).toBe("No threads yet.");
  });

  it("returns null as soon as there is a single item", () => {
    expect(emptyStateLabel(1, "No threads yet.")).toBeNull();
  });

  it("returns null for a populated list", () => {
    expect(emptyStateLabel(250, "No threads yet.")).toBeNull();
  });

  it("returns null when no label was configured, even while empty", () => {
    // A caller that opts out must not get an empty `data-empty` attribute, which would
    // render as a zero-height ::after box — worse than no empty state at all.
    expect(emptyStateLabel(0, undefined)).toBeNull();
  });

  it("treats an empty-string label as opting out", () => {
    expect(emptyStateLabel(0, "")).toBeNull();
  });
});

describe("VirtualList construction", () => {
  it("turns the viewport into a positioned scroll container with a sizer", () => {
    const { viewport, sizer } = mount({ count: 5, viewportHeight: 100 });

    expect(viewport.style.overflowY).toBe("auto");
    expect(viewport.style.position).toBe("relative");
    expect(sizer.style.position).toBe("relative");
    expect(sizer.style.width).toBe("100%");
  });

  it("observes the viewport so a resize repaints", () => {
    const { viewport, observer } = mount({ count: 5, viewportHeight: 100 });

    expect(observer().observed).toEqual([viewport]);
  });

  it("applies the empty label BEFORE any setItems call", () => {
    // The search panel only calls setItems from its input handler, so a label applied
    // lazily would appear only after the user typed and cleared the box — never on the
    // first paint, which is the one time it matters.
    const viewport = document.createElement("div");
    document.body.append(viewport);

    new VirtualList<string>(viewport, {
      itemHeight: ITEM_HEIGHT,
      renderRow: oneButtonRow,
      emptyLabel: "No threads yet.",
    });

    expect(viewport.getAttribute("data-empty")).toBe("No threads yet.");
  });

  it("leaves data-empty off entirely when no label was configured", () => {
    const { viewport } = mount({ count: 0 });

    expect(viewport.hasAttribute("data-empty")).toBe(false);
  });

  it("clears the empty label once items arrive, and restores it when they go", () => {
    const { viewport, list } = mount({
      count: 3,
      viewportHeight: 100,
      emptyLabel: "No threads yet.",
    });
    expect(viewport.hasAttribute("data-empty")).toBe(false);

    list.setItems([]);
    expect(viewport.getAttribute("data-empty")).toBe("No threads yet.");
  });
});

describe("VirtualList windowing", () => {
  it("materializes only the visible window plus overscan, not the whole corpus", () => {
    // 100px viewport / 20px rows = 5 visible, + 4 overscan above and below = 13.
    const { rows, indices, sizer } = mount({ count: 1000, viewportHeight: 100 });

    expect(rows()).toHaveLength(13);
    expect(indices()).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
    // The sizer still spans the full corpus, so the scrollbar is honest.
    expect(sizer.style.height).toBe(`${1000 * ITEM_HEIGHT}px`);
  });

  it("clamps the first index at zero at the top of the list", () => {
    // floor(0 / 20) - 4 = -4, which must not index backwards off the array.
    const { indices } = mount({ count: 1000, viewportHeight: 100 });

    expect(indices()[0]).toBe(0);
  });

  it("advances the window as the viewport scrolls", () => {
    const { scrollTo, indices } = mount({ count: 1000, viewportHeight: 100 });

    scrollTo(400); // floor(400 / 20) = 20, minus 4 overscan
    expect(indices()).toEqual([16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28]);
  });

  it("renders each row absolutely at index * itemHeight", () => {
    const { scrollTo, rows } = mount({ count: 1000, viewportHeight: 100 });

    scrollTo(400);
    const first = rows()[0];
    expect(first.dataset.vlIndex).toBe("16");
    expect(first.style.position).toBe("absolute");
    expect(first.style.top).toBe(`${16 * ITEM_HEIGHT}px`);
    expect(first.style.height).toBe(`${ITEM_HEIGHT}px`);
    expect(first.style.left).toBe("0px");
    expect(first.style.right).toBe("0px");
  });

  it("only ever builds rows for the items in the window", () => {
    const { renderedFor } = mount({ count: 1000, viewportHeight: 100 });

    // 13 renderRow calls for 1000 items is the entire point of the component.
    expect(renderedFor).toEqual(
      Array.from({ length: 13 }, (_, i) => `item-${i}`),
    );
  });

  it("falls back to one row's height when the viewport has no measured height", () => {
    // The `clientHeight || itemHeight` fallback. It is a REAL production case (a pane
    // that is display:none, or measured before first layout), and it is also the ONLY
    // case a DOM-less runner can produce — hence the explicit split from the tests
    // above, which inject a height so the maths is genuinely exercised.
    const { viewport, rows } = mount({ count: 1000 });

    expect(viewport.clientHeight).toBe(0);
    // ceil(20 / 20) = 1 visible, + 8 overscan = 9.
    expect(rows()).toHaveLength(9);
  });

  it("stops at the end of a list shorter than the window", () => {
    const { rows, indices } = mount({ count: 3, viewportHeight: 100 });

    expect(rows()).toHaveLength(3);
    expect(indices()).toEqual([0, 1, 2]);
  });

  it("renders no rows at all for an empty list", () => {
    const { rows } = mount({ count: 0, viewportHeight: 100 });

    expect(rows()).toHaveLength(0);
  });

  it("honours a custom overscan", () => {
    const { scrollTo, indices } = mount({ count: 1000, viewportHeight: 100, overscan: 0 });

    scrollTo(400);
    expect(indices()).toEqual([20, 21, 22, 23, 24]);
  });

  it("repaints when the ResizeObserver reports a new viewport size", () => {
    const { rows, setViewportHeight, observer } = mount({ count: 1000, viewportHeight: 100 });
    expect(rows()).toHaveLength(13);

    setViewportHeight(400); // ceil(400 / 20) = 20 visible, + 8 overscan
    observer().fire();

    expect(rows()).toHaveLength(28);
  });
});

describe("VirtualList setItems", () => {
  it("resizes the sizer and returns to the top of the new content", () => {
    const { list, viewport, sizer, scrollTo, indices } = mount({
      count: 1000,
      viewportHeight: 100,
    });
    scrollTo(400);
    expect(indices()[0]).toBe(16);

    list.setItems(["a", "b", "c"]);

    expect(viewport.scrollTop).toBe(0);
    expect(sizer.style.height).toBe(`${3 * ITEM_HEIGHT}px`);
    expect(indices()).toEqual([0, 1, 2]);
  });

  it("reports the backing item count", () => {
    const { list } = mount({ count: 42, viewportHeight: 100 });

    expect(list.length).toBe(42);

    list.setItems([]);
    expect(list.length).toBe(0);
  });
});

describe("VirtualList destroy", () => {
  it("removes the sizer, disconnects the observer, and stops repainting", () => {
    const { list, viewport, sizer, renderedFor, observer } = mount({
      count: 1000,
      viewportHeight: 100,
    });
    const paintedBefore = renderedFor.length;

    list.destroy();

    expect(observer().disconnected).toBe(true);
    expect(sizer.parentElement).toBeNull();
    expect(viewport.children).toHaveLength(0);

    // The scroll listener is gone, so a further scroll must not build any more rows.
    viewport.scrollTop = 400;
    viewport.dispatchEvent(new Event("scroll"));
    expect(renderedFor).toHaveLength(paintedBefore);
  });
});

/**
 * The focus-across-repaint behaviour, which is a real WCAG 2.1.1 / 2.4.3 defect class in
 * this repo rather than an incidental line.
 *
 * MECHANISM, reproduced faithfully by happy-dom (measured): `paint()` rebuilds the window
 * with `sizer.replaceChildren()`, which DETACHES the focused row, and a detached focused
 * element hands `document.activeElement` back to `<body>`. The next Tab then restarts from
 * the top of the page, so tabbing down a virtualized list is a closed loop.
 */
describe("VirtualList focus across a repaint", () => {
  /** A search-hit shaped row: two controls, so position within the row matters. */
  function twoButtonRow(item: string): HTMLElement {
    const row = document.createElement("div");
    const locate = document.createElement("button");
    locate.textContent = `locate ${item}`;
    const read = document.createElement("button");
    read.textContent = `read ${item}`;
    row.append(locate, read);
    return row;
  }

  function rowWithIndex(harness: Harness, index: number): HTMLElement {
    const row = harness.rows().find((candidate) => candidate.dataset.vlIndex === String(index));
    if (row === undefined) throw new Error(`row ${index} is not rendered`);
    return row;
  }

  it("keeps focus on the SAME control after a repaint the user did not cause", () => {
    const harness = mount({ count: 1000, viewportHeight: 100, renderRow: twoButtonRow });
    const before = rowWithIndex(harness, 5);
    const readBefore = before.children[1] as HTMLElement;
    readBefore.focus();
    expect(document.activeElement).toBe(readBefore);

    harness.scrollTo(40); // row 5 is still inside the window

    const after = rowWithIndex(harness, 5);
    // The row really was rebuilt — otherwise this test would pass by doing nothing.
    expect(after).not.toBe(before);
    expect(readBefore.isConnected).toBe(false);
    // ...and focus landed on the equivalent control, not the row and not the sibling.
    expect(document.activeElement).toBe(after.children[1]);
    expect(document.activeElement).not.toBe(after.children[0]);
  });

  it("would otherwise drop focus to the body — which is what setItems deliberately does", () => {
    // setItems repaints with preserveFocus=false ON PURPOSE: the content is different, so
    // "row 5" is a different thing and restoring into it would hijack the caret out of the
    // search box mid-typing. This test pins BOTH halves: the deliberate non-restore here,
    // and (above) the restore on a scroll repaint.
    const harness = mount({ count: 1000, viewportHeight: 100, renderRow: twoButtonRow });
    (rowWithIndex(harness, 5).children[1] as HTMLElement).focus();

    harness.list.setItems(["fresh-0", "fresh-1"]);

    expect(document.activeElement).toBe(document.body);
  });

  it("restores the FIRST control of a row, not the row wrapping it", () => {
    // Position 0 has to be told apart from "the row itself" (position -1). Conflating them
    // lands focus on a bare div, which is not focusable, so focus falls to the body and the
    // Tab loop reopens — silently, because the row is still on screen.
    const harness = mount({ count: 1000, viewportHeight: 100, renderRow: twoButtonRow });
    const locateBefore = rowWithIndex(harness, 5).children[0] as HTMLElement;
    locateBefore.focus();

    harness.scrollTo(40);

    const after = rowWithIndex(harness, 5);
    expect(after.children[0]).not.toBe(locateBefore);
    expect(document.activeElement).toBe(after.children[0]);
  });

  it("restores focus to the row itself when the row is the focusable thing", () => {
    const harness = mount({
      count: 1000,
      viewportHeight: 100,
      renderRow: (item) => {
        const row = document.createElement("div");
        row.tabIndex = 0; // focusable row, no focusable children
        row.textContent = item;
        return row;
      },
    });
    rowWithIndex(harness, 5).focus();

    harness.scrollTo(40);

    expect(document.activeElement).toBe(rowWithIndex(harness, 5));
  });

  it("falls back to the row when the rebuilt row has fewer controls than before", () => {
    // A search hit whose "read" button disappears on rebuild: position 1 no longer
    // exists, and focus must land on the row rather than vanish to the body.
    let shrunk = false;
    const harness = mount({
      count: 1000,
      viewportHeight: 100,
      renderRow: (item) => {
        const row = document.createElement("div");
        row.tabIndex = -1;
        const locate = document.createElement("button");
        locate.textContent = `locate ${item}`;
        row.append(locate);
        if (!shrunk) {
          const read = document.createElement("button");
          read.textContent = `read ${item}`;
          row.append(read);
        }
        return row;
      },
    });
    (rowWithIndex(harness, 5).children[1] as HTMLElement).focus();

    shrunk = true;
    harness.scrollTo(40);

    const after = rowWithIndex(harness, 5);
    expect(after.children).toHaveLength(1);
    expect(document.activeElement).toBe(after);
  });

  it("gives up when the focused row scrolled clean out of the window", () => {
    const harness = mount({ count: 1000, viewportHeight: 100, renderRow: twoButtonRow });
    (rowWithIndex(harness, 0).children[0] as HTMLElement).focus();

    harness.scrollTo(4000); // window moves to rows 196..208

    expect(harness.indices()).not.toContain(0);
    expect(document.activeElement).toBe(document.body);
  });

  it("ignores focus that is outside the list entirely", () => {
    const outside = document.createElement("button");
    document.body.append(outside);
    const harness = mount({ count: 1000, viewportHeight: 100, renderRow: twoButtonRow });
    outside.focus();

    harness.scrollTo(400);

    // A repaint must not steal the caret from a search box that happens to sit above it.
    expect(document.activeElement).toBe(outside);
  });

  it("ignores focus on the sizer, which belongs to no row", () => {
    const harness = mount({ count: 1000, viewportHeight: 100, renderRow: twoButtonRow });
    harness.sizer.tabIndex = -1;
    harness.sizer.focus();
    expect(document.activeElement).toBe(harness.sizer);

    harness.scrollTo(400);

    // The sizer survives the repaint, so focus simply stays put and nothing is restored.
    expect(document.activeElement).toBe(harness.sizer);
    expect(harness.indices()[0]).toBe(16);
  });

  it("ignores a focused element that is not an HTMLElement", () => {
    // An inline SVG icon carrying tabindex is focusable and is an SVGElement, so the
    // `instanceof HTMLElement` guard is a reachable path, not a formality.
    const harness = mount({
      count: 1000,
      viewportHeight: 100,
      renderRow: (item) => {
        const row = document.createElement("div");
        const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        icon.setAttribute("tabindex", "0");
        icon.setAttribute("aria-label", item);
        row.append(icon);
        return row;
      },
    });
    const icon = rowWithIndex(harness, 5).firstElementChild as SVGElement;
    icon.focus();
    expect(document.activeElement).toBe(icon);

    harness.scrollTo(40);

    // Nothing was captured, so nothing is restored: the detached icon's focus falls away.
    expect(document.activeElement).toBe(document.body);
    expect(harness.indices()[0]).toBe(0);
  });
});
