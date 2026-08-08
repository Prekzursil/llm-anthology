// @vitest-environment happy-dom
/**
 * {@link MetadataPanel} — the DOM half of the annotation panel.
 *
 * WHY A SECOND FILE. `metadataPanel.test.ts` specifies the pure decisions and the DOM-free
 * controller and must stay on the suite default (`environment: "node"`). This file opts in
 * per-file to `happy-dom`, the pattern `ui/scrubber.test.ts` and `ui/virtualList.test.ts`
 * already use and the one `cockpit/vitest.config.ts` documents — a GLOBAL DOM flip is measured
 * there to break four unrelated test files.
 *
 * WHAT IS WORTH ASSERTING HERE, given the module's own claim that this class is "deliberately
 * decision-free". Three things, and none of them is reachable without a document:
 *
 *   1. THE WIRING. A control that renders but is wired to nothing is the "dead button" defect,
 *      and it is invisible to a controller test: every assertion below goes through a real
 *      `click`/`input` event so a listener that was never attached fails here.
 *   2. THE CARET GUARD. `paint` skips the field that currently holds focus. That rule is
 *      stated in a comment and enforced by `document.activeElement`, so it CANNOT be checked
 *      without focus — and `focus()` only moves `activeElement` for an element attached to the
 *      document, so every panel here is mounted into `document.body` rather than a detached div.
 *   3. THE DISABLED/HIDDEN ARITHMETIC. `Save`/`Clear` are disabled from three different facts
 *      each, and the module's whole thesis is that the UI must never make a claim the state
 *      does not support — a `Save alias` button over a write that cannot happen is exactly that.
 *
 * NOT ASSERTED HERE: anything the controller already specifies (stale-load guards, the
 * singular-`tag` search key, tag canonicalisation). Those live in the node file.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ANNOTATION_SCOPE_NOTE, MetadataPanel, type MetadataIpc } from "./metadataPanel";
import type { Annotation, MetadataSearchRow } from "../ipc/types";

// ---------------------------------------------------------------------------
// fixtures
// ---------------------------------------------------------------------------

/** `is_empty` is RECOMPUTED, never passed in, so a fixture cannot contradict itself. */
function makeAnnotation(over: Partial<Annotation> = {}): Annotation {
  const merged: Annotation = {
    conversation_id: "c1", alias: "", tags: [], notes: "", is_empty: true, ...over,
  };
  return {
    ...merged,
    is_empty: merged.alias === "" && merged.tags.length === 0 && merged.notes === "",
  };
}

function searchRow(id: string, over: Partial<MetadataSearchRow> = {}): MetadataSearchRow {
  return {
    conversation_id: id, provider: "codex", account: "", title: `title of ${id}`,
    created_at: "2026-01-01", updated_at: "2026-01-02", turn_count: 3, thread_id: "t1",
    annotation: makeAnnotation({ conversation_id: id, tags: ["x"] }),
    ...over,
  };
}

/**
 * A fake engine that honours the wire's TRI-STATE `metadata.set` — an absent key leaves that
 * field alone, a present one overwrites. Modelled rather than stubbed because the panel's
 * "only send what changed" rule is only safe if that is how the store behaves, and a fake that
 * replaced the whole annotation would hide a panel that sent all three fields every time.
 */
function fakeEngine(over: Partial<MetadataIpc> = {}): MetadataIpc & { calls: string[] } {
  const calls: string[] = [];
  const store = new Map<string, Annotation>([
    ["c1", makeAnnotation({ conversation_id: "c1", alias: "boot", tags: ["alpha"], notes: "n" })],
    ["c2", makeAnnotation({ conversation_id: "c2", alias: "other", tags: ["zeta"], notes: "m" })],
    ["blank", makeAnnotation({ conversation_id: "blank" })],
  ]);
  const base: MetadataIpc = {
    async metadataGet(id) {
      calls.push(`get(${id})`);
      return store.get(id) ?? makeAnnotation({ conversation_id: id });
    },
    async metadataSet(params) {
      calls.push(`set(${JSON.stringify(params)})`);
      const prev = store.get(params.conversation_id)
        ?? makeAnnotation({ conversation_id: params.conversation_id });
      const next = makeAnnotation({
        conversation_id: params.conversation_id,
        alias: params.alias ?? prev.alias,
        tags: params.tags ?? prev.tags,
        notes: params.notes ?? prev.notes,
      });
      store.set(params.conversation_id, next);
      return next;
    },
    async metadataClear(id) {
      calls.push(`clear(${id})`);
      const next = makeAnnotation({ conversation_id: id });
      store.set(id, next);
      return next;
    },
    async metadataSearch(params) {
      calls.push(`search(${JSON.stringify(params ?? {})})`);
      return [
        // One row WITH an alias and one without, because the row label falls back to the
        // conversation title only for the second kind.
        searchRow("c1", { annotation: makeAnnotation({ conversation_id: "c1", alias: "boot" }) }),
        searchRow("c2", { title: "Second thread" }),
      ];
    },
    async metadataTags() {
      calls.push("tags()");
      return [{ tag: "alpha", count: 4 }, { tag: "zeta", count: 1 }];
    },
  };
  return Object.assign({ calls }, base, over);
}

// ---------------------------------------------------------------------------
// mounting
// ---------------------------------------------------------------------------

/** `querySelector` narrowed to "or fail loudly", so a missing node is never a silent null. */
function must<T extends Element>(root: ParentNode, selector: string): T {
  const el = root.querySelector<T>(selector);
  if (el === null) throw new Error(`no element matched ${selector}`);
  return el;
}

function all<T extends Element>(root: ParentNode, selector: string): T[] {
  return [...root.querySelectorAll<T>(selector)];
}

interface Mounted {
  ipc: MetadataIpc & { calls: string[] };
  panel: MetadataPanel;
  container: HTMLElement;
  alias: HTMLInputElement;
  tags: HTMLInputElement;
  notes: HTMLTextAreaElement;
  save: HTMLButtonElement;
  clear: HTMLButtonElement;
  query: HTMLInputElement;
  status: HTMLElement;
  error: HTMLElement;
}

/**
 * Mount into `document.body`, not a detached div: `focus()` silently does not move
 * `document.activeElement` for an unattached element, so the caret-guard tests would measure
 * nothing and pass.
 */
function mount(over: Partial<MetadataIpc> = {}): Mounted {
  const ipc = fakeEngine(over);
  const container = document.createElement("div");
  document.body.append(container);
  const panel = new MetadataPanel(ipc, container);
  return {
    ipc,
    panel,
    container,
    alias: must<HTMLInputElement>(container, ".metadata-alias"),
    tags: must<HTMLInputElement>(container, ".metadata-tags"),
    notes: must<HTMLTextAreaElement>(container, ".metadata-notes"),
    save: must<HTMLButtonElement>(container, ".metadata-save"),
    clear: must<HTMLButtonElement>(container, ".metadata-clear"),
    query: must<HTMLInputElement>(container, ".metadata-q"),
    status: must<HTMLElement>(container, ".metadata-status"),
    error: must<HTMLElement>(container, ".metadata-error"),
  };
}

/** Type into a field the way a user does — value THEN the event the panel listens for. */
function type(el: HTMLInputElement | HTMLTextAreaElement, value: string): void {
  el.value = value;
  el.dispatchEvent(new Event("input"));
}

/**
 * Drain the promise chain a click started.
 *
 * The click handlers are `() => void this.controller.save()`, so the call returns before the
 * engine does and there is no promise for a test to await. Every fake here resolves without a
 * timer, so the whole chain's microtasks are queued and drained BEFORE the next macrotask —
 * which makes one `setTimeout(0)` a deterministic barrier rather than a sleep.
 */
function settle(): Promise<void> {
  return new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });
}

/** A promise the test opens by hand, for observing the in-flight (`busy`) paint. */
function gate(): { wait: Promise<void>; release: () => void } {
  let release = (): void => {};
  const wait = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { wait, release: () => release() };
}

beforeEach(() => {
  document.body.replaceChildren();
});

afterEach(() => {
  // A focused element that is then detached can leave `document.activeElement` pointing at a
  // node outside the document, which would leak the caret guard's premise into the next test.
  if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
  document.body.replaceChildren();
});

// ---------------------------------------------------------------------------
// the skeleton
// ---------------------------------------------------------------------------

describe("MetadataPanel skeleton", () => {
  it("builds a labelled editor, the actions, the search and the two result regions", () => {
    const m = mount();
    expect(m.container.classList.contains("metadata-panel")).toBe(true);

    // Each input is nested INSIDE its label, which is what makes the label its accessible
    // name without an id/for pair that a later rename could silently break.
    for (const [cls, text] of [
      ["metadata-alias", "Alias"],
      ["metadata-tags", "Tags (comma separated)"],
      ["metadata-notes", "Notes"],
      ["metadata-q", "Find in annotations"],
    ] as const) {
      const label = must<HTMLLabelElement>(m.container, `.${cls}-label`);
      expect(label.textContent).toContain(text);
      expect(label.querySelector(`.${cls}`)).not.toBeNull();
    }

    expect(m.notes.tagName).toBe("TEXTAREA");
    // MEASURED happy-dom 20.11.2 deviation: `rows` reads back as the STRING "4" although the
    // DOM spec and TypeScript's own lib both type it `number` (an `unsigned long`). Coerced
    // rather than loosened, so the assertion still fails if the notes box stops being 4 rows.
    expect(Number(m.notes.rows)).toBe(4);
    expect(m.alias.type).toBe("text");
    expect(m.save.type).toBe("button");
    expect(all(m.container, ".metadata-facet").length).toBe(1);
    expect(all(m.container, ".metadata-results").length).toBe(1);
  });

  it("shows the search SCOPE permanently, because that is the one wrong conclusion available", () => {
    // `metadata.search` never reads message bodies (`ipc/types.ts:495`). A tooltip would let a
    // user read a false negative as proof their own transcripts are unsearchable, so the note
    // is a static paragraph inside the search block and not a title attribute.
    const m = mount();
    const scope = must<HTMLElement>(m.container, ".metadata-scope");
    expect(scope.textContent).toBe(ANNOTATION_SCOPE_NOTE);
    expect(scope.hidden).toBe(false);
    expect(must(m.container, ".metadata-search").contains(scope)).toBe(true);
  });

  it("announces engine failures through a live region that starts empty and hidden", () => {
    const m = mount();
    expect(m.error.getAttribute("role")).toBe("alert");
    expect(m.error.textContent).toBe("");
    expect(m.error.hidden).toBe(true);
  });

  it("starts IDLE rather than claiming zero matches", () => {
    // The panel's crux, at the DOM level: nothing has been searched, and `dataset.kind` has to
    // say so in a form a stylesheet and a test can both read.
    const m = mount();
    expect(m.status.dataset.kind).toBe("idle");
    expect(m.status.textContent).not.toMatch(/\b0\b|no match/i);
    expect(all(m.container, ".metadata-row")).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// the editor
// ---------------------------------------------------------------------------

describe("MetadataPanel editor", () => {
  it("fills the fields from the store and refreshes the facet in one open", async () => {
    const m = mount();
    await m.panel.open("c1");
    expect(m.alias.value).toBe("boot");
    expect(m.tags.value).toBe("alpha");
    expect(m.notes.value).toBe("n");
    expect(m.ipc.calls).toEqual(["get(c1)", "tags()"]);
    expect(all(m.container, ".metadata-tag").map((b) => b.textContent))
      .toEqual(["alpha (4)", "zeta (1)"]);
  });

  it("wires all three inputs, and names the unsaved fields on the Save button", async () => {
    // A control that renders but is wired to nothing is the dead-button defect. Each field is
    // typed into separately so a single missing `addEventListener` cannot hide behind the
    // other two.
    const m = mount();
    await m.panel.open("c1");
    expect(m.save.disabled).toBe(true);
    expect(m.save.textContent).toBe("Save");

    type(m.alias, "renamed");
    expect(m.save.textContent).toBe("Save alias");
    type(m.tags, "alpha, beta");
    expect(m.save.textContent).toBe("Save alias, tags");
    type(m.notes, "rewritten");
    expect(m.save.textContent).toBe("Save alias, tags, notes");
    expect(m.save.disabled).toBe(false);
  });

  it("saves ONLY the edited field and adopts what the store returns", async () => {
    const m = mount();
    await m.panel.open("c1");
    type(m.alias, "  renamed  ");
    m.save.click();
    await settle();

    // Present-but-empty CLEARS on this wire, so `tags`/`notes` must be ABSENT, not blank.
    expect(m.ipc.calls).toContain('set({"conversation_id":"c1","alias":"renamed"})');
    expect(m.alias.value).toBe("renamed");
    expect(m.tags.value).toBe("alpha");
    expect(m.notes.value).toBe("n");
    expect(m.save.textContent).toBe("Save");
    expect(m.save.disabled).toBe(true);
  });

  it("clears the whole annotation and then has nothing left to clear", async () => {
    const m = mount();
    await m.panel.open("c1");
    expect(m.clear.disabled).toBe(false);

    m.clear.click();
    await settle();
    expect(m.ipc.calls).toContain("clear(c1)");
    expect([m.alias.value, m.tags.value, m.notes.value]).toEqual(["", "", ""]);
    // Disabled again because the annotation is now empty — not because a request is in flight.
    expect(m.clear.disabled).toBe(true);
  });

  it("disables Clear before anything is loaded and for an un-annotated conversation", async () => {
    // Two different reasons, both of which would otherwise offer a destructive-sounding button
    // for a record that does not exist.
    const m = mount();
    expect(m.clear.disabled).toBe(true);
    await m.panel.open("blank");
    expect(m.clear.disabled).toBe(true);
    expect(m.save.disabled).toBe(true);
  });

  it("disables BOTH buttons while a write is in flight, even with edits outstanding", async () => {
    // `Save alias` is still the right label — the edit has not landed — but the button must not
    // be clickable, or a double-click sends the same write twice.
    const g = gate();
    const m = mount({
      async metadataSet(params) {
        await g.wait;
        return makeAnnotation({ conversation_id: params.conversation_id, alias: params.alias });
      },
    });
    await m.panel.open("c1");
    type(m.alias, "renamed");
    expect(m.save.disabled).toBe(false);

    m.save.click();
    expect(m.save.disabled).toBe(true);
    expect(m.clear.disabled).toBe(true);
    expect(m.save.textContent).toBe("Save alias");

    g.release();
    await settle();
    expect(m.save.disabled).toBe(true);
    expect(m.clear.disabled).toBe(false);
  });

  it("shows a genuine engine failure and keeps the retry available", async () => {
    const m = mount({
      async metadataGet() {
        throw new Error("index is corrupt");
      },
    });
    await m.panel.open("c1");
    expect(m.error.hidden).toBe(false);
    expect(m.error.textContent).toContain("index is corrupt");
  });
});

// ---------------------------------------------------------------------------
// the caret guard
// ---------------------------------------------------------------------------

describe("MetadataPanel caret guard", () => {
  const fields = ["alias", "tags", "notes"] as const;

  it.each(fields)(
    "leaves the focused %s field exactly as typed while the others adopt the stored form",
    async (focused) => {
      // The sharp version of "does not clobber the field being edited". All three fields are
      // typed with SURROUNDING SPACE, which the store strips, so after the save the draft and
      // the input genuinely differ — an unconditional write would be visible as a caret jump
      // in a browser and as a trimmed value here. Without the whitespace both values would be
      // identical and the assertion would hold whether the guard existed or not.
      const m = mount();
      await m.panel.open("c1");
      type(m.alias, "  A  ");
      type(m.tags, "  b  ");
      type(m.notes, "  C  ");
      m[focused].focus();
      expect(document.activeElement).toBe(m[focused]);

      m.save.click();
      await settle();

      const raw = { alias: "  A  ", tags: "  b  ", notes: "  C  " };
      const stored = { alias: "A", tags: "b", notes: "C" };
      for (const field of fields) {
        expect(m[field].value).toBe(field === focused ? raw[field] : stored[field]);
      }
    },
  );

  it("KNOWN LIMIT: a conversation switch does not reclaim the focused field", async () => {
    // Same guard, different story, and this one is a real (narrow) divergence rather than a
    // feature: `open()` reseeds from another conversation while the alias box has focus, so the
    // INPUT keeps the old text while the draft holds the new conversation's alias.
    //
    // What stops it being a data defect is asserted here rather than assumed: the draft and
    // `dirty` both follow the store, so `changedSetParams` returns null and the stray text
    // cannot be written to the newly-opened conversation. It is a display divergence — the box
    // shows text that is not in the draft and is not marked unsaved — bounded to the case where
    // focus stays in the field across a programmatic switch.
    const m = mount();
    await m.panel.open("c1");
    m.alias.focus();
    type(m.alias, "half-typed");
    await m.panel.open("c2");

    expect(m.alias.value).toBe("half-typed");   // the divergence
    expect(m.tags.value).toBe("zeta");          // the fields that did reseed
    expect(m.notes.value).toBe("m");
    // ...and the safety property: nothing is claimed unsaved, so nothing can be written.
    expect(m.save.disabled).toBe(true);
    expect(m.save.textContent).toBe("Save");
    m.save.click();
    await settle();
    expect(m.ipc.calls.filter((c) => c.startsWith("set("))).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// search, facet and results
// ---------------------------------------------------------------------------

describe("MetadataPanel search", () => {
  it("searches as the user types and reports the count it actually rendered", async () => {
    const m = mount();
    type(m.query, "boot");
    await settle();
    expect(m.ipc.calls).toContain('search({"text":"boot"})');
    expect(m.status.dataset.kind).toBe("hits");
    expect(m.status.textContent).toBe("2 annotated conversations");
    expect(all(m.container, ".metadata-row")).toHaveLength(2);
  });

  it("labels a row by its alias, falling back to the conversation title", async () => {
    const m = mount();
    type(m.query, "boot");
    await settle();
    const rows = all<HTMLButtonElement>(m.container, ".metadata-row");
    expect(rows[0].textContent).toContain("boot");
    expect(rows[1].textContent).toContain("Second thread");
    expect(must(rows[1], ".metadata-row-sub").textContent).toBe("1 tag");
  });

  it("writes a title as TEXT, never as markup", async () => {
    // A title and an alias are arbitrary bytes off the owner's disk; this is the one place they
    // reach the DOM.
    const hostile = "<img src=x onerror=alert(1)>";
    const m = mount({
      async metadataSearch() {
        return [searchRow("evil", { title: hostile })];
      },
    });
    type(m.query, "x");
    await settle();
    const row = must<HTMLButtonElement>(m.container, ".metadata-row");
    expect(row.querySelector("img")).toBeNull();
    expect(row.textContent).toContain(hostile);
  });

  it("hands the picked conversation to the host, and tolerates no host at all", async () => {
    const m = mount();
    type(m.query, "boot");
    await settle();
    const rows = all<HTMLButtonElement>(m.container, ".metadata-row");

    // Before `setOnPick` the callback is a no-op default. Clicking must be inert, not a crash:
    // the row is rendered by the same paint whether or not a host has registered yet.
    expect(() => rows[1].click()).not.toThrow();

    const picked: string[] = [];
    m.panel.setOnPick((id) => picked.push(id));
    rows[1].click();
    expect(picked).toEqual(["c2"]);
  });

  it("filters by a facet tag, marks it pressed, and unpresses it on a second click", async () => {
    const m = mount();
    await m.panel.open("c1");
    const facet = all<HTMLButtonElement>(m.container, ".metadata-tag");
    expect(facet.map((b) => b.getAttribute("aria-pressed"))).toEqual(["false", "false"]);

    facet[0].click();
    await settle();
    // The singular `tag` key is the wire's whole trap, so the DOM route has to hit it too.
    expect(m.ipc.calls).toContain('search({"tag":"alpha"})');
    expect(all(m.container, ".metadata-tag")[0].getAttribute("aria-pressed")).toBe("true");
    expect(all(m.container, ".metadata-tag")[1].getAttribute("aria-pressed")).toBe("false");

    // Clicking the ACTIVE tag clears the filter, which leaves no filter at all — so the panel
    // must go back to IDLE and not report the resulting empty list as "no matches".
    all<HTMLButtonElement>(m.container, ".metadata-tag")[0].click();
    await settle();
    expect(all(m.container, ".metadata-tag")[0].getAttribute("aria-pressed")).toBe("false");
    expect(m.status.dataset.kind).toBe("idle");
    expect(all(m.container, ".metadata-row")).toEqual([]);
  });

  it("combines the typed text with the facet tag", async () => {
    const m = mount();
    await m.panel.open("c1");
    type(m.query, "boot");
    await settle();
    all<HTMLButtonElement>(m.container, ".metadata-tag")[1].click();
    await settle();
    expect(m.ipc.calls).toContain('search({"text":"boot","tag":"zeta"})');
  });

  it("does not leave a stale hit count over a list a failure emptied", async () => {
    // This repo's recurring defect, asserted where the user would actually see it: the rows go
    // away and the status line must stop boasting about them.
    let fail = false;
    const m = mount({
      async metadataSearch() {
        if (fail) throw new Error("pipe died");
        return [searchRow("c1"), searchRow("c2")];
      },
    });
    type(m.query, "boot");
    await settle();
    expect(all(m.container, ".metadata-row")).toHaveLength(2);

    fail = true;
    type(m.query, "boots");
    await settle();
    expect(all(m.container, ".metadata-row")).toEqual([]);
    expect(m.status.dataset.kind).toBe("failed");
    expect(m.status.textContent).not.toMatch(/\b2\b/);
    expect(m.error.hidden).toBe(false);
  });
});
