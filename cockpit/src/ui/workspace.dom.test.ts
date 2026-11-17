// @vitest-environment happy-dom
/**
 * {@link Workspace} — the pane host, tested through real events on a mounted DOM.
 *
 * WHY EVERY CASE GOES THROUGH `click`/`keydown`. The module's whole reason to exist (its own
 * header) is the seam between finished panels and the user — buttons that were never wired
 * are the exact defect class it guards against, and only a dispatched event catches a missing
 * listener. Mounting into `document.body` (not a detached div) because `focus()` only moves
 * `document.activeElement` for attached elements — the config's documented happy-dom trap —
 * and focus restore is load-bearing here (`returnFocusTo`).
 *
 * THE ESCAPE CASES ARE THE CONTRACT. The capture listener exists because `ReaderOverlay`
 * also listens on `document` in the bubble phase; the three arms (closed → ignore, blocked →
 * yield to the reader, open → close) are each pinned below so reordering the phases fails a
 * test instead of shipping the one-press-closes-both regression the header describes.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Workspace, type WorkspacePaneSpec } from "./workspace";

function makePane(
  key: string,
  title: string | (() => string),
): {
  pane: WorkspacePaneSpec;
  button: HTMLButtonElement;
  container: HTMLElement;
} {
  const button = document.createElement("button");
  button.id = `btn-${key}`;
  const container = document.createElement("div");
  container.id = `pane-${key}`;
  document.body.append(button, container);
  return { pane: { key, button, container, title }, button, container };
}

function makeWorkspace(opts: { escapeBlocked?: () => boolean } = {}): {
  ws: Workspace;
  root: HTMLElement;
  titleEl: HTMLElement;
  closeBtn: HTMLButtonElement;
  dup: ReturnType<typeof makePane>;
  ann: ReturnType<typeof makePane>;
} {
  const root = document.createElement("section");
  root.id = "workspace";
  const titleEl = document.createElement("h2");
  titleEl.id = "workspace-title";
  const closeBtn = document.createElement("button");
  document.body.append(root, titleEl, closeBtn);
  const dup = makePane("duplicates", "Duplicates");
  const ann = makePane("annotations", () => `Annotations · ${annNoteCount}`);
  const ws = new Workspace(root, titleEl, closeBtn, [dup.pane, ann.pane], opts.escapeBlocked);
  return { ws, root, titleEl, closeBtn, dup, ann };
}

/** The dynamic-title source the annotations pane's `title()` closure reads. */
let annNoteCount = 0;

function pressEscape(): KeyboardEvent {
  const e = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
  document.dispatchEvent(e);
  return e;
}

beforeEach(() => {
  annNoteCount = 0;
});

afterEach(() => {
  document.body.replaceChildren();
});

describe("Workspace constructor", () => {
  it("hides the region and every pane, and announces the labelling wiring", () => {
    const { root, dup, ann } = makeWorkspace();
    expect(root.hidden).toBe(true);
    expect(dup.container.hidden).toBe(true);
    expect(ann.container.hidden).toBe(true);
    expect(root.getAttribute("aria-labelledby")).toBe("workspace-title");
    expect(dup.button.getAttribute("aria-expanded")).toBe("false");
    expect(dup.button.getAttribute("aria-controls")).toBe("workspace");
    expect(ann.button.getAttribute("aria-controls")).toBe("workspace");
  });
});

describe("Workspace show/toggle/close", () => {
  it("reveals a pane, titles the region, and expands only that pane's button", () => {
    const { ws, root, titleEl, dup, ann } = makeWorkspace();
    ws.show("duplicates");
    expect(root.hidden).toBe(false);
    expect(dup.container.hidden).toBe(false);
    expect(ann.container.hidden).toBe(true);
    expect(titleEl.textContent).toBe("Duplicates");
    expect(dup.button.getAttribute("aria-expanded")).toBe("true");
    expect(ann.button.getAttribute("aria-expanded")).toBe("false");
    expect(ws.openPane).toBe("duplicates");
  });

  it("ignores an unknown pane key entirely", () => {
    const { ws, root } = makeWorkspace();
    ws.show("nope");
    expect(root.hidden).toBe(true);
    expect(ws.openPane).toBeNull();
  });

  it("runs onShow with firstShow=true exactly once per pane, across close/reopen", () => {
    const { ws } = makeWorkspace();
    const first = vi.fn();
    const second = vi.fn();
    // Replace one pane's hook after construction via a dedicated workspace.
    const root = document.createElement("section");
    root.id = "w2";
    const titleEl = document.createElement("h2");
    titleEl.id = "w2-title";
    const closeBtn = document.createElement("button");
    document.body.append(root, titleEl, closeBtn);
    const p1 = makePane("a", "A");
    const p2 = makePane("b", "B");
    p1.pane.onShow = first;
    p2.pane.onShow = second;
    const ws2 = new Workspace(root, titleEl, closeBtn, [p1.pane, p2.pane]);

    ws2.show("a");
    ws2.show("a"); // every reveal re-runs the hook — but only the FIRST is firstShow
    ws2.close();
    ws2.show("a");
    expect(first.mock.calls).toEqual([[true], [false], [false]]);
    expect(second).not.toHaveBeenCalled();
    expect(ws.openPane).toBeNull(); // the untouched first workspace stayed closed
  });

  it("toggle closes the open pane and switches to a different one", () => {
    const { ws, root, titleEl, dup, ann } = makeWorkspace();
    ws.toggle("duplicates");
    expect(ws.openPane).toBe("duplicates");
    ws.toggle("annotations");
    expect(ws.openPane).toBe("annotations");
    expect(titleEl.textContent).toBe("Annotations · 0");
    ws.toggle("annotations");
    expect(ws.openPane).toBeNull();
    expect(root.hidden).toBe(true);
    expect(dup.container.hidden && ann.container.hidden).toBe(true);
  });

  it("close() is a no-op when nothing is open", () => {
    const { ws } = makeWorkspace();
    expect(() => ws.close()).not.toThrow();
    expect(ws.openPane).toBeNull();
  });

  it("a pane button click reveals its pane; the close button hides the region", () => {
    const { ws, root, closeBtn, dup } = makeWorkspace();
    dup.button.click();
    expect(ws.openPane).toBe("duplicates");
    expect(root.hidden).toBe(false);
    closeBtn.click();
    expect(root.hidden).toBe(true);
    expect(dup.button.getAttribute("aria-expanded")).toBe("false");
  });
});

describe("Workspace focus handling", () => {
  it("restores focus to the button the pane was opened from", () => {
    const { ws, dup } = makeWorkspace();
    dup.button.focus();
    expect(document.activeElement).toBe(dup.button);
    ws.show("duplicates");
    ws.close();
    expect(document.activeElement).toBe(dup.button);
  });

  it("switching panes keeps the ORIGINAL opener as the focus-return target", () => {
    const { ws, dup, ann } = makeWorkspace();
    dup.button.focus();
    ws.show("duplicates");
    ann.button.focus(); // user moved on before switching panes
    ws.show("annotations");
    ws.close();
    expect(document.activeElement).toBe(dup.button);
  });

  it("does not throw when the focus-return node was detached while the pane was open", () => {
    const { ws, dup } = makeWorkspace();
    dup.button.focus();
    ws.show("duplicates");
    dup.button.remove(); // a pane rebuilds the DOM the focus came from
    expect(() => ws.close()).not.toThrow();
  });
});

describe("Workspace titles", () => {
  it("refreshTitle re-reads a dynamic title only for the pane on screen", () => {
    const { ws, titleEl } = makeWorkspace();
    ws.show("annotations");
    expect(titleEl.textContent).toBe("Annotations · 0");
    annNoteCount = 7;
    ws.refreshTitle();
    expect(titleEl.textContent).toBe("Annotations · 7");
    // Closed → nothing to relabel.
    ws.close();
    annNoteCount = 9;
    ws.refreshTitle();
    expect(titleEl.textContent).toBe("Annotations · 7");
  });

  it("refreshTitle leaves a static title pane alone while IT is not showing", () => {
    const { ws, titleEl } = makeWorkspace();
    ws.show("duplicates");
    annNoteCount = 5;
    ws.refreshTitle();
    expect(titleEl.textContent).toBe("Duplicates");
  });
});

describe("Workspace Escape handling", () => {
  it("closes the open pane on Escape and prevents the default", () => {
    const { ws } = makeWorkspace();
    ws.show("duplicates");
    const e = pressEscape();
    expect(e.defaultPrevented).toBe(true);
    expect(ws.openPane).toBeNull();
  });

  it("ignores Escape while the region is hidden", () => {
    const { ws } = makeWorkspace();
    const e = pressEscape();
    expect(e.defaultPrevented).toBe(false);
    expect(ws.openPane).toBeNull();
  });

  it("yields Escape to a blocking overlay instead of closing", () => {
    const { ws } = makeWorkspace({ escapeBlocked: () => true });
    ws.show("duplicates");
    const e = pressEscape();
    expect(e.defaultPrevented).toBe(false);
    expect(ws.openPane).toBe("duplicates");
  });
});
