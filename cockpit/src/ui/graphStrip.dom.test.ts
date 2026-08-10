// @vitest-environment happy-dom
/**
 * The graph pane's PAINT half (DECISION D-4) — {@link GraphStrip}.
 *
 * A DOM file because there is no decision left to test here: `graphStrip.test.ts` owns the
 * whole table, and what remains is exactly the part that needs elements — is the canvas gone,
 * is one line of chrome in its place, is the pane's reason readable, does a live region survive
 * being updated. Those are the four things a reviewer looking at a blank pane cannot otherwise
 * be told apart, so they are asserted rather than described.
 *
 * The docblock on line 1 is the per-file opt-in `vitest.config.ts` documents; the default
 * environment stays `node` because a global DOM flip takes four unrelated test files down.
 *
 * NOTE ON ORDER OF CONSTRUCTION: this file was written AFTER the class it covers (both halves
 * of the module landed together), so its red-proof is not fail-first. It was instead measured by
 * mutation — three separate reverts inside `apply()` (skip the canvas hide, keep the pane
 * attribute on a drawn graph, always show the strip) were each confirmed to turn cases in this
 * file RED before being restored.
 */
import { beforeEach, describe, expect, it } from "vitest";

import {
  EMPTY_CORPUS_LABEL,
  GraphStrip,
  NO_LINEAGE_LABEL,
  graphStripState,
  type GraphStripState,
} from "./graphStrip";

interface Pane {
  strip: GraphStrip;
  pane: HTMLElement;
  canvas: HTMLCanvasElement;
  /** The element the class inserted; located the way a CSS rule or probe would. */
  el(): HTMLElement | null;
}

/**
 * Build the pane as `index.html` has it: a toolbar, then the canvas last
 * (`index.html:88-112`). The strip has to land BETWEEN them, so both siblings exist here.
 */
function mount(): Pane {
  const pane = document.createElement("main");
  pane.id = "graph-pane";
  const toolbar = document.createElement("div");
  toolbar.id = "graph-toolbar";
  const canvas = document.createElement("canvas");
  canvas.id = "tree-canvas";
  canvas.setAttribute("role", "img");
  pane.append(toolbar, canvas);
  document.body.append(pane);
  return {
    strip: new GraphStrip(pane, canvas),
    pane,
    canvas,
    el: () => pane.querySelector<HTMLElement>(".graph-strip"),
  };
}

/** A decided state, taken through the real decision so the two halves cannot drift apart. */
function state(over: Partial<Parameters<typeof graphStripState>[0]> = {}): GraphStripState {
  return graphStripState({
    nodeCount: 0,
    complete: true,
    conversations: 1_284,
    discoveryShowing: false,
    ...over,
  });
}

beforeEach(() => {
  document.body.replaceChildren();
});

describe("GraphStrip construction", () => {
  it("inserts one strip, hidden, immediately BEFORE the canvas", () => {
    // Before the canvas so reading and Tab order stay toolbar -> … -> strip, and so a
    // screen reader meets the explanation where the graph would have been.
    const shell = mount();
    const el = shell.el();
    expect(el).not.toBeNull();
    expect(el?.hidden).toBe(true);
    expect(el?.nextElementSibling).toBe(shell.canvas);
    expect(shell.pane.querySelectorAll(".graph-strip").length).toBe(1);
  });

  it("touches nothing else before a state is applied", () => {
    // Construction must be inert: `app.ts` builds this in its constructor, long before any
    // corpus is attached, and a canvas hidden at that moment would flash.
    const shell = mount();
    expect(shell.canvas.hidden).toBe(false);
    expect(shell.pane.dataset.graphStrip).toBeUndefined();
  });

  it("is a polite live region, so the reason for a blank pane is announced", () => {
    const el = mount().el();
    expect(el?.getAttribute("role")).toBe("status");
    expect(el?.getAttribute("aria-live")).toBe("polite");
  });

  it("styles itself as chrome, from theme tokens, without a style ATTRIBUTE", () => {
    // The shipped CSP is `style-src 'self'` with no `'unsafe-inline'`
    // (`src-tauri/tauri.conf.json:23`), which blocks a `style="…"` attribute parsed from
    // markup but not CSSOM. `el.style.setProperty` is the CSSOM seam `app.ts` already uses
    // for provider dots; asserting the VALUES here is what stops a later edit from switching
    // to `setAttribute("style", …)`, which the webview would silently drop.
    const el = mount().el();
    expect(el?.style.getPropertyValue("flex")).toBe("0 0 auto");
    expect(el?.style.getPropertyValue("background-color")).toBe("var(--panel)");
    expect(el?.style.getPropertyValue("color")).toBe("var(--muted)");
  });

  it("keeps every token-bearing declaration a LONGHAND, so none is silently destroyed", () => {
    // Measured, not stylistic: `border-bottom: 1px solid var(--border)` read back out of
    // happy-dom's CSSOM as `var(--border) var(--border) var(--border)` — a mangled shorthand
    // is a lost declaration. A real browser defers `var()` substitution and accepts it, so
    // the shorthand form would have shipped working and been impossible to pin here.
    const el = mount().el();
    expect(el?.style.getPropertyValue("border-bottom-width")).toBe("1px");
    expect(el?.style.getPropertyValue("border-bottom-color")).toBe("var(--border)");
    expect(el?.style.getPropertyValue("padding-top")).toBe("var(--space-3)");
    expect(el?.style.getPropertyValue("padding-left")).toBe("var(--space-4)");
    // And nothing reached the element as a mangled shorthand.
    expect(el?.style.getPropertyValue("border-bottom")).not.toContain("var(--border) var(--border)");
  });
});

describe("GraphStrip.apply — a graph to draw", () => {
  it("shows the canvas, hides the strip and clears the pane's reason", () => {
    const shell = mount();
    shell.strip.apply(state()); // collapse first, so the restore is a real transition
    expect(shell.canvas.hidden).toBe(true);

    shell.strip.apply(state({ nodeCount: 9 }));
    expect(shell.canvas.hidden).toBe(false);
    expect(shell.el()?.hidden).toBe(true);
    // Removed, not set to "none": a CSS rule keyed on `[data-graph-strip]` must not match a
    // pane that is drawing a graph.
    expect(shell.pane.hasAttribute("data-graph-strip")).toBe(false);
  });
});

describe("GraphStrip.apply — the collapse", () => {
  it("hides the canvas and puts the no-lineage line in its place", () => {
    // The DECISION D-4 case. `hidden` on the canvas, not merely an unfilled canvas: the
    // element carries `role="img"` with a label naming a spawn-tree graph, so leaving it in
    // the tree announces a graph that is not there.
    const shell = mount();
    shell.strip.apply(state());

    expect(shell.canvas.hidden).toBe(true);
    expect(shell.el()?.hidden).toBe(false);
    expect(shell.el()?.textContent).toBe(NO_LINEAGE_LABEL);
    expect(shell.pane.dataset.graphStrip).toBe("no-lineage");
  });

  it("ALSO sets display:none inline, because `hidden` alone cannot hide this canvas", () => {
    // The trap this codebase has already hit eight times: `#tree-canvas { display: block }`
    // (`styles.css:445-452`) is an AUTHOR rule, and the `[hidden] { display: none }` that makes
    // the attribute work lives in the UA stylesheet — author origin wins, so `hidden` alone
    // leaves the canvas painted. That is exactly why `styles.css` already carries eight
    // explicit `[hidden]` rules (`#reader[hidden]`, `#workspace[hidden]`,
    // `.workspace-pane[hidden]`, …), one per element that has an author `display`.
    //
    // Neither happy-dom nor jsdom does the cascade, so no DOM test can OBSERVE the override;
    // this asserts the inline declaration instead, which beats any author rule and is the one
    // thing that makes the collapse real. `styles.css` is outside this unit's file scope, so
    // the stylesheet rule that would let this line go is left to a follow-up.
    const shell = mount();
    shell.strip.apply(state());
    expect(shell.canvas.style.getPropertyValue("display")).toBe("none");
  });

  it("REMOVES the inline display when the graph comes back, not set it to block", () => {
    // Removed rather than overwritten: writing `block` would freeze the stylesheet's current
    // choice into an inline declaration nothing can override, so a later CSS change to the
    // canvas's display would silently not apply.
    const shell = mount();
    shell.strip.apply(state());
    shell.strip.apply(state({ nodeCount: 3 }));
    expect(shell.canvas.style.getPropertyValue("display")).toBe("");
    expect(shell.canvas.getAttribute("style") ?? "").not.toContain("display");
  });

  it("reports each reason on the pane, so four blank panes are told apart", () => {
    const shell = mount();
    const cases: Array<[Partial<Parameters<typeof state>[0]>, string]> = [
      [{ conversations: null }, "no-corpus"],
      [{ conversations: 0 }, "empty-corpus"],
      [{ conversations: 7 }, "no-lineage"],
      [{ complete: false }, "declined"],
    ];
    for (const [over, reason] of cases) {
      shell.strip.apply(state(over));
      expect(shell.pane.dataset.graphStrip).toBe(reason);
      expect(shell.canvas.hidden).toBe(true);
    }
  });

  it("writes the empty-corpus line through textContent, never as markup", () => {
    const shell = mount();
    shell.strip.apply(state({ conversations: 0 }));
    expect(shell.el()?.textContent).toBe(EMPTY_CORPUS_LABEL);
    expect(shell.el()?.querySelector("*")).toBeNull();
  });

  it("keeps the SAME element across repaints, so the live region stays announceable", () => {
    // Replacing the node would land each message in an element that was not yet in the
    // accessibility tree, so none would be announced — the trap `maintenanceShell` documents
    // for its own output line.
    const shell = mount();
    const first = shell.el();
    shell.strip.apply(state());
    shell.strip.apply(state({ conversations: 0 }));
    shell.strip.apply(state({ nodeCount: 4 }));
    expect(shell.el()).toBe(first);
  });
});

describe("GraphStrip.apply — auto-discovery suppression", () => {
  it("hides the STRIP the same double way, so styling `.graph-strip` cannot un-hide it", () => {
    // The follow-up this module asks for is a `.graph-strip` rule in `styles.css`. The moment
    // that rule sets a `display` — `flex` for alignment is the obvious one — `hidden` alone
    // stops hiding this element, and the suppressed case grows an empty bordered band. Same
    // author-beats-UA cascade as the canvas, pre-empted rather than waited for.
    const shell = mount();
    shell.strip.apply(state({ discoveryShowing: true }));
    expect(shell.el()?.style.getPropertyValue("display")).toBe("none");

    shell.strip.apply(state());
    expect(shell.el()?.style.getPropertyValue("display")).toBe("");
  });

  it("leaves the canvas collapsed but shows NO strip while discovery is on screen", () => {
    // An empty bordered band saying nothing is worse than no band: it is the same visual
    // noise the suppression exists to remove.
    const shell = mount();
    shell.strip.apply(state({ discoveryShowing: true }));

    expect(shell.canvas.hidden).toBe(true);
    expect(shell.el()?.hidden).toBe(true);
    expect(shell.el()?.textContent).toBe("");
    // Still honest about WHY, for a probe and for the CSS that will narrow the column.
    expect(shell.pane.dataset.graphStrip).toBe("no-lineage");
  });

  it("brings the line back when the discovery panel goes away", () => {
    const shell = mount();
    shell.strip.apply(state({ discoveryShowing: true }));
    shell.strip.apply(state({ discoveryShowing: false }));
    expect(shell.el()?.hidden).toBe(false);
    expect(shell.el()?.textContent).toBe(NO_LINEAGE_LABEL);
  });
});
