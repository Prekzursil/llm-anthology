/**
 * What a search result actually SAYS, and what the status line admits.
 *
 * Pure, because vitest runs `environment: "node"` here — the same split `emptyStateLabel`
 * uses. Each of these encodes a defect the shipped panel had:
 *
 *   * a hit row rendered the snippet and nothing else, so an untitled conversation was a
 *     BLANK, unlabelled, clickable button;
 *   * `ts_ms` came back on every hit and was thrown away, so results had no time context in
 *     a tool whose entire subject is sessions over time;
 *   * the panel asked for 200 and reported the true total, so "1,432 hits" sat above a list
 *     holding 200 — the same silent-truncation class as the 1000-root sidebar.
 */
import { describe, expect, it } from "vitest";

import {
  hitLabel,
  nextFilterValue,
  providerOptions,
  relativeWhen,
  resultStatus,
  searchParams,
} from "./searchPresent";
import type { SearchHit } from "../ipc/types";

const hit = (over: Partial<SearchHit> = {}): SearchHit => ({
  conversation_id: "conv-abc123",
  snippet: "fixing the layout timeout",
  score: 1,
  provider: "codex",
  ...over,
});

describe("hitLabel", () => {
  it("uses the snippet when there is one", () => {
    expect(hitLabel(hit())).toBe("fixing the layout timeout");
  });

  it("never returns empty, because an empty row is an invisible clickable button", () => {
    for (const snippet of ["", "   ", "\t\n"]) {
      expect(hitLabel(hit({ snippet })).trim()).not.toBe("");
    }
  });

  it("identifies the conversation when there is no title to show", () => {
    // "Untitled" alone would make every untitled hit look identical; the id is the only
    // thing that distinguishes them.
    expect(hitLabel(hit({ snippet: "" }))).toBe("untitled · conv-abc123");
  });

  it("collapses whitespace so a multi-line title cannot break the row height", () => {
    expect(hitLabel(hit({ snippet: "a\n\n  long   \t title" }))).toBe("a long title");
  });
});

describe("relativeWhen", () => {
  const NOW = Date.UTC(2026, 7, 7, 12, 0, 0);
  const ago = (ms: number) => relativeWhen(NOW - ms, NOW);

  it("says nothing at all when the hit has no timestamp", () => {
    // An undated conversation is real (the legacy canonical DB has no updated_at_ms).
    // Inventing "now" for it would be a lie; an empty cell is honest.
    expect(relativeWhen(undefined, NOW)).toBe("");
  });

  it("buckets recent hits by elapsed time", () => {
    expect(ago(30 * 60_000)).toBe("just now");
    expect(ago(5 * 3600_000)).toBe("5h ago");
    expect(ago(3 * 86_400_000)).toBe("3d ago");
    expect(ago(20 * 86_400_000)).toBe("2w ago");
  });

  it("falls back to an absolute UTC date once relative stops being useful", () => {
    // "43w ago" tells nobody anything. The date is labelled UTC because that is what it is;
    // deriving a local calendar day here would make this test depend on the runner's TZ.
    expect(ago(300 * 86_400_000)).toBe("2025-10-11");
  });

  it("does not render a future timestamp as a negative age", () => {
    // Clock skew and a rollout written a moment ahead of the query both produce this.
    expect(relativeWhen(NOW + 60_000, NOW)).toBe("just now");
  });

  it("is exact at the bucket boundaries", () => {
    expect(ago(3600_000 - 1)).toBe("just now");
    expect(ago(3600_000)).toBe("1h ago");
    expect(ago(86_400_000 - 1)).toBe("23h ago");
    expect(ago(86_400_000)).toBe("1d ago");
    expect(ago(7 * 86_400_000 - 1)).toBe("6d ago");
    expect(ago(7 * 86_400_000)).toBe("1w ago");
  });
});

describe("resultStatus", () => {
  it("reports the total when everything matched is on screen", () => {
    expect(resultStatus({ total: 12, shown: 12, tookMs: 4 })).toBe("12 hits · 4ms");
  });

  it("says so when the list is a TRUNCATED view of the total", () => {
    // The defect: "1432 hits" above a list of 200, with nothing saying which you are
    // looking at. A user scrolls to the bottom and concludes the rest do not exist.
    expect(resultStatus({ total: 1432, shown: 200, tookMs: 31 }))
      .toBe("showing 200 of 1,432 hits · 31ms");
  });

  it("groups thousands so a big number is readable at a glance", () => {
    expect(resultStatus({ total: 1_234_567, shown: 200, tookMs: 9 }))
      .toContain("of 1,234,567 hits");
  });

  it("uses the singular for exactly one", () => {
    expect(resultStatus({ total: 1, shown: 1, tookMs: 2 })).toBe("1 hit · 2ms");
  });

  it("says nothing matched rather than reporting zero of zero", () => {
    expect(resultStatus({ total: 0, shown: 0, tookMs: 7 })).toBe("No matches · 7ms");
  });

  it("names the filter when one is narrowing the result", () => {
    // Otherwise a filtered search that finds nothing reads as an empty corpus.
    expect(resultStatus({ total: 0, shown: 0, tookMs: 3, provider: "grok" }))
      .toBe("No matches in grok · 3ms");
    expect(resultStatus({ total: 5, shown: 5, tookMs: 3, provider: "grok" }))
      .toBe("5 hits in grok · 3ms");
  });
});

describe("searchParams", () => {
  it("SENDS the provider when one is selected", () => {
    // The defect this pins: `SearchParams.provider` was on the wire contract from the
    // start and the panel never sent it. A capability nothing exercises is
    // indistinguishable from one that does not work.
    expect(searchParams("timeout", "grok", 200))
      .toEqual({ q: "timeout", limit: 200, provider: "grok" });
  });

  it("OMITS it rather than sending an empty string", () => {
    // The engine accepts any string as a filter, so `provider: ""` filters to conversations
    // whose provider is the empty string — every search would return nothing.
    expect(searchParams("timeout", "", 200)).toEqual({ q: "timeout", limit: 200 });
    expect("provider" in searchParams("timeout", "", 200)).toBe(false);
  });
});

describe("nextFilterValue", () => {
  const options = [
    { value: "", label: "All providers (10)" },
    { value: "codex", label: "codex (7)" },
    { value: "grok", label: "grok (3)" },
  ];

  it("keeps a selection that still exists after a corpus reload", () => {
    expect(nextFilterValue(options, "grok")).toBe("grok");
  });

  it("falls back to everything when the selected provider is gone", () => {
    // Otherwise opening a codex-only corpus with "grok" selected leaves the filter pinned
    // to a provider with no rows, and the empty list reads as an empty corpus.
    expect(nextFilterValue(options, "gemini")).toBe("");
    expect(nextFilterValue([], "codex")).toBe("");
  });

  it("treats no selection as everything", () => {
    expect(nextFilterValue(options, "")).toBe("");
  });
});

describe("providerOptions", () => {
  it("offers everything plus one entry per provider actually in the corpus", () => {
    expect(providerOptions({ codex: 2042, grok: 71 })).toEqual([
      { value: "", label: "All providers (2,113)" },
      { value: "codex", label: "codex (2,042)" },
      { value: "grok", label: "grok (71)" },
    ]);
  });

  it("orders by size, because the big store is the one people filter to", () => {
    expect(providerOptions({ grok: 71, codex: 2042, gemini: 300 }).map((o) => o.value))
      .toEqual(["", "codex", "gemini", "grok"]);
  });

  it("breaks a tie by name so the list does not reshuffle between renders", () => {
    expect(providerOptions({ zulu: 5, alpha: 5 }).map((o) => o.value))
      .toEqual(["", "alpha", "zulu"]);
  });

  it("offers no filter at all for a single-provider corpus", () => {
    // A filter with one real choice is pure noise — the corpus IS that provider.
    expect(providerOptions({ codex: 10 })).toEqual([]);
  });

  it("offers no filter for an empty corpus", () => {
    expect(providerOptions({})).toEqual([]);
  });

  it("ignores a provider with no conversations", () => {
    expect(providerOptions({ codex: 10, grok: 0, gemini: 4 }).map((o) => o.value))
      .toEqual(["", "codex", "gemini"]);
  });
});
