/**
 * COPY DIAGNOSTICS — DECISION G-8's user-facing half.
 *
 * The problem this closes is not a missing feature, it is a missing CHANNEL. Engine stderr
 * used to be read into a throwaway array and discarded (`src-tauri/src/sidecar.rs`'s old
 * `drain_stderr`), so when a stranger's import failed there was nothing to report and nothing
 * to ask for. The Rust parent now keeps a bounded, scrubbed ring plus a capped crash file;
 * this module is what turns that into one block of text a user can paste into an issue.
 *
 * WHAT IT ADDS OVER THE RUST BUNDLE. Two things the parent process cannot know:
 *
 *   * the ENGINE-side numbers — index stats, engine/IR version — which the UI already holds
 *     from `corpusStats` / `healthPing`. Fetching them in Rust would have put an RPC round
 *     trip on the boot path, since the bundle rides the `app_info` command.
 *   * the framing a human reading a bug report needs: a header that states the privacy
 *     contract, and explicit "(none)" text where a section is empty — an absent section
 *     reads as a broken tool, and a reporter who thinks the tool is broken stops reporting.
 *
 * PRIVACY IS THIS MODULE'S JOB TOO, not only the Rust side's. Everything crossing from Rust
 * is already allowlisted and home-relativized; everything added HERE has to meet the same
 * bar. The one dangerous field is the corpus index PATH, which embeds the username — so it
 * is reduced to a BASENAME ({@link indexLabel}) before it can reach the text, exactly as the
 * engine reduces a filesystem path for the same reason (`llm_anthology/sidecar.py`'s
 * `_build_error`, cited by NAME because that file is under concurrent edit and a line number
 * rots — `tests/test_citation_anchors.py` pins `mock.ts`/`types.ts` only, not `ui/**`).
 * There is no code path in this file that can emit a full path.
 *
 * Split as `ui/corpusBar` and `ui/exportPanel` are: pure derivations plus a DOM-free
 * controller carrying every decision, and a thin shell that owns only the button and the
 * paint. vitest runs `environment: "node"` here, so that split is the only reason any of it
 * is testable.
 */

import type { CorpusStats, HealthInfo } from "../ipc/types";

// ---------------------------------------------------------------------------
// the shape crossing the wire from Rust
// ---------------------------------------------------------------------------

/** Host platform, as the Rust parent reports it (`std::env::consts`). */
export interface DiagnosticsPlatform {
  os: string;
  arch: string;
  family: string;
}

/** A crash-file generation: where it is, how big, and its hard ceiling. */
export interface DiagnosticsFile {
  path: string;
  bytes: number;
}

/**
 * The `diagnostics` sub-object of `app_info`, produced by
 * `src-tauri/src/sidecar.rs`'s `Diagnostics::bundle`.
 *
 * `crash_file` / `previous_run` are present-and-null rather than absent when there is no
 * file (the app-data folder could not be resolved, or the suite is running), so a consumer
 * branches on shape — the same contract `locations` / `locations_error` use.
 */
export interface EngineDiagnostics {
  platform: DiagnosticsPlatform | null;
  engine_stderr: string;
  engine_stderr_cap_bytes: number;
  crash_file: (DiagnosticsFile & { cap_bytes: number }) | null;
  previous_run: (DiagnosticsFile & { tail: string }) | null;
}

// ---------------------------------------------------------------------------
// pure derivations
// ---------------------------------------------------------------------------

/** Header text. States the privacy contract, because both sides of a report need it. */
export const BUNDLE_HEADER = "LLM Anthology diagnostics";

/**
 * The privacy note. Written for the person PASTING, who is about to publish this: it says
 * what is in the text so they do not have to take it on trust, and it names `~` so a reader
 * of the issue does not report the tildes as corruption.
 */
export const BUNDLE_PRIVACY_NOTE =
  "No conversation content: every engine stderr line is matched against a structural " +
  "allowlist (traceback frames and exception TYPES survive, messages do not), your home " +
  "folder reads as ~ and your username as <user>.";

/** Shown where a section has nothing in it — an absent section reads as a broken tool. */
export const EMPTY_STDERR_TEXT = "(none — the engine has written nothing to stderr)";

/**
 * A corpus index path reduced to something safe to publish.
 *
 * The path is the one field in this bundle that carries the username, and a bug report is
 * pasted into a public tracker, so it is reduced to its final segment and nothing else. This
 * duplicates three lines of `ui/corpusBar`'s `basenameOf` deliberately: importing that module
 * would pull `@tauri-apps/plugin-dialog` and `../ipc` (which selects the mock adapter and
 * loads `elkjs`) into a module whose whole point is to be a leaf.
 */
export function indexLabel(path: string | null): string {
  if (path === null || path.trim() === "") return "(none)";
  const trimmed = path.replace(/[\\/]+$/, "");
  const cut = Math.max(trimmed.lastIndexOf("/"), trimmed.lastIndexOf("\\"));
  const base = cut === -1 ? trimmed : trimmed.slice(cut + 1);
  return base === "" ? "(none)" : base;
}

/**
 * Narrow the `diagnostics` payload out of an `app_info` result.
 *
 * DEFENSIVE on purpose. This crosses an IPC boundary from Rust, and a version skew between
 * a shipped shell and a shipped frontend is a real state — the installer updates both, but a
 * dev run does not. A malformed payload must degrade to "no diagnostics" and still let the
 * user copy the rest, never throw inside a click handler.
 */
export function readEngineDiagnostics(raw: unknown): EngineDiagnostics | null {
  if (typeof raw !== "object" || raw === null) return null;
  const source = raw as Record<string, unknown>;
  const nested = source["diagnostics"];
  if (typeof nested !== "object" || nested === null) return null;
  const bundle = nested as Record<string, unknown>;
  if (typeof bundle["engine_stderr"] !== "string") return null;
  return {
    platform: readPlatform(bundle["platform"]),
    engine_stderr: bundle["engine_stderr"],
    engine_stderr_cap_bytes: readNumber(bundle["engine_stderr_cap_bytes"]),
    crash_file: readCrashFile(bundle["crash_file"]),
    previous_run: readPreviousRun(bundle["previous_run"]),
  };
}

function readNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function readString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function readPlatform(value: unknown): DiagnosticsPlatform | null {
  if (typeof value !== "object" || value === null) return null;
  const platform = value as Record<string, unknown>;
  if (typeof platform["os"] !== "string") return null;
  return {
    os: platform["os"],
    arch: readString(platform["arch"]),
    family: readString(platform["family"]),
  };
}

function readCrashFile(value: unknown): EngineDiagnostics["crash_file"] {
  if (typeof value !== "object" || value === null) return null;
  const file = value as Record<string, unknown>;
  if (typeof file["path"] !== "string") return null;
  return {
    path: file["path"],
    bytes: readNumber(file["bytes"]),
    cap_bytes: readNumber(file["cap_bytes"]),
  };
}

function readPreviousRun(value: unknown): EngineDiagnostics["previous_run"] {
  if (typeof value !== "object" || value === null) return null;
  const file = value as Record<string, unknown>;
  if (typeof file["path"] !== "string") return null;
  return {
    path: file["path"],
    bytes: readNumber(file["bytes"]),
    tail: readString(file["tail"]),
  };
}

/** Everything the bundle is assembled from. Every field nullable — a report from a broken
 * app is the report that matters most, so no missing piece may prevent one. */
export interface BundleInput {
  appVersion: string | null;
  health: HealthInfo | null;
  stats: CorpusStats | null;
  /** The full index path. Reduced to a basename by {@link indexLabel} before use. */
  indexPath: string | null;
  diagnostics: EngineDiagnostics | null;
}

function platformLine(platform: DiagnosticsPlatform | null): string {
  if (platform === null) return "platform: (unknown)";
  const family = platform.family === "" ? "" : ` (${platform.family})`;
  return `platform: ${platform.os} ${platform.arch}${family}`.replace("  ", " ");
}

function engineLine(health: HealthInfo | null): string {
  if (health === null) return "engine: (not reached — no corpus attached, or the engine failed to start)";
  const corpus = health.corpus_ready ? "corpus attached" : "NO corpus attached";
  return `engine: ${health.engine_version} (IR ${health.ir_version}), ${corpus}`;
}

function statsLines(stats: CorpusStats | null): string[] {
  if (stats === null) return ["index: (no stats — nothing attached)"];
  const providers = Object.entries(stats.providers)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, count]) => `${name}=${count}`)
    .join(", ");
  return [
    `index: ${stats.conversations} conversations · ${stats.records} records · ` +
      `${stats.threads} threads · ${stats.edges} edges · ${stats.bytes} bytes`,
    `providers: ${providers === "" ? "(none)" : providers}`,
  ];
}

function fileLine(label: string, file: DiagnosticsFile | null, cap: number | null): string {
  if (file === null) return `${label}: (none)`;
  const ceiling = cap === null ? "" : ` of ${cap} max`;
  return `${label}: ${file.path} (${file.bytes} bytes${ceiling})`;
}

/**
 * The pasteable bundle.
 *
 * Plain text with no markup, because it goes into a GitHub issue body where a stray
 * backtick or angle bracket would eat part of it. Sections are separated by named `---`
 * rules so a maintainer can see at a glance whether a section is empty or missing.
 */
export function formatDiagnosticsBundle(input: BundleInput): string {
  const diagnostics = input.diagnostics;
  const lines: string[] = [
    BUNDLE_HEADER,
    BUNDLE_PRIVACY_NOTE,
    "",
    `app version: ${input.appVersion ?? "(unknown)"}`,
    platformLine(diagnostics?.platform ?? null),
    engineLine(input.health),
    `corpus index: ${indexLabel(input.indexPath)}`,
    ...statsLines(input.stats),
    fileLine("crash file", diagnostics?.crash_file ?? null, diagnostics?.crash_file?.cap_bytes ?? null),
    fileLine("previous run", diagnostics?.previous_run ?? null, null),
  ];

  const cap = diagnostics?.engine_stderr_cap_bytes ?? 0;
  const stderr = diagnostics?.engine_stderr ?? "";
  lines.push("", `--- engine stderr (${stderr.length} of ${cap} bytes retained) ---`);
  lines.push(stderr.trim() === "" ? EMPTY_STDERR_TEXT : stderr);

  const previous = diagnostics?.previous_run ?? null;
  if (previous !== null && previous.tail.trim() !== "") {
    lines.push("", "--- previous run (the app before this one, which is the one that crashed) ---");
    lines.push(previous.tail);
  }

  return lines.join("\n");
}

// ---------------------------------------------------------------------------
// controller
// ---------------------------------------------------------------------------

/** What the button shows after an attempt. */
export interface DiagnosticsView {
  busy: boolean;
  /** Status text beside the button. Empty before the first press. */
  message: string;
  /** The last bundle built, so a failed COPY does not lose the text. */
  bundle: string;
}

export const COPIED_MESSAGE = "Diagnostics copied — paste it into your bug report.";

/** Shown when the clipboard refuses. The bundle is kept so the UI can still show it. */
export const COPY_FAILED_PREFIX = "Could not reach the clipboard";

export type InvokeLike = (command: string) => Promise<unknown>;
export type ClipboardWrite = (text: string) => Promise<void>;

/** The live numbers the UI already holds. Read at CLICK time, never cached. */
export type EngineSnapshot = () => Pick<BundleInput, "health" | "stats" | "indexPath">;

export interface DiagnosticsDeps {
  invoke: InvokeLike;
  snapshot: EngineSnapshot;
  copy: ClipboardWrite;
}

/**
 * Build the bundle and put it on the clipboard.
 *
 * NOTHING here may throw. It runs from a click handler in an app the user is already
 * reporting as broken, so every failure — the command missing, the payload malformed, the
 * clipboard refused — has to become status text plus a bundle the user can still read.
 */
export class DiagnosticsController {
  private view: DiagnosticsView = { busy: false, message: "", bundle: "" };

  constructor(
    private readonly deps: DiagnosticsDeps,
    private readonly onView: (view: DiagnosticsView) => void,
  ) {}

  get current(): DiagnosticsView {
    return this.view;
  }

  async copyBundle(): Promise<DiagnosticsView> {
    if (this.view.busy) return this.view;
    this.emit({ busy: true, message: "Collecting…", bundle: this.view.bundle });

    let appVersion: string | null = null;
    let diagnostics: EngineDiagnostics | null = null;
    try {
      const info = await this.deps.invoke("app_info");
      diagnostics = readEngineDiagnostics(info);
      if (typeof info === "object" && info !== null) {
        const version = (info as Record<string, unknown>)["version"];
        if (typeof version === "string") appVersion = version;
      }
    } catch {
      // A shell that cannot answer app_info is itself the report. Fall through with nulls
      // rather than abandon the bundle — the engine numbers below may still be present.
      diagnostics = null;
    }

    const live = this.deps.snapshot();
    const bundle = formatDiagnosticsBundle({
      appVersion,
      health: live.health,
      stats: live.stats,
      indexPath: live.indexPath,
      diagnostics,
    });

    try {
      await this.deps.copy(bundle);
      return this.emit({ busy: false, message: COPIED_MESSAGE, bundle });
    } catch (err) {
      const why = err instanceof Error ? err.message : String(err);
      return this.emit({
        busy: false,
        message: `${COPY_FAILED_PREFIX}: ${why}`,
        bundle,
      });
    }
  }

  private emit(view: DiagnosticsView): DiagnosticsView {
    this.view = view;
    this.onView(view);
    return view;
  }
}

/**
 * The default clipboard writer.
 *
 * Rejects with a readable reason where `navigator.clipboard` is absent — a browser preview
 * over plain HTTP, or the node test runner — instead of throwing `TypeError: Cannot read
 * properties of undefined`, which tells a reporter nothing.
 */
export async function systemClipboardWrite(text: string): Promise<void> {
  const clipboard = globalThis.navigator?.clipboard;
  if (clipboard === undefined) {
    throw new Error("this environment has no clipboard API");
  }
  await clipboard.writeText(text);
}

// ---------------------------------------------------------------------------
// DOM shell
// ---------------------------------------------------------------------------

/** The button's accessible label. */
export const BUTTON_LABEL = "Copy diagnostics";

/**
 * The button plus its status line. Text is written with `textContent`, never `innerHTML`, so
 * an engine error string in the bundle can never inject markup.
 *
 * WIRING, stated plainly because it is NOT done yet: nothing in the app constructs this. The
 * shell that owns the topbar is `src/app.ts` and the container is `index.html`, both outside
 * this work unit's declared file scope, so the one line that makes the button visible —
 * `mountDiagnosticsButton(requireEl("topbar"), { invoke, snapshot, copy })` — has to be added
 * by whoever owns those files. Until it is, the ON-DISK half of DECISION G-8 still works
 * with no UI at all: the Rust parent writes `<logs_dir>/engine-stderr.log` unconditionally,
 * and `.github/ISSUE_TEMPLATE/bug_report.yml` tells a reporter where to find it.
 */
export class DiagnosticsButton {
  private readonly controller: DiagnosticsController;
  private readonly onClick: () => void;

  constructor(
    private readonly button: HTMLButtonElement,
    private readonly status: HTMLElement,
    deps: DiagnosticsDeps,
  ) {
    this.controller = new DiagnosticsController(deps, (view) => this.paint(view));
    this.onClick = () => void this.controller.copyBundle();
    this.button.addEventListener("click", this.onClick);
    this.paint(this.controller.current);
  }

  private paint(view: DiagnosticsView): void {
    this.button.disabled = view.busy;
    this.status.textContent = view.message;
  }

  /** Drop the click listener. The elements belong to the shell, not to this class. */
  destroy(): void {
    this.button.removeEventListener("click", this.onClick);
  }
}

/**
 * Create the button and its status line inside `host`, and return the controller wrapper.
 *
 * The status line is `role="status"` (a polite live region) so pressing the button is
 * ANNOUNCED rather than silently painted — the same treatment `index.html` gives the corpus
 * bar's error line, and the reason it matters more here is that the whole interaction is one
 * invisible clipboard write.
 */
export function mountDiagnosticsButton(host: HTMLElement, deps: DiagnosticsDeps): DiagnosticsButton {
  const button = host.ownerDocument.createElement("button");
  button.type = "button";
  button.id = "btn-diagnostics";
  button.textContent = BUTTON_LABEL;

  const status = host.ownerDocument.createElement("span");
  status.id = "diagnostics-status";
  status.setAttribute("role", "status");

  host.append(button, status);
  return new DiagnosticsButton(button, status, deps);
}
