import { describe, expect, it } from "vitest";

import type { CorpusDiffDto } from "../ipc/types";
import { DIFF_STYLES, diffToOverlay, edgeKey } from "./diffOverlay";

/** The all-empty diff (two structurally-identical corpora). */
const EMPTY: CorpusDiffDto = {
  added_nodes: [],
  removed_nodes: [],
  added_edges: [],
  removed_edges: [],
  changed_nodes: {},
};

/** Build a CorpusDiffDto from a partial, defaulting every unset field to empty. */
function makeDiff(partial: Partial<CorpusDiffDto>): CorpusDiffDto {
  return { ...EMPTY, ...partial };
}

describe("edgeKey", () => {
  it("joins parent and child with a single space (the layout edge-key convention)", () => {
    expect(edgeKey("A", "B")).toBe("A B");
  });

  it("is order-sensitive: parent->child differs from child->parent", () => {
    expect(edgeKey("A", "B")).not.toBe(edgeKey("B", "A"));
  });
});

describe("DIFF_STYLES", () => {
  it("covers exactly the three diff kinds", () => {
    expect(Object.keys(DIFF_STYLES).sort()).toEqual(["added", "changed", "removed"]);
  });

  it("maps added->green (solid), removed->red (ghost), changed->amber (solid)", () => {
    // added is a solid green, changed a solid amber; removed is a red GHOST (faded /
    // outline, since a removed node no longer exists in the new graph).
    expect(DIFF_STYLES.added.ghost).toBe(false);
    expect(DIFF_STYLES.changed.ghost).toBe(false);
    expect(DIFF_STYLES.removed.ghost).toBe(true);
  });

  it("gives each kind a distinct colour", () => {
    const colors = [
      DIFF_STYLES.added.color,
      DIFF_STYLES.removed.color,
      DIFF_STYLES.changed.color,
    ];
    expect(new Set(colors).size).toBe(3);
    for (const c of colors) expect(c).toMatch(/^#[0-9a-f]{6}$/i);
  });
});

describe("diffToOverlay", () => {
  it("maps an empty diff to three empty maps", () => {
    const overlay = diffToOverlay(EMPTY);
    expect(overlay.nodeClass).toEqual({});
    expect(overlay.edgeClass).toEqual({});
    expect(overlay.tooltips).toEqual({});
  });

  it("classifies added nodes as 'added' with no tooltip", () => {
    const overlay = diffToOverlay(makeDiff({ added_nodes: ["N1"] }));
    expect(overlay.nodeClass).toEqual({ N1: "added" });
    expect(overlay.tooltips).toEqual({});
  });

  it("classifies removed nodes as 'removed' with no tooltip", () => {
    const overlay = diffToOverlay(makeDiff({ removed_nodes: ["N9"] }));
    expect(overlay.nodeClass).toEqual({ N9: "removed" });
    expect(overlay.tooltips).toEqual({});
  });

  it("classifies changed nodes as 'changed' with a field-delta tooltip", () => {
    const overlay = diffToOverlay(
      makeDiff({ changed_nodes: { N5: { tokens_used: [100, 250] } } }),
    );
    expect(overlay.nodeClass).toEqual({ N5: "changed" });
    expect(overlay.tooltips).toEqual({ N5: "tokens_used: 100 -> 250" });
  });

  it("renders every changed field, in declaration order, one per line", () => {
    const overlay = diffToOverlay(
      makeDiff({
        changed_nodes: {
          N5: { title: ["Old title", "New title"], tokens_used: [1, 2] },
        },
      }),
    );
    expect(overlay.tooltips.N5).toBe(
      ["title: Old title -> New title", "tokens_used: 1 -> 2"].join("\n"),
    );
  });

  it("renders null on either side of a field delta", () => {
    const overlay = diffToOverlay(
      makeDiff({
        changed_nodes: {
          N5: { created_at_ms: [null, 1700], git_branch: ["main", null] },
        },
      }),
    );
    expect(overlay.tooltips.N5).toBe(
      ["created_at_ms: null -> 1700", "git_branch: main -> null"].join("\n"),
    );
  });

  it("classifies added edges as 'added', keyed by edgeKey", () => {
    const overlay = diffToOverlay(
      makeDiff({ added_edges: [{ parent: "A", child: "B" }] }),
    );
    expect(overlay.edgeClass).toEqual({ [edgeKey("A", "B")]: "added" });
  });

  it("classifies removed edges as 'removed', keyed by edgeKey", () => {
    const overlay = diffToOverlay(
      makeDiff({ removed_edges: [{ parent: "C", child: "D" }] }),
    );
    expect(overlay.edgeClass).toEqual({ [edgeKey("C", "D")]: "removed" });
  });

  it("ignores edge status when classifying (edge identity is parent->child)", () => {
    const overlay = diffToOverlay(
      makeDiff({ added_edges: [{ parent: "A", child: "B", status: "failed" }] }),
    );
    expect(overlay.edgeClass).toEqual({ "A B": "added" });
  });

  it("orders nodeClass keys by id regardless of input order", () => {
    const overlay = diffToOverlay(
      makeDiff({
        added_nodes: ["B", "A"],
        removed_nodes: ["D", "C"],
        changed_nodes: { E: { title: ["x", "y"] } },
      }),
    );
    expect(Object.keys(overlay.nodeClass)).toEqual(["A", "B", "C", "D", "E"]);
    expect(overlay.nodeClass).toEqual({
      A: "added",
      B: "added",
      C: "removed",
      D: "removed",
      E: "changed",
    });
  });

  it("orders edgeClass keys deterministically regardless of input order", () => {
    const overlay = diffToOverlay(
      makeDiff({
        added_edges: [
          { parent: "B", child: "z" },
          { parent: "A", child: "b" },
        ],
        removed_edges: [{ parent: "A", child: "a" }],
      }),
    );
    expect(Object.keys(overlay.edgeClass)).toEqual(["A a", "A b", "B z"]);
  });

  it("orders tooltips keys by id regardless of input order", () => {
    const overlay = diffToOverlay(
      makeDiff({
        changed_nodes: {
          Z: { title: ["a", "b"] },
          A: { title: ["c", "d"] },
        },
      }),
    );
    expect(Object.keys(overlay.tooltips)).toEqual(["A", "Z"]);
  });

  it("handles a combined diff across all four categories", () => {
    const overlay = diffToOverlay(
      makeDiff({
        added_nodes: ["n-add"],
        removed_nodes: ["n-rem"],
        changed_nodes: { "n-chg": { tokens_used: [5, 9] } },
        added_edges: [{ parent: "n-add", child: "n-chg" }],
        removed_edges: [{ parent: "n-rem", child: "n-chg" }],
      }),
    );
    expect(overlay.nodeClass).toEqual({
      "n-add": "added",
      "n-chg": "changed",
      "n-rem": "removed",
    });
    expect(overlay.edgeClass).toEqual({
      "n-add n-chg": "added",
      "n-rem n-chg": "removed",
    });
    expect(overlay.tooltips).toEqual({ "n-chg": "tokens_used: 5 -> 9" });
  });

  it("is pure: it does not mutate the input diff", () => {
    const input = makeDiff({
      added_nodes: ["B", "A"],
      added_edges: [{ parent: "A", child: "B" }],
      changed_nodes: { A: { tokens_used: [1, 2] } },
    });
    const snapshot = JSON.parse(JSON.stringify(input));
    diffToOverlay(input);
    expect(input).toEqual(snapshot);
  });

  it("is referentially transparent: same input -> byte-identical output", () => {
    const input = makeDiff({
      added_nodes: ["B", "A"],
      removed_edges: [{ parent: "C", child: "D" }],
      changed_nodes: { E: { title: ["x", "y"], tokens_used: [1, 2] } },
    });
    expect(JSON.stringify(diffToOverlay(input))).toBe(
      JSON.stringify(diffToOverlay(input)),
    );
  });
});
