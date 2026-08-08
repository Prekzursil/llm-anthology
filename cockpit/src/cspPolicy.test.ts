/**
 * The webview's Content Security Policy, pinned.
 *
 * The cockpit renders untrusted text — conversation titles, previews and whole transcripts
 * out of arbitrary third-party session exports. Every writer currently uses `textContent`
 * (there is no `innerHTML`/`eval` sink in `src/` today, only comments saying so), so this
 * policy is not patching a live hole; it is the blast wall for the day one of those writers
 * regresses or a dependency ships something hostile.
 *
 * WHY THE POLICY LIVES IN `tauri.conf.json` AND NOT IN A `<meta>` TAG:
 *
 *  - Tauri PROCESSES the config value. `set_csp` (tauri 2.11.5 `src/manager/mod.rs:53`)
 *    swaps `__TAURI_SCRIPT_NONCE__`/`__TAURI_STYLE_NONCE__` tokens in the served HTML for
 *    real nonces and appends the matching `'nonce-…'` sources to `script-src`/`style-src`.
 *    A hand-written meta tag cannot participate in that.
 *  - Worse, it would FIGHT it. Multiple policies are enforced as an intersection, so a meta
 *    tag saying `script-src 'self'` would veto the nonce Tauri just granted itself.
 *  - `frame-ancestors` (and `sandbox`, `report-uri`) are ignored in a meta tag outright.
 *  - The config CSP is delivered as a real `Content-Security-Policy` response header on the
 *    custom protocol (`src/protocol/tauri.rs:183`), which is where those directives work.
 *
 * That is also why a meta tag must not be ADDED later, which the last case here guards.
 *
 * SCOPE, stated inline: everything here is STATIC. It reads a string out of a JSON file. No
 * assertion in this file has ever loaded the app, so nothing here can tell you the policy
 * works.
 *
 * The runtime half is `tools/probe_csp.mjs`, which serves the built `dist/` behind the header
 * read from `tauri.conf.json` (never retyped) and drives boot plus search -> Read -> Escape
 * in real Chromium, failing on any CSP violation. Run `node tools/probe_csp.mjs`; run it with
 * `--self-test` to confirm its violation listener still FIRES under a deliberately broken
 * policy, because one that is silent in both states measures nothing.
 *
 * Still UNVERIFIED even with that probe, because a localhost static server is not Tauri's
 * custom protocol:
 *   * `'self'` there is `http://127.0.0.1:<port>`, not `http://tauri.localhost`.
 *   * There is no Tauri runtime, so no real IPC call is ever made. `connect-src ipc:
 *     http://ipc.localhost` is only shown to be PERMITTED, never shown to carry an RPC.
 *   * Tauri's nonce/hash injection does not run.
 * The experiment that would settle all three: `npm run tauri build`, launch the installed
 * app, and confirm the sidecar answers with the CSP header live.
 */
import { describe, expect, it } from "vitest";

// Read through Vite rather than `node:fs`: the cockpit tsconfig is browser-only and has no
// `@types/node`. Same reasoning as `graph/palette.test.ts`. Parsing the raw bytes (rather
// than importing the JSON as a module) also proves the file on disk is still valid JSON.
import TAURI_CONF_RAW from "../src-tauri/tauri.conf.json?raw";
import INDEX_HTML from "../index.html?raw";

/** Directive name -> its source list, e.g. `{"script-src": ["'self'"]}`. */
function parseCsp(policy: string): Map<string, string[]> {
  const directives = new Map<string, string[]>();
  for (const chunk of policy.split(";")) {
    const [name, ...sources] = chunk.trim().split(/\s+/).filter(Boolean);
    if (name) directives.set(name.toLowerCase(), sources);
  }
  return directives;
}

function csp(): Map<string, string[]> {
  const conf = JSON.parse(TAURI_CONF_RAW) as {
    app: { security: { csp: string | null } };
  };
  const policy = conf.app.security.csp;
  // A null CSP is the Tauri scaffold default and means "no policy at all".
  expect(typeof policy).toBe("string");
  return parseCsp(policy as string);
}

describe("webview content security policy", () => {
  it("is set at all", () => {
    expect(csp().size).toBeGreaterThan(0);
  });

  it("defaults to same-origin only", () => {
    expect(csp().get("default-src")).toEqual(["'self'"]);
  });

  it("permits the Tauri IPC endpoint on every platform", () => {
    // IPC is a `fetch()`, so it is governed by connect-src (tauri 2.11.5
    // `scripts/ipc-protocol.js:38`). The target URL is built by `convertFileSrc`
    // (`scripts/core.js:13-20`): `ipc://localhost/…` on macOS/Linux, and
    // `http://ipc.localhost/…` on Windows/Android. Both forms must be allowed or the
    // custom-protocol transport is refused and Tauri silently degrades to the
    // `window.ipc.postMessage` fallback.
    const connect = csp().get("connect-src") ?? [];
    expect(connect).toContain("ipc:");
    expect(connect).toContain("http://ipc.localhost");
  });

  it("permits the same-origin ELK web worker", () => {
    // `graph/elkLayout.ts` does `new Worker(url, { type: "classic" })` against a
    // Vite-emitted asset URL. Without worker-src the graph layout dies at runtime, which
    // neither tsc nor vitest can see.
    expect(csp().get("worker-src")).toEqual(["'self'"]);
  });

  it("keeps script and style same-origin with no inline escape hatch", () => {
    // The production build emits ONE external module script and ONE external stylesheet,
    // and there is no `<style>`, `style=` attribute or `cssText` write anywhere, so
    // neither directive needs relaxing. Tauri appends its own nonce here when it needs one.
    expect(csp().get("script-src")).toEqual(["'self'"]);
    expect(csp().get("style-src")).toEqual(["'self'"]);
  });

  it.each(["'unsafe-inline'", "'unsafe-eval'", "'wasm-unsafe-eval'", "*", "data:", "blob:"])(
    "never grants %s in any directive",
    (token) => {
      // Measured on the built bundle: zero `eval(`, zero `new Function`, zero
      // `WebAssembly`, zero `data:` URIs. Nothing here has earned an exception, so any
      // future appearance of one of these should have to argue for itself in review.
      for (const sources of csp().values()) {
        expect(sources).not.toContain(token);
      }
    },
  );

  it.each([
    ["object-src", "'none'"],
    ["base-uri", "'self'"],
    ["frame-ancestors", "'none'"],
    ["form-action", "'none'"],
  ])("hardens %s to %s", (directive, expected) => {
    expect(csp().get(directive)).toEqual([expected]);
  });

  it("has no competing meta-tag policy in index.html", () => {
    // A second policy would be intersected with the header and would neutralise the
    // nonce Tauri injects for its own bootstrap. See the file header.
    expect(INDEX_HTML).not.toMatch(/http-equiv\s*=\s*["']?Content-Security-Policy/i);
  });
});
