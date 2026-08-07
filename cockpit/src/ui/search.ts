/**
 * The SEARCH box: a debounced input wired to `ipc.searchQuery`, rendering hits into a
 * virtualized result list. Selecting a hit reports its thread id (falling back to the
 * conversation id) so the caller can focus that thread in the spawn tree.
 */

import type { IpcClient, SearchHit } from "../ipc";
import { engineErrorText } from "./errors";
import {
  hitLabel,
  nextFilterValue,
  providerOptions,
  relativeWhen,
  resultStatus,
  searchParams,
} from "./searchPresent";
import { VirtualList } from "./virtualList";

export type HitHandler = (hit: SearchHit) => void;

const DEBOUNCE_MS = 180;
const ROW_HEIGHT = 46;

/**
 * How many rows one query returns.
 *
 * The list is virtualized, so this is a transport bound rather than a rendering one. It is
 * NOT hidden any more: when the corpus holds more matches than this, the status line says
 * "showing 200 of N" instead of printing N over a list of 200.
 */
const PAGE_SIZE = 200;

export class SearchPanel {
  private readonly input: HTMLInputElement;
  private readonly status: HTMLElement;
  private readonly filter: HTMLSelectElement | null;
  private readonly list: VirtualList<SearchHit>;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private onHit: HitHandler | null = null;
  private onRead: HitHandler | null = null;
  private latest = 0;

  constructor(
    private readonly ipc: IpcClient,
    input: HTMLInputElement,
    resultsViewport: HTMLElement,
    status: HTMLElement,
    filter: HTMLSelectElement | null = null,
  ) {
    this.input = input;
    this.status = status;
    this.filter = filter;
    this.list = new VirtualList<SearchHit>(resultsViewport, {
      itemHeight: ROW_HEIGHT,
      renderRow: (hit) => this.renderHit(hit),
      // Covers both "you have not searched yet" and "that query matched nothing";
      // the live status line above distinguishes them.
      emptyLabel: "Search across every provider at once.",
    });
    this.input.addEventListener("input", () => this.schedule());
    // Re-run immediately on a filter change rather than debouncing: it is a deliberate
    // click, not typing, and waiting 180ms after it reads as lag.
    this.filter?.addEventListener("change", () => void this.run());
  }

  setHitHandler(handler: HitHandler | null): void {
    this.onHit = handler;
  }

  /** Called when the user asks to READ a hit, as opposed to locating it in the graph. */
  setReadHandler(handler: HitHandler | null): void {
    this.onRead = handler;
  }

  /**
   * Populate the provider filter from the corpus the engine actually holds.
   *
   * Hidden entirely for a single-provider corpus — a filter whose only real choice is
   * "everything" is noise. Re-entrant: `reload` calls it on every corpus open, and the
   * previous corpus's providers must not linger as choices that now match nothing.
   */
  setProviders(providers: Record<string, number>): void {
    if (this.filter === null) return;
    const options = providerOptions(providers);
    const previous = this.filter.value;
    this.filter.replaceChildren(
      ...options.map((o) => {
        const el = document.createElement("option");
        el.value = o.value;
        el.textContent = o.label;
        return el;
      }),
    );
    this.filter.hidden = options.length === 0;
    this.filter.value = nextFilterValue(options, previous);
  }

  private schedule(): void {
    if (this.timer !== undefined) clearTimeout(this.timer);
    this.timer = setTimeout(() => void this.run(), DEBOUNCE_MS);
  }

  private async run(): Promise<void> {
    const q = this.input.value.trim();
    if (q === "") {
      this.list.setItems([]);
      this.status.textContent = "";
      return;
    }
    const token = ++this.latest;
    const provider = this.filter?.value ?? "";
    this.status.textContent = "Searching…";
    try {
      const result = await this.ipc.searchQuery(searchParams(q, provider, PAGE_SIZE));
      if (token !== this.latest) return; // a newer query superseded this one
      this.list.setItems(result.hits);
      this.status.textContent = resultStatus({
        total: result.total,
        shown: result.hits.length,
        tookMs: result.took_ms,
        provider,
      });
    } catch (err) {
      if (token !== this.latest) return;
      this.list.setItems([]);
      // Searching before a corpus is attached is not a search failure — route it through
      // the shared presenter so it does not print the engine's internal method name.
      this.status.textContent = engineErrorText(err, "Search failed");
    }
  }

  private renderHit(hit: SearchHit): HTMLElement {
    // A DIV holding two buttons, not one button: the row carries two distinct actions
    // (locate in the graph / read the transcript) and a button inside a button is invalid
    // HTML that screen readers and browsers both handle unpredictably.
    const row = document.createElement("div");
    row.className = "hit-row";

    const locate = document.createElement("button");
    locate.className = "hit-main";
    locate.type = "button";

    const provider = document.createElement("span");
    provider.className = `provider-dot provider-${cssClass(hit.provider)}`;
    provider.title = hit.provider;

    const snippet = document.createElement("span");
    snippet.className = "hit-snippet";
    // Never `hit.snippet` raw: the engine's snippet is the conversation title, and a title
    // is optional, so an untitled hit rendered as a blank clickable button.
    snippet.textContent = hitLabel(hit);

    // `ts_ms` was on every hit already and discarded. In a tool about sessions over time, a
    // result with no time is hard to place at all.
    const when = document.createElement("time");
    when.className = "hit-when";
    when.textContent = relativeWhen(hit.ts_ms, Date.now());
    if (hit.ts_ms !== undefined) when.dateTime = new Date(hit.ts_ms).toISOString();

    locate.append(provider, snippet, when);
    locate.addEventListener("click", () => {
      if (this.onHit !== null) this.onHit(hit);
    });

    // Reading the transcript is the point of finding it, and until now there was no way to
    // do it from anywhere in the app. Kept as its OWN control so the existing behaviour of
    // the row -- locate this thread in the spawn graph -- is unchanged.
    const read = document.createElement("button");
    read.className = "hit-read";
    read.type = "button";
    read.textContent = "Read";
    read.title = "Open the transcript";
    read.addEventListener("click", () => {
      if (this.onRead !== null) this.onRead(hit);
    });

    row.append(locate, read);
    return row;
  }

  destroy(): void {
    if (this.timer !== undefined) clearTimeout(this.timer);
    this.list.destroy();
  }
}

/** Reduce a provider string to a safe CSS class suffix. */
function cssClass(provider: string): string {
  return provider.replace(/[^a-z0-9]+/gi, "-").toLowerCase() || "unknown";
}
