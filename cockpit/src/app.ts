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
import type { SearchHit, SpawnEdge, ThreadMeta, ThreadNode } from "./ipc";
import { collapseLinearChains } from "./graph/aggregate";
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
import { DiscoveryPanel } from "./ui/discoveryPanel";
import { engineErrorText, engineStatusText } from "./ui/errors";
import { ExportPanel, renderView, type ExportIpc } from "./ui/exportPanel";
import { SearchPanel } from "./ui/search";
import { TimeScrubber } from "./ui/scrubber";
import { emptyStateLabel, VirtualList } from "./ui/virtualList";

const ROOT_ROW_HEIGHT = 52;
const EDGE_KEY_SEP = "\u0000";

/**
 * What the graph pane says when it has nothing to draw. Applied through the SAME
 * `data-empty` mechanism the virtualized lists use (see `ui/virtualList`), so the rule
 * stays the already-tested `emptyStateLabel` rather than a second, parallel one. Without
 * it the pane is a bare black rectangle, indistinguishable from a broken renderer —
 * which is exactly how it read on a corpus-less boot.
 */
const GRAPH_EMPTY_LABEL =
  "No spawn tree yet. Use “Open corpus…” in the top bar to attach a corpus index.";

/** A cleared canvas, for the transition out of a graph that no longer has any nodes. */
const EMPTY_GRAPH: PositionedGraph = { nodes: [], edges: [], width: 0, height: 0 };

function requireEl<T extends HTMLElement>(id: string): T {
  const el = document.getElementById(id);
  if (el === null) throw new Error(`cockpit: missing #${id} in index.html`);
  return el as T;
}

export class CockpitApp {
  private readonly canvas: SpawnTreeCanvas;
  private readonly engine = new ElkLayoutEngine();
  private readonly rootsList: VirtualList<ThreadNode>;
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
  private readonly graphPaneEl = requireEl("graph-pane");
  private readonly graphStatusEl = requireEl("graph-status");
  private readonly detailEl = requireEl("detail");
  private readonly aggregateBtn = requireEl<HTMLButtonElement>("btn-aggregate");
  private readonly diffBtn = requireEl<HTMLButtonElement>("btn-diff");

  /**
   * The base (expanded) graph currently displayed, remembered so the aggregated
   * toggle and the diff overlay can re-derive from it without a refetch.
   */
  private currentInput: LayoutInput | null = null;
  private currentSelect: string | null = null;
  /** Fold linear chains into super-nodes when true (the aggregated↔expanded toggle). */
  private aggregated = false;
  /** Tint what changed since the scrub point over the full forest when true. */
  private diffMode = false;
  /** The last scrub position (epoch ms), or null before any scrub. */
  private scrubAsOf: number | null = null;
  private scrubber: TimeScrubber | null = null;
  /** Set the moment {@link reload} starts, so boot never loads the same corpus twice. */
  private loaded = false;

  constructor() {
    this.canvas = new SpawnTreeCanvas(requireEl<HTMLCanvasElement>("tree-canvas"));
    this.canvas.setSelectHandler((id) => void this.onNodeSelected(id));

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
    );
    this.search.setHitHandler((hit) => void this.onHitSelected(hit));

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
    if (this.attachedIndex === null) await this.discovery?.scan();
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
      this.healthEl.textContent = h.corpus_ready
        ? `engine ${h.engine_version} · IR ${h.ir_version}`
        : "engine idle";
    } catch (err) {
      // The not-attached case is the app's INITIAL STATE, not a fault, and the CorpusBar
      // already reports it — so this must not render the engine's internal
      // "call open_corpus first" text here as well.
      this.healthEl.textContent = engineStatusText(err, "engine unavailable");
    }
  }

  private async loadStats(): Promise<void> {
    try {
      const s = await ipc.corpusStats();
      const providers = Object.entries(s.providers)
        .map(([p, n]) => `${p} ${n}`)
        .join(" · ");
      this.statsEl.textContent = `${s.conversations} conversations · ${s.threads} threads · ${s.edges} edges · ${providers}`;
    } catch (err) {
      this.statsEl.textContent = engineStatusText(err, "stats unavailable");
    }
  }

  private async loadRoots(): Promise<void> {
    try {
      // "recent", not "created". `order: "created"` sorts ASCENDING (sidecar.py's
      // graph.roots), so with a 1000 cap the sidebar showed the user their one thousand
      // OLDEST threads and silently hid everything since. On a 2,000-session store that
      // means the list never contains anything from recent months. "recent" has existed
      // in the engine the whole time and was simply never sent.
      this.rootsList.setItems(await ipc.graphRoots({ limit: 1000, order: "recent" }));
    } catch {
      // With no corpus attached every graph read rejects. Show the list's own empty
      // state instead of rejecting out of the boot chain — `main.ts` fires `init()`
      // with `void`, so that rejection was unhandled and the whole UI stayed blank.
      this.rootsList.setItems([]);
    }
  }

  /** Merge every root subtree into the full forest and render it. */
  private async showForest(): Promise<void> {
    let nodeMap = new Map<string, ThreadNode>();
    let edgeMap = new Map<string, SpawnEdge>();
    try {
      const roots = await ipc.graphRoots({ limit: 1000 });
      for (const root of roots) {
        const sub = await ipc.graphSubtree(root.id);
        for (const n of sub.nodes) nodeMap.set(n.id, n);
        for (const e of sub.edges) edgeMap.set(`${e.parent}${EDGE_KEY_SEP}${e.child}`, e);
      }
    } catch {
      // Same reason as loadRoots. Discard any partial harvest rather than drawing a
      // forest that is silently missing subtrees — a half-graph is a worse lie than an
      // empty one in a tool whose whole subject is the graph.
      nodeMap = new Map();
      edgeMap = new Map();
    }
    await this.present(
      { nodes: [...nodeMap.values()], edges: [...edgeMap.values()] },
      null,
    );
  }

  /** Focus one thread: render just its subtree and select its root. */
  private async focusThread(threadId: string): Promise<void> {
    const sub = await ipc.graphSubtree(threadId);
    await this.present({ nodes: sub.nodes, edges: sub.edges }, threadId);
  }

  /**
   * Remember `input` as the current base graph and render it — folded into
   * super-nodes when the aggregated toggle is on, expanded otherwise. Every entry
   * point (forest, focus, time-travel) funnels through here so the toggle can
   * re-derive the view from the same base without a refetch.
   */
  private async present(input: LayoutInput, selectId: string | null): Promise<void> {
    this.currentInput = input;
    this.currentSelect = selectId;
    const view = this.aggregated ? aggregateInput(input) : input;
    await this.renderGraph(view, selectId);
  }

  private async renderGraph(input: LayoutInput, selectId: string | null): Promise<void> {
    if (input.nodes.length === 0) {
      // Nothing to lay out. Clear the canvas (a previous corpus may still be drawn on
      // it) and hand the pane its empty state rather than running ELK over an empty
      // graph, which yields a zero-size layout and leaves a dead black rectangle.
      this.canvas.setGraph(EMPTY_GRAPH, false);
      this.applyGraphEmptyState(0);
      this.graphStatusEl.textContent = "";
      return;
    }
    this.graphStatusEl.textContent = `laying out ${input.nodes.length} nodes…`;
    try {
      const laid = await this.engine.layout(buildElkGraph(input));
      const positioned = extractLayout(laid, input);
      this.canvas.setGraph(positioned);
      this.applyGraphEmptyState(positioned.nodes.length);
      if (selectId !== null) this.canvas.select(selectId);
      const crossCount = positioned.edges.filter((e) => e.cross).length;
      this.graphStatusEl.textContent = `${positioned.nodes.length} nodes · ${positioned.edges.length} edges · ${crossCount} cross-provider`;
    } catch (err) {
      const msg =
      err instanceof LayoutTimeoutError ? err.message : engineErrorText(err, "layout failed");
      this.graphStatusEl.textContent = msg;
    }
  }

  /**
   * Reflect "the graph pane has nothing to draw" onto the pane as `data-empty="<label>"`,
   * exactly as `VirtualList` does for the sidebar lists — same attribute, same
   * `[data-empty]::after` CSS, and the same already-tested `emptyStateLabel` decision, so
   * there is one empty-state mechanism in this app rather than two.
   */
  private applyGraphEmptyState(nodeCount: number): void {
    const label = emptyStateLabel(nodeCount, GRAPH_EMPTY_LABEL);
    if (label === null) this.graphPaneEl.removeAttribute("data-empty");
    else this.graphPaneEl.setAttribute("data-empty", label);
  }

  private async onNodeSelected(id: string | null): Promise<void> {
    if (id === null) {
      this.detailEl.replaceChildren();
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
    await this.focusThread(hit.thread_id ?? hit.conversation_id);
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
    if (ipc.exportPlan === undefined || ipc.exportRun === undefined) {
      container.textContent = "Export unavailable — engine not wired.";
      return;
    }
    const exportIpc: ExportIpc = {
      exportPlan: (dest) => ipc.exportPlan!(dest),
      exportRun: (destPath) => ipc.exportRun!(destPath),
    };

    const destInput = document.createElement("input");
    destInput.type = "text";
    destInput.className = "export-dest";
    destInput.placeholder = "Destination path…";
    destInput.autocomplete = "off";

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

    planBtn.addEventListener("click", () => void panel.plan(destInput.value || undefined));
    runBtn.addEventListener("click", () => void panel.run(destInput.value));

    container.append(destInput, actions, output);
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

/**
 * Fold a base graph's linear chains into super-nodes and re-shape the result as a
 * {@link LayoutInput} the canvas can lay out. Each super-node renders with its fold
 * label and synthetic id; its representative supplies the provider tint and creation
 * time, and the re-pointed, status-free aggregate edges carry the structure.
 */
function aggregateInput(input: LayoutInput): LayoutInput {
  const agg = collapseLinearChains(input.nodes, input.edges);
  return {
    nodes: agg.nodes.map((n) => ({
      ...n.representative,
      id: n.id,
      title: n.label,
      tokens: n.tokens,
    })),
    edges: agg.edges,
  };
}
