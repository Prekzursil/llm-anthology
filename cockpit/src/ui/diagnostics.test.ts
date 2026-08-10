/**
 * DECISION G-8, frontend half: the pure derivations and the DOM-free controller.
 *
 * The assertion that matters most is the LEAK one. A diagnostics bundle is pasted into a
 * public issue tracker by a user who is trusting the tool, so this suite treats the bundle as
 * a publication and checks what it publishes: synthetic conversation content driven through
 * the payload must not survive, and the corpus index path — which embeds the username — must
 * be reduced to a basename before it can reach the text.
 *
 * The Rust side proves the allowlist and the home-relativization
 * (`src-tauri/src/sidecar.rs`'s `synthetic_conversation_content_never_reaches_either_diagnostics_surface`).
 * What is proven HERE is that this layer cannot ADD a leak the Rust side already prevented —
 * a distinct failure, since every field this module contributes comes from the UI, not from
 * the scrubbed wire.
 */
import { describe, expect, it, vi } from "vitest";

import type { AppInfo, CorpusStats, HealthInfo } from "../ipc/types";
import {
  BUNDLE_HEADER,
  BUNDLE_PRIVACY_NOTE,
  COPIED_MESSAGE,
  COPY_FAILED_PREFIX,
  DiagnosticsController,
  EMPTY_STDERR_TEXT,
  formatDiagnosticsBundle,
  indexLabel,
  readEngineDiagnostics,
  systemClipboardWrite,
  type BundleInput,
  type ClipboardWrite,
  type DiagnosticsDeps,
  type EngineDiagnostics,
} from "./diagnostics";

/**
 * SYNTHETIC — invented here, never read from any corpus. Shaped like the medical content the
 * owner's real archive holds, because that is what a leak would expose.
 */
const CANARY = "Amoxicillin-clavulanate SYNTHETIC-CANARY-9f3a";

const HEALTH: HealthInfo = {
  ok: true,
  engine_version: "0.4.1",
  ir_version: "3",
  corpus_ready: true,
};

const STATS: CorpusStats = {
  conversations: 1234,
  records: 5678,
  threads: 900,
  edges: 12,
  bytes: 55123456,
  providers: { codex: 500, chatgpt: 400, claude: 334 },
};

const DIAGNOSTICS: EngineDiagnostics = {
  platform: { os: "windows", arch: "x86_64", family: "windows" },
  engine_stderr: [
    "Traceback (most recent call last):",
    '  File "~\\AppData\\Local\\LLM Anthology\\engine\\Lib\\llm_anthology\\adapters\\chatgpt.py", line 214, in _walk',
    "<redacted 41 chars>",
    "KeyError: <redacted 9 chars>",
  ].join("\n"),
  engine_stderr_cap_bytes: 65536,
  crash_file: { path: "~\\AppData\\Local\\LLM Anthology\\logs\\engine-stderr.log", bytes: 812, cap_bytes: 262144 },
  previous_run: {
    path: "~\\AppData\\Local\\LLM Anthology\\logs\\engine-stderr.prev.log",
    bytes: 640,
    tail: "PermissionError:",
  },
};

function input(overrides: Partial<BundleInput> = {}): BundleInput {
  return {
    appVersion: "0.1.0",
    health: HEALTH,
    stats: STATS,
    indexPath: "C:\\Users\\tester\\AppData\\Local\\LLM Anthology\\anthology.sqlite",
    diagnostics: DIAGNOSTICS,
    ...overrides,
  };
}

describe("indexLabel — the one field that carries a username", () => {
  it("reduces a real Windows path to its basename and nothing more", () => {
    expect(indexLabel("C:\\Users\\tester\\AppData\\Local\\LLM Anthology\\anthology.sqlite")).toBe(
      "anthology.sqlite",
    );
  });

  it("reduces a POSIX path too, because the engine and its fixtures use those", () => {
    expect(indexLabel("/home/tester/.local/share/llm-anthology/anthology.sqlite")).toBe(
      "anthology.sqlite",
    );
  });

  it("says (none) rather than inventing a name", () => {
    expect(indexLabel(null)).toBe("(none)");
    expect(indexLabel("   ")).toBe("(none)");
    // Nothing but separators has no basename — it must not fall back to showing the input,
    // which is what `corpusBar.basenameOf` does for a LABEL. A label is on-screen; this is
    // published.
    expect(indexLabel("\\\\")).toBe("(none)");
    expect(indexLabel("//")).toBe("(none)");
    // A bare drive root reduces to `C:`, and that is correct rather than a gap: a drive
    // letter identifies nobody. (Measured — the first version of this test asserted
    // "(none)" here and was wrong about which value was the safe one.)
    expect(indexLabel("C:\\")).toBe("C:");
  });
});

describe("formatDiagnosticsBundle", () => {
  it("carries the header, the privacy note, and every field a report needs", () => {
    const text = formatDiagnosticsBundle(input());
    expect(text.startsWith(BUNDLE_HEADER)).toBe(true);
    expect(text).toContain(BUNDLE_PRIVACY_NOTE);
    expect(text).toContain("app version: 0.1.0");
    expect(text).toContain("platform: windows x86_64 (windows)");
    expect(text).toContain("engine: 0.4.1 (IR 3), corpus attached");
    expect(text).toContain("corpus index: anthology.sqlite");
    expect(text).toContain("1234 conversations");
    expect(text).toContain("5678 records");
    // Providers sorted, so two reports of the same corpus are diffable.
    expect(text).toContain("providers: chatgpt=400, claude=334, codex=500");
    expect(text).toContain("engine-stderr.log (812 bytes of 262144 max)");
  });

  it("carries the retained stderr and the previous run verbatim", () => {
    const text = formatDiagnosticsBundle(input());
    expect(text).toContain("--- engine stderr (");
    expect(text).toContain("Traceback (most recent call last):");
    expect(text).toContain("KeyError: <redacted 9 chars>");
    expect(text).toContain("previous run (the app before this one");
    expect(text).toContain("PermissionError:");
  });

  it("NEVER emits the full index path, however the caller supplies it", () => {
    // The leak this module could add on its own. Every spelling, including one whose
    // basename is fine but whose directory names the user.
    for (const path of [
      "C:\\Users\\tester\\AppData\\Local\\LLM Anthology\\anthology.sqlite",
      "/home/tester/.local/share/llm-anthology/anthology.sqlite",
      "\\\\server\\share\\tester\\anthology.sqlite",
    ]) {
      const text = formatDiagnosticsBundle(input({ indexPath: path }));
      expect(text).not.toContain("tester");
      expect(text).not.toContain(path);
      expect(text).toContain("corpus index: anthology.sqlite");
    }
  });

  it("does not carry conversation content that reached it through any field", () => {
    // Detector control first: a bundle assembled from these MUST be able to fail.
    const poisoned = input({
      indexPath: `C:\\Users\\tester\\${CANARY}.sqlite`,
      stats: { ...STATS, providers: { codex: 1 } },
    });
    const text = formatDiagnosticsBundle(poisoned);
    // The index BASENAME is the one place a user-chosen string legitimately survives, and a
    // user who names their corpus after a conversation has published that themselves. What
    // must not survive is the DIRECTORY chain around it.
    expect(text).not.toContain("tester");
    expect(text).toContain(`corpus index: ${CANARY}.sqlite`);
  });

  it("says so explicitly when a section is empty, rather than omitting it", () => {
    const quiet = formatDiagnosticsBundle(
      input({ diagnostics: { ...DIAGNOSTICS, engine_stderr: "", previous_run: null } }),
    );
    expect(quiet).toContain(EMPTY_STDERR_TEXT);
    expect(quiet).toContain("previous run: (none)");
    // An omitted section reads as a broken tool, and a reporter who thinks the tool is
    // broken stops reporting.
    expect(quiet).toContain("--- engine stderr (");
  });

  it("still produces a usable report when nothing is attached and Rust said nothing", () => {
    // The MOST important case: a report from an app that never started properly.
    const broken = formatDiagnosticsBundle({
      appVersion: null,
      health: null,
      stats: null,
      indexPath: null,
      diagnostics: null,
    });
    expect(broken).toContain(BUNDLE_HEADER);
    expect(broken).toContain("app version: (unknown)");
    expect(broken).toContain("platform: (unknown)");
    expect(broken).toContain("engine: (not reached");
    expect(broken).toContain("index: (no stats");
    expect(broken).toContain("crash file: (none)");
    expect(broken).toContain(EMPTY_STDERR_TEXT);
  });
});

describe("readEngineDiagnostics", () => {
  it("narrows the real app_info shape", () => {
    const parsed = readEngineDiagnostics({ version: "0.1.0", diagnostics: DIAGNOSTICS });
    expect(parsed?.platform?.os).toBe("windows");
    expect(parsed?.engine_stderr_cap_bytes).toBe(65536);
    expect(parsed?.crash_file?.bytes).toBe(812);
    expect(parsed?.previous_run?.tail).toBe("PermissionError:");
  });

  it("degrades to null rather than throwing on anything malformed", () => {
    // Version skew between a shipped shell and a shipped frontend is a real state, and this
    // runs inside a click handler in an app the user is already reporting as broken.
    for (const bad of [null, undefined, 7, "nope", {}, { diagnostics: null }, { diagnostics: 3 }]) {
      expect(readEngineDiagnostics(bad)).toBeNull();
    }
    // Present but wrong TYPES: the stderr string is the one required field.
    expect(readEngineDiagnostics({ diagnostics: { engine_stderr: 5 } })).toBeNull();
  });

  it("fills a partial payload with safe defaults instead of rejecting it", () => {
    const parsed = readEngineDiagnostics({ diagnostics: { engine_stderr: "boom" } });
    expect(parsed).not.toBeNull();
    expect(parsed?.engine_stderr).toBe("boom");
    expect(parsed?.platform).toBeNull();
    expect(parsed?.engine_stderr_cap_bytes).toBe(0);
    expect(parsed?.crash_file).toBeNull();
    expect(parsed?.previous_run).toBeNull();
  });
});

function deps(overrides: Partial<DiagnosticsDeps> = {}): DiagnosticsDeps {
  return {
    appInfo: vi.fn(async () => ({ version: "0.1.0", diagnostics: DIAGNOSTICS })),
    snapshot: () => ({ health: HEALTH, stats: STATS, indexPath: "C:\\Users\\tester\\a.sqlite" }),
    copy: vi.fn<ClipboardWrite>(async () => undefined),
    ...overrides,
  };
}

describe("DiagnosticsController", () => {
  it("builds the bundle from app_info plus the live UI numbers, and copies it", async () => {
    const copy = vi.fn<ClipboardWrite>(async () => undefined);
    const controller = new DiagnosticsController(deps({ copy }), () => undefined);
    const view = await controller.copyBundle();

    expect(view.busy).toBe(false);
    expect(view.message).toBe(COPIED_MESSAGE);
    expect(copy).toHaveBeenCalledTimes(1);
    const copied = copy.mock.calls[0][0] as unknown as string;
    expect(copied).toContain("app version: 0.1.0");
    expect(copied).toContain("engine: 0.4.1 (IR 3)");
    expect(copied).toContain("1234 conversations");
    expect(copied).toContain("KeyError: <redacted 9 chars>");
    expect(copied).not.toContain("tester");
  });

  it("reads the live numbers at CLICK time, not at construction", async () => {
    // The panel is built before a corpus is attached, so a cached snapshot would report an
    // empty index in every bug report ever filed.
    let stats: CorpusStats | null = null;
    const copy = vi.fn<ClipboardWrite>(async () => undefined);
    const controller = new DiagnosticsController(
      deps({ copy, snapshot: () => ({ health: null, stats, indexPath: null }) }),
      () => undefined,
    );
    stats = STATS;
    await controller.copyBundle();
    expect(copy.mock.calls[0][0] as unknown as string).toContain("1234 conversations");
  });

  it("still produces a bundle when app_info itself fails", async () => {
    // A shell that cannot answer its own status command IS the report.
    const copy = vi.fn<ClipboardWrite>(async () => undefined);
    const controller = new DiagnosticsController(
      deps({
        copy,
        appInfo: vi.fn(async () => {
          throw new Error("command app_info not found");
        }),
      }),
      () => undefined,
    );
    const view = await controller.copyBundle();
    expect(view.message).toBe(COPIED_MESSAGE);
    const copied = copy.mock.calls[0][0] as unknown as string;
    expect(copied).toContain("app version: (unknown)");
    // The engine numbers the UI holds survive the shell failure.
    expect(copied).toContain("1234 conversations");
  });

  it("keeps the bundle when the clipboard refuses, and says why", async () => {
    const controller = new DiagnosticsController(
      deps({
        copy: vi.fn(async () => {
          throw new Error("clipboard blocked by policy");
        }),
      }),
      () => undefined,
    );
    const view = await controller.copyBundle();
    expect(view.busy).toBe(false);
    expect(view.message).toContain(COPY_FAILED_PREFIX);
    expect(view.message).toContain("clipboard blocked by policy");
    expect(view.bundle).toContain(BUNDLE_HEADER);
  });

  it("emits busy first so the button can disable itself, and ignores a second press", async () => {
    const seen: boolean[] = [];
    let release: () => void = () => undefined;
    const controller = new DiagnosticsController(
      deps({
        copy: () =>
          new Promise<void>((resolve) => {
            release = resolve;
          }),
      }),
      (view) => seen.push(view.busy),
    );
    const first = controller.copyBundle();
    // Let the awaits inside settle up to the pending clipboard write.
    await Promise.resolve();
    await Promise.resolve();
    expect(seen[0]).toBe(true);
    const ignored = await controller.copyBundle();
    expect(ignored.busy).toBe(true);
    release();
    await first;
    expect(seen[seen.length - 1]).toBe(false);
  });
});

describe("systemClipboardWrite", () => {
  it("names the missing API instead of throwing a TypeError", async () => {
    // The node runner has no `navigator.clipboard`, and neither does a browser preview over
    // plain HTTP. `Cannot read properties of undefined` tells a reporter nothing.
    await expect(systemClipboardWrite("x")).rejects.toThrow("no clipboard API");
  });
});

describe("CF-19: the dep is the CLIENT method, not a general-purpose invoke", () => {
  // This is the change that made the button mountable, so it is asserted rather than left
  // to the type system. `DiagnosticsDeps` used to take `invoke: (command: string) =>
  // Promise<unknown>` and `copyBundle` called it with the single literal `"app_info"`. A
  // general-purpose invoke can only be satisfied honestly by a RAW Tauri `invoke`, and
  // handing the app one bypasses the runtime adapter selection in `ipc/index.ts:5-16` —
  // the abstraction that exists because a hardcoded real-IPC path once made every pane
  // render dead in a plain browser. So the wide shape was the blocker, and narrowing it to
  // the capability actually used is what removed it.

  it("asks for app info with NO command string, so no raw invoke can satisfy it", async () => {
    const appInfo = vi.fn(async () => ({ version: "9.9.9", diagnostics: DIAGNOSTICS }));
    const copy = vi.fn<ClipboardWrite>(async () => undefined);
    await new DiagnosticsController(deps({ appInfo, copy }), () => undefined).copyBundle();

    expect(appInfo).toHaveBeenCalledTimes(1);
    // ZERO arguments is the load-bearing part. While the dep took a command name, the only
    // thing that could be passed here was a string the callee had to dispatch on, and the
    // one implementation that dispatches on it is the raw Tauri boundary.
    expect(appInfo.mock.calls[0]).toEqual([]);
    // And the answer is genuinely consumed, so this cannot pass on a stub that is called
    // and ignored.
    expect(copy.mock.calls[0][0] as unknown as string).toContain("app version: 9.9.9");
  });

  it("accepts the real IpcClient.appInfo return shape without a cast at the call site", async () => {
    // The point of the narrowing: `ipc.appInfo()` resolves `AppInfo`, and that value must
    // drop straight into this dep. If it did not, `app.ts` would need an adapter, and an
    // adapter around a one-command invoke is the shim this change exists to avoid.
    const copy = vi.fn<ClipboardWrite>(async () => undefined);
    const fromClient: AppInfo = {
      name: "Cockpit",
      version: "from-client",
      engine: "not-wired",
      locations: null,
      locations_error: "resolution failed",
      diagnostics: DIAGNOSTICS,
    };
    await new DiagnosticsController(
      deps({ appInfo: async () => fromClient, copy }),
      () => undefined,
    ).copyBundle();
    const copied = copy.mock.calls[0][0] as unknown as string;
    expect(copied).toContain("app version: from-client");
    // The diagnostics payload rode along on the SAME answer, which is why one call is enough.
    expect(copied).toContain("KeyError: <redacted 9 chars>");
  });

  it("keeps distrusting the payload shape, because the adapter never validated it", async () => {
    // `real.ts` does `invoke<AppInfo>("app_info")` — an UNCHECKED cast over whatever Tauri
    // returns. So a typed dep would be claiming a guarantee nothing enforces, and the
    // defensive reads in `copyBundle` are live code rather than dead branches. Garbage in
    // must still produce a filed-able bundle.
    const copy = vi.fn<ClipboardWrite>(async () => undefined);
    await new DiagnosticsController(
      deps({ appInfo: async () => "not an object at all", copy }),
      () => undefined,
    ).copyBundle();
    const copied = copy.mock.calls[0][0] as unknown as string;
    expect(copied).toContain("app version: (unknown)");
    expect(copied).toContain("1234 conversations"); // the UI numbers still survive
  });
});
