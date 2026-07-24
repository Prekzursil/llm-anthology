/**
 * CANVAS 2D renderer for the spawn tree.
 *
 * Canvas (not React Flow / SVG) per the SOTA render decision: it draws the ~2k-node
 * sparse forest and free text labels cheaply. Responsibilities: draw provider-tinted
 * labelled nodes, edges (cross-provider edges as a distinct dashed class), pan (drag),
 * zoom (wheel about the cursor), and hit-test clicks back to a record id (node-id =
 * record-id, so selection maps 1:1). Read-only: it never mutates graph data.
 *
 * Browser-only (needs a real canvas + DOM), so it is exercised at runtime, not in the
 * node vitest — the tested logic is the pure mapping in `./layout.ts`.
 */

import type { LayoutEdge, LayoutNode, PositionedGraph } from "./layout";
import {
  CROSS_EDGE_COLOR,
  EDGE_COLOR,
  providerTint,
} from "./palette";

export type SelectHandler = (nodeId: string | null, node: LayoutNode | null) => void;

interface Transform {
  scale: number;
  offsetX: number;
  offsetY: number;
}

const MIN_SCALE = 0.1;
const MAX_SCALE = 4;
const ZOOM_SENSITIVITY = 0.0016;
const CLICK_SLOP_PX = 4; // pointer travel under this still counts as a click
const LABEL_MIN_SCALE = 0.45; // hide labels when zoomed out past this
const NODE_RADIUS = 8;

export class SpawnTreeCanvas {
  private readonly ctx: CanvasRenderingContext2D;
  private graph: PositionedGraph = { nodes: [], edges: [], width: 0, height: 0 };
  private readonly nodeIndex = new Map<string, LayoutNode>();
  private tf: Transform = { scale: 1, offsetX: 0, offsetY: 0 };
  private selectedId: string | null = null;
  private onSelect: SelectHandler | null = null;

  private dpr = 1;
  private cssWidth = 0;
  private cssHeight = 0;

  private dragging = false;
  private dragMoved = 0;
  private lastPointerX = 0;
  private lastPointerY = 0;

  private readonly resizeObserver: ResizeObserver;
  private frame = 0;

  constructor(private readonly canvas: HTMLCanvasElement) {
    const ctx = canvas.getContext("2d");
    if (ctx === null) throw new Error("2D canvas context unavailable");
    this.ctx = ctx;

    this.onWheel = this.onWheel.bind(this);
    this.onPointerDown = this.onPointerDown.bind(this);
    this.onPointerMove = this.onPointerMove.bind(this);
    this.onPointerUp = this.onPointerUp.bind(this);

    canvas.addEventListener("wheel", this.onWheel, { passive: false });
    canvas.addEventListener("pointerdown", this.onPointerDown);
    canvas.addEventListener("pointermove", this.onPointerMove);
    canvas.addEventListener("pointerup", this.onPointerUp);
    canvas.addEventListener("pointerleave", this.onPointerUp);

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(canvas);
    this.resize();
  }

  /** Register (or clear) the selection callback. */
  setSelectHandler(handler: SelectHandler | null): void {
    this.onSelect = handler;
  }

  /** Swap in a new positioned graph and fit it to the viewport. */
  setGraph(graph: PositionedGraph, fit = true): void {
    this.graph = graph;
    this.nodeIndex.clear();
    for (const n of graph.nodes) this.nodeIndex.set(n.id, n);
    if (this.selectedId !== null && !this.nodeIndex.has(this.selectedId)) {
      this.selectedId = null;
    }
    if (fit) this.fitToView();
    else this.scheduleRender();
  }

  /** Programmatically set the highlighted node (does not fire the callback). */
  select(nodeId: string | null): void {
    this.selectedId = nodeId !== null && this.nodeIndex.has(nodeId) ? nodeId : null;
    this.scheduleRender();
  }

  /** Center + scale the whole graph within the viewport with padding. */
  fitToView(padding = 48): void {
    const { width, height } = this.graph;
    if (width <= 0 || height <= 0 || this.cssWidth === 0 || this.cssHeight === 0) {
      this.tf = { scale: 1, offsetX: padding, offsetY: padding };
      this.scheduleRender();
      return;
    }
    const scale = Math.max(
      MIN_SCALE,
      Math.min(
        MAX_SCALE,
        Math.min(
          (this.cssWidth - padding * 2) / width,
          (this.cssHeight - padding * 2) / height,
        ),
      ),
    );
    this.tf = {
      scale,
      offsetX: (this.cssWidth - width * scale) / 2,
      offsetY: (this.cssHeight - height * scale) / 2,
    };
    this.scheduleRender();
  }

  destroy(): void {
    this.resizeObserver.disconnect();
    this.canvas.removeEventListener("wheel", this.onWheel);
    this.canvas.removeEventListener("pointerdown", this.onPointerDown);
    this.canvas.removeEventListener("pointermove", this.onPointerMove);
    this.canvas.removeEventListener("pointerup", this.onPointerUp);
    this.canvas.removeEventListener("pointerleave", this.onPointerUp);
    if (this.frame !== 0) cancelAnimationFrame(this.frame);
  }

  // -- sizing ------------------------------------------------------------------

  private resize(): void {
    const rect = this.canvas.getBoundingClientRect();
    this.dpr = window.devicePixelRatio || 1;
    this.cssWidth = rect.width;
    this.cssHeight = rect.height;
    this.canvas.width = Math.max(1, Math.round(rect.width * this.dpr));
    this.canvas.height = Math.max(1, Math.round(rect.height * this.dpr));
    this.scheduleRender();
  }

  // -- interaction -------------------------------------------------------------

  private pointerPos(e: PointerEvent): { x: number; y: number } {
    const rect = this.canvas.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  private onWheel(e: WheelEvent): void {
    e.preventDefault();
    const rect = this.canvas.getBoundingClientRect();
    const cx = e.clientX - rect.left;
    const cy = e.clientY - rect.top;
    const factor = Math.exp(-e.deltaY * ZOOM_SENSITIVITY);
    const newScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, this.tf.scale * factor));
    const ratio = newScale / this.tf.scale;
    this.tf.offsetX = cx - (cx - this.tf.offsetX) * ratio;
    this.tf.offsetY = cy - (cy - this.tf.offsetY) * ratio;
    this.tf.scale = newScale;
    this.scheduleRender();
  }

  private onPointerDown(e: PointerEvent): void {
    this.dragging = true;
    this.dragMoved = 0;
    const p = this.pointerPos(e);
    this.lastPointerX = p.x;
    this.lastPointerY = p.y;
    this.canvas.setPointerCapture(e.pointerId);
  }

  private onPointerMove(e: PointerEvent): void {
    if (!this.dragging) return;
    const p = this.pointerPos(e);
    const dx = p.x - this.lastPointerX;
    const dy = p.y - this.lastPointerY;
    this.dragMoved += Math.abs(dx) + Math.abs(dy);
    this.tf.offsetX += dx;
    this.tf.offsetY += dy;
    this.lastPointerX = p.x;
    this.lastPointerY = p.y;
    this.scheduleRender();
  }

  private onPointerUp(e: PointerEvent): void {
    if (!this.dragging) return;
    this.dragging = false;
    if (this.canvas.hasPointerCapture(e.pointerId)) {
      this.canvas.releasePointerCapture(e.pointerId);
    }
    if (this.dragMoved <= CLICK_SLOP_PX) {
      const p = this.pointerPos(e);
      const hit = this.hitTest(p.x, p.y);
      this.selectedId = hit?.id ?? null;
      this.scheduleRender();
      if (this.onSelect !== null) this.onSelect(hit?.id ?? null, hit);
    }
  }

  /** Screen (CSS px) -> the node under it, or null. Last drawn wins (topmost). */
  private hitTest(sx: number, sy: number): LayoutNode | null {
    const wx = (sx - this.tf.offsetX) / this.tf.scale;
    const wy = (sy - this.tf.offsetY) / this.tf.scale;
    for (let i = this.graph.nodes.length - 1; i >= 0; i--) {
      const n = this.graph.nodes[i];
      if (wx >= n.x && wx <= n.x + n.width && wy >= n.y && wy <= n.y + n.height) {
        return n;
      }
    }
    return null;
  }

  // -- rendering ---------------------------------------------------------------

  private scheduleRender(): void {
    if (this.frame !== 0) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      this.render();
    });
  }

  private render(): void {
    const ctx = this.ctx;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.cssWidth, this.cssHeight);
    ctx.translate(this.tf.offsetX, this.tf.offsetY);
    ctx.scale(this.tf.scale, this.tf.scale);

    for (const edge of this.graph.edges) this.drawEdge(ctx, edge);
    const showLabels = this.tf.scale >= LABEL_MIN_SCALE;
    for (const node of this.graph.nodes) this.drawNode(ctx, node, showLabels);
  }

  private drawEdge(ctx: CanvasRenderingContext2D, edge: LayoutEdge): void {
    if (edge.points.length < 2) return;
    ctx.beginPath();
    ctx.moveTo(edge.points[0].x, edge.points[0].y);
    for (let i = 1; i < edge.points.length; i++) {
      ctx.lineTo(edge.points[i].x, edge.points[i].y);
    }
    ctx.lineWidth = edge.cross ? 2 : 1.25;
    ctx.strokeStyle = edge.cross ? CROSS_EDGE_COLOR : EDGE_COLOR;
    if (edge.cross) ctx.setLineDash([6, 4]);
    else ctx.setLineDash([]);
    if (edge.status === "failed") ctx.strokeStyle = "#c0392b";
    ctx.stroke();
    ctx.setLineDash([]);
    this.drawArrowHead(ctx, edge);
  }

  private drawArrowHead(ctx: CanvasRenderingContext2D, edge: LayoutEdge): void {
    const n = edge.points.length;
    const tip = edge.points[n - 1];
    const prev = edge.points[n - 2];
    const angle = Math.atan2(tip.y - prev.y, tip.x - prev.x);
    const size = 7;
    ctx.beginPath();
    ctx.moveTo(tip.x, tip.y);
    ctx.lineTo(
      tip.x - size * Math.cos(angle - Math.PI / 7),
      tip.y - size * Math.sin(angle - Math.PI / 7),
    );
    ctx.lineTo(
      tip.x - size * Math.cos(angle + Math.PI / 7),
      tip.y - size * Math.sin(angle + Math.PI / 7),
    );
    ctx.closePath();
    ctx.fillStyle = edge.cross ? CROSS_EDGE_COLOR : EDGE_COLOR;
    ctx.fill();
  }

  private drawNode(
    ctx: CanvasRenderingContext2D,
    node: LayoutNode,
    showLabels: boolean,
  ): void {
    const tint = providerTint(node.node.provider);
    const selected = node.id === this.selectedId;
    roundRect(ctx, node.x, node.y, node.width, node.height, NODE_RADIUS);
    ctx.fillStyle = tint.fill;
    ctx.fill();
    ctx.lineWidth = selected ? 3 : 1.5;
    ctx.strokeStyle = selected ? "#ffd54a" : tint.stroke;
    ctx.stroke();

    if (!showLabels) return;
    ctx.fillStyle = tint.text;
    ctx.font = "13px Inter, system-ui, sans-serif";
    ctx.textBaseline = "middle";
    const label = clipText(ctx, node.node.title || node.id, node.width - 20);
    ctx.fillText(label, node.x + 10, node.y + node.height / 2);
  }
}

function roundRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): void {
  const radius = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.arcTo(x + w, y, x + w, y + h, radius);
  ctx.arcTo(x + w, y + h, x, y + h, radius);
  ctx.arcTo(x, y + h, x, y, radius);
  ctx.arcTo(x, y, x + w, y, radius);
  ctx.closePath();
}

/** Truncate `text` with an ellipsis so it fits `maxWidth` in the current font. */
function clipText(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string {
  if (ctx.measureText(text).width <= maxWidth) return text;
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (ctx.measureText(text.slice(0, mid) + "…").width <= maxWidth) lo = mid;
    else hi = mid - 1;
  }
  return text.slice(0, lo) + "…";
}
