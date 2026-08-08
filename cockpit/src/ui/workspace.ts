/**
 * The workspace: one region that hosts the panels too big for the 300px columns, and the
 * top-bar buttons that reveal them.
 *
 * WHY THIS EXISTS AT ALL. Three finished, fully-tested panels — duplicates, annotations and
 * maintenance — shipped with no import and no container, so no user could reach any of them.
 * That is the seventh "built but never wired" surface in this codebase and the pattern is
 * always the same: each surface green on its own, nothing crossing the seam between them.
 * This module IS that seam, which is why it is a module and not a method: it is the part
 * worth naming.
 *
 * WHY A DISCLOSURE, NOT A TABLIST. A `role="tablist"` is the prettier ARIA fit, but its
 * contract puts `tabindex="-1"` on every inactive tab and moves between them with the arrow
 * keys. This repo has already broken Tab traversal once and ships a probe
 * (`tools/probe_keyboard_reach.mjs`) because of it, so a pattern whose correct
 * implementation REMOVES two of three controls from the Tab order is the wrong trade here.
 * Three plain buttons, each independently Tab-reachable, carrying `aria-expanded` +
 * `aria-controls`, is the disclosure pattern — no arrow-key contract to get wrong, and every
 * control reachable with the one key everybody has.
 *
 * WHY IT OVERLAYS ROW 2 RATHER THAN THE VIEWPORT. `#reader` is `position: fixed; inset: 0`
 * because a transcript wants every pixel. A workspace panel does not: covering the top bar
 * would take the corpus control, the health line AND these very buttons off screen, so
 * switching panels would mean closing one first. Placing it in the grid's second row instead
 * leaves the top bar live, and leaves the reader — at a higher z-index — free to open OVER a
 * workspace panel, which is exactly what the annotations panel does when a result is picked.
 */

/** One pane: the button that reveals it, the container it lives in, and its name. */
export interface WorkspacePaneSpec {
  /** Stable key, used by {@link Workspace.show} and reported by `openPane`. */
  key: string;
  button: HTMLButtonElement;
  container: HTMLElement;
  /**
   * The region's accessible name while this pane is showing.
   *
   * A function when the name depends on what the pane is currently showing — the annotations
   * pane names the conversation being edited, which is a fact the panel itself does not
   * display anywhere. Re-resolved by {@link Workspace.refreshTitle}.
   */
  title: string | (() => string);
  /**
   * Runs on every reveal, told whether this is the FIRST one.
   *
   * One hook rather than two because the two panes that need it need different things and
   * each knows which: the duplicates pane loads its candidate list once, because doing it
   * again would re-walk the filesystem (1.8-7.5s, measured — the same cost that keeps
   * `init()` from scanning when a corpus was restored), while the annotations pane re-syncs
   * to the currently-selected conversation on every reveal. Nothing runs at boot either way,
   * which is the point: no panel spends the user's first seconds on work they did not ask for.
   */
  onShow?: (firstShow: boolean) => void;
}

export class Workspace {
  private openKey: string | null = null;
  private readonly firstShown = new Set<string>();
  /** Focus to restore on close, so a keyboard user is not dumped at the top of the page. */
  private returnFocusTo: HTMLElement | null = null;

  constructor(
    private readonly root: HTMLElement,
    private readonly titleEl: HTMLElement,
    closeBtn: HTMLButtonElement,
    private readonly panes: readonly WorkspacePaneSpec[],
    /**
     * True while some other overlay owns Escape.
     *
     * Without it, one Escape press would close the reader AND the workspace panel it was
     * opened from, because both listen on `document`. The reader is on top, so it wins.
     */
    private readonly escapeBlocked: () => boolean = () => false,
  ) {
    this.root.hidden = true;
    this.root.setAttribute("aria-labelledby", this.titleEl.id);

    for (const pane of this.panes) {
      pane.container.hidden = true;
      pane.button.setAttribute("aria-expanded", "false");
      pane.button.setAttribute("aria-controls", this.root.id);
      pane.button.addEventListener("click", () => this.toggle(pane.key));
    }

    closeBtn.addEventListener("click", () => this.close());
    // CAPTURE, and that is load-bearing. `ReaderOverlay` also listens for Escape on
    // `document`, in the bubble phase, and it is constructed FIRST — so with a bubble
    // listener here the reader had already closed itself and cleared `isOpen` by the time
    // this ran, `escapeBlocked()` answered false, and ONE Escape press closed the reader AND
    // the panel it was opened from. Measured by `tools/smoke_boot.mjs`, which is the only
    // thing that could have seen it: both handlers are individually correct.
    //
    // A capture listener on `document` runs before any bubble listener on `document`, so
    // this observes the reader while it is still open. When it is, this returns without
    // calling `preventDefault`, leaving the press to the reader untouched.
    document.addEventListener("keydown", (e) => {
      if (this.root.hidden || e.key !== "Escape" || this.escapeBlocked()) return;
      e.preventDefault();
      this.close();
    }, true);
  }

  /** The showing pane's key, or null when the workspace is closed. */
  get openPane(): string | null {
    return this.openKey;
  }

  /** Reveal one pane, replacing whichever was showing. */
  show(key: string): void {
    const pane = this.panes.find((p) => p.key === key);
    if (pane === undefined) return;
    // Captured BEFORE anything is revealed, and only when opening from closed, so switching
    // panels does not overwrite the button the user actually came from.
    if (this.openKey === null) this.returnFocusTo = document.activeElement as HTMLElement | null;
    this.openKey = key;
    this.root.hidden = false;
    this.titleEl.textContent = resolveTitle(pane);
    for (const p of this.panes) {
      const isOpen = p.key === key;
      p.container.hidden = !isOpen;
      p.button.setAttribute("aria-expanded", String(isOpen));
    }
    const firstShow = !this.firstShown.has(key);
    this.firstShown.add(key);
    pane.onShow?.(firstShow);
  }

  /**
   * Re-read the showing pane's title.
   *
   * Called after something changes what the pane is about — a new conversation loaded into
   * the annotations editor, say. A no-op when that pane is not the one on screen, so a
   * background change cannot relabel the region the user is actually looking at.
   */
  refreshTitle(): void {
    const pane = this.panes.find((p) => p.key === this.openKey);
    if (pane !== undefined) this.titleEl.textContent = resolveTitle(pane);
  }

  /** Reveal a pane, or hide it when it is already the one showing. */
  toggle(key: string): void {
    if (this.openKey === key) this.close();
    else this.show(key);
  }

  close(): void {
    if (this.openKey === null) return;
    this.openKey = null;
    this.root.hidden = true;
    for (const p of this.panes) {
      p.container.hidden = true;
      p.button.setAttribute("aria-expanded", "false");
    }
    // isConnected, because a pane can rebuild the DOM the focus came from; focusing a
    // detached node silently sends focus to <body> instead of back to the top bar.
    if (this.returnFocusTo?.isConnected === true) this.returnFocusTo.focus();
    this.returnFocusTo = null;
  }
}

function resolveTitle(pane: WorkspacePaneSpec): string {
  return typeof pane.title === "function" ? pane.title() : pane.title;
}
