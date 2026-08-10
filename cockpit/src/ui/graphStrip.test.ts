/**
 * The graph pane's EMPTY-STATE DECISION (DECISION D-4), as a pure table.
 *
 * Node environment on purpose: everything asserted here is a decision, not a paint. The
 * painting half lives in `graphStrip.dom.test.ts` under happy-dom, the same split
 * `corpusBar.test.ts` / `corpusBar.dom.test.ts` uses.
 *
 * WHAT THIS FILE IS PROTECTING. Under DECISION I-2 the layout is a permanent split, so the
 * graph pane is on screen even when the corpus can never fill it — and under DECISION G-1 a
 * plain downloaded export is the guaranteed input. Those collide: an export-only corpus has
 * ZERO spawn nodes, so a stranger's graph pane is empty BY CONSTRUCTION. The old rule
 * (`graph/forest.ts` `graphEmptyLabel`) had only two answers and would have told that user to
 * "Use “Open corpus…”" — advice to attach the corpus they already attached. Confirmed against
 * the engine rather than assumed: the export ingest path calls
 * `corpus.add_conversation(conn, conv, thread_id="", …)` and never `add_thread`/`_add_edge`
 * (`llm_anthology/loaders.py:1147`, inside `_admit_export_conversation`), so such a corpus
 * genuinely holds conversations with no threads and no edges.
 */
import { describe, expect, it } from "vitest";

import { FOREST_EMPTY_LABEL, FOREST_INCOMPLETE_LABEL } from "../graph/forest";
import {
  EMPTY_CORPUS_LABEL,
  NO_LINEAGE_LABEL,
  graphStripState,
  type GraphStripInput,
} from "./graphStrip";

/** A drawn graph over an attached, lineage-bearing corpus — the ordinary case. */
function graphInput(over: Partial<GraphStripInput> = {}): GraphStripInput {
  return {
    nodeCount: 12,
    complete: true,
    conversations: 15,
    discoveryShowing: false,
    ...over,
  };
}

describe("graphStripState — when there IS a graph", () => {
  it("draws the canvas and says nothing", () => {
    expect(graphStripState(graphInput())).toEqual({ mode: "graph", reason: "none", text: "" });
  });

  it("still draws when the forest is INCOMPLETE but drew something", () => {
    // `loadForest` returns an EMPTY forest when it declines, never a partial one, so
    // `complete: false` with nodes can only mean a subtree render (`focusThread`), which is
    // complete for what it claims to show. Anything drawn beats any strip.
    expect(graphStripState(graphInput({ complete: false })).mode).toBe("graph");
  });

  it("draws even with no stats at all — one node is proof of a graph", () => {
    // A failed `corpus.stats` must never blank a pane that has nodes in it.
    expect(graphStripState(graphInput({ conversations: null })).mode).toBe("graph");
  });

  it("draws while auto-discovery is on screen — the strip suppression is not a hide", () => {
    expect(graphStripState(graphInput({ discoveryShowing: true }))).toEqual({
      mode: "graph",
      reason: "none",
      text: "",
    });
  });
});

describe("graphStripState — the four empty states", () => {
  it("collapses to a strip that says to attach a corpus when NOTHING is attached", () => {
    // `conversations: null` is "no stats" — either nothing is attached or the read failed.
    // The advice is the same in both, and it is the ONE case where "Open corpus…" is right.
    expect(graphStripState(graphInput({ nodeCount: 0, conversations: null }))).toEqual({
      mode: "strip",
      reason: "no-corpus",
      text: FOREST_EMPTY_LABEL,
    });
  });

  it("says the corpus is empty when it is attached and holds nothing", () => {
    const state = graphStripState(graphInput({ nodeCount: 0, conversations: 0 }));
    expect(state.reason).toBe("empty-corpus");
    expect(state.text).toBe(EMPTY_CORPUS_LABEL);
    // Not the attach-a-corpus text: one IS attached, and re-opening it changes nothing.
    expect(state.text).not.toBe(FOREST_EMPTY_LABEL);
  });

  it("says there is NO SPAWN LINEAGE when the corpus holds conversations but no graph", () => {
    // The DECISION D-4 case, and the one an ordinary user actually lands in.
    const state = graphStripState(graphInput({ nodeCount: 0, conversations: 1_284 }));
    expect(state).toEqual({ mode: "strip", reason: "no-lineage", text: NO_LINEAGE_LABEL });
    expect(state.text).not.toBe(FOREST_EMPTY_LABEL);
  });

  it("says the forest was DECLINED when a ceiling stopped the walk", () => {
    // Reason precedence: `complete: false` outranks every stats-derived reason, because the
    // corpus demonstrably HAS lineage — it has too much of it to lay out in one pass.
    expect(graphStripState(graphInput({ nodeCount: 0, complete: false }))).toEqual({
      mode: "strip",
      reason: "declined",
      text: FOREST_INCOMPLETE_LABEL,
    });
  });

  it("keeps DECLINED ahead of the no-corpus reason when stats are missing too", () => {
    const state = graphStripState(graphInput({ nodeCount: 0, complete: false, conversations: null }));
    expect(state.reason).toBe("declined");
  });
});

describe("graphStripState — auto-discovery suppression", () => {
  /**
   * The rule the CSS used to own: `#graph-pane:has(#discovery:not(:empty))[data-empty]::after
   * { content: none }` (`styles.css:618`). It exists because on a first run the discovery
   * panel is on screen saying "here are the corpora I found" in the same pane, and the strip
   * would print a second, weaker version of that immediately above it.
   *
   * It suppresses the TEXT and nothing else: the mode stays `strip` so the canvas is still
   * collapsed (an empty canvas under the panel is the exact black rectangle D-4 removes) and
   * the reason is still reported, so the pane's own attribute stays honest.
   */
  it("drops the strip TEXT while the discovery panel is showing", () => {
    for (const conversations of [null, 0, 1_284]) {
      const state = graphStripState(graphInput({ nodeCount: 0, conversations, discoveryShowing: true }));
      expect(state.mode).toBe("strip");
      expect(state.text).toBe("");
    }
  });

  it("keeps reporting the real reason while suppressed", () => {
    expect(
      graphStripState(graphInput({ nodeCount: 0, conversations: 1_284, discoveryShowing: true })).reason,
    ).toBe("no-lineage");
    expect(
      graphStripState(graphInput({ nodeCount: 0, conversations: null, discoveryShowing: true })).reason,
    ).toBe("no-corpus");
    expect(
      graphStripState(graphInput({ nodeCount: 0, complete: false, discoveryShowing: true })).reason,
    ).toBe("declined");
  });
});

describe("the strip's own text", () => {
  it("names WHY there is no lineage and WHAT would populate it", () => {
    // Both halves are the decision, not decoration: "no spawn tree" alone reads as a defect,
    // and a cause with no next step leaves the user with nothing to do.
    expect(NO_LINEAGE_LABEL).toMatch(/session store/i);
    expect(NO_LINEAGE_LABEL).toMatch(/export/i);
    // And it must NOT send them back to the corpus control they already used.
    expect(NO_LINEAGE_LABEL).not.toMatch(/Open corpus/);
  });

  it("stays one strip-sized line, not a paragraph", () => {
    // A strip is a line of chrome. Both labels are held to the same budget as the two the
    // graph pane already ships (FOREST_INCOMPLETE_LABEL is the longest at ~230 chars).
    expect(NO_LINEAGE_LABEL.length).toBeLessThanOrEqual(240);
    expect(EMPTY_CORPUS_LABEL.length).toBeLessThanOrEqual(240);
    for (const label of [NO_LINEAGE_LABEL, EMPTY_CORPUS_LABEL]) {
      expect(label).not.toContain("\n");
    }
  });

  it("tells the empty corpus how to fill it", () => {
    expect(EMPTY_CORPUS_LABEL).toMatch(/import|ingest/i);
  });
});
