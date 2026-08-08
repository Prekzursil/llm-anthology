/**
 * The transcript reader: a full-width overlay that shows one conversation, plainly.
 *
 * WHY IT EXISTS. `conversation.get` has been implemented on both sides of the wire — engine
 * handler, DTO, `real.ts` binding, `mock.ts` fixture — and NOTHING ever called it. The app
 * could find a conversation and not read it, which is the one thing a session browser is
 * for. This is the sixth "built but never wired" surface found in this codebase, and the
 * pattern behind all six is the same: per-surface work with no test crossing the seam
 * BETWEEN surfaces, so every piece was individually green and the journey did not exist.
 *
 * WHY AN OVERLAY, not the detail pane. The detail pane is a 300px column; a transcript needs
 * width to be readable and the owner's chosen layout keeps the list and graph permanently on
 * screen, so replacing either is not available. An overlay is the only surface that gives
 * full width without taking a layout decision that has not been made.
 *
 * Content is written with `textContent` throughout — never `innerHTML`. Transcript text is
 * arbitrary content from files on disk, including whatever a model or a tool emitted, so it
 * is treated as data at every point.
 */

import type {
  IpcClient,
  Conversation,
  ConversationAvailable,
  ConversationTurn,
} from "../ipc";
import { engineErrorText } from "./errors";
import {
  blockClass,
  blockDisplay,
  readerSubtitle,
  readerTitle,
  roleLabel,
  stubExplanation,
  turnWhen,
} from "./readerPresent";
import { moreButtonLabel, turnWindow, windowStatus } from "./readerWindow";

export class ReaderOverlay {
  private readonly root: HTMLElement;
  private readonly titleEl: HTMLElement;
  private readonly subtitleEl: HTMLElement;
  private readonly bodyEl: HTMLElement;
  private readonly closeBtn: HTMLButtonElement;
  /** Focus to restore on close, so keyboard users are not dumped at the top of the page. */
  private returnFocusTo: HTMLElement | null = null;
  /** Guards against a slow open being overwritten by, or overwriting, a newer one. */
  private latest = 0;
  private readonly onKeyDown: (e: KeyboardEvent) => void;
  /** The turns of the open conversation; only the first {@link shownTurns} are in the DOM. */
  private turns: ConversationTurn[] = [];
  private shownTurns = 0;
  /** The reveal footer, kept as a permanent LAST child so appends never move it. */
  private moreEl: HTMLElement | null = null;
  private moreStatusEl: HTMLElement | null = null;
  private moreBtn: HTMLButtonElement | null = null;

  constructor(private readonly ipc: IpcClient, container: HTMLElement) {
    this.root = container;
    this.root.classList.add("reader");
    this.root.setAttribute("role", "dialog");
    this.root.setAttribute("aria-modal", "true");
    this.root.hidden = true;

    const header = document.createElement("header");
    header.className = "reader-header";

    const heading = document.createElement("div");
    heading.className = "reader-heading";
    this.titleEl = document.createElement("h2");
    this.titleEl.id = "reader-title";
    this.subtitleEl = document.createElement("p");
    this.subtitleEl.className = "reader-subtitle";
    heading.append(this.titleEl, this.subtitleEl);

    this.closeBtn = document.createElement("button");
    this.closeBtn.type = "button";
    this.closeBtn.className = "reader-close";
    this.closeBtn.textContent = "Close";
    this.closeBtn.addEventListener("click", () => this.close());

    header.append(heading, this.closeBtn);

    this.bodyEl = document.createElement("div");
    this.bodyEl.className = "reader-body";
    // Focusable so the transcript itself can be scrolled by keyboard, which a plain
    // overflow container cannot be (WCAG 2.1.1).
    this.bodyEl.tabIndex = 0;

    this.root.setAttribute("aria-labelledby", this.titleEl.id);
    this.root.append(header, this.bodyEl);

    this.onKeyDown = (e) => {
      if (this.root.hidden) return;
      if (e.key === "Escape") {
        e.preventDefault();
        this.close();
      }
    };
    document.addEventListener("keydown", this.onKeyDown);
  }

  /** Is the reader currently showing? */
  get isOpen(): boolean {
    return !this.root.hidden;
  }

  /**
   * Fetch and show one conversation.
   *
   * Opens immediately with a loading state rather than after the round-trip: re-parsing a
   * rollout reads and parses a whole session file, so a click with no feedback until it
   * lands reads as a dead button.
   */
  async open(conversationId: string): Promise<void> {
    const token = ++this.latest;
    this.returnFocusTo = document.activeElement as HTMLElement | null;
    this.root.hidden = false;
    this.titleEl.textContent = "Loading…";
    this.subtitleEl.textContent = "";
    this.resetBody();
    this.closeBtn.focus();

    let conv: Conversation;
    try {
      conv = await this.ipc.conversationGet(conversationId);
    } catch (err) {
      if (token !== this.latest) return;
      this.titleEl.textContent = conversationId;
      this.subtitleEl.textContent = engineErrorText(err, "Could not open this conversation");
      return;
    }
    if (token !== this.latest) return;
    this.render(conv);
  }

  close(): void {
    // Invalidate any in-flight open, or a slow fetch would repaint a closed overlay and
    // then reappear when the user next opens one.
    this.latest++;
    this.root.hidden = true;
    this.resetBody();
    this.returnFocusTo?.focus();
    this.returnFocusTo = null;
  }

  destroy(): void {
    document.removeEventListener("keydown", this.onKeyDown);
  }

  private render(conv: Conversation): void {
    this.titleEl.textContent = readerTitle(conv);
    if (conv.available === false) {
      this.subtitleEl.textContent = conv.provider ?? "";
      const note = document.createElement("p");
      note.className = "reader-stub";
      note.textContent = stubExplanation(conv);
      this.resetBody();
      this.bodyEl.append(note);
      return;
    }
    const available = conv as ConversationAvailable;
    this.subtitleEl.textContent = readerSubtitle({
      provider: available.provider,
      turns: available.turns.length,
      parseErrors: available.parse_errors ?? 0,
    });
    if (available.turns.length === 0) {
      const note = document.createElement("p");
      note.className = "reader-stub";
      // A real case: a rollout of pure bookkeeping parses cleanly into zero turns. Saying
      // so beats an empty pane that is indistinguishable from a failed render.
      note.textContent = "This session parsed successfully but contains no messages — it "
        + "holds only bookkeeping records.";
      this.resetBody();
      this.bodyEl.append(note);
      return;
    }
    // Windowed, not all at once. Measured over this machine's 13,099 real Claude Code
    // sessions: the worst case is 2,562 turns / 22,319 blocks / 25.0M characters, which this
    // method used to build in a single `replaceChildren` — roughly 66,000 elements. See
    // `readerWindow.ts` for the distribution and for why `VirtualList` is not the tool.
    this.resetBody();
    this.turns = available.turns;
    this.mountMoreFooter();
    this.revealMore();
    // First paint only. `revealMore` must NOT touch scrollTop — the user is mid-transcript
    // when they ask for more, and yanking them back to the top is the same class of defect
    // as losing their focus.
    this.bodyEl.scrollTop = 0;
  }

  /**
   * Empty the transcript AND forget the footer.
   *
   * The two must happen together. Clearing `bodyEl` alone leaves `moreEl` pointing at a node
   * that is no longer a child, and `insertBefore` against a detached anchor throws
   * `NotFoundError` — so a stub conversation opened after a long one would break the next
   * long one rather than the one that caused it.
   */
  private resetBody(): void {
    this.bodyEl.replaceChildren();
    this.turns = [];
    this.shownTurns = 0;
    this.moreEl = null;
    this.moreStatusEl = null;
    this.moreBtn = null;
  }

  /** The reveal control, mounted once and never rebuilt, so appending cannot move it. */
  private mountMoreFooter(): void {
    const foot = document.createElement("div");
    foot.className = "reader-more";
    const status = document.createElement("p");
    status.className = "reader-more-status";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "reader-more-btn";
    btn.addEventListener("click", () => this.revealMore());
    foot.append(status, btn);
    this.bodyEl.append(foot);
    this.moreEl = foot;
    this.moreStatusEl = status;
    this.moreBtn = btn;
  }

  /**
   * Add the next window of turns.
   *
   * APPEND-ONLY, and that is the whole safety argument. `VirtualList.paint()` rebuilds its
   * window with `sizer.replaceChildren()`, which detaches whatever held focus and drops the
   * user at `<body>` — measured at 38 of 1,000 rows reachable by Tab, and the reason
   * `tools/probe_keyboard_reach.mjs` exists. Nothing here rebuilds an existing node: new
   * turns are inserted BEFORE a footer that is mounted once, so focus and scroll position
   * are untouched by construction rather than restored afterwards.
   */
  private revealMore(): void {
    const win = turnWindow(this.turns.length, this.shownTurns);
    const fragment = document.createDocumentFragment();
    for (let i = win.from; i < win.end; i++) fragment.append(this.renderTurn(this.turns[i]));
    this.bodyEl.insertBefore(fragment, this.moreEl);
    this.shownTurns = win.end;

    const label = moreButtonLabel(win.remaining);
    if (this.moreStatusEl !== null) {
      this.moreStatusEl.textContent = windowStatus(this.shownTurns, this.turns.length);
    }
    if (label === null) {
      // Everything is shown. Removing the button would orphan focus at `<body>` if it is
      // what the user just clicked, so hand focus to the transcript itself — which is
      // `tabIndex = 0` precisely so it can be scrolled by keyboard.
      const hadFocus = this.moreBtn !== null && document.activeElement === this.moreBtn;
      this.moreEl?.remove();
      this.moreEl = null;
      this.moreStatusEl = null;
      this.moreBtn = null;
      if (hadFocus) this.bodyEl.focus({ preventScroll: true });
      return;
    }
    if (this.moreBtn !== null) this.moreBtn.textContent = label;
  }

  private renderTurn(turn: ConversationTurn): HTMLElement {
    const el = document.createElement("article");
    el.className = `reader-turn reader-turn-${blockClass(turn.role)}`;

    const meta = document.createElement("div");
    meta.className = "reader-turn-meta";
    const who = document.createElement("span");
    who.className = "reader-role";
    who.textContent = roleLabel(turn.role);
    const when = document.createElement("time");
    when.className = "reader-when";
    when.textContent = turnWhen(turn.timestamp ?? "");
    if (turn.timestamp) when.dateTime = turn.timestamp;
    meta.append(who, when);

    el.append(meta, ...turn.blocks.map((raw) => {
      const shown = blockDisplay(raw);
      const wrap = document.createElement("div");
      wrap.className = `reader-block reader-block-${blockClass(shown.kind)}`;
      if (shown.label !== "") {
        const badge = document.createElement("span");
        badge.className = "reader-badge";
        badge.textContent = shown.label;
        wrap.append(badge);
      }
      const body = document.createElement("pre");
      body.className = "reader-text";
      // `pre` + textContent: transcripts are whitespace-significant (code, diffs, tool
      // output) and are never interpreted as markup.
      body.textContent = shown.body;
      wrap.append(body);
      return wrap;
    }));
    return el;
  }
}
