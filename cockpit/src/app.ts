/**
 * The cockpit shell: wires the mock/real {@link ipc} to the three views —
 *   * a virtualized THREAD list (roots) in the sidebar,
 *   * the CANVAS spawn-tree (ELK-laid-out) in the main pane,
 *   * a debounced SEARCH panel,
 * plus a detail panel fed by canvas selection.
 *
 * Everything talks to `ipc` only; flipping to the real sidecar is the one-line switch
 * in `./ipc/index.ts`. This module builds+runs against the mock forest today.
 */

import { ipc } from "./ipc";
import type { SearchHit, ThreadMeta, ThreadNode } from "./ipc";
// Direct from the wire contract: `./ipc` (the adapter selector) re-exports the DTOs the
// app already used, and widening its surface is a change to a module outside this unit.
import type { CorpusStats, ExportMode, HealthInfo } from "./ipc/types";
import { isMoreId, moreParentId } from "./graph/capFanOut";
import { buildView, loadAllRoots, loadForest, rootsStatus } from "./graph/forest";
import { SpawnTreeCanvas } from "./graph/canvas";
import { diffToOverlay } from "./graph/diffOverlay";
import { ElkLayoutEngine, LayoutTimeoutError } from "./graph/elkLayout";
import {
  buildElkGraph,
  extractLayout,
  type LayoutInput,
  type PositionedGraph,
} from "./graph/layout";
import { knownProviders, providerTint } from "./graph/palette";
import { CorpusBar, corpusLabel, localCorpusStore, type CorpusStore } from "./ui/corpusBar";
import { DedupPanel } from "./ui/dedupPanel";
import { mountDiagnosticsButton, systemClipboardWrite } from "./ui/diagnostics";
import { DiscoveryPanel } from "./ui/discoveryPanel";
import { engineErrorText, engineStatusText } from "./ui/errors";
import { asExportMode, ExportPanel, exportIpcFrom, renderView } from "./ui/exportPanel";
import { GraphStrip, graphStripState } from "./ui/graphStrip";
import { MaintenanceGate } from "./ui/maintenanceGate";
import { MaintenanceShell } from "./ui/maintenanceShell";
import { MetadataPanel } from "./ui/metadataPanel";
import { ReaderOverlay } from "./ui/reader";
import { SearchPanel } from "./ui/search";
import { TimeScrubber } from "./ui/scrubber";
import { VirtualList } from "./ui/virtualList";
import { Workspace } from "./ui/workspace";

const ROOT_ROW_HEIGHT = 52;

/*
 * The graph pane's empty state is DECISION D-4 and lives in `ui/graphStrip.ts`: when there is
 * nothing to draw the pane collapses to a slim explanatory strip, and the canvas is hidden
 * rather than left blank. That replaces the `data-empty` overlay this pane used to share with
 * the virtualized lists — not because the mechanism was wrong for a list, but because it cannot
 * express the case a permanent split layout (DECISION I-2) makes ordinary: a corpus imported
 * from a plain export (DECISION G-1) has no spawn lineage at all, and the two-answer rule sent
 * that user to re-open the corpus they had already opened. The lists keep `data-empty`.
 */

/** A cleared canvas, for the transition out of a graph that no longer has any nodes. */
const EMPTY_GRAPH: PositionedGraph = { nodes: [], edges: [], width: 0, height: 0 };

function requireEl<T extends HTMLElement>(id: string): T {
  const el = document.getElementById(id);
  if (el === null) throw new Error(`cockpit: missing #${id} in index.html`);
  return el as T;
}

/**
 * The thread list's status line, mounted above `#roots-list`.
 *
 * Built here rather than declared in `index.html` for the same reason the search panel's
 * `#search-status` is declared there and this one is not: the markup is out of this change's
 * scope, and the element needs no styling of its own — `.muted` and the `role`/`aria-live`
 * pair are exactly what `#search-status` already uses, so the two status lines look and
 * announce identically. `#sidebar` is a flex column, so inserting a self-sizing child above
 * the list shifts nothing else.
 */
function mountRootsStatus(list: HTMLElement): HTMLElement {
  const el = document.createElement("div");
  el.id = "roots-status";
  el.className = "muted";
  el.setAttribute("role", "status");
  el.setAttribute("aria-live", "polite");
  list.before(el);
  return el;
}

export class CockpitApp {
  private readonly canvas: SpawnTreeCanvas;
  private readonly engine = new ElkLayoutEngine();
  private readonly rootsList: VirtualList<ThreadNode>;
  /** Says how many threads the list holds — and, when a ceiling cut the walk, that it did. */
  private readonly rootsStatusEl = mountRootsStatus(requireEl("roots-list"));
  private readonly search: SearchPanel;

  private readonly corpusBar: CorpusBar;
  /** The first-run auto-discovery surface; null when `#discovery` is absent. */
  private readonly discovery: DiscoveryPanel | null;
  /** Remembers the last attached index, so the discovery panel can update it too. */
  private readonly corpusStore: CorpusStore = localCorpusStore();
  private readonly corpusLabelEl = requireEl("corpus-current");

  /**
   * The attached index path, or null when nothing is attached.
   *
   * Tracked HERE rather than read off `CorpusBar`, which exposes no such getter: the
   * `onOpened` callback fires only after a successful attach, so it is an exact signal,
   * and boot needs it to decide whether a scan is even warranted.
   */
  private attachedIndex: string | null = null;

  private readonly healthEl = requireEl("health");
  private readonly statsEl = requireEl("stats");

  /**
   * The last successful `health` / `stats` answers, RETAINED rather than only painted.
   *
   * `loadHealth` and `loadStats` used to reduce each answer straight to a string, so the
   * numbers existed only as topbar text and the diagnostics bundle had nothing to report.
   * Both are set back to null when their load FAILS, following `conversationCount`'s
   * precedent immediately below: carrying a previous corpus's numbers forward would put
   * figures in a bug report that no longer describe anything the reporter is looking at.
   */
  private lastHealth: HealthInfo | null = null;
  private lastStats: CorpusStats | null = null;
  private readonly graphPaneEl = requireEl("graph-pane");
  /** Collapses the pane to a slim explanatory strip when there is nothing to draw (D-4). */
  private readonly graphStrip: GraphStrip;
  private readonly graphStatusEl = requireEl("graph-status");
  private readonly detailEl = requireEl("detail");
  private readonly aggregateBtn = requireEl<HTMLButtonElement>("btn-aggregate");
  private readonly diffBtn = requireEl<HTMLButtonElement>("btn-diff");

  /**
   * The base (expanded) graph currently displayed, remembered so the aggregated
   * toggle and the diff overlay can re-derive from it without a refetch.
   */
  private currentInput: (LayoutInput & { complete?: boolean }) | null = null;
  /** Whether {@link currentInput} is the WHOLE graph; drives which empty state shows. */
  private currentComplete = true;
  /**
   * `corpus.stats().conversations`, or null when there are no stats.
   *
   * The signal that separates the two blank panes a user can actually reach: an attached
   * corpus holding conversations but no spawn lineage (the export case) from nothing attached
   * at all. Null on a FAILED stats read as well as before the first one — the engine refuses
   * every read until a corpus is attached, so from here the two are the same situation and
   * "attach a corpus" is the right advice for both.
   */
  private conversationCount: number | null = null;
  /**
   * Nodes the last render drew, so the pane's state can be re-decided without re-rendering.
   *
   * Needed because one of the decision's inputs changes OUTSIDE a render: auto-discovery
   * paints into this same pane after `reload()` has already finished.
   */
  private graphNodeCount = 0;
  private currentSelect: string | null = null;
  private readonly reader: ReaderOverlay;
  /** The annotations editor. Its subject is set from whichever search hit is selected. */
  private readonly metadata: MetadataPanel;
  /** Reveals the three panels that do not fit a 300px column. */
  private readonly workspace: Workspace;
  /** The opt-in that admits the maintenance plane at all (DECISION G-11). */
  private readonly maintenanceGate: MaintenanceGate;
  /**
   * The maintenance plane, or null until it has been revealed once.
   *
   * Held so a second reveal does not build a second copy on top of the first. Deliberately NOT
   * built in the constructor: with the flag off there is no reveal, so there is no plane.
   */
  private maintenance: MaintenanceShell | null = null;
  /**
   * The conversation the annotations editor should be showing.
   *
   * A CONVERSATION id, and only ever taken from a `SearchHit`, which carries one exactly.
   * Deliberately NOT taken from a graph selection: a canvas node id is a THREAD id, and
   * `thread.get` returns no conversation id at all (`ipc/types.ts:97-115`), so treating the
   * two as interchangeable would send a thread id to `metadata.get` and annotate either
   * nothing or the wrong row. They happen to coincide in the mock forest, which is exactly
   * what would make the bug invisible in every preview.
   */
  private metadataSubject: string | null = null;
  /** What the editor last actually loaded, so a re-reveal is not two pointless round trips. */
  private metadataLoaded: string | null = null;
  /** Fold linear chains into super-nodes when true (the aggregated↔expanded toggle). */
  private aggregated = false;
  /** How many nodes the last render hid behind "+N more", for the status line. */
  private hiddenByCap = 0;
  /** Per-placeholder hidden counts from the last render, keyed by `more:<parent>`. */
  private moreCounts = new Map<string, number>();
  /** Tint what changed since the scrub point over the full forest when true. */
  private diffMode = false;
  /** The last scrub position (epoch ms), or null before any scrub. */
  private scrubAsOf: number | null = null;
  private scrubber: TimeScrubber | null = null;
  /** Set the moment {@link reload} starts, so boot never loads the same corpus twice. */
  private loaded = false;

  constructor() {
    const canvasEl = requireEl<HTMLCanvasElement>("tree-canvas");
    this.canvas = new SpawnTreeCanvas(canvasEl);
    this.canvas.setSelectHandler((id) => void this.onNodeSelected(id));
    // Built from the same element the renderer owns, because the collapse is a HIDE of that
    // canvas — the pane's own background is not what read as broken, the empty canvas was.
    this.graphStrip = new GraphStrip(this.graphPaneEl, canvasEl);

    this.rootsList = new VirtualList<ThreadNode>(requireEl("roots-list"), {
      itemHeight: ROOT_ROW_HEIGHT,
      renderRow: (node) => this.renderRootRow(node),
      emptyLabel: "No threads in this corpus yet.",
    });

    this.search = new SearchPanel(
      ipc,
      requireEl<HTMLInputElement>("search-input"),
      requireEl("search-results"),
      requireEl("search-status"),
      // Optional: `requireEl` would throw on a shell without the control, and the panel
      // already treats a null filter as "no filtering offered".
      document.getElementById("search-provider") as HTMLSelectElement | null,
    );
    this.search.setHitHandler((hit) => void this.onHitSelected(hit));

    // The transcript reader. `conversation.get` was implemented on both sides of the wire
    // and never called by anything, so the app could FIND a conversation and not read it --
    // the one thing a session browser exists for.
    this.reader = new ReaderOverlay(ipc, requireEl("reader"));
    this.search.setReadHandler((hit) => void this.reader.open(hit.conversation_id));

    // The app's PRIMARY action. Every pane below reads through the engine, and the
    // engine answers nothing until a corpus is attached — so without this control the
    // app boots into a state it can never leave.
    this.corpusBar = new CorpusBar(
      ipc,
      requireEl<HTMLButtonElement>("btn-open-corpus"),
      this.corpusLabelEl,
      requireEl("corpus-error"),
      (indexPath) => {
        this.attachedIndex = indexPath;
        // An import would now land in THIS corpus rather than a new one, and the panel's
        // row labels have to say so.
        this.discovery?.setCorpusAttached(indexPath);
        void this.reload();
      },
    );

    // Auto-discovery: the other half of the same problem the corpus bar solves. The bar
    // gave the app a control; this removes the need to know a path at all.
    this.discovery = this.mountDiscovery();

    // The three panels that had no import and no container until now. Constructed HERE
    // rather than in a `mount…` helper because the fields are readonly and TypeScript only
    // permits assigning those from the constructor itself.
    // CF-19. DECISION G-8's in-app half: one button that puts a filed-able bundle on the
    // clipboard. `mountDiagnosticsButton` existed, fully tested, with ZERO production callers.
    //
    // The dep is `ipc.appInfo`, NOT a raw Tauri `invoke`. That distinction is the reason this
    // sat unmounted: the old `invoke: (command) => …` shape could only be satisfied by the raw
    // boundary, and reaching past `ipc` re-creates the failure `ipc/index.ts:5-16` records —
    // a hardcoded real-IPC path that rendered every pane dead outside the Tauri webview.
    //
    // `snapshot` is a CLOSURE, not a captured value: it is called at click time, so a bundle
    // filed after attaching a corpus reports that corpus rather than the empty state the app
    // booted into.
    mountDiagnosticsButton(requireEl("topbar"), {
      appInfo: () => ipc.appInfo(),
      snapshot: () => ({
        health: this.lastHealth,
        stats: this.lastStats,
        indexPath: this.attachedIndex,
      }),
      copy: systemClipboardWrite,
    });

    const dedup = new DedupPanel(ipc, requireEl("dedup-panel"));
    this.metadata = new MetadataPanel(ipc, requireEl("metadata-panel"));
    // The other direction of the same seam: picking an annotation result opens that
    // conversation's transcript. The reader is a fixed overlay at a higher z-index, so it
    // opens OVER the workspace and closing it returns the user to their results.
    this.metadata.setOnPick((conversationId) => void this.reader.open(conversationId));
    // NOT built here. The maintenance plane is flag-gated OFF by default (DECISION G-11) and is
    // constructed on its FIRST reveal instead — see `mountMaintenance`. With the flag off its
    // button is hidden, so that reveal cannot happen and the plane is never instantiated at
    // all: "nothing on the floor touches it" is then a fact about the object graph, not a
    // promise about behaviour.

    this.workspace = new Workspace(
      requireEl("workspace"),
      requireEl("workspace-title"),
      requireEl<HTMLButtonElement>("workspace-close"),
      [
        {
          key: "dedup",
          button: requireEl<HTMLButtonElement>("btn-dedup"),
          container: requireEl("dedup-panel"),
          title: "Duplicate session copies",
          // First reveal only. `load()` calls `discover.sources`, which walks the
          // filesystem; it also deliberately does NOT scan — a scan reads the owner's live
          // Codex sessions and is always their own click (`ui/dedupPanel.ts:641-644`).
          onShow: (firstShow) => {
            if (firstShow) void dedup.load();
          },
        },
        {
          key: "metadata",
          button: requireEl<HTMLButtonElement>("btn-metadata"),
          container: requireEl("metadata-panel"),
          // The panel shows the alias, tags and notes but never says WHOSE they are, so the
          // region's name carries the subject. Nothing here re-derives what the panel
          // computes — the panel computes no such label.
          title: () =>
            this.metadataSubject === null
              ? "Annotations"
              : `Annotations — ${this.metadataSubject}`,
          onShow: () => void this.syncMetadata(),
        },
        {
          key: "maintenance",
          button: requireEl<HTMLButtonElement>("btn-maintenance"),
          container: requireEl("maintenance-panel"),
          title: "Maintenance",
          // Built on first reveal, which the flag gate controls (DECISION G-11).
          onShow: (firstShow) => {
            if (firstShow) this.mountMaintenance();
          },
        },
      ],
      // The reader listens for Escape on `document` too, and it is the one on top. Without
      // this, one press would close the reader AND the panel it was opened from.
      () => this.reader.isOpen,
    );

    // AFTER the workspace, deliberately: revoking the flag has to be able to close a pane that
    // is currently open, and the gate's callback would otherwise fire against an unassigned
    // `this.workspace`. (It never fires on construction — see `MaintenanceGate` — so this
    // ordering is a belt on top of that, not the thing holding it up.)
    this.maintenanceGate = new MaintenanceGate({
      container: requireEl("workspace-nav"),
      button: requireEl<HTMLButtonElement>("btn-maintenance"),
      onChange: (enabled) => {
        // Revoked while the plane was on screen. Hiding only the BUTTON would leave the whole
        // destructive form live in the workspace region, which is the opposite of gating it.
        if (!enabled && this.workspace.openPane === "maintenance") this.workspace.close();
      },
    });

    requireEl("btn-forest").addEventListener("click", () => void this.showForest());
    requireEl("btn-fit").addEventListener("click", () => this.canvas.fitToView());
    this.aggregateBtn.addEventListener("click", () => void this.toggleAggregate());
    this.diffBtn.addEventListener("click", () => void this.toggleDiff());

    this.mountExportPanel();
    this.renderLegend();
  }

  /**
   * Boot. Re-attaches the corpus remembered from the last session first — a successful
   * restore fires the corpus-bar callback, which is already a full {@link reload}, so
   * the guard below stops boot from loading the same corpus twice. With nothing
   * remembered (or a restore that failed) the engine stays unattached and the load
   * paints the empty states that point at the Open-corpus button.
   */
  async init(): Promise<void> {
    await this.corpusBar.restore();
    if (!this.loaded) await this.reload();
    // Scan ONLY when the restore left us with nothing. A returning user already has their
    // corpus back, and a scan they did not ask for would spend 1.8-7.5s walking their
    // Downloads folder to offer them something they are not looking for.
    if (this.attachedIndex === null) {
      await this.discovery?.scan();
      // The scan paints INTO the graph pane, so the strip's suppression has to be re-decided:
      // the render above ran while `#discovery` was still empty.
      this.refreshGraphPane();
    }
  }

  /**
   * Build the discovery panel over `#discovery`, if the shell has one.
   *
   * The panel attaches corpora itself (it opens a discovered index, and creates one to
   * import into), which the corpus bar cannot be told about — `CorpusBar` exposes no
   * "adopt this path" entry point. So the adoption is completed here instead, reusing the
   * bar's OWN exported `corpusLabel` derivation and storage key rather than re-deriving
   * either: the top-bar label then names the corpus the panel attached, and the next
   * launch restores it exactly as a manual open would.
   */
  private mountDiscovery(): DiscoveryPanel | null {
    const container = document.getElementById("discovery");
    if (container === null) return null;
    return new DiscoveryPanel(ipc, container, (indexPath) => {
      this.adoptCorpus(indexPath);
      void this.reload();
    });
  }

  /**
   * Build the maintenance plane, once, on its first reveal (DECISION G-11).
   *
   * Guarded on the gate as well as on the null field, so a programmatic `show("maintenance")`
   * cannot construct the plane while the flag is off. That is not the primary defence — the
   * button is hidden, which is — but this is the one that holds if a later caller reaches past
   * the button.
   */
  private mountMaintenance(): void {
    if (this.maintenance !== null || !this.maintenanceGate.enabled) return;
    this.maintenance = new MaintenanceShell(ipc, requireEl("maintenance-panel"));
  }

  /** Record an index attached outside the corpus bar, and make the top bar say so. */
  private adoptCorpus(indexPath: string): void {
    this.attachedIndex = indexPath;
    const { label, title } = corpusLabel(indexPath);
    this.corpusLabelEl.textContent = label;
    if (title === "") this.corpusLabelEl.removeAttribute("title");
    else this.corpusLabelEl.setAttribute("title", title);
    this.corpusStore.write(indexPath);
  }

  /**
   * Load — or RE-load — every view from the currently attached corpus: health, stats,
   * roots, rollup badges, the forest, then the time scrubber. Attaching a corpus calls
   * this, so opening one populates the whole UI with no restart.
   */
  async reload(): Promise<void> {
    this.loaded = true;
    // A new corpus has its own timeline; carrying the previous one's scrub position
    // forward would time-travel the new graph to a meaningless instant.
    this.scrubAsOf = null;
    await Promise.all([
      this.loadHealth(),
      this.loadStats(),
      this.loadRoots(),
      this.loadRollup(),
    ]);
    await this.showForest();
    await this.mountScrubber();
  }

  /** Load the per-node subtree aggregates so the canvas can draw count badges. */
  private async loadRollup(): Promise<void> {
    if (ipc.graphRollup === undefined) return;
    try {
      this.canvas.setRollup(await ipc.graphRollup());
    } catch {
      // Badges are a decoration; a rollup failure must not block the graph.
    }
  }

  private async loadHealth(): Promise<void> {
    try {
      const h = await ipc.healthPing();
      this.lastHealth = h;
      this.healthEl.textContent = h.corpus_ready
        ? `engine ${h.engine_version} · IR ${h.ir_version}`
        : "engine idle";
    } catch (err) {
      // The not-attached case is the app's INITIAL STATE, not a fault, and the CorpusBar
      // already reports it — so this must not render the engine's internal
      // "call open_corpus first" text here as well.
      this.lastHealth = null;
      this.healthEl.textContent = engineStatusText(err, "engine unavailable");
    }
  }

  private async loadStats(): Promise<void> {
    try {
      const s = await ipc.corpusStats();
      this.lastStats = s;
      // Recorded before anything is painted: `reload` awaits this alongside the other three
      // loads and only then renders the forest, so the graph pane's empty state is always
      // decided against THIS corpus's stats rather than the previous one's.
      this.conversationCount = s.conversations;
      // The same call feeds the search filter, so its choices are the providers this corpus
      // ACTUALLY holds rather than every provider the app can ingest -- offering "gemini"
      // against a codex-only store is a filter that can only ever return nothing.
      this.search.setProviders(s.providers);
      const providers = Object.entries(s.providers)
        .map(([p, n]) => `${p} ${n}`)
        .join(" · ");
      this.statsEl.textContent = `${s.conversations} conversations · ${s.threads} threads · ${s.edges} edges · ${providers}`;
    } catch (err) {
      // Forget the previous corpus's count. Carrying it forward would let a failed stats read
      // describe a pane as "no spawn lineage" on the strength of a number that no longer
      // refers to anything.
      this.conversationCount = null;
      this.lastStats = null;
      this.statsEl.textContent = engineStatusText(err, "stats unavailable");
    }
  }

  private async loadRoots(): Promise<void> {
    try {
      // EVERY root, not the first 1,000. This list is virtualized, so the rows past the
      // thousandth cost nothing to render — the old `limit: 1000` bounded what the user was
      // ALLOWED TO SEE for no rendering benefit at all. Measured on the build corpus: ~1,140
      // roots (2,112 threads minus 972 distinct children), so 140 threads were unreachable
      // and the list gave no hint that it stopped early. `loadAllRoots` pages to the end of
      // the corpus; `rootsStatus` speaks up when a ceiling stopped it anyway.
      //
      // (It also still asks for "recent" rather than the engine's ASCENDING "created"
      // default — under any cap, `created` returns the OLDEST page and hides every recent
      // month. That ordering lives in `graph/forest.ts` now, next to the paging it qualifies.)
      const { roots, complete } = await loadAllRoots(ipc);
      this.rootsList.setItems(roots);
      this.rootsStatusEl.textContent = rootsStatus(roots.length, complete);
    } catch {
      // With no corpus attached every graph read rejects. Show the list's own empty
      // state instead of rejecting out of the boot chain — `main.ts` fires `init()`
      // with `void`, so that rejection was unhandled and the whole UI stayed blank.
      this.rootsList.setItems([]);
      // And say nothing above it: a stale "1,140 threads" over an empty list would be the
      // very lie this pair exists to prevent.
      this.rootsStatusEl.textContent = "";
    }
  }

  /**
   * Render the whole spawn forest. The fetch decision — one snapshot round-trip, or the
   * legacy per-root walk when the engine has no `graph.at` — lives in `graph/forest.ts`
   * so it can be tested without a DOM.
   */
  private async showForest(): Promise<void> {
    await this.present(await loadForest(ipc, Date.now()), null);
  }

  /** Focus one thread: render just its subtree and select its root. */
  private async focusThread(threadId: string): Promise<void> {
    const sub = await ipc.graphSubtree(threadId);
    await this.present({ nodes: sub.nodes, edges: sub.edges }, threadId);
    // `present` highlights the node through `canvas.select()`, which is documented at
    // `canvas.ts:104` as NOT firing the selection callback — so arriving here from the
    // sidebar or a search hit moved the graph while leaving the detail pane showing the
    // previous thread, or nothing at all. Clicking a thread and being told nothing about it
    // is not a graph feature.
    await this.onNodeSelected(threadId);
  }

  /**
   * Remember `input` as the current base graph and render it — folded into
   * super-nodes when the aggregated toggle is on, expanded otherwise. Every entry
   * point (forest, focus, time-travel) funnels through here so the toggle can
   * re-derive the view from the same base without a refetch.
   */
  private async present(
    input: LayoutInput & { complete?: boolean },
    selectId: string | null,
  ): Promise<void> {
    this.currentInput = input;
    this.currentSelect = selectId;
    // A SUBTREE render (focusThread) is always complete for what it claims to show, so
    // an input with no flag defaults to complete. Only loadForest can decline to draw.
    this.currentComplete = input.complete ?? true;
    // Fold (the aggregated toggle) then bound the widest layer. Both decisions live in
    // `graph/forest.ts`; see `graph/capFanOut.ts` for why the bound is not optional.
    const { view, hiddenCount, moreCounts } = buildView(input, this.aggregated);
    this.hiddenByCap = hiddenCount;
    this.moreCounts = moreCounts;
    await this.renderGraph(view, selectId);
  }

  private async renderGraph(input: LayoutInput, selectId: string | null): Promise<void> {
    // Decide the pane FIRST, and for a second reason beyond tidiness: when this render is the
    // one that ends a collapse, the canvas has to have a real box before `setGraph` fits a
    // graph to it. `fitToView` reads a size only the `ResizeObserver` refreshes
    // (`graph/canvas.ts:175-182`), and the awaited ELK round-trip below is the gap in which
    // that observer fires. Revealing after the fit would fit to a 0x0 viewport.
    this.applyGraphPaneState(input.nodes.length);
    if (input.nodes.length === 0) {
      // Nothing to lay out. Clear the canvas (a previous corpus may still be drawn on it)
      // rather than running ELK over an empty graph, which yields a zero-size layout.
      this.canvas.setGraph(EMPTY_GRAPH, false);
      this.graphStatusEl.textContent = "";
      return;
    }
    this.graphStatusEl.textContent = `laying out ${input.nodes.length} nodes…`;
    try {
      const laid = await this.engine.layout(buildElkGraph(input));
      const positioned = extractLayout(laid, input);
      this.canvas.setGraph(positioned);
      if (selectId !== null) this.canvas.select(selectId);
      const crossCount = positioned.edges.filter((e) => e.cross).length;
      // Say how many nodes are behind a "+N more". The graph becomes navigable by ceasing to
      // be complete, and a user who is not told that will read a capped view as their whole
      // corpus — on the measured store that would be ~102 nodes standing for 12,791.
      const hidden = this.hiddenByCap > 0 ? ` · ${this.hiddenByCap} hidden behind “+N more”` : "";
      this.graphStatusEl.textContent = `${positioned.nodes.length} nodes · ${positioned.edges.length} edges · ${crossCount} cross-provider${hidden}`;
    } catch (err) {
      const msg =
      err instanceof LayoutTimeoutError ? err.message : engineErrorText(err, "layout failed");
      this.graphStatusEl.textContent = msg;
    }
  }

  /**
   * Hand the pane its state (DECISION D-4): draw the canvas, or collapse to the strip.
   *
   * Every input is gathered here and the decision itself is `graphStripState`, which is pure
   * and tested — this method exists only because three of the four inputs live on `this` and
   * the fourth is a DOM read.
   */
  private applyGraphPaneState(nodeCount: number): void {
    this.graphNodeCount = nodeCount;
    this.graphStrip.apply(graphStripState({
      nodeCount,
      complete: this.currentComplete,
      conversations: this.conversationCount,
      discoveryShowing: this.discoveryShowing(),
    }));
  }

  /**
   * Whether auto-discovery is painting into this pane right now.
   *
   * A child-node count, which is the exact mirror of the `#discovery:not(:empty)` selector the
   * CSS used for the same suppression (`styles.css:618`): `DiscoveryPanel` leaves the container
   * genuinely EMPTY when it has nothing to show, and `index.html` declares it with no
   * whitespace inside, so "has any child node" and ":not(:empty)" agree here.
   */
  private discoveryShowing(): boolean {
    const el = document.getElementById("discovery");
    return el !== null && el.childNodes.length > 0;
  }

  /**
   * Re-decide the pane without re-rendering the graph.
   *
   * For the one input that changes outside a render: the first-run scan paints into this pane
   * AFTER `reload()` has finished, so the suppression above was decided before there was
   * anything to suppress it.
   */
  private refreshGraphPane(): void {
    this.applyGraphPaneState(this.graphNodeCount);
  }

  private async onNodeSelected(id: string | null): Promise<void> {
    if (id === null) {
      this.detailEl.replaceChildren();
      return;
    }
    if (isMoreId(id)) {
      // A "+N more" placeholder is synthetic — `thread.get` would 404 on it and the catch
      // below would then describe it as a dangling edge target, which is a different and
      // wrong thing to tell the user.
      this.renderMoreDetail(id);
      return;
    }
    try {
      const meta = await ipc.threadGet(id);
      this.renderDetail(meta);
    } catch {
      // A dangling node (edge-only) has no thread row; show what the edge implies.
      this.renderDanglingDetail(id);
    }
  }

  private async onHitSelected(hit: SearchHit): Promise<void> {
    // A hit is the one place in this app that carries BOTH ids, so it is the only honest
    // source for the annotations editor's subject. `thread_id` drives the graph; the
    // conversation id — never the node id — drives `metadata.*`.
    this.metadataSubject = hit.conversation_id;
    await this.focusThread(hit.thread_id ?? hit.conversation_id);
    // Only when the pane is already on screen. Loading an annotation for a panel nobody has
    // opened would spend two round trips per click on something invisible; the pane's own
    // `onShow` picks the subject up when it is next revealed.
    if (this.workspace.openPane === "metadata") await this.syncMetadata();
  }

  /**
   * Point the annotations editor at {@link metadataSubject}, if that is not already what it
   * is showing.
   *
   * `open()` is two engine calls (the annotation, then the tag facet), so the
   * already-loaded guard is what makes it safe to call on every reveal. With no hit selected
   * yet this does nothing and the panel stays in its own idle state — still usable, because
   * its annotation search needs no subject.
   */
  private async syncMetadata(): Promise<void> {
    const subject = this.metadataSubject;
    if (subject === null || subject === this.metadataLoaded) return;
    this.metadataLoaded = subject;
    await this.metadata.open(subject);
    this.workspace.refreshTitle();
  }

  // -- aggregated↔expanded toggle ----------------------------------------------

  /** Flip the aggregated↔expanded view and re-render the current base graph. */
  private async toggleAggregate(): Promise<void> {
    this.aggregated = !this.aggregated;
    this.aggregateBtn.textContent = this.aggregated ? "Expanded" : "Aggregated";
    this.aggregateBtn.setAttribute("aria-pressed", String(this.aggregated));
    if (this.currentInput !== null) {
      await this.present(this.currentInput, this.currentSelect);
    }
  }

  // -- time travel + diff overlay ----------------------------------------------

  /**
   * Build the time scrubber into its container and load the birth-event axis. Re-entrant:
   * {@link reload} calls it again on every corpus open, and the scrubber appends its own
   * DOM, so the previous instance is torn down first or each open would stack another
   * slider on the pane.
   */
  private async mountScrubber(): Promise<void> {
    const container = document.getElementById("scrubber");
    if (container === null) return;
    this.scrubber?.destroy();
    this.scrubber = new TimeScrubber(container, ipc, (asOfMs) => void this.onScrub(asOfMs));
    await this.scrubber.load();
  }

  /**
   * Advance the graph to a scrub instant. In diff mode, re-tint the forest against
   * that instant; otherwise swap in the time-travel snapshot (`graph.at`) as of it.
   */
  private async onScrub(asOfMs: number): Promise<void> {
    this.scrubAsOf = asOfMs;
    if (this.diffMode) {
      await this.applyDiffOverlay();
      return;
    }
    if (ipc.graphAt === undefined) return;
    try {
      const snap = await ipc.graphAt(asOfMs);
      await this.present({ nodes: snap.nodes, edges: snap.edges }, null);
    } catch (err) {
      this.graphStatusEl.textContent = engineErrorText(err, "time-travel failed");
    }
  }

  /**
   * Toggle diff mode. On: show the full forest and tint what changed since the
   * scrub point (`graph.diff`) — nothing at "now". Off: drop the overlay.
   */
  private async toggleDiff(): Promise<void> {
    this.diffMode = !this.diffMode;
    this.diffBtn.textContent = this.diffMode ? "Diff: on" : "Diff mode";
    this.diffBtn.setAttribute("aria-pressed", String(this.diffMode));
    if (this.diffMode) {
      await this.showForest();
      await this.applyDiffOverlay();
    } else {
      this.canvas.setDiffOverlay(null);
    }
  }

  /** Diff the scrub point against "now" and paint the tint classes on the canvas. */
  private async applyDiffOverlay(): Promise<void> {
    if (ipc.graphDiff === undefined) return;
    try {
      const diff = await ipc.graphDiff(this.scrubAsOf ?? undefined, undefined);
      this.canvas.setDiffOverlay(diffToOverlay(diff));
    } catch (err) {
      this.graphStatusEl.textContent = engineErrorText(err, "diff failed");
    }
  }

  // -- export panel ------------------------------------------------------------

  /**
   * Build the export panel: a destination input, Plan (dry-run `export.plan`) and
   * Export (`export.run`) buttons, and an output area painted from the headless
   * {@link ExportPanel} controller's view via `textContent` (never innerHTML).
   */
  private mountExportPanel(): void {
    const container = document.getElementById("export-panel");
    if (container === null) return;
    // Adapted through `exportIpcFrom` rather than an inline literal: the forwarding is the
    // part that can silently drop `mode`/`scrub`, and inline here it is unreachable by any
    // test (see that function's note — a mutation reverting this left the suite GREEN).
    const exportIpc = exportIpcFrom(ipc);
    if (exportIpc === null) {
      container.textContent = "Export unavailable — engine not wired.";
      return;
    }

    const destInput = document.createElement("input");
    destInput.type = "text";
    destInput.className = "export-dest";
    destInput.placeholder = "Destination path…";
    destInput.autocomplete = "off";

    // The G-6 projection chooser. FULL is first and therefore the default, which is the
    // behaviour that changes nothing — an export mode must never silently become something
    // other than the archive of record. The option text is deliberately modest about what
    // shareable does: it drops the preview excerpt and rewrites cwd/rollout_path, and it does
    // NOT strip titles or branch names, so calling it "anonymised" here would overpromise on
    // the engine's behalf.
    const modeSelect = document.createElement("select");
    modeSelect.className = "export-mode";
    modeSelect.setAttribute("aria-label", "Export mode");
    for (const [value, text] of [
      ["full", "Full — every field (archive of record)"],
      ["shareable", "Shareable — drops preview, scrubs home paths (see note)"],
    ] as const) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      modeSelect.append(option);
    }

    // The G-5 scrub opt-in. UNCHECKED by default: the default is WARN, because an archive of
    // record must not be altered behind the owner's back. Checking it is the explicit ask.
    const scrubWrap = document.createElement("label");
    scrubWrap.className = "export-scrub";
    const scrubBox = document.createElement("input");
    scrubBox.type = "checkbox";
    scrubBox.className = "export-scrub-box";
    scrubWrap.append(scrubBox, document.createTextNode(" Replace credential shapes in the export"));

    const actions = document.createElement("div");
    actions.className = "export-actions";
    const planBtn = document.createElement("button");
    planBtn.type = "button";
    planBtn.textContent = "Plan";
    const runBtn = document.createElement("button");
    runBtn.type = "button";
    runBtn.textContent = "Export";
    actions.append(planBtn, runBtn);

    const output = document.createElement("pre");
    output.className = "export-output muted";

    const panel = new ExportPanel(exportIpc, (view) => {
      output.textContent = renderView(view).join("\n");
    });
    output.textContent = renderView(panel.current).join("\n");

    // The chosen mode goes to BOTH calls. Planning in one mode and writing in another would
    // show a preview and a credential scan measured against an artifact the user does not
    // then receive — the preview is only meaningful if it describes the write it precedes.
    const chosenMode = (): ExportMode => asExportMode(modeSelect.value);
    planBtn.addEventListener(
      "click",
      () => void panel.plan(destInput.value || undefined, chosenMode()),
    );
    runBtn.addEventListener(
      "click",
      () => void panel.run(destInput.value, chosenMode(), scrubBox.checked),
    );

    container.append(destInput, modeSelect, scrubWrap, actions, output);
  }

  // -- view builders -----------------------------------------------------------

  private renderRootRow(node: ThreadNode): HTMLElement {
    const row = document.createElement("button");
    row.className = "root-row";
    row.type = "button";

    const dot = document.createElement("span");
    dot.className = "provider-dot";
    dot.style.background = providerTint(node.provider).fill;
    dot.title = node.provider || "unknown";

    const label = document.createElement("span");
    label.className = "root-label";
    label.textContent = node.title || node.id;

    const badge = document.createElement("span");
    badge.className = "root-badge";
    badge.textContent = `d${node.depth} · ${node.child_count}▸`;
    badge.title = `depth ${node.depth}, ${node.child_count} children`;

    row.append(dot, label, badge);
    row.addEventListener("click", () => void this.focusThread(node.id));
    return row;
  }

  private renderDetail(meta: ThreadMeta): void {
    const rows: Array<[string, string]> = [
      ["id", meta.id],
      ["provider", meta.provider || "—"],
    ];
    if (meta.agent_role) rows.push(["role", meta.agent_role]);
    if (meta.agent_nickname) rows.push(["agent", meta.agent_nickname]);
    if (meta.tokens !== null) rows.push(["tokens", meta.tokens.toLocaleString()]);
    rows.push(["depth", String(meta.depth)]);
    rows.push(["children", String(meta.child_count)]);
    if (meta.git_branch) rows.push(["branch", meta.git_branch]);
    if (meta.cwd) rows.push(["cwd", meta.cwd]);
    if (meta.created_at_ms !== null) {
      rows.push(["created", new Date(meta.created_at_ms).toISOString()]);
    }
    this.paintDetail(meta.title || meta.id, rows, meta.preview);
  }

  /**
   * Detail for a "+N more" placeholder.
   *
   * It deliberately does NOT offer to expand in place. Showing N children of one parent
   * costs the same whether you reached N by collapsing down or expanding up, so the
   * measured collapse table is also the expansion curve: past ~200 siblings the layout
   * exceeds its own guard, and the 4,844-child hub in the real corpus can never be fully
   * expanded in a graph at all — not with a longer timeout, not with paging that
   * accumulates. Those children belong in a list, which is what the pane beside the canvas
   * is for; until that list can be driven from here, saying so plainly beats an "expand"
   * button that would hang the UI.
   */
  private renderMoreDetail(id: string): void {
    const parent = moreParentId(id);
    const parentNode = this.currentInput?.nodes.find((n) => n.id === parent);
    const hidden = this.moreCounts.get(id);
    this.paintDetail(
      hidden === undefined ? "hidden children" : `${hidden} hidden children`,
      [
        ["parent", parentNode?.title || parent],
        [
          "why",
          "the graph draws a bounded number of children per node — layout cost is driven by "
          + "the widest single layer, not by how big the graph is",
        ],
        [
          "to see them",
          "open the parent in the thread list; a list holds thousands of siblings, a "
          + "laid-out graph cannot",
        ],
      ],
      null,
    );
  }

  private renderDanglingDetail(id: string): void {
    this.paintDetail(
      id,
      [
        ["id", id],
        ["status", "dangling — referenced by an edge, no thread row"],
      ],
      null,
    );
  }

  private paintDetail(
    title: string,
    rows: Array<[string, string]>,
    preview: string | null,
  ): void {
    const frag = document.createDocumentFragment();
    const h = document.createElement("h2");
    h.textContent = title;
    frag.appendChild(h);

    const dl = document.createElement("dl");
    for (const [k, v] of rows) {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = v;
      dl.append(dt, dd);
    }
    frag.appendChild(dl);

    if (preview !== null && preview !== "") {
      const p = document.createElement("p");
      p.className = "detail-preview";
      p.textContent = preview;
      frag.appendChild(p);
    }
    this.detailEl.replaceChildren(frag);
  }

  private renderLegend(): void {
    const legend = requireEl("legend");
    const frag = document.createDocumentFragment();
    for (const provider of knownProviders()) {
      const chip = document.createElement("span");
      chip.className = "legend-chip";
      const dot = document.createElement("span");
      dot.className = "provider-dot";
      dot.style.background = providerTint(provider).fill;
      chip.append(dot, document.createTextNode(provider));
      frag.appendChild(chip);
    }
    const cross = document.createElement("span");
    cross.className = "legend-chip";
    const swatch = document.createElement("span");
    swatch.className = "cross-swatch";
    cross.append(swatch, document.createTextNode("cross-provider"));
    frag.appendChild(cross);
    legend.replaceChildren(frag);
  }
}

