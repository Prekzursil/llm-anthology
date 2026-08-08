/**
 * `capFanOut` — the level-of-detail rule that makes the real corpus renderable.
 *
 * The measurement this exists for (`.scratch/maturation/MEASURED-layout-scale.md`): ELK's
 * `layered` cost is driven by the width of a SINGLE LAYER, not by graph size. 502 nodes with
 * 501 edges hanging off one parent took over 20 seconds; 202 nodes took 2.1s and 102 took
 * 868ms. The real corpus has one node with 4,844 children, so no timeout, tuning or algorithm
 * swap renders it — `layered` variants all exceeded 20s, `mrtree` overflowed the stack, and
 * the two that finished (`box`, `rectpacking`) are PACKING algorithms that would draw the
 * spawn tree as a meaningless grid.
 *
 * So the fix is to hand the layout less: no parent renders more than N children at once.
 */
import { describe, expect, it } from "vitest";

import {
  capFanOut,
  DEFAULT_MAX_CHILDREN,
  isMoreId,
  MORE_ID_PREFIX,
  moreParentId,
  type FanOutGraph,
} from "./capFanOut";
import type { SpawnEdge, ThreadNode } from "../ipc/types";

function node(id: string, createdMs: number | null = null): ThreadNode {
  return {
    id,
    title: id,
    provider: "codex",
    model_provider: "openai",
    created_at_ms: createdMs,
    child_count: 0,
    depth: 0,
  };
}

/** A parent with `n` children, newest last (child-0 oldest). */
function fanOut(n: number, parentId = "p"): { nodes: ThreadNode[]; edges: SpawnEdge[] } {
  const nodes = [node(parentId, 0)];
  const edges: SpawnEdge[] = [];
  for (let i = 0; i < n; i++) {
    nodes.push(node(`c${String(i).padStart(4, "0")}`, 1000 + i));
    edges.push({ parent: parentId, child: `c${String(i).padStart(4, "0")}` });
  }
  return { nodes, edges };
}

const idsOf = (g: { nodes: ThreadNode[] }) => g.nodes.map((n) => n.id).sort();
const placeholders = (g: { nodes: ThreadNode[] }) =>
  g.nodes.filter((n) => n.id.startsWith(MORE_ID_PREFIX));

describe("capFanOut", () => {
  it("leaves a graph already under the threshold completely untouched", () => {
    const input = fanOut(5);
    const out = capFanOut(input, 10);
    expect(idsOf(out)).toEqual(idsOf(input));
    expect(out.edges).toHaveLength(5);
    expect(placeholders(out)).toEqual([]);
  });

  it("is a no-op at exactly the threshold, and caps at one more", () => {
    // Off-by-one here would either cap graphs that render fine or fail to cap one that
    // does not, and both are invisible without an explicit boundary test.
    expect(placeholders(capFanOut(fanOut(10), 10))).toEqual([]);
    expect(placeholders(capFanOut(fanOut(11), 10))).toHaveLength(1);
  });

  it("counts the placeholder against the budget, not on top of it", () => {
    // The placeholder is a node ELK has to place, so a cap that emitted `threshold` real
    // children PLUS a placeholder would exceed its own limit by one on every wide parent.
    const out = capFanOut(fanOut(500), 100);
    // 1 parent + 99 kept children + 1 placeholder.
    expect(out.nodes).toHaveLength(101);
    expect(out.edges).toHaveLength(100);
  });

  it("keeps the NEWEST children, because that is what the sidebar shows too", () => {
    const out = capFanOut(fanOut(10), 3);
    const kept = out.nodes.filter((n) => n.id.startsWith("c")).map((n) => n.id);
    expect(kept.sort()).toEqual(["c0008", "c0009"]);
  });

  it("orders undated children last but still keeps them deterministically", () => {
    // A node with no created_at_ms cannot be ranked by recency. It must not sort as
    // "epoch" (silently always dropped) nor as "now" (silently always kept).
    const nodes = [
      node("p", 0), node("dated", 5000),
      node("undated-c"), node("undated-b"), node("undated-a"),
    ];
    const edges: SpawnEdge[] = ["dated", "undated-c", "undated-b", "undated-a"]
      .map((child) => ({ parent: "p", child }));
    const out = capFanOut({ nodes, edges }, 3);
    const kept = out.nodes.filter((n) => n.id !== "p" && !n.id.startsWith(MORE_ID_PREFIX));
    // Budget 3 over 4 children leaves room for 2 real ones + the placeholder: the dated one
    // ranks first, and the undated pair behind it breaks on id rather than arbitrarily.
    expect(kept.map((n) => n.id)).toEqual(["dated", "undated-a"]);
  });

  it("labels the placeholder with the number actually hidden", () => {
    const [more] = placeholders(capFanOut(fanOut(500), 100));
    expect(more.title).toBe("+401 more");
    expect(more.id).toBe(`${MORE_ID_PREFIX}p`);
  });

  it("hangs the placeholder off the capped parent, not the root", () => {
    const out = capFanOut(fanOut(20), 5);
    const edge = out.edges.find((e) => e.child.startsWith(MORE_ID_PREFIX));
    expect(edge?.parent).toBe("p");
  });

  it("drops the subtree beneath a hidden child", () => {
    // Hiding a child while leaving its descendants behind would strand them as floating
    // roots — visually a graph of orphans, and it would not reduce the node count.
    const { nodes, edges } = fanOut(4);
    nodes.push(node("grandchild", 9999));
    edges.push({ parent: "c0000", child: "grandchild" });
    const out = capFanOut({ nodes, edges }, 2);
    expect(idsOf(out)).not.toContain("grandchild");
    expect(idsOf(out)).not.toContain("c0000");
  });

  it("keeps a hidden child that is still reachable through another parent", () => {
    // The spawn graph is a DAG, not a tree — a node can have two parents. Dropping it
    // because ONE parent hid it would delete a node the other parent still shows.
    const nodes = [node("p", 0), node("q", 0), node("a", 10), node("b", 20), node("c", 30)];
    const edges: SpawnEdge[] = [
      { parent: "p", child: "a" },
      { parent: "p", child: "b" },
      { parent: "p", child: "c" },
      { parent: "q", child: "a" },
    ];
    const out = capFanOut({ nodes, edges }, 2);
    // Budget 2 leaves p one real child (c, the newest) plus its placeholder. `a` is hidden
    // by p but q still shows it; `b` is hidden by its only parent and goes.
    expect(idsOf(out)).toContain("a");
    expect(idsOf(out)).not.toContain("b");
    expect(out.edges).toContainEqual({ parent: "q", child: "a" });
  });

  it("counts every child not drawn beneath THIS parent, even one visible elsewhere", () => {
    // Following on from the DAG case. The placeholder answers "how many of p's children am
    // I not seeing under p" — which is 2 — not "how many nodes are off screen". `a` being
    // drawn under q is a different relationship, and reporting "+1" would understate p's
    // real fan-out and disagree with its child_count.
    const nodes = [node("p", 0), node("q", 0), node("a", 10), node("b", 20), node("c", 30)];
    const edges: SpawnEdge[] = [
      { parent: "p", child: "a" },
      { parent: "p", child: "b" },
      { parent: "p", child: "c" },
      { parent: "q", child: "a" },
    ];
    const [more] = placeholders(capFanOut({ nodes, edges }, 2));
    expect(more.title).toBe("+2 more");
  });

  it("caps every wide parent, not just the first", () => {
    const nodes = [node("r", 0)];
    const edges: SpawnEdge[] = [];
    for (const p of ["p1", "p2"]) {
      nodes.push(node(p, 1));
      edges.push({ parent: "r", child: p });
      for (let i = 0; i < 30; i++) {
        nodes.push(node(`${p}-c${i}`, 100 + i));
        edges.push({ parent: p, child: `${p}-c${i}` });
      }
    }
    const out = capFanOut({ nodes, edges }, 5);
    expect(placeholders(out).map((n) => n.id).sort())
      .toEqual([`${MORE_ID_PREFIX}p1`, `${MORE_ID_PREFIX}p2`]);
  });

  it("caps a wide parent that is itself only reachable below another cap", () => {
    // A deep graph must not be capped at the top layer and left wide underneath.
    const nodes = [node("r", 0), node("mid", 1)];
    const edges: SpawnEdge[] = [{ parent: "r", child: "mid" }];
    for (let i = 0; i < 20; i++) {
      nodes.push(node(`g${i}`, 100 + i));
      edges.push({ parent: "mid", child: `g${i}` });
    }
    const out = capFanOut({ nodes, edges }, 3);
    expect(placeholders(out).map((n) => n.id)).toEqual([`${MORE_ID_PREFIX}mid`]);
    expect(out.nodes.filter((n) => n.id.startsWith("g"))).toHaveLength(2);
  });

  it("survives a cycle without hanging", () => {
    // Reachability walks must terminate on malformed data rather than lock the UI.
    const nodes = [node("a", 1), node("b", 2), node("c", 3)];
    const edges: SpawnEdge[] = [
      { parent: "a", child: "b" },
      { parent: "b", child: "c" },
      { parent: "c", child: "a" },
    ];
    const out = capFanOut({ nodes, edges }, 100);
    expect(idsOf(out)).toEqual(["a", "b", "c"]);
  });

  it("keeps a node in a cycle that no root reaches", () => {
    // A component with no entry point is still data. Silently deleting it would be the
    // same class of bug as the 1000-root truncation.
    const nodes = [node("root", 0), node("x", 1), node("y", 2)];
    const edges: SpawnEdge[] = [
      { parent: "x", child: "y" },
      { parent: "y", child: "x" },
    ];
    expect(idsOf(capFanOut({ nodes, edges }, 100))).toEqual(["root", "x", "y"]);
  });

  it("synthesizes a node for an id that appears only on an edge", () => {
    // Matches what layout.ts and aggregate.ts already do for a dangling endpoint.
    const out = capFanOut({ nodes: [node("p", 0)], edges: [{ parent: "p", child: "ghost" }] }, 10);
    expect(idsOf(out)).toEqual(["ghost", "p"]);
  });

  it("ignores a self-loop rather than counting it as a child", () => {
    const out = capFanOut({ nodes: [node("p", 0)], edges: [{ parent: "p", child: "p" }] }, 10);
    expect(idsOf(out)).toEqual(["p"]);
    expect(out.edges).toEqual([]);
  });

  it("is deterministic and idempotent", () => {
    const input = fanOut(50);
    const once = capFanOut(input, 10);
    expect(capFanOut(input, 10)).toEqual(once);
    // Re-capping an already-capped graph must not cap it again or renumber anything.
    expect(capFanOut(once, 10)).toEqual(once);
  });

  /** Two wide parents — a single-parent fixture cannot vary CROSS-parent ordering at all. */
  function twoWideParents(): FanOutGraph {
    const nodes = [node("r", 0), node("alpha", 1), node("beta", 2)];
    const edges: SpawnEdge[] = [
      { parent: "r", child: "alpha" },
      { parent: "r", child: "beta" },
    ];
    for (const p of ["alpha", "beta"]) {
      for (let i = 0; i < 30; i++) {
        nodes.push(node(`${p}-c${String(i).padStart(2, "0")}`, 100 + i));
        edges.push({ parent: p, child: `${p}-c${String(i).padStart(2, "0")}` });
      }
    }
    return { nodes, edges };
  }

  it("returns the same graph however the input happens to be ordered", () => {
    // The engine promises no particular edge order, and the canvas must not redraw
    // differently because a query came back in a different sequence. Within one parent the
    // children are already rank-sorted; it is the order BETWEEN parents that varies, which
    // is why this needs a two-parent fixture to detect anything.
    const input = twoWideParents();
    const reversed = { nodes: [...input.nodes].reverse(), edges: [...input.edges].reverse() };
    // Deterministic reshuffles — no Math.random, because a flaky test is worse than none.
    const shuffled = {
      nodes: [...input.nodes].sort((a, b) => (a.id.slice(-1) < b.id.slice(-1) ? -1 : 1)),
      edges: [...input.edges].sort((a, b) => (a.child < b.child ? 1 : -1)),
    };
    const expected = capFanOut(input, 10);
    expect(capFanOut(reversed, 10)).toEqual(expected);
    expect(capFanOut(shuffled, 10)).toEqual(expected);
  });

  it("emits nodes and edges in canonical id order", () => {
    // Same contract aggregate.ts states for its own output. Placeholders are appended after
    // the id-sorted real nodes, so without the final sort `more:alpha` trails `beta` instead
    // of sorting between them — deterministic, but not canonical, and it means two modules
    // in the same folder disagree about what their output ordering means.
    const out = capFanOut(twoWideParents(), 10);
    expect(out.nodes.map((n) => n.id)).toEqual([...out.nodes.map((n) => n.id)].sort());
    const keys = out.edges.map((e) => `${e.parent} ${e.child}`);
    expect(keys).toEqual([...keys].sort());
  });

  it("does not mutate its input", () => {
    const input = fanOut(50);
    const before = JSON.stringify(input);
    capFanOut(input, 10);
    expect(JSON.stringify(input)).toBe(before);
  });

  it("never keeps an undated child while dropping a dated one", () => {
    // Stated as the PROPERTY rather than as one expected ordering. A mutation that made the
    // recency comparator inconsistent (both `a<b` and `b<a`) slipped past the outcome-based
    // test above, because V8's sort happened to land on the same answer for that one input.
    // A rule cannot be verified by a single lucky arrangement.
    const nodes = [node("p", 0)];
    const edges: SpawnEdge[] = [];
    for (let i = 0; i < 6; i++) {
      nodes.push(node(`d${i}`, 1000 + i));
      nodes.push(node(`u${i}`, null));
      edges.push({ parent: "p", child: `d${i}` }, { parent: "p", child: `u${i}` });
    }
    for (const budget of [2, 3, 5, 8]) {
      const out = capFanOut({ nodes, edges }, budget);
      const kept = new Set(out.nodes.map((n) => n.id));
      const keptUndated = [...kept].filter((id) => id.startsWith("u"));
      const droppedDated = ["d0", "d1", "d2", "d3", "d4", "d5"].filter((id) => !kept.has(id));
      expect(
        keptUndated.length === 0 || droppedDated.length === 0,
        `budget ${budget}: kept undated ${keptUndated} while dropping dated ${droppedDated}`,
      ).toBe(true);
    }
  });

  it("deepens one placeholder when re-capped tighter, instead of duplicating it", () => {
    // Re-capping is what happens when the threshold changes on an already-capped view. The
    // placeholder id is derived from the parent, so emitting a second one produces TWO nodes
    // sharing an id -- which ELK and the canvas both key by. Found by mutation testing.
    const once = capFanOut(fanOut(50), 10);
    const twice = capFanOut(once, 5);
    const more = placeholders(twice);
    expect(more).toHaveLength(1);
    expect(new Set(twice.nodes.map((n) => n.id)).size).toBe(twice.nodes.length);
    // 50 children: the first pass hid 41, the second hides 5 more of the 9 it had kept.
    expect(more[0].title).toBe("+46 more");
    expect(more[0].child_count).toBe(46);
    // And the tally still adds up to the original fan-out.
    const realKept = twice.nodes.filter((n) => n.id.startsWith("c")).length;
    expect(realKept + more[0].child_count).toBe(50);
  });

  it("defaults to the measured threshold", () => {
    // 100 was measured at 868ms median against an 8s guard (9x headroom); 200 was the hard
    // ceiling at 5.5s, and 250 already exceeded the guard at 14s. Changing this default
    // without re-measuring is how the app becomes unrenderable again.
    expect(DEFAULT_MAX_CHILDREN).toBe(100);
    expect(placeholders(capFanOut(fanOut(101)))).toHaveLength(1);
  });

  it("names the parent every placeholder stands for, recoverably from the id alone", () => {
    // `app.ts:713-720` renders the detail pane for a selected placeholder by doing exactly
    // this: `moreParentId(id)` and then looking that id up in the graph it laid out. So the
    // contract is a ROUND TRIP — the recovered id must be a real node in the SAME output —
    // not merely "strips a prefix". A placeholder whose parent could not be found renders as
    // the raw `more:<id>` string in the pane, with no hidden count and no parent title.
    const out = capFanOut(twoWideParents(), 10);
    const ids = new Set(out.nodes.map((n) => n.id));
    const found = placeholders(out);
    expect(found).toHaveLength(2);
    for (const more of found) {
      expect(isMoreId(more.id)).toBe(true);
      const parent = moreParentId(more.id);
      expect(ids.has(parent)).toBe(true);
      // …and it is the node the placeholder actually hangs off, not just any node.
      expect(out.edges).toContainEqual({ parent, child: more.id });
      expect(isMoreId(parent)).toBe(false);
    }
    expect(found.map((m) => moreParentId(m.id)).sort()).toEqual(["alpha", "beta"]);
  });

  it("emits no placeholder for a capped parent that the cap itself hid", () => {
    // A parent is capped LOCALLY, before reachability is known, so a wide parent that is
    // itself dropped behind another cap still has a hidden-count entry. Emitting its
    // placeholder anyway would attach an edge to a parent that is not in the node list —
    // ELK rejects an edge whose endpoint it was never given, so this is a layout error, not
    // a cosmetic one.
    const nodes = [node("r", 0)];
    const edges: SpawnEdge[] = [];
    // THREE wide children of r, so a budget of 2 caps r itself and keeps only the newest.
    for (const [p, born] of [["old", 10], ["mid", 20], ["new", 30]] as const) {
      nodes.push(node(p, born));
      edges.push({ parent: "r", child: p });
      for (let i = 0; i < 8; i++) {
        nodes.push(node(`${p}-c${i}`, 100 + i));
        edges.push({ parent: p, child: `${p}-c${i}` });
      }
    }
    const out = capFanOut({ nodes, edges }, 2);
    const ids = new Set(out.nodes.map((n) => n.id));
    expect(ids.has("new")).toBe(true);
    expect(ids.has("old")).toBe(false);
    expect(ids.has("mid")).toBe(false);
    // `old` and `mid` were capped too, but they are off screen, so neither gets a placeholder
    // and no edge points at a node ELK was never handed.
    expect(placeholders(out).map((n) => n.id)).toEqual([`${MORE_ID_PREFIX}new`,
                                                        `${MORE_ID_PREFIX}r`]);
    for (const e of out.edges) {
      expect(ids.has(e.parent)).toBe(true);
      expect(ids.has(e.child)).toBe(true);
    }
  });

  it("inherits an undated parent's null date rather than substituting a number", () => {
    // The placeholder is ranked by date like any other child if a later pass re-caps it, so
    // its date must be the parent's. A parent with no `created_at_ms` must yield null, NOT 0
    // — sorting an undated node as epoch is precisely the silent-always-dropped bug the
    // recency comparator exists to avoid.
    const nodes = [node("undated", null)];
    const edges: SpawnEdge[] = [];
    for (let i = 0; i < 6; i++) {
      nodes.push(node(`k${i}`, 500 + i));
      edges.push({ parent: "undated", child: `k${i}` });
    }
    const [more] = placeholders(capFanOut({ nodes, edges }, 3));
    expect(more.created_at_ms).toBeNull();
    // The dated case, for contrast — same code path, different input.
    const [dated] = placeholders(capFanOut(fanOut(6), 3));
    expect(dated.created_at_ms).toBe(0); // `fanOut` gives the parent created_at_ms 0
  });

  it("ignores an incoming placeholder that claims to hide nothing", () => {
    // A `more:` edge is treated as this function's own output and folded into the new tally
    // rather than counted as a child. One that reports 0 hidden children carries no tally to
    // fold, so it must not manufacture a "+0 more" node on a parent that is not over budget.
    const out = capFanOut(
      {
        nodes: [node("p", 0), node("real", 5)],
        edges: [{ parent: "p", child: "real" }, { parent: "p", child: `${MORE_ID_PREFIX}p` }],
      },
      10,
    );
    expect(placeholders(out)).toEqual([]);
    expect(out.nodes.map((n) => n.id)).toEqual(["p", "real"]);
    expect(out.edges).toEqual([{ parent: "p", child: "real" }]);
  });

  it("emits no duplicate node id and no duplicate edge, on any shape", () => {
    // This is the PRECONDITION that makes three comparator arms in this module dead code —
    // the `: 0` equal-case in the child rank (`capFanOut.ts:97`), in the node sort (`:219`)
    // and in the edge sort (`:226`). Each can only fire on two entries that compare equal,
    // and none can, because children are collected into a `Set` per parent, nodes come from
    // `Map` keys, and there is at most one placeholder per parent. Rather than assert those
    // arms are unreachable in prose, assert the property that makes them so — if a future
    // change ever lets a duplicate through, this fails and the arms become live at once.
    const shapes: Array<[string, FanOutGraph, number]> = [
      ["wide single parent", fanOut(50), 10],
      ["two wide parents", twoWideParents(), 10],
      ["re-capped output", capFanOut(fanOut(50), 10), 5],
      ["diamond", {
        nodes: [node("p", 0), node("q", 0), node("a", 10), node("b", 20), node("c", 30)],
        edges: [
          { parent: "p", child: "a" }, { parent: "p", child: "b" },
          { parent: "p", child: "c" }, { parent: "q", child: "a" },
        ],
      }, 2],
      ["cycle with no root", {
        nodes: [node("x", 1), node("y", 2)],
        edges: [{ parent: "x", child: "y" }, { parent: "y", child: "x" }],
      }, 100],
      ["self-loop and dangling endpoint", {
        nodes: [node("p", 0)],
        edges: [{ parent: "p", child: "p" }, { parent: "p", child: "ghost" }],
      }, 1],
    ];
    for (const [name, graph, budget] of shapes) {
      const out = capFanOut(graph, budget);
      const ids = out.nodes.map((n) => n.id);
      expect(new Set(ids).size, `${name}: duplicate node id`).toBe(ids.length);
      const keys = out.edges.map((e) => `${e.parent} ${e.child}`);
      expect(new Set(keys).size, `${name}: duplicate edge`).toBe(keys.length);
      // The rank comparator's own input: distinct children per parent, by construction.
      const perParent = new Map<string, string[]>();
      for (const e of out.edges) perParent.set(e.parent, [...(perParent.get(e.parent) ?? []),
                                                          e.child]);
      for (const [parent, kids] of perParent) {
        expect(new Set(kids).size, `${name}: ${parent} has a repeated child`)
          .toBe(kids.length);
      }
    }
  });

  it("holds the whole graph under the measured layer-width limit", () => {
    // The actual contract, stated as the property that matters rather than as a count:
    // after capping, NO node has more than `threshold` children.
    const out = capFanOut(fanOut(4844), 100);
    const childCount = new Map<string, number>();
    for (const e of out.edges) childCount.set(e.parent, (childCount.get(e.parent) ?? 0) + 1);
    expect(Math.max(...childCount.values())).toBeLessThanOrEqual(100);
  });
});
