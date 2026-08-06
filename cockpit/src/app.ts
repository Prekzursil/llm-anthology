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
import { buildElkGraph, extractLayout, type LayoutInput } from "./graph/layout";
import { knownProviders, providerTint } from "./graph/palette";
import { ExportPanel, renderView, type ExportIpc } from "./ui/exportPanel";
import { SearchPanel } from "./ui/search";
import { TimeScrubber } from "./ui/scrubber";
import { VirtualList } from "./ui/virtualList";

const ROOT_ROW_HEIGHT = 52;
const EDGE_KEY_SEP = "\u0000";

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

  private readonly healthEl = requireEl("health");
  private readonly statsEl = requireEl("stats");
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

    requireEl("btn-forest").addEventListener("click", () => void this.showForest());
    requireEl("btn-fit").addEventListener("click", () => this.canvas.fitToView());
    this.aggregateBtn.addEventListener("click", () => void this.toggleAggregate());
    this.diffBtn.addEventListener("click", () => void this.toggleDiff());

    this.mountExportPanel();
    this.renderLegend();
  }

  /** Boot: health, stats, roots, rollup badges, the forest, then the time scrubber. */
  async init(): Promise<void> {
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
        : "no corpus attached";
    } catch (err) {
      this.healthEl.textContent = `engine unavailable: ${String(err)}`;
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
      this.statsEl.textContent = `stats unavailable: ${String(err)}`;
    }
  }

  private async loadRoots(): Promise<void> {
    const roots = await ipc.graphRoots({ limit: 1000, order: "created" });
    this.rootsList.setItems(roots);
  }

  /** Merge every root subtree into the full forest and render it. */
  private async showForest(): Promise<void> {
    const roots = await ipc.graphRoots({ limit: 1000 });
    const nodeMap = new Map<string, ThreadNode>();
    const edgeMap = new Map<string, SpawnEdge>();
    for (const root of roots) {
      const sub = await ipc.graphSubtree(root.id);
      for (const n of sub.nodes) nodeMap.set(n.id, n);
      for (const e of sub.edges) edgeMap.set(`${e.parent}${EDGE_KEY_SEP}${e.child}`, e);
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
    this.graphStatusEl.textContent = `laying out ${input.nodes.length} nodes…`;
    try {
      const laid = await this.engine.layout(buildElkGraph(input));
      const positioned = extractLayout(laid, input);
      this.canvas.setGraph(positioned);
      if (selectId !== null) this.canvas.select(selectId);
      const crossCount = positioned.edges.filter((e) => e.cross).length;
      this.graphStatusEl.textContent = `${positioned.nodes.length} nodes · ${positioned.edges.length} edges · ${crossCount} cross-provider`;
    } catch (err) {
      const msg = err instanceof LayoutTimeoutError ? err.message : `layout failed: ${String(err)}`;
      this.graphStatusEl.textContent = msg;
    }
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

  /** Build the time scrubber into its container and load the birth-event axis. */
  private async mountScrubber(): Promise<void> {
    const container = document.getElementById("scrubber");
    if (container === null) return;
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
      this.graphStatusEl.textContent = `time-travel failed: ${String(err)}`;
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
      this.graphStatusEl.textContent = `diff failed: ${String(err)}`;
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
