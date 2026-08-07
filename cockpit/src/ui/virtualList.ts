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
    // No focus restore: the content is DIFFERENT now, so "row 7" is a different thing and
    // pulling focus into it would hijack the caret out of the search box mid-typing.
    this.paint(false);
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

  /**
   * Render the window of rows around the current scroll position.
   *
   * `preserveFocus` re-focuses the equivalent control after the rebuild. It matters far more
   * than it sounds. MEASURED before the fix, driving this component with 1,000 rows: 160 Tab
   * presses reached **38 distinct rows**, with focus dropping to `<body>` five times — against
   * a plain non-virtualized control list of 200 rows that reached 160 in the same 160
   * presses. The mechanism is right here: a scroll repaint calls `replaceChildren()`, which
   * detaches the focused row, so focus falls to the document and the next Tab restarts from
   * the top of the page. Tabbing down the list is therefore a closed loop, and every list in
   * this app is virtualized — both primary navigation surfaces were keyboard-unusable past
   * the first screenful or two (WCAG 2.1.1, 2.4.3).
   */
  private paint(preserveFocus = true): void {
    const total = this.items.length;
    const viewportHeight = this.viewport.clientHeight || this.itemHeight;
    const first = Math.max(0, Math.floor(this.viewport.scrollTop / this.itemHeight) - this.overscan);
    const visibleCount = Math.ceil(viewportHeight / this.itemHeight) + this.overscan * 2;
    const last = Math.min(total, first + visibleCount);

    const held = preserveFocus ? this.capturedFocus() : null;

    this.sizer.replaceChildren();
    for (let i = first; i < last; i++) {
      const row = this.renderRow(this.items[i], i);
      // The row's index, so focus can find its way back to the SAME item after a rebuild.
      row.dataset.vlIndex = String(i);
      row.style.position = "absolute";
      row.style.top = `${i * this.itemHeight}px`;
      row.style.left = "0";
      row.style.right = "0";
      row.style.height = `${this.itemHeight}px`;
      this.sizer.appendChild(row);
    }

    if (held !== null) this.restoreFocus(held);
  }

  /**
   * Which item held focus, and where within its row.
   *
   * The position matters because a row is not always the focusable thing: a search hit is a
   * div holding a "locate" and a "read" button, so restoring to the row would silently move
   * the user from one control to the other.
   */
  private capturedFocus(): { index: string; position: number } | null {
    const active = document.activeElement;
    if (!(active instanceof HTMLElement) || !this.sizer.contains(active)) return null;
    const row = active.closest<HTMLElement>("[data-vl-index]");
    const index = row?.dataset.vlIndex;
    if (row === null || row === undefined || index === undefined) return null;
    return { index, position: focusablesIn(row).indexOf(active) };
  }

  /** Put focus back on the equivalent control in the rebuilt row, if it is still rendered. */
  private restoreFocus(held: { index: string; position: number }): void {
    const row = this.sizer.querySelector<HTMLElement>(
      `[data-vl-index="${CSS.escape(held.index)}"]`);
    if (row === null) return;    // scrolled out of the window; nothing to hold on to
    const target = held.position < 0 ? row : focusablesIn(row)[held.position] ?? row;
    // `preventScroll` is load-bearing: focusing normally scrolls the element into view, which
    // fires another scroll event, which repaints again. Without it this fix can chase its
    // own tail instead of settling.
    target.focus({ preventScroll: true });
  }
}

/** The focusable descendants of `row`, in document order. */
function focusablesIn(row: HTMLElement): HTMLElement[] {
  return [...row.querySelectorAll<HTMLElement>(
    'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),'
    + 'textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')];
}
