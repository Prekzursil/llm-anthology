/**
 * The annotation panel: owner-authored alias, tags and notes on one conversation, plus the
 * annotation search and the tag facet.
 *
 * Three layers, deliberately, because vitest runs `environment: "node"` here and anything
 * touching `document` cannot be unit-tested at all:
 *
 *   1. PURE DECISIONS — tag normalisation, the search-scope story, the tri-state write.
 *   2. {@link MetadataController} — all the async state, talking only to {@link MetadataIpc}.
 *      No DOM, so the whole state machine is testable.
 *   3. {@link MetadataPanel} — DOM only. It builds elements and copies controller state onto
 *      them; every decision it renders was made above.
 *
 * WHAT THE PLANE IS. `metadata.*` is LOCAL-ONLY by design: alias/tags/notes are absent from
 * `redact.MetadataView`, so they can never ride the cloud research plane
 * (`ipc/types.ts:449-451`). They cross only the stdio wire to this UI.
 *
 * THE THREE TRAPS THIS FILE EXISTS TO NOT FALL INTO
 *
 * `metadata.set` takes `tags` (a LIST) while `metadata.search` takes `tag` (a SINGULAR
 * STRING) — adjacent methods, different words for one concept. Passing the list to search
 * returns `[]`, which reads exactly like a broken search.
 *
 * With NEITHER filter, `metadata.search` returns `[]` ON PURPOSE, so a blank query cannot
 * dump the whole catalogue into the UI (`sidecar.py:1367-1368`, `metadata.py:501-502`). An
 * empty result from a blank query is therefore NOT "no matches", and this panel says so in
 * different words — see {@link searchOutcome}.
 *
 * `metadata.set` is a PARTIAL update: an omitted field is left alone, a present-but-empty
 * field CLEARS (`ipc/types.ts:470-478`). So {@link changedSetParams} sends only what actually
 * changed; sending all three would blank the two the user never touched.
 *
 * AND ONE THING IT DOES NOT ATTEMPT. The store dedups tags with Python `casefold()`
 * (`llm_anthology/metadata.py:236`) and strips hidden codepoints with
 * `sanitize.sanitize_for_copy` (`llm_anthology/sanitize.py:140-147`). Neither is reproduced
 * here. JS has no `casefold`, and MEASURED, `toLowerCase()` disagrees with it: `Straße` folds
 * to `strasse` but lowercases to `straße`; likewise the `ﬁ` ligature, and `ΣΣ` (`σσ` vs
 * `σς`). Re-implementing a security-relevant sanitiser in a second language would drift from
 * it besides.
 *
 * So the client normalisation is a conservative PREVIEW and the ENGINE IS AUTHORITATIVE: it
 * only ever collapses further than the client did, never less, so the client cannot hide a
 * distinction the store keeps — and the controller re-renders from the annotation `set`
 * RETURNS rather than from what it guessed, which makes the divergence self-correcting
 * instead of a silent disagreement.
 */

import { engineStatusText } from "./errors";
import type {
  Annotation,
  MetadataSearchParams,
  MetadataSearchRow,
  MetadataSetParams,
  TagCount,
} from "../ipc/types";

/** The slice of the IPC surface this panel needs. `IpcClient` satisfies it. */
export interface MetadataIpc {
  metadataGet(conversationId: string): Promise<Annotation>;
  metadataSet(params: MetadataSetParams): Promise<Annotation>;
  metadataClear(conversationId: string): Promise<Annotation>;
  metadataSearch(params?: MetadataSearchParams): Promise<MetadataSearchRow[]>;
  metadataTags(): Promise<TagCount[]>;
}

/**
 * What annotation search actually covers, in words a user can act on.
 *
 * Load-bearing rather than decorative. `metadata.search` reads the annotation columns and
 * NEVER message bodies (`ipc/types.ts:495`) — that is `search.query`'s job. A user who
 * assumed a tag search covered conversation text would read a false negative as proof their
 * own corpus is unsearchable.
 */
export const ANNOTATION_SCOPE_NOTE =
  "Searches your annotations — aliases, tags and notes — not the message text. "
  + "Use search for transcripts.";

/** The three editable annotation fields, in the order the panel shows them. */
export type AnnotationField = "alias" | "tags" | "notes";

/** What the editor currently holds. `tagText` is the raw field, not yet parsed. */
export interface Draft {
  alias: string;
  tagText: string;
  notes: string;
}

/**
 * A raw tag list -> the canonical form, mirroring `clean_tags` (`metadata.py:214-240`) as
 * closely as JS allows.
 *
 * Each tag has its whitespace RUNS collapsed to one space, which trims it, drops a blank and
 * neutralises an embedded newline in a single step. The newline part is not cosmetic: `\n` is
 * the store's wire separator, so a tag carrying one would split into two on the round trip.
 *
 * Dedup is case-insensitive keeping the FIRST-SEEN casing, so re-adding a tag in other casing
 * does not rewrite what the owner typed. Order comes from the tag TEXT (lowercase, then the
 * exact string as tiebreak) and never from insertion history, so two paths that add the same
 * tags in different orders produce identical output.
 *
 * `toLowerCase` where the engine uses `casefold`: see this module's header. The difference can
 * only leave the client with MORE tags than the store keeps, never fewer, and the round trip
 * settles it.
 */
export function normalizeTags(raw: readonly string[]): string[] {
  const seen = new Map<string, string>();
  for (const item of raw) {
    const tag = item.trim().split(/\s+/).join(" ");
    if (tag === "") continue;
    const key = tag.toLowerCase();
    if (!seen.has(key)) seen.set(key, tag);
  }
  // The engine's sort key is `(casefold, exact)` (`metadata.py:240`), but the exact-string
  // tiebreak is UNREACHABLE after a fold-keyed dedup and so is not reproduced: `seen` is keyed
  // by `toLowerCase()` and holds one value per key, so two survivors always differ in their
  // folded form. Carrying the branch anyway would be dead code with a permanent coverage hole.
  // The dedup is what guarantees that — a test pins the property, so weakening dedup fails
  // there rather than silently making this order non-deterministic.
  return [...seen.values()].sort((a, b) => {
    const la = a.toLowerCase();
    const lb = b.toLowerCase();
    return la < lb ? -1 : la > lb ? 1 : 0;
  });
}

/**
 * The tag field -> tags. Commas AND newlines both separate, because both are natural to type
 * and a newline must never survive into a single tag (see {@link normalizeTags}).
 */
export function parseTagInput(text: string): string[] {
  return normalizeTags(text.split(/[,\n\r]/));
}

/** Tags -> the tag field. Paired with {@link parseTagInput} so a load/save cycle is stable. */
export function formatTagInput(tags: readonly string[]): string {
  return tags.join(", ");
}

/** A draft seeded from what the store holds. */
export function draftFrom(annotation: Annotation): Draft {
  return {
    alias: annotation.alias,
    tagText: formatTagInput(annotation.tags),
    notes: annotation.notes,
  };
}

/**
 * True when neither filter carries anything.
 *
 * This — not `rows.length` — is what separates "we have not asked yet" from "we asked and
 * nothing matched". Whitespace counts as nothing, so a stray space is not a filter that
 * matches nothing.
 */
export function isBlankQuery(text: string, tag: string): boolean {
  return text.trim() === "" && tag.trim() === "";
}

/**
 * Build `metadata.search` params.
 *
 * `tag`, SINGULAR — the whole trap. An empty filter is OMITTED rather than sent as `""`, so
 * the request states exactly which filters are meant to apply.
 */
export function searchQueryParams(text: string, tag: string): MetadataSearchParams {
  const params: MetadataSearchParams = {};
  const t = text.trim();
  const g = tag.trim();
  if (t !== "") params.text = t;
  if (g !== "") params.tag = g;
  return params;
}

/** What the result line says, and which of the three situations produced it. */
export type SearchOutcome =
  | { kind: "idle"; message: string }
  | { kind: "empty"; message: string }
  | { kind: "hits"; message: string }
  /**
   * The search ITSELF failed. Never produced by {@link searchOutcome} — only the controller
   * can know it — but part of the union because the status line must have somewhere honest to
   * go when the rows are gone for a reason that is not "nothing matched". Leaving a stale
   * "4 annotated conversations" over an emptied list is this repo's recurring defect (the
   * 1,000-root sidebar, "1,432 hits" printed over 200).
   */
  | { kind: "failed"; message: string };

/**
 * Which of three situations the search is in — decided from the QUERY first, never from the
 * row count.
 *
 * That ordering is the point. A blank query legitimately yields zero rows, so a panel keyed on
 * `rows.length === 0` would print "no matches" over a perfectly working engine and send
 * someone hunting a bug in the store. Only once a filter exists does zero mean zero.
 */
export function searchOutcome(text: string, tag: string, rowCount: number): SearchOutcome {
  if (isBlankQuery(text, tag)) {
    return { kind: "idle", message: "Type a tag or some text to search your annotations." };
  }
  if (rowCount === 0) {
    return { kind: "empty", message: "No annotation matches that." };
  }
  const noun = rowCount === 1 ? "conversation" : "conversations";
  return { kind: "hits", message: `${rowCount} annotated ${noun}` };
}

/** Which fields the draft changes relative to the store, in panel order. */
export function dirtyFields(stored: Annotation, draft: Draft): AnnotationField[] {
  const changed: AnnotationField[] = [];
  if (draft.alias.trim() !== stored.alias) changed.push("alias");
  const tags = parseTagInput(draft.tagText);
  if (tags.join("\0") !== normalizeTags(stored.tags).join("\0")) changed.push("tags");
  if (draft.notes.trim() !== stored.notes) changed.push("notes");
  return changed;
}

/**
 * The `metadata.set` payload for exactly what changed, or `null` when nothing did.
 *
 * Only changed keys appear, which is what makes the engine's tri-state safe to use: an absent
 * key leaves that field alone, so editing the alias cannot blank the notes. A key that IS
 * present carries `""`/`[]` when the user emptied the field, which is how a clear is
 * expressed. Never `null` — the engine type-checks with `isinstance` and a null is -32602
 * (`ipc/types.ts:477-478`).
 *
 * `null` for "no change" rather than an empty params object, so a caller cannot accidentally
 * send a conversation_id with no fields and call it a save.
 */
export function changedSetParams(stored: Annotation, draft: Draft): MetadataSetParams | null {
  const changed = dirtyFields(stored, draft);
  if (changed.length === 0) return null;
  const params: MetadataSetParams = { conversation_id: stored.conversation_id };
  if (changed.includes("alias")) params.alias = draft.alias.trim();
  if (changed.includes("tags")) params.tags = parseTagInput(draft.tagText);
  if (changed.includes("notes")) params.notes = draft.notes.trim();
  return params;
}

/**
 * Order the tag facet.
 *
 * Biggest first by default, because a facet is for finding the tags actually in use; ties
 * break on the tag so two renders of one corpus are identical. `"tag"` keeps the store's own
 * ordering instead — `metadata.tags` already sorts by the casefolded tag
 * (`metadata.py:544`) — so choosing count-first is a UI decision layered on top rather than a
 * second rule that disagrees with the store.
 *
 * Copies rather than sorting in place: the array belongs to the caller's last response.
 */
export function facetOrder(
  counts: readonly TagCount[],
  by: "count" | "tag" = "count",
): TagCount[] {
  const out = [...counts];
  if (by === "tag") {
    return out.sort((a, b) => (a.tag.toLowerCase() < b.tag.toLowerCase() ? -1 : 1));
  }
  return out.sort((a, b) => (b.count - a.count) || (a.tag < b.tag ? -1 : 1));
}

/**
 * A one-line summary of an annotation, for a list row or the detail pane.
 *
 * Empty for an un-annotated conversation — `is_empty` exists so a caller need not guess
 * (`ipc/types.ts:463`) — because a row that printed "0 tags" on every unannotated
 * conversation would be noise on almost all of them.
 */
export function annotationSummary(annotation: Annotation): string {
  if (annotation.is_empty) return "";
  const parts: string[] = [];
  if (annotation.alias !== "") parts.push(`“${annotation.alias}”`);
  if (annotation.tags.length > 0) {
    parts.push(`${annotation.tags.length} tag${annotation.tags.length === 1 ? "" : "s"}`);
  }
  if (annotation.notes !== "") parts.push("notes");
  return parts.join(" · ");
}

/** Everything the panel draws. */
export interface MetadataViewState {
  /** The stored annotation, or null before a load / after a failure. */
  annotation: Annotation | null;
  draft: Draft;
  dirty: AnnotationField[];
  outcome: SearchOutcome;
  rows: MetadataSearchRow[];
  facet: TagCount[];
  /** Engine failure text; "" when fine. */
  error: string;
  busy: boolean;
}

const EMPTY_DRAFT: Draft = { alias: "", tagText: "", notes: "" };

/**
 * The panel's state and every engine call it makes — with NO DOM, so all of it is testable
 * under this project's DOM-less vitest.
 *
 * Writes go through {@link changedSetParams} and the result is adopted from the engine's
 * reply, never from the draft: the store canonicalises tags in ways the client deliberately
 * does not reproduce (module header), so the reply is the only honest source for what is now
 * stored.
 */
export class MetadataController {
  private view: MetadataViewState = {
    annotation: null,
    draft: { ...EMPTY_DRAFT },
    dirty: [],
    outcome: searchOutcome("", "", 0),
    rows: [],
    facet: [],
    error: "",
    busy: false,
  };

  /** Guards against a slow load being overwritten by, or overwriting, a newer one. */
  private latest = 0;

  constructor(
    private readonly ipc: MetadataIpc,
    private readonly onChange: (state: MetadataViewState) => void,
  ) {}

  get state(): MetadataViewState {
    return this.view;
  }

  /** Replace state and tell the renderer. Immutable, so a renderer may hold the old object. */
  private emit(patch: Partial<MetadataViewState>): void {
    this.view = { ...this.view, ...patch };
    this.onChange(this.view);
  }

  /** Load one conversation's annotation and seed the editor from it. */
  async load(conversationId: string): Promise<void> {
    const token = ++this.latest;
    this.emit({ busy: true, error: "" });
    let annotation: Annotation;
    try {
      annotation = await this.ipc.metadataGet(conversationId);
    } catch (err) {
      if (token !== this.latest) return;
      this.emit({
        busy: false,
        annotation: null,
        draft: { ...EMPTY_DRAFT },
        dirty: [],
        error: engineStatusText(err, "Could not read this conversation's annotation"),
      });
      return;
    }
    // A load that a newer selection has already superseded must not paint: it would show one
    // conversation's annotation under another's heading, and nothing on screen would say so.
    if (token !== this.latest) return;
    this.emit({ busy: false, annotation, draft: draftFrom(annotation), dirty: [] });
  }

  /** Apply an editor edit and recompute what is unsaved. */
  editDraft(patch: Partial<Draft>): void {
    const draft = { ...this.view.draft, ...patch };
    const dirty = this.view.annotation === null
      ? []
      : dirtyFields(this.view.annotation, draft);
    this.emit({ draft, dirty });
  }

  /** Write the changed fields, then adopt whatever the engine says is now stored. */
  async save(): Promise<void> {
    const stored = this.view.annotation;
    if (stored === null) return;
    const params = changedSetParams(stored, this.view.draft);
    // Nothing changed: a write here would be a round trip that cannot alter anything.
    if (params === null) return;
    this.emit({ busy: true, error: "" });
    try {
      const annotation = await this.ipc.metadataSet(params);
      this.emit({ busy: false, annotation, draft: draftFrom(annotation), dirty: [] });
    } catch (err) {
      this.emit({ busy: false, error: engineStatusText(err, "Could not save the annotation") });
      return;
    }
    // Tag counts just moved, so a facet left alone would be stale the moment it mattered.
    await this.refreshFacet();
  }

  /** Drop the whole annotation, adopting the empty one the engine returns. */
  async clear(): Promise<void> {
    const stored = this.view.annotation;
    if (stored === null) return;
    this.emit({ busy: true, error: "" });
    try {
      const annotation = await this.ipc.metadataClear(stored.conversation_id);
      this.emit({ busy: false, annotation, draft: draftFrom(annotation), dirty: [] });
    } catch (err) {
      this.emit({ busy: false, error: engineStatusText(err, "Could not clear the annotation") });
      return;
    }
    await this.refreshFacet();
  }

  /**
   * Search annotations.
   *
   * A blank query is answered LOCALLY and the engine is never called. Not an optimisation:
   * the engine would return `[]` by design, and a panel that then said "no matches" would be
   * reporting a working search as broken.
   */
  async search(text: string, tag: string): Promise<void> {
    if (isBlankQuery(text, tag)) {
      this.emit({ rows: [], outcome: searchOutcome(text, tag, 0), error: "" });
      return;
    }
    this.emit({ busy: true, error: "" });
    try {
      const rows = await this.ipc.metadataSearch(searchQueryParams(text, tag));
      this.emit({ busy: false, rows, outcome: searchOutcome(text, tag, rows.length) });
    } catch (err) {
      const message = engineStatusText(err, "Could not search annotations");
      this.emit({
        busy: false,
        rows: [],
        outcome: { kind: "failed", message },
        error: message,
      });
    }
  }

  /** Reload the tag facet. */
  async refreshFacet(): Promise<void> {
    try {
      this.emit({ facet: facetOrder(await this.ipc.metadataTags()) });
    } catch (err) {
      this.emit({ error: engineStatusText(err, "Could not read the tag facet") });
    }
  }
}

/**
 * The DOM half: build the elements once, then copy controller state onto them.
 *
 * Deliberately decision-free. Every rule it renders — what the status line says, which fields
 * are unsaved, how tags are ordered — was decided above and is unit-tested; this only moves
 * strings onto nodes, which is all that a DOM-less test environment cannot check.
 */
export class MetadataPanel {
  private readonly controller: MetadataController;
  private readonly aliasEl: HTMLInputElement;
  private readonly tagsEl: HTMLInputElement;
  private readonly notesEl: HTMLTextAreaElement;
  private readonly saveBtn: HTMLButtonElement;
  private readonly clearBtn: HTMLButtonElement;
  private readonly statusEl: HTMLElement;
  private readonly errorEl: HTMLElement;
  private readonly facetEl: HTMLElement;
  private readonly resultsEl: HTMLElement;
  private readonly searchTextEl: HTMLInputElement;
  private searchTag = "";
  /** Told the host a result row was chosen, so the app can open that conversation. */
  private onPick: (conversationId: string) => void = () => {};

  constructor(ipc: MetadataIpc, container: HTMLElement) {
    this.controller = new MetadataController(ipc, () => this.paint());
    container.classList.add("metadata-panel");

    const editor = document.createElement("div");
    editor.className = "metadata-editor";
    this.aliasEl = labelledInput(editor, "Alias", "metadata-alias");
    this.tagsEl = labelledInput(editor, "Tags (comma separated)", "metadata-tags");
    this.notesEl = labelledTextarea(editor, "Notes", "metadata-notes");
    this.aliasEl.addEventListener("input", () => {
      this.controller.editDraft({ alias: this.aliasEl.value });
    });
    this.tagsEl.addEventListener("input", () => {
      this.controller.editDraft({ tagText: this.tagsEl.value });
    });
    this.notesEl.addEventListener("input", () => {
      this.controller.editDraft({ notes: this.notesEl.value });
    });

    const actions = document.createElement("div");
    actions.className = "metadata-actions";
    this.saveBtn = button("Save", "metadata-save", () => void this.controller.save());
    this.clearBtn = button("Clear", "metadata-clear", () => void this.controller.clear());
    actions.append(this.saveBtn, this.clearBtn);

    this.errorEl = document.createElement("p");
    this.errorEl.className = "metadata-error";
    this.errorEl.setAttribute("role", "alert");

    const search = document.createElement("div");
    search.className = "metadata-search";
    this.searchTextEl = labelledInput(search, "Find in annotations", "metadata-q");
    this.searchTextEl.addEventListener("input", () => this.runSearch());
    // The scope note is STATIC and always visible, not a tooltip: the one thing a user can
    // wrongly conclude here is that this searches their transcripts.
    const scope = document.createElement("p");
    scope.className = "metadata-scope";
    scope.textContent = ANNOTATION_SCOPE_NOTE;
    search.append(scope);

    this.statusEl = document.createElement("p");
    this.statusEl.className = "metadata-status";
    this.facetEl = document.createElement("div");
    this.facetEl.className = "metadata-facet";
    this.resultsEl = document.createElement("div");
    this.resultsEl.className = "metadata-results";

    container.append(editor, actions, this.errorEl, search, this.facetEl,
                     this.statusEl, this.resultsEl);
    this.paint();
  }

  /** Show one conversation. */
  async open(conversationId: string): Promise<void> {
    await this.controller.load(conversationId);
    await this.controller.refreshFacet();
  }

  /** Register the "user picked a result row" callback. */
  setOnPick(fn: (conversationId: string) => void): void {
    this.onPick = fn;
  }

  private runSearch(): void {
    void this.controller.search(this.searchTextEl.value, this.searchTag);
  }

  /** Filter by one facet tag. Clicking the active tag clears the filter. */
  private toggleTag(tag: string): void {
    this.searchTag = this.searchTag === tag ? "" : tag;
    this.runSearch();
  }

  private paint(): void {
    const s = this.controller.state;
    // Only write a field the user is not typing in, or the caret jumps to the end mid-edit.
    if (document.activeElement !== this.aliasEl) this.aliasEl.value = s.draft.alias;
    if (document.activeElement !== this.tagsEl) this.tagsEl.value = s.draft.tagText;
    if (document.activeElement !== this.notesEl) this.notesEl.value = s.draft.notes;

    this.saveBtn.disabled = s.busy || s.dirty.length === 0;
    this.clearBtn.disabled = s.busy || s.annotation === null || s.annotation.is_empty;
    this.saveBtn.textContent = s.dirty.length === 0 ? "Save" : `Save ${s.dirty.join(", ")}`;
    this.errorEl.textContent = s.error;
    this.errorEl.hidden = s.error === "";
    this.statusEl.textContent = s.outcome.message;
    this.statusEl.dataset.kind = s.outcome.kind;

    this.facetEl.replaceChildren(...s.facet.map((entry) => {
      const b = button(`${entry.tag} (${entry.count})`, "metadata-tag", () => {
        this.toggleTag(entry.tag);
      });
      b.setAttribute("aria-pressed", String(this.searchTag === entry.tag));
      return b;
    }));

    this.resultsEl.replaceChildren(...s.rows.map((r) => {
      const b = button("", "metadata-row", () => this.onPick(r.conversation_id));
      // textContent, never innerHTML: a title and an alias are arbitrary text off disk.
      b.textContent = r.annotation.alias !== "" ? r.annotation.alias : r.title;
      const sub = document.createElement("span");
      sub.className = "metadata-row-sub";
      sub.textContent = annotationSummary(r.annotation);
      b.append(sub);
      return b;
    }));
  }
}

function labelledInput(parent: HTMLElement, text: string, cls: string): HTMLInputElement {
  const label = document.createElement("label");
  label.className = `${cls}-label`;
  label.textContent = text;
  const input = document.createElement("input");
  input.type = "text";
  input.className = cls;
  label.append(input);
  parent.append(label);
  return input;
}

function labelledTextarea(parent: HTMLElement, text: string, cls: string): HTMLTextAreaElement {
  const label = document.createElement("label");
  label.className = `${cls}-label`;
  label.textContent = text;
  const area = document.createElement("textarea");
  area.className = cls;
  area.rows = 4;
  label.append(area);
  parent.append(label);
  return area;
}

function button(text: string, cls: string, onClick: () => void): HTMLButtonElement {
  const b = document.createElement("button");
  b.type = "button";
  b.className = cls;
  b.textContent = text;
  b.addEventListener("click", onClick);
  return b;
}
