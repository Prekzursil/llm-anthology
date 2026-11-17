// @vitest-environment happy-dom
/**
 * {@link ReaderOverlay} — the transcript overlay, through real `click`/`keydown` events on a
 * mounted DOM and a one-method IPC stub.
 *
 * WHAT IS WORTH ASSERTING (all unreachable without a document):
 *
 *   1. THE OPEN JOURNEY. The overlay opens with a loading state IMMEDIATELY (before the IPC
 *      resolves), then renders title/subtitle/turns — plus the two honest degenerate bodies
 *      (an unavailable stub; a parsed-but-empty session) and the failure line.
 *   2. THE WINDOWED TRANSCRIPT. A conversation longer than `READER_TURN_CHUNK` appends in
 *      windows behind a footer that is never rebuilt; the LAST reveal removes the footer and
 *      hands focus to the transcript body when the reveal button itself held it.
 *   3. THE TOKEN GUARD. A slow open that loses a race with a newer one — or with a close —
 *      must not repaint. Each arm is pinned below.
 *
 * WHY `document.body` MOUNTING. `focus()` only moves `document.activeElement` for ATTACHED
 * elements (the vitest.config.ts trap note), and this overlay's focus contract (close button
 * takes focus on open; opener gets it back on close; body takes it from the reveal button)
 * is exactly what those assertions need.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  Conversation,
  ConversationAvailable,
  ConversationStub,
  ConversationTurn,
  IpcClient,
} from "../ipc";
import { engineErrorText } from "./errors";
import { moreButtonLabel, READER_TURN_CHUNK, windowStatus } from "./readerWindow";
import { ReaderOverlay } from "./reader";

function turn(over: Partial<ConversationTurn> = {}): ConversationTurn {
  return {
    role: "user",
    timestamp: "2026-01-02T03:04:05.000Z",
    blocks: [{ type: "text", text: "hello" }],
    ...over,
  };
}

function available(over: Partial<ConversationAvailable> = {}): ConversationAvailable {
  return {
    id: "c1",
    title: "A conversation",
    provider: "claude-code",
    available: true,
    turns: [turn()],
    ...over,
  };
}

function stub(over: Partial<ConversationStub> = {}): ConversationStub {
  return { id: "c9", turns: [], available: false, reason: "rollout missing", ...over };
}

type GetImpl = (id: string) => Promise<Conversation>;

interface Rig {
  panel: ReaderOverlay;
  root: HTMLElement;
  conversationGet: ReturnType<typeof vi.fn>;
}

function makeReader(impl?: GetImpl): Rig {
  const root = document.createElement("div");
  document.body.append(root);
  const opener = document.createElement("button");
  opener.textContent = "opener";
  document.body.append(opener);
  opener.focus();
  const conversationGet = vi.fn(impl ?? (async (id: string) => available({ id }) as Conversation));
  const panel = new ReaderOverlay({ conversationGet } as unknown as IpcClient, root);
  return { panel, root, conversationGet };
}

/** The mounted `.reader-body` element. */
function body(rig: Rig): HTMLElement {
  return rig.root.querySelector<HTMLElement>(".reader-body") as HTMLElement;
}

function pressEscape(): void {
  document.dispatchEvent(
    new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
  );
}

let rig: Rig = makeReader();

beforeEach(() => {
  rig = makeReader();
});

afterEach(() => {
  rig.panel.destroy();
  document.body.replaceChildren();
});

/** `n` minimal turns, for windowing cases. File scope: two describe blocks need it. */
function longTurns(n: number): ConversationTurn[] {
  return Array.from({ length: n }, (_v, i) =>
    turn({ blocks: [{ type: "text", text: `turn ${i}` }] }),
  );
}

describe("ReaderOverlay construction", () => {
  it("builds the dialog wiring hidden, labelled by its own title", () => {
    expect(rig.root.classList.contains("reader")).toBe(true);
    expect(rig.root.getAttribute("role")).toBe("dialog");
    expect(rig.root.getAttribute("aria-modal")).toBe("true");
    expect(rig.root.getAttribute("aria-labelledby")).toBe("reader-title");
    expect(rig.root.hidden).toBe(true);
    expect(rig.root.querySelector("h2#reader-title")).not.toBeNull();
    expect(rig.root.querySelector("button.reader-close")?.textContent).toBe("Close");
    expect(body(rig).tabIndex).toBe(0);
    expect(rig.panel.isOpen).toBe(false);
  });
});

describe("ReaderOverlay open", () => {
  it("shows a loading state before the fetch resolves", async () => {
    let resolve!: (c: Conversation) => void;
    rig = makeReader(
      () =>
        new Promise<Conversation>((res) => {
          resolve = res;
        }),
    );
    const opening = rig.panel.open("c1");
    expect(rig.panel.isOpen).toBe(true);
    expect(rig.root.querySelector("h2")?.textContent).toBe("Loading…");
    expect(document.activeElement).toBe(rig.root.querySelector("button.reader-close"));
    resolve(available());
    await opening;
  });

  it("renders title, subtitle and turn articles once the conversation lands", async () => {
    const conv = available({
      title: "Debug session",
      provider: "codex",
      parse_errors: 1,
      turns: [
        turn({ role: "user", blocks: [{ type: "text", text: "please help" }] }),
        turn({
          role: "assistant",
          timestamp: null,
          blocks: [{ type: "tool_use", text: "ran a command" }],
        }),
      ],
    });
    rig = makeReader(async () => conv);
    await rig.panel.open("c1");
    const articles = rig.root.querySelectorAll("article.reader-turn");
    expect(articles).toHaveLength(2);
    expect(articles[0]?.classList.contains("reader-turn-user")).toBe(true);
    expect(articles[1]?.classList.contains("reader-turn-assistant")).toBe(true);
    // A timestamped turn carries <time datetime>; an untimestamped one does not.
    const times = [...rig.root.querySelectorAll("time.reader-when")];
    expect(times[0]?.getAttribute("datetime")).toBe("2026-01-02T03:04:05.000Z");
    expect(times[1]?.hasAttribute("datetime")).toBe(false);
    expect(articles[1]?.querySelector(".reader-text")?.textContent).toBe("ran a command");
  });

  it("explains an unavailable conversation instead of rendering an empty pane", async () => {
    const s = stub({ id: "gone", provider: "claude-code", reason: "rollout file deleted" });
    rig = makeReader(async () => s);
    await rig.panel.open("gone");
    const note = rig.root.querySelector("p.reader-stub");
    expect(note).not.toBeNull();
    expect(note?.textContent).toContain("rollout file deleted");
    expect(rig.root.querySelectorAll("article.reader-turn")).toHaveLength(0);
  });

  it("says so when a session parses cleanly into zero turns", async () => {
    rig = makeReader(async () => available({ turns: [] }));
    await rig.panel.open("c1");
    expect(rig.root.querySelector("p.reader-stub")?.textContent).toContain(
      "holds only bookkeeping records",
    );
  });

  it("surfaces a fetch failure under the shared presenter, named by the id", async () => {
    const err = new Error("engine exploded");
    rig = makeReader(() => Promise.reject(err));
    await rig.panel.open("c42");
    expect(rig.root.querySelector("h2")?.textContent).toBe("c42");
    expect(rig.root.querySelector(".reader-subtitle")?.textContent).toBe(
      engineErrorText(err, "Could not open this conversation"),
    );
  });

  it("restores focus to the opener on close and empties the body", async () => {
    // makeReader focused the opener button; capture it BEFORE open — the overlay's own
    // Close button precedes it in document order, so querySelector order is a trap.
    const opener = document.activeElement as HTMLButtonElement;
    expect(opener.textContent).toBe("opener");
    await rig.panel.open("c1");
    rig.panel.close();
    expect(rig.panel.isOpen).toBe(false);
    expect(body(rig).children).toHaveLength(0);
    expect(document.activeElement).toBe(opener);
  });
});

describe("ReaderOverlay windowed transcript", () => {
  it("windows a long transcript behind a persistent footer until the last step", async () => {
    const total = READER_TURN_CHUNK + 5;
    rig = makeReader(async () => available({ turns: longTurns(total) }));
    await rig.panel.open("c1");
    expect(rig.root.querySelectorAll("article.reader-turn")).toHaveLength(READER_TURN_CHUNK);
    const footer = rig.root.querySelector(".reader-more");
    expect(footer).not.toBeNull();
    const statusEl = footer?.querySelector(".reader-more-status");
    const btn = footer?.querySelector<HTMLButtonElement>("button.reader-more-btn");
    expect(statusEl?.textContent).toBe(windowStatus(READER_TURN_CHUNK, total));
    expect(btn?.textContent).toBe(moreButtonLabel(5));

    btn?.click();
    expect(rig.root.querySelectorAll("article.reader-turn")).toHaveLength(total);
    // Everything shown: the footer is GONE, not merely disabled.
    expect(rig.root.querySelector(".reader-more")).toBeNull();
  });

  it("hands focus to the transcript when the final reveal click came from the button", async () => {
    const total = READER_TURN_CHUNK + 1;
    rig = makeReader(async () => available({ turns: longTurns(total) }));
    await rig.panel.open("c1");
    const btn = rig.root.querySelector<HTMLButtonElement>("button.reader-more-btn");
    btn?.focus();
    expect(document.activeElement).toBe(btn);
    btn?.click();
    expect(rig.root.querySelector(".reader-more")).toBeNull();
    expect(document.activeElement).toBe(body(rig));
  });

  it("a short transcript never mounts the footer", async () => {
    rig = makeReader(async () => available({ turns: longTurns(3) }));
    await rig.panel.open("c1");
    expect(rig.root.querySelectorAll("article.reader-turn")).toHaveLength(3);
    expect(rig.root.querySelector(".reader-more")).toBeNull();
  });

  it("a stub opened after a long transcript leaves the next long one working", async () => {
    // The resetBody/footer regression the module header documents: the footer must be
    // forgotten with the body, or the next windowed render inserts before a detached node.
    rig = makeReader(async (id) =>
      id === "stub"
        ? (stub() as Conversation)
        : (available({ id, turns: longTurns(READER_TURN_CHUNK + 2) }) as Conversation),
    );
    await rig.panel.open("long1");
    expect(rig.root.querySelector(".reader-more")).not.toBeNull();
    await rig.panel.open("stub");
    expect(rig.root.querySelector(".reader-more")).toBeNull();
    await rig.panel.open("long2");
    expect(rig.root.querySelectorAll("article.reader-turn")).toHaveLength(READER_TURN_CHUNK);
    expect(rig.root.querySelector("button.reader-more-btn")).not.toBeNull();
  });
});

describe("ReaderOverlay stale-open guards", () => {
  it("a slower older open must not repaint a newer one", async () => {
    let resolveA!: (c: Conversation) => void;
    rig = makeReader((id) =>
      id === "a"
        ? new Promise<Conversation>((res) => {
            resolveA = res;
          })
        : Promise.resolve(available({ id, title: "B wins" })),
    );
    const a = rig.panel.open("a");
    await rig.panel.open("b");
    resolveA(available({ id: "a", title: "A loses" }));
    await a;
    expect(rig.root.querySelector("h2")?.textContent).toBe("B wins");
  });

  it("a failure landing after close leaves the overlay closed and empty", async () => {
    let reject!: (e: Error) => void;
    rig = makeReader(
      () =>
        new Promise<Conversation>((_res, rej) => {
          reject = rej;
        }),
    );
    const opening = rig.panel.open("c1");
    rig.panel.close();
    reject(new Error("too late"));
    await opening;
    expect(rig.panel.isOpen).toBe(false);
    expect(body(rig).children).toHaveLength(0);
    expect(rig.root.querySelector("h2")?.textContent).not.toBe("c1");
  });
});

describe("ReaderOverlay keyboard + teardown", () => {
  it("Escape closes an open overlay but is ignored while hidden", async () => {
    await rig.panel.open("c1");
    pressEscape();
    expect(rig.panel.isOpen).toBe(false);
    // Hidden now: a second press must do nothing (and not throw).
    pressEscape();
    expect(rig.panel.isOpen).toBe(false);
  });

  it("the Close button closes the overlay", async () => {
    await rig.panel.open("c1");
    rig.root.querySelector<HTMLButtonElement>("button.reader-close")?.click();
    expect(rig.panel.isOpen).toBe(false);
  });

  it("ignores a non-Escape keydown while open (only Escape is ours)", async () => {
    await rig.panel.open("c1");
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "a", bubbles: true, cancelable: true }),
    );
    expect(rig.panel.isOpen).toBe(true);
  });

  it("revealMore tolerates a footer that was already torn down (defensive null guards)", async () => {
    // White-box on purpose: the guards at the footer fields exist for a state machine slip
    // (a reveal after the footer was removed), which no DOM journey can produce — the
    // reveal button is gone by then. Driving the method directly with the fields nulled
    // is the only honest way to measure those arms.
    const total = READER_TURN_CHUNK + 2;
    rig = makeReader(async () => available({ turns: longTurns(total) }));
    await rig.panel.open("c1");
    const inner = rig.panel as unknown as {
      turns: ConversationTurn[];
      shownTurns: number;
      moreEl: HTMLElement | null;
      moreStatusEl: HTMLElement | null;
      moreBtn: HTMLButtonElement | null;
      revealMore(): void;
    };
    inner.moreStatusEl = null;
    inner.moreBtn = null;
    inner.moreEl = null;
    expect(() => inner.revealMore()).not.toThrow();
    // The second window IS in the DOM; the absent footer just could not be updated.
    expect(rig.root.querySelectorAll("article.reader-turn").length).toBeGreaterThan(
      READER_TURN_CHUNK,
    );
  });

  it("destroy removes the document listener: Escape no longer closes", async () => {
    await rig.panel.open("c1");
    rig.panel.destroy();
    pressEscape();
    expect(rig.panel.isOpen).toBe(true);
  });
});
