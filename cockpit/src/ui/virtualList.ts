/**
 * A minimal fixed-row-height VIRTUALIZED list: only the rows in (and just around) the
 * viewport are in the DOM, so a corpus of thousands of conversations scrolls smoothly.
 *
 * The scroll container gets a full-height sizer; on every scroll/resize only the
 * visible window (plus an overscan margin) is materialized via the caller's
 * `renderRow`. Generic over the row type so both the roots list and the search-hit
 * list reuse it.
 */

export interface VirtualListOptions<T> {
  /** Fixed height of every row, in CSS px. */
  itemHeight: number;
  /** Build the DOM for one item. Positioning is handled by the list. */
  renderRow: (item: T, index: number) => HTMLElement;
  /** Extra rows rendered above/below the viewport to avoid blank flashes. */
  overscan?: number;
}

export class VirtualList<T> {
  private items: T[] = [];
  private readonly sizer: HTMLElement;
  private readonly itemHeight: number;
  private readonly overscan: number;
  private readonly renderRow: (item: T, index: number) => HTMLElement;
  private readonly resizeObserver: ResizeObserver;
  private readonly onScroll: () => void;

  constructor(
    private readonly viewport: HTMLElement,
    options: VirtualListOptions<T>,
  ) {
    this.itemHeight = options.itemHeight;
    this.overscan = options.overscan ?? 4;
    this.renderRow = options.renderRow;

    viewport.style.overflowY = "auto";
    viewport.style.position = "relative";

    this.sizer = document.createElement("div");
    this.sizer.style.position = "relative";
    this.sizer.style.width = "100%";
    viewport.appendChild(this.sizer);

    this.onScroll = () => this.paint();
    viewport.addEventListener("scroll", this.onScroll, { passive: true });
    this.resizeObserver = new ResizeObserver(() => this.paint());
    this.resizeObserver.observe(viewport);
  }

  setItems(items: T[]): void {
    this.items = items;
    this.sizer.style.height = `${items.length * this.itemHeight}px`;
    this.viewport.scrollTop = 0;
    this.paint();
  }

  /** Number of items currently backing the list. */
  get length(): number {
    return this.items.length;
  }

  destroy(): void {
    this.viewport.removeEventListener("scroll", this.onScroll);
    this.resizeObserver.disconnect();
    this.sizer.remove();
  }

  private paint(): void {
    const total = this.items.length;
    const viewportHeight = this.viewport.clientHeight || this.itemHeight;
    const first = Math.max(0, Math.floor(this.viewport.scrollTop / this.itemHeight) - this.overscan);
    const visibleCount = Math.ceil(viewportHeight / this.itemHeight) + this.overscan * 2;
    const last = Math.min(total, first + visibleCount);

    this.sizer.replaceChildren();
    for (let i = first; i < last; i++) {
      const row = this.renderRow(this.items[i], i);
      row.style.position = "absolute";
      row.style.top = `${i * this.itemHeight}px`;
      row.style.left = "0";
      row.style.right = "0";
      row.style.height = `${this.itemHeight}px`;
      this.sizer.appendChild(row);
    }
  }
}
