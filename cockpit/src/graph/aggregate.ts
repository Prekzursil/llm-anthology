/**
 * PURE spawn-graph aggregation — the two node-folding transforms behind the
 * cockpit's "aggregated <-> expanded" toggle (the Langfuse count-badge idea) and
 * the claude-analysis linear-chain collapse.
 *
 * Both functions are side-effect-free data transforms over the frozen IPC graph
 * DTOs ({@link ThreadNode} / {@link SpawnEdge}); neither touches ELK, the canvas,
 * the DOM, or the network, so this whole module is unit-testable in a plain node
 * environment (see `aggregate.test.ts`).
 *
 * Two folds:
 *   1. {@link collapseLinearChains} — replace every maximal NON-BRANCHING run
 *      `a -> b -> c` (each interior node has exactly one parent AND one child) with
 *      a single `chain:<first>` super-node labelled `first…last (n)`.
 *   2. {@link foldSubtree} — replace the whole subtree rooted at an id with one
 *      `subtree:<root>` super-node carrying the root's rollup weight.
 *
 * Both return an {@link AggregateGraph}: EVERY original node is represented exactly
 * once (folded into a super-node or passed through as a `single`), edges are
 * re-pointed onto the surviving aggregate ids, and — like the sidecar's diff — the
 * aggregate edges are STATUS-FREE (identity is the `(parent, child)` pair). All
 * outputs are sorted by stable id so the transform is deterministic.
 */

import type { RollupTable, SpawnEdge, ThreadNode } from "../ipc/types";

/** What an {@link AggregateNode} stands in for. */
export type AggregateKind = "single" | "chain" | "subtree";

/** One node in an aggregated graph — an original node or a folded group. */
export interface AggregateNode {
  /**
   * Stable id. A `single` keeps its record id (honouring node-id = record-id); a
   * fold uses a reserved synthetic id (`chain:<first>` / `subtree:<root>`).
   */
  id: string;
  kind: AggregateKind;
  /** Display label: `single` -> its own label; `chain` -> `first…last (n)`; `subtree` -> `root (n)`. */
  label: string;
  /** Every record id folded into this node, sorted and de-duplicated (length >= 1). */
  members: string[];
  /** Representative node — the node itself (single), the chain head, or the subtree root. */
  representative: ThreadNode;
  /** Aggregated token weight (see each fold for how it is derived). */
  tokens: number;
  /** Number of folded members (`== members.length`; `subtree` prefers the rollup count). */
  count: number;
}

/** An aggregated spawn graph: folded nodes + status-free, re-pointed edges. */
export interface AggregateGraph {
  nodes: AggregateNode[];
  edges: SpawnEdge[];
}

/** Default minimum node count for a linear run to be worth folding (matches `a->b->c`). */
export const DEFAULT_MIN_CHAIN_LENGTH = 3;

/** Field separator for an edge key: U+001F cannot appear in a record id. */
const KEY_SEP = "";

/** A node's label: its title, else its id (for a dangling / untitled node). */
function labelOf(node: ThreadNode): string {
  return node.title.trim() !== "" ? node.title : node.id;
}

/** A bare synthesized node for an id that appears only on an edge. */
function bareNode(id: string): ThreadNode {
  return { id, title: "", provider: "", created_at_ms: null, child_count: 0, depth: 0 };
}

/**
 * The full node set: the DTO nodes UNION every id an edge names. An id present only
 * on an edge (a dangling parent/child) is synthesized into a bare node, exactly as
 * the sidecar / `layout.ts` do, so every edge has a valid endpoint node.
 */
function buildNodeIndex(nodes: ThreadNode[], edges: SpawnEdge[]): Map<string, ThreadNode> {
  const index = new Map<string, ThreadNode>();
  for (const node of nodes) index.set(node.id, node);
  for (const edge of edges) {
    for (const id of [edge.parent, edge.child]) {
      if (!index.has(id)) index.set(id, bareNode(id));
    }
  }
  return index;
}

/** Directed adjacency over the index, self-loops excluded, endpoints de-duplicated. */
function adjacency(
  edges: SpawnEdge[],
  index: Map<string, ThreadNode>,
): { children: Map<string, Set<string>>; parents: Map<string, Set<string>> } {
  const children = new Map<string, Set<string>>();
  const parents = new Map<string, Set<string>>();
  for (const id of index.keys()) {
    children.set(id, new Set());
    parents.set(id, new Set());
  }
  for (const edge of edges) {
    if (edge.parent === edge.child) continue; // ignore self-loops
    children.get(edge.parent)!.add(edge.child);
    parents.get(edge.child)!.add(edge.parent);
  }
  return { children, parents };
}

/** Pass-through aggregate node for an un-folded original node. */
function singleNode(node: ThreadNode): AggregateNode {
  return {
    id: node.id,
    kind: "single",
    label: labelOf(node),
    members: [node.id],
    representative: node,
    tokens: node.tokens ?? 0,
    count: 1,
  };
}

/** Sort aggregate nodes by their (unique) id, deterministically. */
function sortNodesById(nodes: AggregateNode[]): AggregateNode[] {
  const byId = new Map(nodes.map((node) => [node.id, node] as const));
  return [...byId.keys()].sort().map((id) => byId.get(id)!);
}

/**
 * Re-point every edge through `aggIdOf`, dropping self-loops and edges that become
 * internal to one super-node, de-duplicating, and emitting STATUS-FREE edges sorted
 * by `(parent, child)`.
 */
function remapEdges(edges: SpawnEdge[], aggIdOf: (id: string) => string): SpawnEdge[] {
  const byKey = new Map<string, SpawnEdge>();
  for (const edge of edges) {
    if (edge.parent === edge.child) continue; // self-loop
    const parent = aggIdOf(edge.parent);
    const child = aggIdOf(edge.child);
    if (parent === child) continue; // collapsed inside one aggregate
    byKey.set(`${parent}${KEY_SEP}${child}`, { parent, child });
  }
  return [...byKey.keys()].sort().map((key) => byKey.get(key)!);
}

/**
 * Fold every maximal linear (non-branching) chain into one super-node.
 *
 * A chain edge `p -> c` requires `p` to have exactly ONE child AND `c` exactly ONE
 * parent, so a branch (fan-out > 1) or a merge (fan-in > 1) terminates the chain.
 * A maximal run of at least `minLength` nodes folds to a `chain:<first>` node whose
 * `tokens` is the SUM of its members' self-tokens (a chain never reaches past its
 * tail, so its rollup is exactly that sum); shorter runs and all other nodes pass
 * through as `single`s. Output nodes/edges are sorted by id.
 */
export function collapseLinearChains(
  nodes: ThreadNode[],
  edges: SpawnEdge[],
  minLength: number = DEFAULT_MIN_CHAIN_LENGTH,
): AggregateGraph {
  const index = buildNodeIndex(nodes, edges);
  const { children, parents } = adjacency(edges, index);
  const ids = [...index.keys()].sort();

  const aggIdByMember = new Map<string, string>();
  const outNodes: AggregateNode[] = [];

  for (const headId of ids) {
    const headChildren = children.get(headId)!;
    if (headChildren.size !== 1) continue; // a head must flow into exactly one child

    // Skip a node that CONTINUES a chain from its parent (it is not a head): its
    // single parent's single child is this node, so the parent already claims it.
    const headParents = parents.get(headId)!;
    if (headParents.size === 1 && children.get([...headParents][0]!)!.size === 1) continue;

    // Walk the chain forward while each step stays non-branching / non-merging.
    // The walk follows only chain edges (single child, single parent), which form
    // disjoint SIMPLE paths — a pure cycle has no head to start from, and a path
    // cannot enter a cycle (its entry would need chain-fan-in >= 2) — so this always
    // terminates on a finite graph with no revisits; no cycle guard is needed.
    const path = [headId];
    let cur = headId;
    for (;;) {
      const curChildren = children.get(cur)!;
      if (curChildren.size !== 1) break; // cur branches or is a leaf -> tail
      const next = [...curChildren][0]!;
      if (parents.get(next)!.size !== 1) break; // next is a merge -> stop before it
      path.push(next);
      cur = next;
    }

    if (path.length < minLength) continue; // too short to be worth folding

    const first = index.get(path[0]!)!;
    const last = index.get(path[path.length - 1]!)!;
    const aggId = `chain:${path[0]}`;
    const tokens = path.reduce((sum, id) => sum + (index.get(id)!.tokens ?? 0), 0);
    outNodes.push({
      id: aggId,
      kind: "chain",
      label: `${labelOf(first)}…${labelOf(last)} (${path.length})`,
      members: [...path].sort(),
      representative: first,
      tokens,
      count: path.length,
    });
    for (const member of path) aggIdByMember.set(member, aggId);
  }

  // Everything not folded into a chain passes through as a single.
  for (const id of ids) {
    if (aggIdByMember.has(id)) continue;
    outNodes.push(singleNode(index.get(id)!));
    aggIdByMember.set(id, id);
  }

  return {
    nodes: sortNodesById(outNodes),
    edges: remapEdges(edges, (id) => aggIdByMember.get(id)!),
  };
}

/**
 * Fold the subtree rooted at `rootId` — the root plus every node reachable from it —
 * into one `subtree:<root>` super-node, leaving the rest of the graph as `single`s.
 *
 * The super-node carries the root's rollup weight when `rollupById[rootId]` is
 * present (`subtree_tokens` / `subtree_count`, the diamond-deduped authority);
 * otherwise weight is DERIVED (member self-token sum / member count). A `rootId`
 * absent from the graph is synthesized into a bare node so it can still be folded.
 * Edges from outside INTO the subtree re-point onto the super-node; edges internal
 * to the subtree are dropped. Output nodes/edges are sorted by id.
 */
export function foldSubtree(
  nodes: ThreadNode[],
  edges: SpawnEdge[],
  rollupById: RollupTable,
  rootId: string,
): AggregateGraph {
  const index = buildNodeIndex(nodes, edges);
  if (!index.has(rootId)) index.set(rootId, bareNode(rootId));
  const { children } = adjacency(edges, index);

  // Reachable closure from the root (BFS), including the root itself.
  const inSubtree = new Set<string>([rootId]);
  const queue = [rootId];
  while (queue.length > 0) {
    const cur = queue.shift()!;
    for (const child of children.get(cur)!) {
      if (!inSubtree.has(child)) {
        inSubtree.add(child);
        queue.push(child);
      }
    }
  }

  const members = [...inSubtree].sort();
  const representative = index.get(rootId)!;
  const metrics = rollupById[rootId];
  const tokens = metrics
    ? metrics.subtree_tokens
    : members.reduce((sum, id) => sum + (index.get(id)!.tokens ?? 0), 0);
  const count = metrics ? metrics.subtree_count : members.length;
  const aggId = `subtree:${rootId}`;

  const outNodes: AggregateNode[] = [
    {
      id: aggId,
      kind: "subtree",
      label: `${labelOf(representative)} (${count})`,
      members,
      representative,
      tokens,
      count,
    },
  ];
  for (const id of index.keys()) {
    if (!inSubtree.has(id)) outNodes.push(singleNode(index.get(id)!));
  }

  return {
    nodes: sortNodesById(outNodes),
    edges: remapEdges(edges, (id) => (inSubtree.has(id) ? aggId : id)),
  };
}
