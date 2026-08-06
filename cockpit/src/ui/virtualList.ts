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
  /**
   * Message shown when the list holds nothing, e.g. "No threads yet".
   *
   * A CSS `:empty` selector CANNOT express this: the list always mounts a sizer
   * child, so the viewport is never empty in the DOM sense even when it shows
   * nothing. Without this the panes render as blank grey rectangles that read as
   * broken rather than as "nothing here yet" — which is exactly how they looked in
   * the first visual capture of this UI.
   */
  emptyLabel?: string;
}

/**
 * The empty-state label for a list of `count` items, or `null` when none should be
 * shown. Pure, so the rule is unit-testable in this project's DOM-less test env
 * (vitest runs `environment: "node"`); the DOM attribute is applied by `setItems`.
 */
export function emptyStateLabel(count: number, label?: string): string | null {
  if (count > 0 || !label) {
    return null;
  }
  return label;
}

export class VirtualList<T> {
  private items: T[] = [];
  private readonly sizer: HTMLElement;
  private readonly itemHeight: number;
  private readonly overscan: number;
  private readonly renderRow: (item: T, index: number) => HTMLElement;
  private readonly resizeObserver: ResizeObserver;
  private readonly onScroll: () => void;
  private readonly emptyLabel?: string;

  constructor(
    private readonly viewport: HTMLElement,
    options: VirtualListOptions<T>,
  ) {
    this.itemHeight = options.itemHeight;
    this.overscan = options.overscan ?? 4;
    this.renderRow = options.renderRow;
    this.emptyLabel = options.emptyLabel;

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

    // A list starts empty, so the empty state must be applied NOW rather than waiting
    // for a first setItems. The search panel only calls setItems from its input
    // handler, so without this the label appeared only after the user typed and
    // cleared the box — never on first paint, which is the one time it matters most.
    this.applyEmptyState();
  }

  setItems(items: T[]): void {
    this.items = items;
    this.sizer.style.height = `${items.length * this.itemHeight}px`;
    this.viewport.scrollTop = 0;
    this.applyEmptyState();
    this.paint();
  }

  /**
   * Reflect emptiness onto the viewport as `data-empty="<label>"`. Presentation lives
   * in CSS (`[data-empty]::after { content: attr(data-empty) }`); this only states the
   * fact. The decision itself is `emptyStateLabel`, which is unit-tested.
   */
  private applyEmptyState(): void {
    const label = emptyStateLabel(this.items.length, this.emptyLabel);
    if (label === null) {
      this.viewport.removeAttribute("data-empty");
    } else {
      this.viewport.setAttribute("data-empty", label);
    }
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
