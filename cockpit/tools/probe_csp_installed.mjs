/**
 * Does the app work under the shipped CSP in a REAL Tauri build, at the REAL origin?
 *
 * `probe_csp_installed.mjs` closes the residual that `probe_csp.mjs` and `src/cspPolicy.test.ts`
 * both name and cannot reach. Those two verify the policy against a localhost static server,
 * where `'self'` is `http://127.0.0.1:<port>`, there is no Tauri runtime, and no real IPC call
 * is ever made. The directive most likely to be wrong -- `connect-src ipc: http://ipc.localhost`
 * -- is exactly the one a static server cannot exercise.
 *
 * THE CONFOUND THIS PROBE IS BUILT AROUND. "IPC works" does NOT prove `connect-src` is right.
 * Tauri's transport tries `fetch()` to the ipc origin FIRST, and on failure -- explicitly
 * including a CSP refusal -- falls back to `window.ipc.postMessage` and keeps working
 * (tauri-2.11.5 scripts/ipc-protocol.js:63-70). So a wrong `connect-src` degrades SILENTLY.
 * Distinguishing the two paths needs the console warning and the violation list, which is the
 * whole reason this probe attaches a debugger instead of just clicking the UI like
 * `verify_installed.ps1` / `drive_installed.ps1` do.
 *
 * HOW IT ATTACHES. WebView2 exposes no debug port by default and
 * WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS is ignored (wry-0.55.1 src/webview2/mod.rs:327 sets
 * the option unconditionally, and WebView2 honours the env var only when it is unset). So the
 * build is instrumented through `tools/csp_installed_debug.conf.json5`, which adds ONLY
 * `--remote-debugging-port` on top of wry's own default browser args.
 *
 * RUN IT (from `cockpit/`). `npm run build` is skipped by the patch, so build the frontend
 * yourself: tauri.conf.json's beforeBuildCommand is `tsc && vite build`, and a type error in
 * ANY file -- including one this probe has nothing to do with -- would otherwise block the
 * Rust build and make the CSP unobservable.
 *
 *   npx vite build
 *   export CARGO_TARGET_DIR=<scratch>          # optional; leaves a peer's build cache alone
 *   npx tauri build --no-bundle --config tools/csp_installed_debug.conf.json5
 *   node tools/probe_csp_installed.mjs --exe "<scratch>/release/LLM Anthology.exe"
 *
 * DETECTOR CONTROL -- run this too, or a "no violations" reading cannot be distinguished from
 * a probe that is not looking. Build the same thing with the policy nulled and assert the
 * violation results INVERT (inline script RUNS, injected style APPLIES, zero violations):
 *
 *   npx tauri build --no-bundle --config tools/csp_installed_debug.conf.json5 \
 *                                --config '{"app":{"security":{"csp":null}}}'
 *   node tools/probe_csp_installed.mjs --exe "<same path>" --expect-no-csp
 *
 * Both variants land on the SAME exe path, so run each probe right after its own build. The
 * exe must also stay NEXT TO the `engine/` and `resources/` dirs the build puts beside it --
 * Tauri resolves bundled resources relative to the executable -- so do not copy it out.
 *
 * DISCLOSED DELTAS FROM THE SHIPPED ARTIFACT: this binary carries an open debug port, and the
 * frontend is whatever `vite build` last emitted (no typecheck). Everything else -- including
 * `app.security.csp` -- comes from `tauri.conf.json`, and the probe greps the exe to confirm
 * the policy string is really baked in rather than trusting the merge.
 */
import { spawn, execFileSync } from "node:child_process";
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL, fileURLToPath } from "node:url";

const COCKPIT = dirname(dirname(fileURLToPath(import.meta.url)));
const EXE = join(COCKPIT, "src-tauri", "target", "release", "LLM Anthology.exe");
const CONF = join(COCKPIT, "src-tauri", "tauri.conf.json");
const PORT = 9222;

const SKILL = join(
  process.env.USERPROFILE ?? process.env.HOME ?? "",
  ".claude", "skills", "ui-audit", "package.json",
);
const { chromium } = createRequire(pathToFileURL(SKILL))("playwright");

const checks = [];
const check = (name, ok, detail = "") => {
  checks.push({ name, ok });
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${name}${detail ? `  -- ${detail}` : ""}`);
};

const shippedCsp = () =>
  JSON.parse(readFileSync(CONF, "utf8")).app?.security?.csp ?? null;

/** Kill any stray instance so a previous run cannot be mistaken for this one. */
function killApp() {
  try {
    execFileSync("taskkill", ["/F", "/IM", "LLM Anthology.exe"], { stdio: "ignore" });
  } catch {
    /* not running */
  }
}

async function waitForCdp(timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (res.ok) return await res.json();
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  return null;
}

/**
 * Launch the app, attach, and observe. Returns raw observations; assertions live in the
 * callers so the same observation set can be judged against a CSP or a no-CSP build.
 */
async function observe(exePath, elkAsset) {
  killApp();
  await new Promise((r) => setTimeout(r, 600));

  const child = spawn(exePath, [], { detached: false, stdio: ["ignore", "pipe", "pipe"] });
  let stderr = "";
  child.stderr.on("data", (d) => (stderr += d));

  const version = await waitForCdp();
  if (!version) {
    killApp();
    throw new Error(
      `no CDP endpoint on :${PORT} after 30s -- was the exe built with ` +
        `--config tools/csp_installed_debug.conf.json5? stderr: ${stderr.slice(0, 300)}`,
    );
  }

  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${PORT}`);
  const context = browser.contexts()[0];
  // The webview page already exists by the time we attach, so addInitScript is too late for
  // boot-time violations. Those are recovered from the CDP Log domain instead, which is
  // retroactive over entries the renderer already emitted.
  const page = context.pages()[0] ?? (await context.waitForEvent("page"));

  const consoleMsgs = [];
  const cdpLog = [];
  page.on("console", (m) => consoleMsgs.push(`${m.type()}: ${m.text()}`));
  page.on("pageerror", (e) => consoleMsgs.push(`pageerror: ${String(e)}`));

  const cdp = await context.newCDPSession(page);
  await cdp.send("Log.enable");
  cdp.on("Log.entryAdded", ({ entry }) => cdpLog.push(`${entry.source}: ${entry.text}`));
  await cdp.send("Runtime.enable");

  // Let boot, the engine handshake and the discovery scan finish.
  await page.waitForTimeout(9000);

  const observations = await page.evaluate(async (elkAsset) => {
    const r = { violations: [] };
    // Kept STRUCTURED, not pre-joined into a string. A violation message quotes the entire
    // effective directive, so `connect-src 'self' ipc: http://ipc.localhost` appears verbatim
    // inside the report for a DIFFERENT blocked URI. Substring-matching the formatted text for
    // "ipc.localhost" therefore reports the ipc origin as refused when it was in fact the
    // allow-list being echoed back. Only `blockedURI` names what was actually refused.
    document.addEventListener("securitypolicyviolation", (e) =>
      r.violations.push({
        directive: e.effectiveDirective || e.violatedDirective,
        blocked: e.blockedURI,
      }),
    );

    r.origin = location.origin;
    r.href = location.href;
    r.hasTauriInternals = typeof window.__TAURI_INTERNALS__ !== "undefined";
    r.hasGlobalTauri = typeof window.__TAURI__ !== "undefined";
    r.appRendered = (document.querySelector("#app")?.childElementCount ?? 0) > 0;
    r.rootRowCount = document.querySelectorAll(".root-row").length;
    // Nonce injection: Tauri rewrites __TAURI_SCRIPT_NONCE__/__TAURI_STYLE_NONCE__ tokens in
    // the served HTML. Report what actually reached the DOM rather than assuming.
    r.nonced = [...document.querySelectorAll("[nonce]")].map(
      (el) => `${el.tagName.toLowerCase()}[nonce=${el.nonce ? "set" : "empty"}]`,
    );
    r.unreplacedNonceToken = document.documentElement.innerHTML.includes("__TAURI_");
    r.metaCsp =
      document.querySelector('meta[http-equiv="Content-Security-Policy" i]')?.content ?? null;

    // Q2 -- a REAL IPC round trip.
    //
    // `app_info` is the probe target because it needs NO attached corpus. `health_ping` was
    // tried first and rejected with "no corpus attached: call open_corpus first" -- which is
    // an APPLICATION error authored by the Rust side, so it actually proves the round trip
    // completed. Asserting on "resolved" alone would have scored that as an IPC failure and
    // reported the opposite of the truth. Both are recorded: a transport failure looks
    // completely different (a CSP violation naming ipc.localhost, or the postMessage-fallback
    // warning), and neither is an app-authored message.
    const invoke = window.__TAURI__?.core?.invoke ?? window.__TAURI_INTERNALS__?.invoke;
    if (typeof invoke !== "function") {
      r.ipc = "no invoke function on window";
      r.ipcCorpusFree = "not attempted";
    } else {
      try {
        const out = await invoke("app_info", {});
        r.ipcCorpusFree = `resolved: ${JSON.stringify(out).slice(0, 200)}`;
      } catch (e) {
        r.ipcCorpusFree = `rejected: ${String(e).slice(0, 200)}`;
      }
      try {
        const out = await invoke("health_ping", { params: {} });
        r.ipc = `resolved: ${JSON.stringify(out).slice(0, 200)}`;
      } catch (e) {
        r.ipc = `rejected: ${String(e).slice(0, 200)}`;
      }
    }

    // Q3 -- the ELK worker at the REAL origin. `'self'` resolves to tauri.localhost here, not
    // to a localhost port, so this is the part probe_csp.mjs structurally cannot answer. The
    // asset name is passed in from disk so a rebuilt content hash cannot stale it.
    if (!elkAsset) {
      r.worker = "skipped: elk asset name unknown";
    } else {
      try {
        const w = new Worker(`/assets/${elkAsset}`, { type: "classic" });
        w.terminate();
        r.worker = "constructed";
      } catch (e) {
        r.worker = `threw: ${e.name}`;
      }
    }
    r.scriptSrcs = [...document.querySelectorAll("script")]
      .map((s) => s.src)
      .join(" ")
      .slice(0, 300);

    // Enforcement, at the real origin: an inline script MUST be refused, and a foreign
    // connect MUST be refused. If neither fires, the CSP is not active here and every
    // "no violations" reading above is vacuous.
    const s = document.createElement("script");
    s.textContent = "window.__INLINE_RAN = true;";
    document.head.appendChild(s);
    r.inlineScriptRan = window.__INLINE_RAN === true;

    const styleEl = document.createElement("style");
    styleEl.textContent = "#app { outline: 3px solid rgb(9,9,9); }";
    document.head.appendChild(styleEl);
    r.inlineStyleSheetAttached = styleEl.sheet !== null;

    try {
      await fetch("https://blocked.invalid/x", { method: "POST" });
      r.foreignFetch = "resolved";
    } catch {
      r.foreignFetch = "threw";
    }

    await new Promise((res) => setTimeout(res, 500));
    return r;
  }, elkAsset);

  await browser.close();
  killApp();
  return { version, observations, consoleMsgs, cdpLog, stderr };
}

/** The elk worker asset name, read off disk so a rebuilt hash cannot stale this probe. */
function elkAssetName() {
  const dir = join(COCKPIT, "dist", "assets");
  if (!existsSync(dir)) return null;
  return readdirSync(dir).find((f) => f.startsWith("elk-worker")) ?? null;
}

function report(label, o, expectCspActive) {
  const { observations: r, consoleMsgs, cdpLog } = o;
  console.log(`\n=============== ${label} ===============`);
  console.log(`  origin ................ ${r.origin}`);
  console.log(`  href .................. ${r.href}`);
  console.log(`  __TAURI_INTERNALS__ ... ${r.hasTauriInternals}`);
  console.log(`  meta CSP in document .. ${r.metaCsp ?? "(none -- header-delivered)"}`);
  console.log(`  nonced elements ....... ${JSON.stringify(r.nonced)}`);
  console.log(`  unreplaced __TAURI_ ... ${r.unreplacedNonceToken}`);
  console.log(`  IPC app_info .......... ${r.ipcCorpusFree}`);
  console.log(`  IPC health_ping ....... ${r.ipc}`);
  console.log(`  ELK worker ............ ${r.worker}`);
  console.log(`  #app rendered ......... ${r.appRendered} (root rows: ${r.rootRowCount})`);
  console.log(`  inline <script> ran ... ${r.inlineScriptRan}`);
  console.log(`  inline <style> sheet .. ${r.inlineStyleSheetAttached}`);
  console.log(`  foreign fetch ......... ${r.foreignFetch}`);
  console.log(
    `  violations ............ ${
      r.violations.length
        ? r.violations.map((v) => `${v.directive} <- ${v.blocked}`).join(" | ")
        : "(none)"
    }`,
  );

  const ipcFellBack = [...consoleMsgs, ...cdpLog].some((m) =>
    m.includes("IPC custom protocol failed"),
  );
  console.log(`  IPC postMessage fallback warning present? ${ipcFellBack}`);
  // Q4 -- the EFFECTIVE policy, which is not necessarily the DECLARED one. Chromium quotes the
  // directive it enforced in the violation text, so a refusal is the cheapest way to read back
  // what Tauri actually installed after its own hash/nonce augmentation.
  // Not a quote-delimited regex: the directive is wrapped in single quotes AND contains single
  // quotes ('self', 'sha256-...'), so any `'([^']*)'` capture stops after "script-src ". Slice
  // from the directive name to the end of the line instead.
  const scriptMsg = [...consoleMsgs, ...cdpLog].find((m) =>
    /Executing inline script violates/.test(m),
  );
  // Cut at the directive's closing `''.` -- everything after it is Chromium's remediation
  // advice, which SUGGESTS a further hash. Counting the whole line reports one hash too many
  // and credits Tauri with an injection it did not make.
  const effective = scriptMsg
    ? scriptMsg.slice(scriptMsg.indexOf("script-src")).split("''.")[0]
    : null;
  console.log(`  EFFECTIVE script-src ... ${effective ?? "(not observed)"}`);
  if (effective) {
    console.log(
      `  -> tauri-injected sha256 hashes: ${(effective.match(/sha256-/g) ?? []).length}` +
        " (DECLARED policy has none -- this is Tauri's own bootstrap being pinned)",
    );
  }

  const cspLog = [...consoleMsgs, ...cdpLog].filter((m) =>
    /Content Security Policy/i.test(m),
  );
  console.log(`  CSP log entries (${cspLog.length}):`);
  for (const m of cspLog.slice(0, 8)) console.log(`     - ${m.slice(0, 180)}`);

  // Q1 boot
  check(`${label}: app boots at the Tauri custom-protocol origin`,
    /^https?:\/\/tauri\.localhost$/.test(r.origin), r.origin);
  check(`${label}: the frontend bundle executed (window.__TAURI_INTERNALS__ + #app painted)`,
    r.hasTauriInternals && r.appRendered);
  // Q2 IPC
  check(`${label}: a real IPC round trip RESOLVED (corpus-free app_info)`,
    String(r.ipcCorpusFree).startsWith("resolved"), r.ipcCorpusFree);
  // A response of EITHER kind is proof of transport, as long as it came from the app rather
  // than from the CSP: an app-authored rejection still crossed the whole webview->Rust bridge.
  check(`${label}: the IPC bridge answered at all (resolved or app-level rejection)`,
    /^(resolved|rejected)/.test(String(r.ipc)), r.ipc);
  check(`${label}: IPC used the custom protocol, NOT the postMessage fallback`, !ipcFellBack);
  // The decisive connect-src evidence: the ipc origin must never appear as a BLOCKED URI.
  const ipcRefused = r.violations.some(
    (v) => v.directive.startsWith("connect-src") && v.blocked.includes("ipc.localhost"),
  );
  check(`${label}: connect-src never refused the ipc origin`, !ipcRefused);
  // Teeth for that assertion: a foreign origin MUST appear as blocked, otherwise "the ipc
  // origin was not refused" only says nothing was refused at all. Asserted ONLY in the CSP
  // mode -- with no policy installed, nothing can be refused and demanding a refusal here
  // would fail the control for behaving exactly as a control should.
  const foreignRefused = r.violations.some(
    (v) => v.directive.startsWith("connect-src") && v.blocked.includes("blocked.invalid"),
  );
  if (expectCspActive) {
    check(`${label}: connect-src DID refuse a foreign origin (so the check above has teeth)`,
      foreignRefused);
  } else {
    check(`${label}: CONTROL -- no connect-src refusal at all without a policy`,
      !foreignRefused);
  }
  // Q3 worker, at the real origin
  check(`${label}: the ELK web worker constructs at the tauri.localhost origin`,
    r.worker === "constructed", r.worker);
  // Q4 nonces
  check(`${label}: no unreplaced __TAURI_*_NONCE__ token left in the DOM`,
    r.unreplacedNonceToken === false);

  if (expectCspActive) {
    check(`${label}: an inline <script> is REFUSED (CSP is live at the real origin)`,
      r.inlineScriptRan === false);
    check(`${label}: an injected <style> is REFUSED`, r.inlineStyleSheetAttached === false);
  } else {
    check(`${label}: CONTROL -- inline <script> RUNS with no CSP`, r.inlineScriptRan === true);
    check(`${label}: CONTROL -- injected <style> APPLIES with no CSP`,
      r.inlineStyleSheetAttached === true);
  }
}

const main = async () => {
  // `--exe <path>` because an instrumented build is often produced into a private
  // CARGO_TARGET_DIR rather than src-tauri/target (leaving a peer agent's build cache alone).
  // The exe must stay NEXT TO its `engine/` and `resources/` dirs -- Tauri resolves bundled
  // resources relative to the executable -- so point at it in place instead of copying it out.
  const exeIdx = process.argv.indexOf("--exe");
  const exe = exeIdx === -1 ? EXE : process.argv[exeIdx + 1];
  // The no-CSP control needs its OWN instrumented build (csp:null + the debug port): the
  // pre-CSP binary already on disk has no debug port, so it cannot be attached at all.
  const expectCspActive = !process.argv.includes("--expect-no-csp");

  console.log(`exe under test: ${exe}`);
  console.log(`shipped csp (from tauri.conf.json):\n  ${shippedCsp()}`);
  console.log(`mode: ${expectCspActive ? "expect CSP ACTIVE" : "CONTROL, expect NO CSP"}\n`);
  if (!existsSync(exe)) {
    console.log(`FAILED:csp-installed no exe at ${exe} -- run the tauri build first`);
    process.exit(2);
  }

  // Do not trust the --config merge: confirm from the BINARY whether the policy is baked in.
  const exeBytes = readFileSync(exe, "latin1");
  const cspInBinary = exeBytes.includes("ipc.localhost");
  check(
    expectCspActive
      ? "the shipped CSP string is baked into the built exe"
      : "CONTROL: the exe carries NO CSP string",
    cspInBinary === expectCspActive,
    `ipc.localhost in binary: ${cspInBinary}`,
  );
  check("the exe carries the debug port (instrumented build)",
    exeBytes.includes("remote-debugging-port"));

  const elk = elkAssetName();
  console.log(`  elk worker asset on disk: ${elk ?? "(dist/assets missing)"}`);

  const observed = await observe(exe, elk);
  console.log(`\nWebView2 build: ${observed.version.Browser}`);
  report(expectCspActive ? "CSP" : "NO-CSP CONTROL", observed, expectCspActive);

  const failed = checks.filter((c) => !c.ok);
  console.log();
  if (failed.length) {
    console.log(`FAILED:csp-installed ${failed.length}/${checks.length} checks failed`);
    process.exit(1);
  }
  console.log(`SUCCESS:csp-installed ${checks.length}/${checks.length} checks passed`);
};

main().catch((e) => {
  killApp();
  console.log(`FAILED:csp-installed ${e.message}`);
  process.exit(2);
});
