/**
 * The SEARCH box: a debounced input wired to `ipc.searchQuery`, rendering hits into a
 * virtualized result list. Selecting a hit reports its thread id (falling back to the
 * conversation id) so the caller can focus that thread in the spawn tree.
 */

import type { IpcClient, SearchHit } from "../ipc";
import { engineErrorText } from "./errors";
import { VirtualList } from "./virtualList";

export type HitHandler = (hit: SearchHit) => void;

const DEBOUNCE_MS = 180;
const ROW_HEIGHT = 46;

export class SearchPanel {
  private readonly input: HTMLInputElement;
  private readonly status: HTMLElement;
  private readonly list: VirtualList<SearchHit>;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private onHit: HitHandler | null = null;
  private latest = 0;

  constructor(
    private readonly ipc: IpcClient,
    input: HTMLInputElement,
    resultsViewport: HTMLElement,
    status: HTMLElement,
  ) {
    this.input = input;
    this.status = status;
    this.list = new VirtualList<SearchHit>(resultsViewport, {
      itemHeight: ROW_HEIGHT,
      renderRow: (hit) => this.renderHit(hit),
      // Covers both "you have not searched yet" and "that query matched nothing";
      // the live status line above distinguishes them.
      emptyLabel: "Search across every provider at once.",
    });
    this.input.addEventListener("input", () => this.schedule());
  }

  setHitHandler(handler: HitHandler | null): void {
    this.onHit = handler;
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
    this.status.textContent = "Searching…";
    try {
      const result = await this.ipc.searchQuery({ q, limit: 200 });
      if (token !== this.latest) return; // a newer query superseded this one
      this.list.setItems(result.hits);
      this.status.textContent = `${result.total} hit${result.total === 1 ? "" : "s"} · ${result.took_ms}ms`;
    } catch (err) {
      if (token !== this.latest) return;
      this.list.setItems([]);
      // Searching before a corpus is attached is not a search failure — route it through
      // the shared presenter so it does not print the engine's internal method name.
      this.status.textContent = engineErrorText(err, "Search failed");
    }
  }

  private renderHit(hit: SearchHit): HTMLElement {
    const row = document.createElement("button");
    row.className = "hit-row";
    row.type = "button";

    const provider = document.createElement("span");
    provider.className = `provider-dot provider-${cssClass(hit.provider)}`;
    provider.title = hit.provider;

    const snippet = document.createElement("span");
    snippet.className = "hit-snippet";
    snippet.textContent = hit.snippet;

    row.append(provider, snippet);
    row.addEventListener("click", () => {
      if (this.onHit !== null) this.onHit(hit);
    });
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
