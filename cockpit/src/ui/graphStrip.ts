/**
 * The graph pane's honest empty state (DECISION D-4): when there is nothing to draw, the
 * pane COLLAPSES to a slim explanatory strip instead of holding an empty canvas.
 *
 * WHY THIS EXISTS. DECISION I-2 makes the layout a permanent split — list and graph side by
 * side, always — and DECISION G-1 makes a plain downloaded export the guaranteed input. Those
 * two collide: an export-only corpus carries no parent/child session relationships at all, so
 * its spawn graph has ZERO nodes and a stranger's graph pane is empty BY CONSTRUCTION, not by
 * accident. That is verified in the engine rather than assumed — the export ingest path admits
 * each conversation with `corpus.add_conversation(conn, conv, thread_id="", …)` and never calls
 * `add_thread` or `_add_edge` (`llm_anthology/loaders.py:1101-1152`), while the session-store
 * path does both (`loaders.py:567-569`). Spawn lineage is a property of an agent SESSION STORE,
 * and no export format has it.
 *
 * A big black rectangle with centred text reads as a broken renderer; a one-line strip reads as
 * information. So the canvas is HIDDEN (not merely unfilled) and one line of chrome takes its
 * place, saying why the pane is blank and what would fill it.
 *
 * WHAT WAS WRONG BEFORE, precisely. The old rule had exactly two answers
 * (`graph/forest.ts` `graphEmptyLabel`: attach-a-corpus, or the walk-declined text), so the
 * commonest real case — a corpus that IS attached and simply has no lineage — was told to
 * "Use “Open corpus…” in the top bar", i.e. to attach the corpus it already had. That is not a
 * cosmetic miss: it sends the user to re-open a file that was never the problem, and it reads
 * as the app failing to see their data.
 *
 * SPLIT, as `corpusBar` is: {@link graphStripState} is the whole decision and is pure, so the
 * table is testable under this project's DOM-less default vitest environment; {@link GraphStrip}
 * only paints. Every string a user reads is written with `textContent`.
 *
 * TWO LIMITS, STATED INLINE.
 *
 *  1. The pane's grid COLUMN is not narrowed. `#app` is
 *     `grid-template-columns: 300px 1fr 300px` (`styles.css:110`), so the middle column keeps
 *     its 1fr share and the collapse is of the pane's CONTENT — canvas gone, strip in its
 *     place, background below. `styles.css` is outside this unit's file scope, so the rule that
 *     would finish the job is left to a follow-up; the pane carries
 *     `data-graph-strip="<reason>"` precisely so that rule can be written without touching this
 *     module. UNVERIFIED here: how the narrowed layout looks. The experiment that settles it is
 *     a `#app:has(#graph-pane[data-graph-strip]) { grid-template-columns: … }` rule plus a
 *     screenshot from `cockpit/tools/`.
 *  2. The strip's own look is set through CSSOM (`el.style.x = …`) rather than a class, for the
 *     same scope reason. CSSOM is deliberate, not lazy: the shipped CSP is `style-src 'self'`
 *     with no `'unsafe-inline'` (`src-tauri/tauri.conf.json:23`), which blocks a `<style>`
 *     element and a `style="…"` ATTRIBUTE but not CSSOM mutation — the same seam `app.ts`
 *     already relies on for provider dots. Every declaration reads a `:root` token, so a
 *     follow-up can delete the block and style `.graph-strip` instead with no visual change.
 *     UNVERIFIED here: that the strip renders as intended in the real webview. The experiment
 *     is `node tools/probe_csp.mjs` (which fails on any CSP violation) plus a screenshot.
 */

import { graphEmptyLabel } from "../graph/forest";

// ---------------------------------------------------------------------------
// the decision
// ---------------------------------------------------------------------------

/** Whether the pane draws its canvas, or collapses to the strip. */
export type GraphPaneMode = "graph" | "strip";

/**
 * WHY the pane is collapsed. Reported onto the pane as `data-graph-strip`, so a probe or a
 * later CSS rule can key on the state without re-deriving it (and so a screenshot review can
 * tell the four blank panes apart, which is otherwise impossible).
 */
export type GraphStripReason =
  /** There is a graph; the canvas is drawn. */
  | "none"
  /** No corpus attached, or `corpus.stats` did not answer. */
  | "no-corpus"
  /** A corpus is attached and holds no conversations at all. */
  | "empty-corpus"
  /** Conversations, but no spawn lineage — the export case DECISION D-4 is about. */
  | "no-lineage"
  /** `loadForest` declined to draw rather than show part of a forest as if it were all. */
  | "declined";

/**
 * The corpus is attached and holds conversations, but no spawn lineage.
 *
 * Two halves, both load-bearing: the CAUSE (so a blank pane does not read as a defect) and the
 * NEXT STEP (so the user is not merely informed that they are stuck). It deliberately does not
 * mention the corpus control — that is the advice this whole decision exists to stop giving.
 */
export const NO_LINEAGE_LABEL =
  "No spawn lineage in this corpus. Parent/child links between sessions are recorded only in "
  + "agent session stores — a downloaded ChatGPT or Claude export carries none. Ingest a Codex "
  + "or Claude Code session store to draw a tree here.";

/**
 * A corpus is attached and there is genuinely nothing in it.
 *
 * Distinct from {@link NO_LINEAGE_LABEL} because the fix is different: this one needs an
 * ingest of anything at all, that one has already ingested and cannot be fixed by ingesting
 * more of the same kind.
 */
export const EMPTY_CORPUS_LABEL =
  "This corpus is empty — nothing has been ingested into it yet. Import a session store or an "
  + "export to fill it.";

/** Everything the decision needs, and nothing that would let it read the DOM. */
export interface GraphStripInput {
  /** Nodes the layout actually produced. */
  nodeCount: number;
  /** `Forest.complete` — false when `loadForest` returned empty rather than partial. */
  complete: boolean;
  /**
   * `corpus.stats().conversations`, or null when there are no stats.
   *
   * Null covers BOTH "nothing attached" and "the stats read failed", on purpose: the engine
   * answers every read with "no corpus attached" until one is, so the two are the same
   * situation from here and the advice ("attach a corpus") is right for both.
   */
  conversations: number | null;
  /** True while the auto-discovery panel is on screen in this same pane. */
  discoveryShowing: boolean;
}

/** What the pane should do, and say. */
export interface GraphStripState {
  mode: GraphPaneMode;
  reason: GraphStripReason;
  /** The strip's single line; empty when there is nothing to say. */
  text: string;
}

/**
 * Decide the pane's state.
 *
 * Precedence, and each step is a rule rather than an ordering accident:
 *
 *   1. ANY drawn node wins. A subtree render (`focusThread`) is complete for what it claims to
 *      show even when the whole-forest walk was not, and a failed `corpus.stats` must never
 *      blank a pane that has nodes in it.
 *   2. `!complete` outranks every stats-derived reason: a corpus that declined the walk
 *      demonstrably HAS lineage — too much of it to lay out in one pass — so telling that user
 *      their corpus has none would be false.
 *   3. Then the stats: no stats -> attach one; zero conversations -> ingest something;
 *      conversations but no graph -> no lineage.
 *
 * The two pre-existing strings come from `graphEmptyLabel`, called rather than copied, so this
 * module does not become a second place those sentences live.
 */
export function graphStripState(input: GraphStripInput): GraphStripState {
  const label = graphEmptyLabel(input.nodeCount, input.complete);
  if (label === null) return { mode: "graph", reason: "none", text: "" };
  const [reason, text] = reasonFor(input, label);
  // Suppressed, not hidden: the canvas stays collapsed and the reason stays reported. The
  // discovery panel is already saying "here are the corpora I found" in this pane, and a
  // second, weaker version of that immediately above it is noise. This is the rule
  // `styles.css:618` owned as `:has(#discovery:not(:empty))[data-empty]::after {content:none}`,
  // moved here with the mechanism it qualifies.
  return { mode: "strip", reason, text: input.discoveryShowing ? "" : text };
}

/** The reason/text pair for an empty pane. `label` is whichever string `graphEmptyLabel` chose. */
function reasonFor(input: GraphStripInput, label: string): [GraphStripReason, string] {
  if (!input.complete) return ["declined", label];
  if (input.conversations === null) return ["no-corpus", label];
  if (input.conversations === 0) return ["empty-corpus", EMPTY_CORPUS_LABEL];
  return ["no-lineage", NO_LINEAGE_LABEL];
}

// ---------------------------------------------------------------------------
// the paint
// ---------------------------------------------------------------------------

/**
 * The strip's declarations, each reading a `:root` token so the strip cannot drift from the
 * theme. Applied through CSSOM for the CSP reason in this module's header.
 *
 * LONGHANDS ONLY, and that is measured rather than stylistic: a multi-value SHORTHAND carrying
 * a `var()` is mangled by happy-dom's CSSOM — `border-bottom: 1px solid var(--border)` reads
 * back as `var(--border) var(--border) var(--border)`, i.e. the declaration is destroyed, not
 * merely stored oddly. A real browser defers `var()` substitution and accepts the shorthand, so
 * this would have shipped working and been untestable; longhands are correct in both engines and
 * are what the DOM test can therefore pin. Spacing values are still the 4px-grid tokens, so the
 * strip stays on the grid the theme defines.
 */
const STRIP_STYLE: ReadonlyArray<readonly [string, string]> = [
  ["margin", "0"],
  ["padding-top", "var(--space-3)"],
  ["padding-bottom", "var(--space-3)"],
  ["padding-left", "var(--space-4)"],
  ["padding-right", "var(--space-4)"],
  ["background-color", "var(--panel)"],
  ["border-bottom-width", "1px"],
  ["border-bottom-style", "solid"],
  ["border-bottom-color", "var(--border)"],
  // --muted on --panel is 5.4:1, past the AA 4.5:1 bar for body text (`styles.css:19`).
  ["color", "var(--muted)"],
  ["font-size", "12px"],
  ["line-height", "1.5"],
  // A strip is chrome, so it must not stretch to fill the pane's flex column.
  ["flex", "0 0 auto"],
];

/**
 * The pane's painting half: hides the canvas and shows one line, or the reverse.
 *
 * The strip is inserted BEFORE the canvas so reading and Tab order stay
 * toolbar -> scrubber -> discovery -> strip, and it is created ONCE with only its
 * `textContent` replaced: it is a `role="status"` live region, and replacing the element would
 * land each message in a node that was not yet in the accessibility tree, so none would be
 * announced. (Same reasoning as `maintenanceShell`'s output line.)
 */
export class GraphStrip {
  private readonly strip: HTMLElement;

  constructor(
    private readonly pane: HTMLElement,
    private readonly canvas: HTMLElement,
  ) {
    this.strip = document.createElement("p");
    this.strip.className = "graph-strip";
    this.strip.setAttribute("role", "status");
    this.strip.setAttribute("aria-live", "polite");
    setHidden(this.strip, true);
    for (const [prop, value] of STRIP_STYLE) this.strip.style.setProperty(prop, value);
    this.canvas.before(this.strip);
  }

  /**
   * Apply a decided state.
   *
   * Both elements are hidden through {@link setHidden}, which is where the cascade trap that
   * makes a plain `hidden` insufficient is written down.
   */
  apply(state: GraphStripState): void {
    const collapsed = state.mode === "strip";
    setHidden(this.canvas, collapsed);
    if (collapsed) this.pane.dataset.graphStrip = state.reason;
    else delete this.pane.dataset.graphStrip;
    // An empty live region is announced by nothing and rendered as a 0-height band; the
    // suppressed case has to leave no trace at all rather than an empty bordered strip.
    this.strip.textContent = state.text;
    setHidden(this.strip, state.text === "");
  }
}

/**
 * Hide or show `el` in a way that survives an author `display` rule.
 *
 * `hidden` ALONE DOES NOT HIDE AN ELEMENT THAT AN AUTHOR RULE GIVES A `display`. The
 * `[hidden] { display: none }` that gives the attribute its effect lives in the UA stylesheet,
 * and author origin beats UA origin regardless of specificity. `#tree-canvas { display: block }`
 * (`styles.css:445-452`) is exactly such a rule, so the collapse silently did nothing until
 * this existed — and `styles.css` already carries EIGHT explicit `[hidden]` rules
 * (`#reader[hidden]`, `#workspace[hidden]`, `.workspace-pane[hidden]`, …) because every element
 * with an author `display` has needed the same repair. The strip is hidden this way too, even
 * though nothing styles `.graph-strip` today, because the follow-up this module asks for is a
 * rule for exactly that class.
 *
 * Both halves are kept: the ATTRIBUTE is what removes the element from the accessibility tree
 * (the canvas carries `role="img"` and a label naming a spawn-tree graph, `index.html:111`) and
 * is what a headless probe can read; the inline DECLARATION is what actually stops it painting.
 *
 * On show the property is REMOVED, never set to `block`: writing the stylesheet's current choice
 * into an inline declaration would freeze it, and a later CSS change would silently not apply.
 *
 * Neither happy-dom nor jsdom does the cascade, so no unit test can observe the override itself.
 * `graphStrip.dom.test.ts` pins the declaration; the experiment that would settle the rendering
 * is a screenshot from `cockpit/tools/` against the real webview.
 */
function setHidden(el: HTMLElement, hidden: boolean): void {
  el.hidden = hidden;
  if (hidden) el.style.setProperty("display", "none");
  else el.style.removeProperty("display");
}
