/**
 * The CORPUS BAR: the app's route to its primary action — attaching the engine to a
 * corpus index.
 *
 * Until this existed the shipped cockpit had NO caller for `open_corpus`, so every data
 * method answered with the engine's raw "no corpus attached: call open_corpus first"
 * (`src-tauri/src/lib.rs:45`) and the app booted into a permanently dead state whose only
 * on-screen instruction was to call a function the user has no way to call.
 *
 * Split the way `ui/exportPanel` is: a DOM-FREE controller plus pure derivations that
 * carry every DECISION — what the label reads, what a dismissed picker means, what a
 * failure says, whether a remembered path is retried — and a thin DOM shell that owns
 * only the native picker and the painting. That split is the sole reason any of this is
 * testable: vitest runs `environment: "node"` here (see `vitest.config.ts`), so anything
 * welded to `document`/`localStorage` could not be asserted at all. It is the same seam
 * `virtualList`'s `emptyStateLabel` uses, at controller granularity.
 */

import { open } from "@tauri-apps/plugin-dialog";

import { isTauriRuntime } from "../ipc";
import type { AppInfo, OpenCorpusResult } from "../ipc/types";

// ---------------------------------------------------------------------------
// constants
// ---------------------------------------------------------------------------

/** Web Storage key holding the last successfully-opened index path. */
export const CORPUS_STORAGE_KEY = "cockpit.lastCorpusIndex";

/** Prominent label shown while no corpus is attached. */
export const NO_CORPUS_LABEL = "No corpus open";

/**
 * Shown when the button is pressed OUTSIDE the Tauri webview. The native picker is the
 * only way to name a filesystem path (a webview `<input type="file">` yields no path in
 * Tauri v2), so in a browser preview there is nothing to open — and `ipc/index.ts` has
 * already bound that environment to the mock forest, which is what the panes are showing.
 */
export const PICKER_UNAVAILABLE_MESSAGE =
  "The file picker is only available in the desktop app — this preview is showing sample data.";

/**
 * File extensions offered by the picker. The corpus index is a single SQLite FILE (the
 * engine hands the path straight to `sqlite3.connect`), not a directory.
 */
const INDEX_EXTENSIONS = ["db", "sqlite3", "sqlite"];

// ---------------------------------------------------------------------------
// pure derivations
// ---------------------------------------------------------------------------

/**
 * The final path segment of `path`, for either separator — the picker returns a native
 * Windows path here while the engine and its fixtures use POSIX ones, and both must
 * shorten correctly. Trailing separators are ignored; a path with no separator is its own
 * basename.
 */
export function basenameOf(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, "");
  const cut = Math.max(trimmed.lastIndexOf("/"), trimmed.lastIndexOf("\\"));
  return cut === -1 ? trimmed : trimmed.slice(cut + 1);
}

/** What the bar's label element shows: prominent text plus the full path as a tooltip. */
export interface CorpusLabel {
  /** The index basename, or {@link NO_CORPUS_LABEL} when nothing is attached. */
  label: string;
  /** The full path, for the element's `title`; empty when nothing is attached. */
  title: string;
}

/**
 * `path` -> the label pair. A basename alone is what the user recognises; the full path
 * stays reachable on hover rather than eating the topbar. A path that is nothing but
 * separators has no basename, so it falls back to showing itself.
 */
export function corpusLabel(path: string | null): CorpusLabel {
  if (path === null || path.trim() === "") {
    return { label: NO_CORPUS_LABEL, title: "" };
  }
  const base = basenameOf(path);
  return { label: base === "" ? path : base, title: path };
}

/**
 * Engine strings that must never reach the user verbatim, and what to say instead. The
 * only entry is the dead-end the whole component exists to remove: an instruction to call
 * an internal command. Anything else keeps its detail — a real cause ("file not found",
 * "database is locked") is far more useful than a generic apology.
 */
const REWRITES: ReadonlyArray<readonly [RegExp, string]> = [
  [
    /no corpus attached/i,
    "No corpus is attached yet — choose a corpus index file to open one.",
  ],
];

/** The bare Tauri/JSON-RPC command identifier, scrubbed out of any surviving detail. */
const COMMAND_IDENTIFIER = /\bopen_corpus\b/g;

/**
 * Engine messages that are ALREADY written for the user, and so are surfaced VERBATIM.
 *
 * `open_corpus` validates the path before spawning and rejects with prose the user can
 * act on directly (`src-tauri/src/lib.rs:56-62`): `no corpus index at <path>` and
 * `<path> is a folder, not a corpus index file`. Both already name the path and imply
 * the fix, so wrapping them in "Could not open that corpus: …" would only print the path
 * a second time. Anything NOT matched here is an internal failure and keeps the wrapper,
 * because a bare `spawn ENOENT` with no context is worse than a wrapped one.
 */
const SELF_EXPLANATORY: readonly RegExp[] = [
  /^no corpus index at\b/i,
  /\bis a folder, not a corpus index file$/i,
];

/** Whether `detail` is engine prose that should reach the user unaltered. */
function isSelfExplanatory(detail: string): boolean {
  return SELF_EXPLANATORY.some((pattern) => pattern.test(detail));
}

/** An unknown throwable -> its display detail, with the internal command name removed. */
function detailOf(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err);
  return raw.replace(COMMAND_IDENTIFIER, "the engine").trim();
}

/** Join a sentence to a follow-up without doubling its terminal punctuation. */
function sentence(head: string, tail: string): string {
  return `${head.replace(/[.!?]+$/, "")}. ${tail}`;
}

/**
 * A failed MANUAL open -> readable text for the line beside the button. Never surfaces
 * an internal RPC method name: a known dead-end string is replaced outright, and any
 * other message has the bare command identifier scrubbed before it is shown.
 */
export function openFailureMessage(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err);
  for (const [pattern, replacement] of REWRITES) {
    if (pattern.test(raw)) return replacement;
  }
  const detail = detailOf(err);
  if (detail === "") return "Could not open that corpus.";
  if (isSelfExplanatory(detail)) return detail;
  return `Could not open that corpus: ${detail}`;
}

/**
 * A failed AUTO-restore of a remembered path -> readable text, always ending in the
 * action to take. The user did not ask for this open (the file moved or was deleted
 * since the last launch), so an unexplained error about a corpus they never picked this
 * session would be baffling. Engine prose already names the path, so only the
 * NON-self-explanatory branch adds the filename itself.
 */
export function restoreFailureMessage(path: string, err: unknown): string {
  const detail = detailOf(err);
  const name = basenameOf(path) || path;
  const next = "Choose a corpus to continue.";
  if (detail === "") return `Could not reopen ${name}. ${next}`;
  if (isSelfExplanatory(detail)) return sentence(detail, next);
  return sentence(`Could not reopen ${name}: ${detail}`, next);
}

// ---------------------------------------------------------------------------
// persistence seam
// ---------------------------------------------------------------------------

/** The three Web Storage members the bar uses; `localStorage` satisfies it structurally. */
export interface WebStorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** Remembering the last corpus, narrowed to the three operations the controller needs. */
export interface CorpusStore {
  read(): string | null;
  write(path: string): void;
  clear(): void;
}

/**
 * A {@link CorpusStore} over `storage`, tolerant of every way Web Storage can be absent
 * or refuse: not present at all (`null` — the node test runner), or throwing on access (a
 * webview with site data disabled, a quota-exceeded write). A remembered path is a
 * convenience; losing it must never take the bar down with it, so every call degrades to
 * a no-op or `null`.
 */
export function makeCorpusStore(
  storage: WebStorageLike | null,
  key: string = CORPUS_STORAGE_KEY,
): CorpusStore {
  return {
    read(): string | null {
      if (storage === null) return null;
      try {
        return storage.getItem(key);
      } catch {
        return null;
      }
    },
    write(path: string): void {
      if (storage === null) return;
      try {
        storage.setItem(key, path);
      } catch {
        // Persisting is best-effort; the corpus is already open either way.
      }
    },
    clear(): void {
      if (storage === null) return;
      try {
        storage.removeItem(key);
      } catch {
        // Same: a store that refuses to forget is not worth failing an open over.
      }
    },
  };
}

/**
 * The default store: `localStorage` when the environment has one. Merely *touching* the
 * global can throw in a hardened webview, so the probe itself is guarded.
 */
export function localCorpusStore(key: string = CORPUS_STORAGE_KEY): CorpusStore {
  let storage: WebStorageLike | null = null;
  try {
    storage = globalThis.localStorage ?? null;
  } catch {
    storage = null;
  }
  return makeCorpusStore(storage, key);
}

// ---------------------------------------------------------------------------
// headless controller
// ---------------------------------------------------------------------------

/** The IPC surface the bar needs, narrowed from `IpcClient` (interface segregation). */
export interface CorpusIpc {
  openCorpus(indexPath: string): Promise<OpenCorpusResult>;
  /**
   * OPTIONAL, unlike `openCorpus`, and the asymmetry is deliberate. The bar cannot do its
   * job without attaching a corpus, but it works perfectly well without knowing where the
   * DEFAULT index would live — that is a hint, not a dependency. Keeping it optional also
   * means every existing narrowed fake still satisfies this interface.
   */
  appInfo?: () => Promise<AppInfo>;
}

/**
 * The tooltip shown on the label while NOTHING is attached: where the app would put an
 * index of its own. `index_path` is a DEFAULT, never a requirement, so the wording must
 * not read as an instruction to open that specific file.
 */
export function defaultIndexHint(indexPath: string): string {
  return `No corpus is open. This app keeps its own index at ${indexPath} by default.`;
}

/** The bar's render-state, emitted on every transition. */
export interface CorpusBarView {
  /** The attached index path, or null when none is attached. */
  path: string | null;
  /** Prominent label: the index basename, or {@link NO_CORPUS_LABEL}. */
  label: string;
  /** Full path for the label's `title`; empty when nothing is attached. */
  title: string;
  /** A readable failure to show beside the button; empty when there is nothing to say. */
  error: string;
  /** True while an open is in flight (the button is disabled for the duration). */
  busy: boolean;
}

/** Called with the current {@link CorpusBarView} on every state transition. */
export type CorpusViewListener = (view: CorpusBarView) => void;

/** Called with the index path after a corpus is successfully attached. */
export type CorpusOpenedListener = (indexPath: string) => void;

/** The view before anything has been attempted. */
function initialView(): CorpusBarView {
  return { path: null, label: NO_CORPUS_LABEL, title: "", error: "", busy: false };
}

/**
 * Headless corpus-bar controller. Attaches a corpus through the injected
 * {@link CorpusIpc}, remembers the last successful path in the injected
 * {@link CorpusStore}, tracks a single-flight busy flag (a re-entrant call while one is
 * in flight is ignored, as in {@link import("./exportPanel").ExportPanel}), and emits the
 * current {@link CorpusBarView} to `onChange`. `current` exposes the latest view for an
 * initial paint.
 */
export class CorpusBarController {
  private view: CorpusBarView = initialView();
  private busy = false;

  constructor(
    private readonly ipc: CorpusIpc,
    private readonly store: CorpusStore,
    private readonly onChange: CorpusViewListener,
    private readonly onOpened: CorpusOpenedListener,
  ) {}

  get current(): CorpusBarView {
    return this.view;
  }

  private emit(view: CorpusBarView): void {
    this.view = view;
    this.onChange(view);
  }

  /**
   * Attach `indexPath`. `null` is the user DISMISSING the picker: a no-op, not an error —
   * it must not clear the attached corpus and must not paint a message, so the view is
   * left byte-identical.
   */
  async open(indexPath: string | null): Promise<void> {
    if (indexPath === null) return;
    await this.attach(indexPath, false);
  }

  /**
   * Re-attach the remembered corpus, if any, so a relaunch does not make the user pick
   * again. Called once on boot. Nothing remembered is silent — a first launch is not an
   * error.
   */
  async restore(): Promise<void> {
    const remembered = this.store.read();
    if (remembered === null || remembered.trim() === "") {
      await this.hintDefaultIndex();
      return;
    }
    await this.attach(remembered, true);
  }

  /**
   * CF-1. With nothing remembered, say where the app keeps its own index instead of
   * showing a bare "No corpus open" and a picker. `app_info` resolves the DECISION G-10
   * locations against the real environment and, until this call existed, nothing on this
   * side of the Tauri boundary read the answer.
   *
   * FAIL-OPEN AT EVERY STEP, because none of them is worth a visible failure: the method
   * may be absent (a narrowed fake, or the mock before this shipped), the call may reject,
   * and resolution itself may legitimately fail — a stripped environment with no
   * `%USERPROFILE%` returns `locations_error` (`lib.rs:26-29`). A missing hint costs the
   * user nothing; an error line about a path they never asked about costs them attention,
   * and that line is reserved for a failed OPEN.
   *
   * Only reached when nothing is remembered, so a returning user does not pay for it.
   */
  private async hintDefaultIndex(): Promise<void> {
    const ask = this.ipc.appInfo;
    if (ask === undefined) return;
    let indexPath: string;
    try {
      const resolved = (await ask.call(this.ipc)).locations?.index_path;
      if (resolved === undefined || resolved.trim() === "") return;
      indexPath = resolved;
    } catch {
      return;
    }
    this.emit({ ...this.view, title: defaultIndexHint(indexPath) });
  }

  private async attach(indexPath: string, remembered: boolean): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    this.emit({ ...this.view, error: "", busy: true });
    try {
      const result = await this.ipc.openCorpus(indexPath);
      if (!result.ok) throw new Error(`the engine did not attach ${indexPath}`);
      this.store.write(result.index);
      const { label, title } = corpusLabel(result.index);
      this.emit({ path: result.index, label, title, error: "", busy: false });
      this.onOpened(result.index);
    } catch (err) {
      // A remembered path that no longer opens FORGETS itself, so the next launch starts
      // clean instead of retrying a dead path on every boot forever.
      if (remembered) this.store.clear();
      // The engine spawns the replacement BEFORE dropping the incumbent
      // (`src-tauri/src/lib.rs:53-56`), so a failed open leaves whatever was already
      // attached still attached. Resetting the label to "No corpus open" here would be a
      // lie about the engine's actual state, so only the error line changes.
      this.emit({
        ...this.view,
        error: remembered
          ? restoreFailureMessage(indexPath, err)
          : openFailureMessage(err),
        busy: false,
      });
    } finally {
      this.busy = false;
    }
  }
}

// ---------------------------------------------------------------------------
// DOM shell
// ---------------------------------------------------------------------------

/**
 * The corpus bar's DOM binding: an "Open corpus…" button, the current-index label, and a
 * failure line beside them. Element refs are passed in (as in {@link
 * import("./search").SearchPanel}); everything else is delegated to
 * {@link CorpusBarController}, so this class holds only the native picker call and the
 * paint. Text is written with `textContent`, never `innerHTML`, so a path or an engine
 * error string can never inject markup.
 */
export class CorpusBar {
  private readonly controller: CorpusBarController;
  private readonly onClick: () => void;

  constructor(
    ipc: CorpusIpc,
    private readonly button: HTMLButtonElement,
    private readonly label: HTMLElement,
    private readonly error: HTMLElement,
    onOpened: CorpusOpenedListener,
    store: CorpusStore = localCorpusStore(),
  ) {
    this.controller = new CorpusBarController(
      ipc,
      store,
      (view) => this.paint(view),
      onOpened,
    );
    this.onClick = () => void this.pick();
    this.button.addEventListener("click", this.onClick);
    this.paint(this.controller.current);
  }

  /** Re-attach the remembered corpus, if any. The app calls this once on boot. */
  async restore(): Promise<void> {
    await this.controller.restore();
  }

  /**
   * Show the native picker and attach whatever it returns. `open` resolves to `null` when
   * the user dismisses the dialog, which the controller treats as a no-op.
   */
  private async pick(): Promise<void> {
    if (!isTauriRuntime()) {
      this.error.textContent = PICKER_UNAVAILABLE_MESSAGE;
      return;
    }
    let picked: string | null;
    try {
      picked = await open({
        multiple: false,
        directory: false,
        title: "Open corpus index",
        filters: [{ name: "Corpus index", extensions: INDEX_EXTENSIONS }],
      });
    } catch (err) {
      this.error.textContent = openFailureMessage(err);
      return;
    }
    await this.controller.open(picked);
  }

  private paint(view: CorpusBarView): void {
    this.label.textContent = view.label;
    if (view.title === "") this.label.removeAttribute("title");
    else this.label.setAttribute("title", view.title);
    this.error.textContent = view.error;
    this.button.disabled = view.busy;
  }

  /** Tear down: drop the click listener. The elements themselves are the shell's, not ours. */
  destroy(): void {
    this.button.removeEventListener("click", this.onClick);
  }
}
