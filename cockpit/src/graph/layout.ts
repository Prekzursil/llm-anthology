/**
 * PURE layout-data mapping between the IPC graph DTOs and ELK.
 *
 * Two directions, both side-effect-free and fully unit-testable (this is the core the
 * vitest suite covers — no worker, no canvas, no DOM):
 *   1. {@link buildElkGraph}  — {nodes, edges} -> an ELK `layered` graph request.
 *   2. {@link extractLayout}  — an ELK layout RESULT -> flat positioned nodes/edges,
 *      with cross-provider edges tagged and a bounding box computed.
 *
 * The ELK runtime itself lives behind the worker in `./elkLayout.ts`; only the TYPES
 * are imported here (`import type`) so this module stays a plain data transform that
 * loads with zero runtime dependencies.
 */

import type { ElkEdgeSection, ElkExtendedEdge, ElkNode, ElkPoint } from "elkjs/lib/elk-api";

import type { SpawnEdge, ThreadNode } from "../ipc/types";

export interface LayoutInput {
  nodes: ThreadNode[];
  edges: SpawnEdge[];
}

export interface LayoutConfig {
  /** ELK layout algorithm. Fixed to "layered" for the spawn tree. */
  algorithm: string;
  /** Layout flow direction. */
  direction: "DOWN" | "RIGHT";
  /**
   * Node-placement strategy. BRANDES_KOEPF per the SOTA render decision — NEVER
   * NETWORK_SIMPLEX (its worst case is the ELK infinite-loop the worker guards).
   */
  nodePlacement: string;
  /** Gap between successive layers. */
  layerSpacing: number;
  /** Gap between siblings within a layer. */
  nodeSpacing: number;
  nodeHeight: number;
  minNodeWidth: number;
  maxNodeWidth: number;
  /** Approx px per label character, used to size a node to its label. */
  charWidth: number;
  /** Horizontal padding inside a node. */
  nodePadding: number;
}

export const DEFAULT_LAYOUT_CONFIG: LayoutConfig = {
  algorithm: "layered",
  direction: "DOWN",
  nodePlacement: "BRANDES_KOEPF",
  layerSpacing: 64,
  nodeSpacing: 28,
  nodeHeight: 44,
  minNodeWidth: 120,
  maxNodeWidth: 280,
  charWidth: 7.2,
  nodePadding: 14,
};

export interface LayoutNode {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
  node: ThreadNode;
}

export interface LayoutEdge {
  parent: string;
  child: string;
  /** True when parent and child sit on different (known) providers. */
  cross: boolean;
  status?: string;
  /** Polyline from parent to child: [start, ...bends, end]. */
  points: ElkPoint[];
}

export interface PositionedGraph {
  nodes: LayoutNode[];
  edges: LayoutEdge[];
  width: number;
  height: number;
}

/** The label a node renders with (title, else id for a dangling node). */
export function nodeLabel(node: ThreadNode): string {
  return node.title.trim() !== "" ? node.title : node.id;
}

/** Fixed node box sized to its label, clamped to the config bounds. */
export function nodeSize(
  node: ThreadNode,
  config: LayoutConfig = DEFAULT_LAYOUT_CONFIG,
): { width: number; height: number } {
  const raw = Math.round(nodeLabel(node).length * config.charWidth) + config.nodePadding * 2;
  const width = Math.max(config.minNodeWidth, Math.min(config.maxNodeWidth, raw));
  return { width, height: config.nodeHeight };
}

/**
 * The full node set for a layout: the DTO nodes UNION every id an edge names. An id
 * present only on an edge (a dangling parent/child) is synthesized into a bare node,
 * exactly as the sidecar does, so every edge has valid ELK endpoints.
 */
export function buildNodeIndex(input: LayoutInput): Map<string, ThreadNode> {
  const index = new Map<string, ThreadNode>();
  for (const n of input.nodes) index.set(n.id, n);
  for (const e of input.edges) {
    for (const id of [e.parent, e.child]) {
      if (!index.has(id)) {
        index.set(id, {
          id,
          title: "",
          provider: "",
          model_provider: "",
          created_at_ms: null,
          child_count: 0,
          depth: 0,
        });
      }
    }
  }
  return index;
}

/** True when a spawn crosses provider boundaries (both providers known & different). */
export function isCrossProvider(
  parent: ThreadNode | undefined,
  child: ThreadNode | undefined,
): boolean {
  if (parent === undefined || child === undefined) return false;
  if (parent.provider === "" || child.provider === "") return false;
  return parent.provider !== child.provider;
}

const EDGE_SEP = "\u0000";

function edgeKey(parent: string, child: string): string {
  return `${parent}${EDGE_SEP}${child}`;
}

/**
 * Map {nodes, edges} to an ELK `layered` graph request. Self-loops are dropped;
 * dangling endpoints are materialized as nodes. The layered / BRANDES_KOEPF options
 * are the pinned SOTA choice.
 */
export function buildElkGraph(
  input: LayoutInput,
  config: LayoutConfig = DEFAULT_LAYOUT_CONFIG,
): ElkNode {
  const index = buildNodeIndex(input);

  const children: ElkNode[] = [...index.values()].map((node) => {
    const size = nodeSize(node, config);
    return {
      id: node.id,
      width: size.width,
      height: size.height,
      labels: [{ text: nodeLabel(node) }],
    };
  });

  const edges: ElkExtendedEdge[] = input.edges
    .filter((e) => e.parent !== e.child)
    .map((e) => ({
      id: edgeKey(e.parent, e.child),
      sources: [e.parent],
      targets: [e.child],
    }));

  return {
    id: "root",
    layoutOptions: {
      "elk.algorithm": config.algorithm,
      "elk.direction": config.direction,
      "elk.layered.nodePlacement.strategy": config.nodePlacement,
      "elk.layered.spacing.nodeNodeBetweenLayers": String(config.layerSpacing),
      "elk.spacing.nodeNode": String(config.nodeSpacing),
      "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
      "elk.edgeRouting": "POLYLINE",
    },
    children,
    edges,
  };
}

/** Flatten an ELK edge's section(s) into a single [start, ...bends, end] polyline. */
function sectionPoints(sections: ElkEdgeSection[] | undefined): ElkPoint[] {
  if (sections === undefined || sections.length === 0) return [];
  const points: ElkPoint[] = [];
  for (const s of sections) {
    points.push(s.startPoint);
    if (s.bendPoints !== undefined) for (const b of s.bendPoints) points.push(b);
    points.push(s.endPoint);
  }
  return points;
}

/**
 * Map an ELK layout RESULT back to flat positioned nodes + edges, attaching the
 * original ThreadNode, the cross-provider flag, the edge status, and the polyline.
 * The bounding box is computed from node boxes and edge points (falling back to the
 * ELK-reported graph size).
 */
export function extractLayout(laidOut: ElkNode, input: LayoutInput): PositionedGraph {
  const index = buildNodeIndex(input);
  const statusByKey = new Map<string, string>();
  for (const e of input.edges) {
    if (e.status !== undefined && e.status !== "") {
      statusByKey.set(edgeKey(e.parent, e.child), e.status);
    }
  }

  let maxX = 0;
  let maxY = 0;

  const nodes: LayoutNode[] = (laidOut.children ?? []).map((c) => {
    const x = c.x ?? 0;
    const y = c.y ?? 0;
    const width = c.width ?? 0;
    const height = c.height ?? 0;
    maxX = Math.max(maxX, x + width);
    maxY = Math.max(maxY, y + height);
    const node =
      index.get(c.id) ??
      ({ id: c.id, title: "", provider: "", created_at_ms: null, child_count: 0, depth: 0 } as ThreadNode);
    return { id: c.id, x, y, width, height, node };
  });

  const edges: LayoutEdge[] = (laidOut.edges ?? []).map((e) => {
    const parent = e.sources[0] ?? "";
    const child = e.targets[0] ?? "";
    const points = sectionPoints(e.sections);
    for (const p of points) {
      maxX = Math.max(maxX, p.x);
      maxY = Math.max(maxY, p.y);
    }
    const status = statusByKey.get(edgeKey(parent, child));
    const layoutEdge: LayoutEdge = {
      parent,
      child,
      cross: isCrossProvider(index.get(parent), index.get(child)),
      points,
    };
    if (status !== undefined) layoutEdge.status = status;
    return layoutEdge;
  });

  return {
    nodes,
    edges,
    width: Math.max(maxX, laidOut.width ?? 0),
    height: Math.max(maxY, laidOut.height ?? 0),
  };
}
