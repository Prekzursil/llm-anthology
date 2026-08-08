/**
 * The annotation panel's decisions: tag entry, the search-scope story, and the tri-state write.
 *
 * Three traps drive almost every test here, and all three are real rather than imagined:
 *
 *  1. `metadata.set` takes `tags` (a LIST); `metadata.search` takes `tag` (a SINGULAR STRING).
 *     Passing the wrong one silently returns `[]` and reads as "search is broken".
 *  2. With NEITHER filter, `metadata.search` returns `[]` BY DESIGN (`sidecar.py:1367-1368`,
 *     `metadata.py:501-502`) so a blank query cannot dump the catalogue. An empty result from a
 *     blank query is therefore NOT "no matches", and saying "0 results" there reports a working
 *     engine as broken.
 *  3. `metadata.set` is a PARTIAL update: an omitted field is left alone, `""`/`[]` CLEARS
 *     (`ipc/types.ts:470-478`). A panel that always sends all three blanks the two the user did
 *     not touch.
 *
 * And one measured fact shapes the tag rules: the engine dedups with Python `casefold()`
 * (`llm_anthology/metadata.py:214-240`), which JS `toLowerCase()` does NOT reproduce —
 * `Straße`/`STRASSE` casefold to one tag but lowercase to two; likewise `ﬁle`, and `ΣΣ`
 * (`σσ` vs `σς`). So the client normalization is a conservative PREVIEW and the engine is
 * authoritative; the panel re-renders from the response, never from its own guess.
 */
import { describe, expect, it, vi } from "vitest";

import {
  ANNOTATION_SCOPE_NOTE,
  annotationSummary,
  changedSetParams,
  dirtyFields,
  draftFrom,
  facetOrder,
  formatTagInput,
  isBlankQuery,
  MetadataController,
  normalizeTags,
  parseTagInput,
  searchOutcome,
  searchQueryParams,
  type MetadataIpc,
} from "./metadataPanel";
import type { Annotation, MetadataSearchRow, TagCount } from "../ipc/types";

function annotation(over: Partial<Annotation> = {}): Annotation {
  const base: Annotation = {
    conversation_id: "c1", alias: "", tags: [], notes: "", is_empty: true,
  };
  const merged = { ...base, ...over };
  merged.is_empty = merged.alias === "" && merged.tags.length === 0 && merged.notes === "";
  return merged;
}

function row(id: string, over: Partial<MetadataSearchRow> = {}): MetadataSearchRow {
  return {
    conversation_id: id, provider: "codex", account: "", title: id,
    created_at: "2026-01-01", updated_at: "2026-01-02", turn_count: 3, thread_id: "t1",
    annotation: annotation({ conversation_id: id, tags: ["x"] }),
    ...over,
  };
}

/** A fake engine that records calls. `setResult` lets a test make the engine DISAGREE. */
function fakeIpc(over: Partial<MetadataIpc> = {}) {
  const calls: string[] = [];
  const base: MetadataIpc = {
    async metadataGet(id) {
      calls.push(`get(${id})`);
      return annotation({ conversation_id: id, alias: "boot", tags: ["alpha"], notes: "n" });
    },
    async metadataSet(params) {
      calls.push(`set(${JSON.stringify(params)})`);
      return annotation({ ...params, conversation_id: params.conversation_id } as Annotation);
    },
    async metadataClear(id) {
      calls.push(`clear(${id})`);
      return annotation({ conversation_id: id });
    },
    async metadataSearch(params) {
      calls.push(`search(${JSON.stringify(params ?? {})})`);
      return [row("c1")];
    },
    async metadataTags() {
      calls.push("tags()");
      return [{ tag: "alpha", count: 2 }];
    },
  };
  return Object.assign({ calls }, base, over);
}

describe("normalizeTags", () => {
  it("collapses every whitespace RUN to a single space, and drops blanks", () => {
    // Mirrors `" ".join(clean_text(raw).split())` (`metadata.py:233`). The newline part is
    // load-bearing: `\n` is the store's wire separator, so a tag carrying one would split
    // into two on the round trip.
    expect(normalizeTags(["  spaced   out  ", "", "   ", "two\nlines", "tab\there"]))
      .toEqual(["spaced out", "tab here", "two lines"]);
  });

  it("dedups case-insensitively, keeping the FIRST-SEEN casing", () => {
    // `metadata.py:236-239`: so a facet can never show 'Renderer' and 'renderer' as two
    // tags, and re-adding one in other casing does not rewrite what the owner typed.
    expect(normalizeTags(["Beta", "alpha", "BETA", "Alpha"])).toEqual(["alpha", "Beta"]);
  });

  it("orders by the FOLDED tag, never by insertion", () => {
    // Mirrors the observable half of `sorted(key=lambda t: (t.casefold(), t))` — two callers
    // adding the same tags in different orders must produce identical output. The engine's
    // exact-string tiebreak is unreachable here and deliberately absent; the distinct-folded-
    // form test below is what keeps that true.
    expect(normalizeTags(["zeta", "b", "A"])).toEqual(["A", "b", "zeta"]);
    expect(normalizeTags(["A", "b", "zeta"])).toEqual(["A", "b", "zeta"]);
  });

  it("does not mutate the array it is handed", () => {
    const input = ["b", "a"];
    normalizeTags(input);
    expect(input).toEqual(["b", "a"]);
  });

  it("leaves every survivor with a DISTINCT folded form", () => {
    // The property the sort depends on. `normalizeTags` omits the engine's exact-string
    // tiebreak because a fold-keyed dedup makes it unreachable; if dedup were ever weakened
    // this fails HERE, rather than the ordering quietly becoming non-deterministic.
    //
    // THIS PROPERTY IS ALSO WHY `metadataPanel.ts` CANNOT REACH 100% BRANCH COVERAGE, and the
    // gap is a proof rather than a missing test. The comparator ends `la > lb ? 1 : 0`, and
    // that final `0` is dead: `seen` is a Map keyed by `tag.toLowerCase()` holding one value
    // per key, so every value's own lowercase IS its key; distinct keys therefore give
    // distinct `la`/`lb`, and `sort` never passes one element as both arguments. So `la === lb`
    // cannot arise and the `0` arm never executes. It is kept anyway because a comparator that
    // answered `1` for equal inputs would be non-symmetric, which makes `sort`'s result
    // implementation-defined the moment the dedup invariant above ever slips.
    const out = normalizeTags(["Beta", "BETA", "beta", "a", "A", "  a  "]);
    const folded = out.map((t) => t.toLowerCase());
    expect(new Set(folded).size).toBe(out.length);
    // Stated directly, so the unreachability argument rests on an assertion and not on prose.
    for (let i = 1; i < out.length; i += 1) {
      expect(folded[i - 1] < folded[i]).toBe(true);
    }
  });

  it("stays CONSERVATIVE where casefold would collapse further", () => {
    // MEASURED: `'Straße'.casefold() === 'strasse'` but `'Straße'.toLowerCase() === 'straße'`.
    // The client therefore keeps two tags the engine will merge into one. That direction is
    // the safe one — the client never hides a distinction the engine preserves — and the
    // round trip corrects it, which the controller test below pins.
    expect(normalizeTags(["Straße", "STRASSE"])).toHaveLength(2);
  });
});

describe("parseTagInput / formatTagInput", () => {
  it("splits on commas AND newlines, so either entry habit works", () => {
    expect(parseTagInput("alpha, beta\ngamma")).toEqual(["alpha", "beta", "gamma"]);
  });

  it("treats a lone separator run as no tags at all", () => {
    expect(parseTagInput(" , ,\n ")).toEqual([]);
    expect(parseTagInput("")).toEqual([]);
  });

  it("round-trips the stored form back into the field", () => {
    expect(formatTagInput(["alpha", "Beta"])).toBe("alpha, Beta");
    expect(formatTagInput([])).toBe("");
  });

  it("is idempotent through a format/parse cycle", () => {
    const once = parseTagInput("BETA, alpha, beta");
    expect(parseTagInput(formatTagInput(once))).toEqual(once);
  });
});

describe("the blank-query rule", () => {
  it("treats whitespace-only filters as no filter", () => {
    expect(isBlankQuery("   ", " \n ")).toBe(true);
    expect(isBlankQuery("x", "")).toBe(false);
    expect(isBlankQuery("", "x")).toBe(false);
  });

  it("reports a blank query as IDLE, never as zero matches", () => {
    // The trap, stated as an assertion. With neither filter the engine returns `[]` by
    // design, so a panel keyed on `rows.length === 0` calls a working search broken.
    const out = searchOutcome("", "", 0);
    expect(out.kind).toBe("idle");
    expect(out.message).not.toMatch(/\b0\b|no match|nothing found/i);
    expect(out.message.toLowerCase()).toContain("tag");
  });

  it("distinguishes a REAL empty result from the blank query", () => {
    const out = searchOutcome("", "nope", 0);
    expect(out.kind).toBe("empty");
    expect(out.message.toLowerCase()).toMatch(/no annotation/);
  });

  it("counts hits, pluralised", () => {
    expect(searchOutcome("x", "", 1).message).toBe("1 annotated conversation");
    expect(searchOutcome("x", "", 4).message).toBe("4 annotated conversations");
    expect(searchOutcome("x", "", 4).kind).toBe("hits");
  });

  it("never lets the UI imply that transcripts are searched", () => {
    // `metadata.search` is over ANNOTATIONS ONLY (`ipc/types.ts:495`). A user who concluded
    // a tag search covers message text would trust a false negative about their own corpus.
    expect(ANNOTATION_SCOPE_NOTE.toLowerCase()).toContain("annotation");
    expect(ANNOTATION_SCOPE_NOTE.toLowerCase()).toMatch(/not|never/);
    for (const out of [searchOutcome("", "", 0), searchOutcome("a", "", 0)]) {
      expect(out.message.toLowerCase()).not.toMatch(/transcript|message text|body/);
    }
  });
});

describe("searchQueryParams", () => {
  it("sends `tag` SINGULAR, which is the whole trap", () => {
    const params = searchQueryParams("hello", "alpha");
    expect(params).toEqual({ text: "hello", tag: "alpha" });
    expect(Object.keys(params)).not.toContain("tags");
  });

  it("OMITS an empty filter rather than sending an empty string", () => {
    expect(Object.keys(searchQueryParams("hello", ""))).toEqual(["text"]);
    expect(Object.keys(searchQueryParams("", "alpha"))).toEqual(["tag"]);
    expect(searchQueryParams("  ", "  ")).toEqual({});
  });

  it("trims, so a stray space does not become a filter that matches nothing", () => {
    expect(searchQueryParams("  hello  ", " alpha ")).toEqual({ text: "hello", tag: "alpha" });
  });
});

describe("changedSetParams — the tri-state write", () => {
  const stored = annotation({ alias: "old", tags: ["alpha"], notes: "note" });

  it("returns null when nothing changed, so a no-op never hits the wire", () => {
    expect(changedSetParams(stored, draftFrom(stored))).toBeNull();
  });

  it("sends ONLY the fields that changed", () => {
    const draft = { ...draftFrom(stored), alias: "new" };
    const params = changedSetParams(stored, draft);
    expect(params).toEqual({ conversation_id: "c1", alias: "new" });
    // The point: `tags` and `notes` are ABSENT, not empty. Present-but-empty would CLEAR them.
    expect(Object.keys(params ?? {}).sort()).toEqual(["alias", "conversation_id"]);
  });

  it("sends an EMPTY value to clear a field the user emptied", () => {
    expect(changedSetParams(stored, { ...draftFrom(stored), notes: "" }))
      .toEqual({ conversation_id: "c1", notes: "" });
    expect(changedSetParams(stored, { ...draftFrom(stored), tagText: "" }))
      .toEqual({ conversation_id: "c1", tags: [] });
  });

  it("never emits null for any field", () => {
    const params = changedSetParams(stored, { alias: "", tagText: "", notes: "" });
    // Asserted BEFORE the loop, and that is the point of the line rather than a courtesy:
    // `Object.values(null ?? {})` is EMPTY, so if this ever returned null the loop below
    // would run zero assertions and the test would pass green while proving nothing.
    expect(params).not.toBeNull();
    for (const value of Object.values(params ?? {})) expect(value).not.toBeNull();
  });

  it("normalises tags on the way out, so the wire gets the canonical form", () => {
    const draft = { ...draftFrom(stored), tagText: "BETA,  alpha ,beta" };
    expect(changedSetParams(stored, draft)?.tags).toEqual(["alpha", "BETA"]);
  });

  it("does NOT treat a reordered tag list as a change", () => {
    const two = annotation({ alias: "a", tags: ["alpha", "beta"], notes: "" });
    expect(changedSetParams(two, { ...draftFrom(two), tagText: "beta, alpha" })).toBeNull();
  });

  it("DOES treat a recased tag as a change, because the store keeps what was typed", () => {
    const two = annotation({ tags: ["Beta"] });
    expect(changedSetParams(two, { ...draftFrom(two), tagText: "beta" })?.tags).toEqual(["beta"]);
  });
});

describe("dirtyFields", () => {
  const stored = annotation({ alias: "a", tags: ["t"], notes: "n" });

  it("is empty for an untouched draft", () => {
    expect(dirtyFields(stored, draftFrom(stored))).toEqual([]);
  });

  it("names each edited field, in a stable order", () => {
    expect(dirtyFields(stored, { alias: "b", tagText: "u", notes: "m" }))
      .toEqual(["alias", "tags", "notes"]);
  });

  it("trims the free-text fields, so trailing whitespace is not an edit", () => {
    expect(dirtyFields(stored, { ...draftFrom(stored), alias: "a  " })).toEqual([]);
  });
});

describe("facetOrder", () => {
  const counts: TagCount[] = [
    { tag: "alpha", count: 2 }, { tag: "beta", count: 9 }, { tag: "gamma", count: 2 },
  ];

  it("puts the biggest tag first by default, tie-broken by tag for determinism", () => {
    expect(facetOrder(counts).map((c) => c.tag)).toEqual(["beta", "alpha", "gamma"]);
  });

  it("can keep the engine's own alphabetical order instead", () => {
    // `metadata.tags` already orders by the CASEFOLDED tag (`metadata.py:544`); count-first
    // is a UI choice layered on top, not a disagreement with the store.
    expect(facetOrder(counts, "tag").map((c) => c.tag)).toEqual(["alpha", "beta", "gamma"]);
  });

  it("does not mutate the engine's array", () => {
    facetOrder(counts);
    expect(counts.map((c) => c.tag)).toEqual(["alpha", "beta", "gamma"]);
  });

  it("sorts a DESCENDING input in both modes, not only an already-ordered one", () => {
    // Both comparators here are `x < y ? -1 : 1`, and every earlier case in this describe
    // hands them input that is ALREADY in the wanted order. V8 then resolves a short
    // already-sorted run without ever needing a positive-then-negative pair, so the `-1` arm
    // of each comparator was never executed and a comparator inverted to `? 1 : -1` would
    // have passed every test above. Reversed input is what actually exercises it.
    const descending: TagCount[] = [{ tag: "gamma", count: 2 }, { tag: "alpha", count: 9 }];
    expect(facetOrder(descending, "tag").map((c) => c.tag)).toEqual(["alpha", "gamma"]);
    expect(facetOrder(descending).map((c) => c.tag)).toEqual(["alpha", "gamma"]);
  });

  it("breaks a COUNT tie on the tag, whichever order the engine listed them in", () => {
    // The tie-break is the half of `(b.count - a.count) || (a.tag < b.tag ? -1 : 1)` that
    // makes two renders of one corpus identical, so it has to hold for either input order.
    const tied: TagCount[] = [
      { tag: "gamma", count: 4 }, { tag: "alpha", count: 4 }, { tag: "beta", count: 4 },
    ];
    expect(facetOrder(tied).map((c) => c.tag)).toEqual(["alpha", "beta", "gamma"]);
    expect(facetOrder([...tied].reverse()).map((c) => c.tag)).toEqual(["alpha", "beta", "gamma"]);
  });
});

describe("annotationSummary", () => {
  it("says nothing at all for an un-annotated conversation", () => {
    // `is_empty` exists precisely so a panel need not guess (`ipc/types.ts:463`).
    expect(annotationSummary(annotation())).toBe("");
  });

  it("lists only the parts that are present", () => {
    expect(annotationSummary(annotation({ alias: "boot" }))).toBe("“boot”");
    expect(annotationSummary(annotation({ tags: ["a", "b"] }))).toBe("2 tags");
    expect(annotationSummary(annotation({ tags: ["a"], notes: "x" }))).toBe("1 tag · notes");
    expect(annotationSummary(annotation({ alias: "boot", tags: ["a"], notes: "x" })))
      .toBe("“boot” · 1 tag · notes");
  });
});

describe("MetadataController", () => {
  it("loads an annotation and seeds the draft from it", async () => {
    const ipc = fakeIpc();
    const ctl = new MetadataController(ipc, () => {});
    await ctl.load("c1");
    expect(ctl.state.annotation?.alias).toBe("boot");
    expect(ctl.state.draft).toEqual({ alias: "boot", tagText: "alpha", notes: "n" });
    expect(ctl.state.dirty).toEqual([]);
  });

  it("re-renders from the ENGINE's annotation, not from its own guess", async () => {
    // The architectural commitment. The client dedups with `toLowerCase()` and so sends two
    // tags; the engine casefolds and stores ONE. Whatever the client believed, what the panel
    // shows afterwards is what the store actually holds — which is why the measured
    // casefold/toLowerCase divergence is harmless rather than a silent inconsistency.
    const ipc = fakeIpc({
      async metadataSet(params) {
        return annotation({ conversation_id: params.conversation_id, tags: ["Straße"] });
      },
    });
    const ctl = new MetadataController(ipc, () => {});
    await ctl.load("c1");
    ctl.editDraft({ tagText: "Straße, STRASSE" });
    expect(parseTagInput(ctl.state.draft.tagText)).toHaveLength(2);   // the client's guess
    await ctl.save();
    expect(ctl.state.annotation?.tags).toEqual(["Straße"]);            // the engine's answer
    expect(ctl.state.draft.tagText).toBe("Straße");
    expect(ctl.state.dirty).toEqual([]);
  });

  it("does NOT call the engine when there is nothing to save", async () => {
    const ipc = fakeIpc();
    const ctl = new MetadataController(ipc, () => {});
    await ctl.load("c1");
    await ctl.save();
    expect(ipc.calls.filter((c) => c.startsWith("set("))).toHaveLength(0);
  });

  it("does NOT call metadata.search for a blank query", async () => {
    // Saves a pointless round trip AND avoids the `[]` that reads as "no matches".
    const ipc = fakeIpc();
    const ctl = new MetadataController(ipc, () => {});
    await ctl.search("  ", "");
    expect(ipc.calls.filter((c) => c.startsWith("search("))).toHaveLength(0);
    expect(ctl.state.outcome.kind).toBe("idle");
    expect(ctl.state.rows).toEqual([]);
  });

  it("searches with the singular `tag` key when a filter is present", async () => {
    const ipc = fakeIpc();
    const ctl = new MetadataController(ipc, () => {});
    await ctl.search("", "alpha");
    expect(ipc.calls).toContain('search({"tag":"alpha"})');
    expect(ctl.state.outcome.kind).toBe("hits");
    expect(ctl.state.rows.map((r) => r.conversation_id)).toEqual(["c1"]);
  });

  it("reports a genuinely empty result as empty, not as idle", async () => {
    const ipc = fakeIpc({ metadataSearch: async () => [] });
    const ctl = new MetadataController(ipc, () => {});
    await ctl.search("zzz", "");
    expect(ctl.state.outcome.kind).toBe("empty");
  });

  it("clears to the empty annotation the engine returns", async () => {
    const ipc = fakeIpc();
    const ctl = new MetadataController(ipc, () => {});
    await ctl.load("c1");
    await ctl.clear();
    expect(ctl.state.annotation?.is_empty).toBe(true);
    expect(ctl.state.draft).toEqual({ alias: "", tagText: "", notes: "" });
  });

  it("keeps the annotation on screen when a CLEAR fails", async () => {
    // A failed clear must not look like a successful one. If the panel blanked its own view
    // optimistically, the user would believe the annotation was gone while the store still
    // holds it — and the next load would resurrect it with no explanation.
    const ipc = fakeIpc({
      metadataClear: async () => {
        throw new Error("readonly database");
      },
    });
    const ctl = new MetadataController(ipc, () => {});
    await ctl.load("c1");
    await ctl.clear();
    expect(ctl.state.error).toContain("readonly database");
    expect(ctl.state.annotation?.alias).toBe("boot");
    expect(ipc.calls).not.toContain("tags()");
  });

  it("surfaces a GENUINE engine failure as text instead of rejecting", async () => {
    const ipc = fakeIpc({
      metadataGet: async () => {
        throw new Error("disk on fire");
      },
    });
    const ctl = new MetadataController(ipc, () => {});
    await ctl.load("c1");
    expect(ctl.state.error).toContain("disk on fire");
    expect(ctl.state.annotation).toBeNull();
  });

  it("goes QUIET for the not-attached state rather than repeating the corpus bar", async () => {
    // `errors.ts:48-62` records this being overcorrected once already: routing every secondary
    // status line through `engineErrorText` put "No corpus open — use “Open corpus…”" on
    // screen THREE times, each telling the user to look at the bar they were looking at. This
    // panel is a secondary surface, so it uses `engineStatusText` and says nothing here —
    // while a real fault above still speaks.
    const ipc = fakeIpc({
      metadataGet: async () => {
        throw new Error("no corpus attached");
      },
    });
    const ctl = new MetadataController(ipc, () => {});
    await ctl.load("c1");
    expect(ctl.state.error).toBe("");
    expect(ctl.state.annotation).toBeNull();
  });

  it("does not leave a stale hit count over an emptied result list", async () => {
    // The recurring defect in this repo, in its search-failure form: rows go away, the status
    // line keeps boasting about them. A failed search gets its own outcome kind.
    let fail = false;
    const ipc = fakeIpc({
      metadataSearch: async () => {
        if (fail) throw new Error("pipe died");
        return [row("c1"), row("c2")];
      },
    });
    const ctl = new MetadataController(ipc, () => {});
    await ctl.search("x", "");
    expect(ctl.state.outcome.message).toBe("2 annotated conversations");
    fail = true;
    await ctl.search("x", "");
    expect(ctl.state.rows).toEqual([]);
    expect(ctl.state.outcome.kind).toBe("failed");
    expect(ctl.state.outcome.message).not.toMatch(/2/);
  });

  it("lets a STALE load lose to a newer one", async () => {
    // Same guard as the reader: two quick selections must not race, or the panel ends up
    // showing conversation A's annotation under conversation B's heading.
    // The gate is built OUTSIDE the fake so `release` is a plain `() => void`. Assigning it
    // from inside a `new Promise` executor works at runtime but narrows to `null` for tsc,
    // which then rejects the call — the executor runs synchronously, and control-flow
    // analysis does not model that.
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const ipc = fakeIpc({
      async metadataGet(id) {
        if (id === "slow") {
          await gate;
          return annotation({ conversation_id: "slow", alias: "STALE" });
        }
        return annotation({ conversation_id: id, alias: "fresh" });
      },
    });
    const ctl = new MetadataController(ipc, () => {});
    const slow = ctl.load("slow");
    await ctl.load("quick");
    release();
    await slow;
    expect(ctl.state.annotation?.alias).toBe("fresh");
  });

  it("notifies the renderer on every state change", async () => {
    const onChange = vi.fn();
    const ctl = new MetadataController(fakeIpc(), onChange);
    await ctl.load("c1");
    const afterLoad = onChange.mock.calls.length;
    expect(afterLoad).toBeGreaterThan(0);
    ctl.editDraft({ alias: "x" });
    expect(onChange.mock.calls.length).toBeGreaterThan(afterLoad);
    expect(ctl.state.dirty).toEqual(["alias"]);
  });

  it("accepts an edit before anything is loaded without inventing an unsaved field", async () => {
    // Reachable: the editor exists before a conversation is chosen, so a keystroke can land
    // with `annotation === null`. There is nothing to diff against, so NOTHING may be marked
    // unsaved — and that matters beyond tidiness, because `save()` keys on `annotation` and
    // would return early anyway: a "Save alias" button over a write that cannot happen is the
    // stale-claim defect this panel exists to avoid.
    const ipc = fakeIpc();
    const ctl = new MetadataController(ipc, () => {});
    ctl.editDraft({ alias: "typed too early", tagText: "a, b" });
    expect(ctl.state.draft.alias).toBe("typed too early");
    expect(ctl.state.dirty).toEqual([]);
    await ctl.save();
    expect(ipc.calls).toEqual([]);
  });

  it("ignores save and clear before anything is loaded", async () => {
    // Both are reachable from a keyboard shortcut before a conversation is chosen. Without
    // the guard they would send a `conversation_id` of nothing at all.
    const ipc = fakeIpc();
    const ctl = new MetadataController(ipc, () => {});
    await ctl.save();
    await ctl.clear();
    expect(ipc.calls).toEqual([]);
  });

  it("reports a failed SAVE and leaves the draft intact to retry", async () => {
    const ipc = fakeIpc({
      metadataSet: async () => {
        throw new Error("readonly database");
      },
    });
    const ctl = new MetadataController(ipc, () => {});
    await ctl.load("c1");
    ctl.editDraft({ alias: "second try" });
    await ctl.save();
    expect(ctl.state.error).toContain("readonly database");
    // The edit survives, so the user does not retype it, and it is still marked unsaved.
    expect(ctl.state.draft.alias).toBe("second try");
    expect(ctl.state.dirty).toEqual(["alias"]);
    // And the facet was NOT refreshed — nothing moved.
    expect(ipc.calls).not.toContain("tags()");
  });

  it("lets a stale load lose even when it FAILS", async () => {
    // The error path needs the same token guard as the success path. Without it a slow
    // failure would blank the annotation a newer, successful load had already painted.
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const ipc = fakeIpc({
      async metadataGet(id) {
        if (id === "slow") {
          await gate;
          throw new Error("too late");
        }
        return annotation({ conversation_id: id, alias: "fresh" });
      },
    });
    const ctl = new MetadataController(ipc, () => {});
    const slow = ctl.load("slow");
    await ctl.load("quick");
    release();
    await slow;
    expect(ctl.state.annotation?.alias).toBe("fresh");
    expect(ctl.state.error).toBe("");
  });

  it("reports a facet failure without losing the annotation", async () => {
    const ipc = fakeIpc({
      metadataTags: async () => {
        throw new Error("index locked");
      },
    });
    const ctl = new MetadataController(ipc, () => {});
    await ctl.load("c1");
    await ctl.refreshFacet();
    expect(ctl.state.error).toContain("index locked");
    expect(ctl.state.facet).toEqual([]);
    expect(ctl.state.annotation?.alias).toBe("boot");
  });

  it("refreshes the tag facet, ordered biggest-first", async () => {
    const ipc = fakeIpc({
      metadataTags: async () => [{ tag: "a", count: 1 }, { tag: "b", count: 7 }],
    });
    const ctl = new MetadataController(ipc, () => {});
    await ctl.refreshFacet();
    expect(ctl.state.facet.map((f) => f.tag)).toEqual(["b", "a"]);
  });

  it("refreshes the facet after a save, because tag counts just moved", async () => {
    const ipc = fakeIpc();
    const ctl = new MetadataController(ipc, () => {});
    await ctl.load("c1");
    ctl.editDraft({ tagText: "alpha, brand-new" });
    await ctl.save();
    expect(ipc.calls.filter((c) => c === "tags()")).toHaveLength(1);
  });
});
