/**
 * The empty-state rule for the virtualized lists.
 *
 * Why a pure function exists at all: this project's vitest runs with
 * `environment: "node"`, so there is no DOM to assert an attribute against. Extracting
 * the DECISION keeps it genuinely testable, while `setItems` does nothing but apply the
 * result. That split is the only reason this behaviour has a test rather than a promise.
 *
 * Why the behaviour matters: a CSS `:empty` selector cannot express it, because
 * VirtualList always mounts a sizer child — the viewport is never empty in the DOM sense
 * even when it displays nothing. Before this, those panes rendered as blank grey
 * rectangles that read as broken, which is exactly how they appeared in the first visual
 * capture of the cockpit.
 */
import { describe, expect, it } from "vitest";

import { emptyStateLabel } from "./virtualList";

describe("emptyStateLabel", () => {
  it("returns the label when the list holds nothing", () => {
    expect(emptyStateLabel(0, "No threads yet.")).toBe("No threads yet.");
  });

  it("returns null as soon as there is a single item", () => {
    expect(emptyStateLabel(1, "No threads yet.")).toBeNull();
  });

  it("returns null for a populated list", () => {
    expect(emptyStateLabel(250, "No threads yet.")).toBeNull();
  });

  it("returns null when no label was configured, even while empty", () => {
    // A caller that opts out must not get an empty `data-empty` attribute, which would
    // render as a zero-height ::after box — worse than no empty state at all.
    expect(emptyStateLabel(0, undefined)).toBeNull();
  });

  it("treats an empty-string label as opting out", () => {
    expect(emptyStateLabel(0, "")).toBeNull();
  });
});
