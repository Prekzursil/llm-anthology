/**
 * Does the app RUN under the shipped Content Security Policy, and is that policy ENFORCED?
 *
 * Neither tsc nor vitest can answer either half. `src/cspPolicy.test.ts` asserts the policy
 * STRING statically; nothing there ever loads the app. A wrong directive is invisible until a
 * browser refuses a resource at runtime — and the two that would hurt most (`worker-src`,
 * `style-src`) fail silently, taking the graph pane or the entire stylesheet with them.
 *
 * The policy is READ FROM `src-tauri/tauri.conf.json`, never retyped, so this cannot drift
 * from what ships. Tauri delivers it as a real `Content-Security-Policy` response header on
 * its custom protocol (tauri 2.11.5 `src/protocol/tauri.rs:183`), so this serves the BUILT
 * `dist/` behind that same header. A vite dev server would prove nothing: `tauri.conf.json`
 * is not applied there at all.
 *
 * WHAT THIS DOES NOT PROVE — do not upgrade these to "verified":
 *   * `'self'` here resolves to `http://127.0.0.1:<port>`, NOT Tauri's `http://tauri.localhost`.
 *     Same-origin directives are exercised structurally, at a different origin.
 *   * There is no Tauri runtime, so `window.__TAURI_INTERNALS__` is absent and no real IPC
 *     call is ever made. `connect-src ipc: http://ipc.localhost` is only shown to be
 *     PERMITTED (not refused by the policy); that it carries a working RPC is untested here.
 *     Only `tauri build` plus a real launch settles that.
 *   * Tauri's nonce/hash injection (`src/manager/mod.rs:53`) does not run; this serves the raw
 *     Vite output. That is faithful today because the build emits no inline script or style.
 *
 * Usage:
 *   node probe_csp.mjs              verify the shipped policy: boot journey + zero violations
 *   node probe_csp.mjs --self-test  detector control: prove the violation listener FIRES
 *
 * Run --self-test whenever you touch this file. A violation listener that stays silent under
 * a policy you know is broken is measuring nothing, and this is exactly where that is easy.
 */
import { createServer } from "node:http";
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { join, extname, dirname } from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL, fileURLToPath } from "node:url";

const COCKPIT = dirname(dirname(fileURLToPath(import.meta.url)));
const DIST = join(COCKPIT, "dist");
const CONF = join(COCKPIT, "src-tauri", "tauri.conf.json");

// Same vendored playwright the other probes use, so this adds no dependency.
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

/** The policy exactly as it ships. Read, never retyped — a copy would drift. */
function shippedCsp() {
  const conf = JSON.parse(readFileSync(CONF, "utf8"));
  const policy = conf.app?.security?.csp;
  if (typeof policy !== "string" || policy.trim() === "") {
    throw new Error(`app.security.csp in ${CONF} is not a policy string (got ${policy})`);
  }
  return policy;
}

/**
 * Replace one directive's sources, or append the directive if absent. Used ONLY by
 * --self-test to manufacture a known-broken policy.
 */
function override(policy, directive, sources) {
  const parts = policy.split(";").map((p) => p.trim()).filter(Boolean);
  const at = parts.findIndex((p) => p.split(/\s+/)[0].toLowerCase() === directive);
  const replacement = `${directive} ${sources}`;
  if (at === -1) parts.push(replacement);
  else parts[at] = replacement;
  return parts.join("; ");
}

/** Drop a directive entirely, so it falls back down the CSP fallback chain. */
function dropDirective(policy, directive) {
  return policy
    .split(";")
    .map((p) => p.trim())
    .filter(Boolean)
    .filter((p) => p.split(/\s+/)[0].toLowerCase() !== directive)
    .join("; ");
}

const MIME = {
  ".html": "text/html",
  ".js": "text/javascript",
  ".mjs": "text/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".svg": "image/svg+xml",
};

/** Serve `dist/` with the given policy as a response header, the way Tauri does. */
function serveDist(policy) {
  const server = createServer((req, res) => {
    const requested = decodeURIComponent((req.url ?? "/").split("?")[0]);
    let file = join(DIST, requested === "/" ? "index.html" : requested);
    let body;
    try {
      body = readFileSync(file);
    } catch {
      file = join(DIST, "index.html"); // SPA fallback
      body = readFileSync(file);
    }
    if (policy) res.setHeader("Content-Security-Policy", policy);
    res.setHeader("Content-Type", MIME[extname(file)] ?? "application/octet-stream");
    res.end(body);
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}

/**
 * Load the app and collect every CSP violation plus the results of a few deliberate probes.
 *
 * Violations are collected TWO ways because each misses something: the page's own
 * `securitypolicyviolation` event (installed via addInitScript, so it is live before the
 * document's own resources load) and CDP `Log.entryAdded`, which also catches entries the
 * page-level event does not surface.
 */
async function inspect(url, workerAsset) {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });

  const pageErrors = [];
  const cdpViolations = [];
  page.on("pageerror", (e) => pageErrors.push(String(e)));

  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Log.enable");
  cdp.on("Log.entryAdded", ({ entry }) => {
    if (entry.source === "security" || /Content Security Policy/i.test(entry.text ?? "")) {
      cdpViolations.push(entry.text);
    }
  });

  await page.addInitScript(() => {
    window.__cspViolations = [];
    document.addEventListener("securitypolicyviolation", (e) => {
      window.__cspViolations.push({
        directive: e.effectiveDirective || e.violatedDirective,
        blocked: e.blockedURI,
      });
    });
  });

  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForSelector("#app");
  await page.waitForTimeout(1500); // corpus restore, then the discovery scan

  // Violations caused by the app's own boot, before any deliberate probe muddies the list.
  const bootViolations = await page.evaluate(() => window.__cspViolations.slice());

  const probes = await page.evaluate(async (asset) => {
    const r = {};

    // worker-src. `graph/elkLayout.ts` does `new Worker(url, { type: "classic" })`; if the
    // policy refuses it the whole graph pane dies with no other symptom.
    try {
      const w = new Worker(asset, { type: "classic" });
      w.terminate();
      r.worker = "constructed";
    } catch (e) {
      r.worker = `threw: ${e.name}`;
    }

    // style-src, CSSOM path. The app writes `el.style.<prop>`, which CSP does NOT govern.
    // This asserts that stays true rather than assuming it.
    try {
      document.body.style.background = "rgb(1, 2, 3)";
      r.cssomStyle = getComputedStyle(document.body).backgroundColor === "rgb(1, 2, 3)"
        ? "applied"
        : "not applied";
    } catch (e) {
      r.cssomStyle = `threw: ${e.name}`;
    }

    // style-src, real inline path. MUST be refused, otherwise style-src is not enforced.
    //
    // Measured via `.sheet`: a refused <style> gets NO CSSStyleSheet attached, which is
    // unambiguous. Do NOT measure this with getComputedStyle -- `outline-width` reports its
    // initial `medium` (= 3px) even when outline-style is `none`, so comparing it to "3px"
    // is true with no rule applied at all. That exact check sat here and PASSED in a state
    // where the style was in fact blocked: a detector that could not fail.
    const styleEl = document.createElement("style");
    styleEl.textContent = "#app { outline: 3px solid rgb(9, 9, 9); }";
    document.head.appendChild(styleEl);
    r.inlineStyleSheetAttached = styleEl.sheet !== null;

    // style-src-attr. `outline-style` initial value is `none`, so this one IS a sound
    // discriminator. The app only ever writes `el.style.<prop>` (CSSOM), never this.
    const attrEl = document.createElement("div");
    attrEl.setAttribute("style", "outline: 5px solid rgb(7, 7, 7)");
    document.body.appendChild(attrEl);
    r.styleAttrApplied = getComputedStyle(attrEl).outlineStyle === "solid";

    // script-src. MUST be refused.
    const scriptEl = document.createElement("script");
    scriptEl.textContent = "window.__INLINE_RAN = true;";
    document.head.appendChild(scriptEl);
    r.inlineScriptRan = window.__INLINE_RAN === true;

    // connect-src. A foreign origin must be refused; the Tauri IPC origin must not be.
    // Both fetches fail at the network layer here (neither host resolves), so the CSP
    // violation list — not the fetch outcome — is the discriminator.
    for (const [key, target] of [
      ["foreign", "https://blocked.invalid/x"],
      ["ipc", "http://ipc.localhost/health_ping"],
    ]) {
      try {
        await fetch(target, { method: "POST" });
        r[`fetch_${key}`] = "resolved";
      } catch {
        r[`fetch_${key}`] = "threw";
      }
    }

    await new Promise((res) => setTimeout(res, 400));
    r.violations = window.__cspViolations.slice();
    return r;
  }, workerAsset);

  await browser.close();
  return { pageErrors, bootViolations, cdpViolations, ...probes };
}

/** Drive the one journey a user cannot avoid: search -> Read -> Escape. */
async function journey(url) {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const violations = [];
  await page.addInitScript(() => {
    window.__cspViolations = [];
    document.addEventListener("securitypolicyviolation", (e) =>
      window.__cspViolations.push(`${e.effectiveDirective || e.violatedDirective} <- ${e.blockedURI}`),
    );
  });
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForSelector("#app");
  await page.waitForTimeout(1500);

  await page.fill("#search-input", "a");
  await page.waitForTimeout(600);
  const hits = await page.locator(".hit-row").count();
  let readerOpened = false;
  let readerClosed = false;
  if (hits > 0) {
    await page.locator(".hit-read").first().click();
    await page.waitForTimeout(900);
    readerOpened = await page.locator("#reader").isVisible();
    await page.keyboard.press("Escape");
    await page.waitForTimeout(300);
    readerClosed = await page.locator("#reader").isHidden();
  }
  violations.push(...(await page.evaluate(() => window.__cspViolations.slice())));
  await browser.close();
  return { hits, readerOpened, readerClosed, violations };
}

function workerAssetPath() {
  const found = readdirSync(join(DIST, "assets")).find((f) => f.startsWith("elk-worker"));
  if (!found) throw new Error("no elk-worker asset in dist/assets — is the build current?");
  return `/assets/${found}`;
}

async function verifyShippedPolicy() {
  const policy = shippedCsp();
  const workerAsset = workerAssetPath();
  console.log(`policy under test (from ${CONF}):\n  ${policy}\n`);

  const server = await serveDist(policy);
  const url = `http://127.0.0.1:${server.address().port}/`;

  // The header must actually arrive, byte-identical to the config.
  const head = await fetch(url);
  check(
    "the served document carries the policy from tauri.conf.json",
    head.headers.get("content-security-policy") === policy,
  );

  const r = await inspect(url, workerAsset);
  check("no uncaught page errors during boot", r.pageErrors.length === 0,
    r.pageErrors.slice(0, 2).join(" | "));
  check("the app's own boot causes ZERO csp violations", r.bootViolations.length === 0,
    r.bootViolations.map((v) => `${v.directive} <- ${v.blocked}`).join(" | "));
  check("the ELK web worker is permitted", r.worker === "constructed", r.worker);
  check("a CSSOM style write still applies", r.cssomStyle === "applied", r.cssomStyle);

  // Enforcement, not just absence of breakage: if these are NOT refused, the policy is not
  // being applied and every "ok" above is vacuous.
  const byDirective = (name, needle) =>
    r.violations.some((v) => v.directive.startsWith(name) && v.blocked.includes(needle));
  check("an injected <style> element is REFUSED", r.inlineStyleSheetAttached === false);
  check("a style= attribute is REFUSED", r.styleAttrApplied === false);
  check("those style refusals were reported as style-src violations",
    byDirective("style-src", "inline"));
  check("an inline <script> is REFUSED", r.inlineScriptRan === false);
  check("a foreign origin is REFUSED by connect-src",
    byDirective("connect-src", "blocked.invalid"));
  check("the Tauri IPC origin is NOT refused by connect-src",
    !byDirective("connect-src", "ipc.localhost"));
  check("violations were also seen on the CDP log channel", r.cdpViolations.length > 0,
    `${r.cdpViolations.length} entries`);

  const j = await journey(url);
  check("search returns rows under the policy", j.hits > 0, `${j.hits} rows`);
  check("Read opens the reader under the policy", j.readerOpened);
  check("Escape closes the reader under the policy", j.readerClosed);
  check("the search -> Read -> Escape journey causes ZERO csp violations",
    j.violations.length === 0, j.violations.join(" | "));

  await new Promise((res) => server.close(res));
}

/**
 * Detector control. Each case manufactures a policy that MUST produce a specific violation;
 * if the listener stays quiet, this probe is measuring nothing and its passes are worthless.
 *
 * Note the `worker-src` pair. DROPPING the directive is deliberately expected NOT to fire:
 * worker-src falls back to child-src and then to `default-src 'self'`, which still permits a
 * same-origin worker. Only `'none'` actually refuses it. That is also the honest reading of
 * `worker-src 'self'` in the shipped policy — it is documentation of intent, redundant with
 * default-src today, and load-bearing only if default-src is ever widened.
 */
async function selfTest() {
  const base = shippedCsp();
  const workerAsset = workerAssetPath();

  // Each case must name the APP'S OWN resource, not just the directive. Matching on the
  // directive alone does not discriminate: the deliberate inline probes in `inspect` already
  // raise `script-src-elem <- inline` and `style-src-elem <- inline` under the CORRECT
  // policy, so a directive-only match would pass in every state and certify nothing.
  const bundle = readdirSync(join(DIST, "assets")).find((f) => /^index-.*\.js$/.test(f));
  const stylesheet = readdirSync(join(DIST, "assets")).find((f) => /^index-.*\.css$/.test(f));
  const cases = [
    ["script-src 'none' blocks the app's own bundle",
      override(base, "script-src", "'none'"), "script-src", bundle, true],
    ["style-src 'none' blocks the app's own stylesheet",
      override(base, "style-src", "'none'"), "style-src", stylesheet, true],
    ["worker-src 'none' blocks the ELK worker",
      override(base, "worker-src", "'none'"), "worker-src", "elk-worker", true],
    ["connect-src 'none' blocks the Tauri IPC origin",
      override(base, "connect-src", "'none'"), "connect-src", "ipc.localhost", true],
    ["DROPPING worker-src does NOT fire (falls back to default-src 'self')",
      dropDirective(base, "worker-src"), "worker-src", "elk-worker", false],
  ];

  for (const [name, policy, directive, needle, expectFire] of cases) {
    const server = await serveDist(policy);
    const url = `http://127.0.0.1:${server.address().port}/`;
    const r = await inspect(url, workerAsset);
    const fired = [...r.bootViolations, ...r.violations].some(
      (v) => v.directive.startsWith(directive) && v.blocked.includes(needle),
    );
    check(name, fired === expectFire, `${fired ? "saw" : "no"} ${directive} <- ${needle}`);
    await new Promise((res) => server.close(res));
  }
}

const main = async () => {
  if (!existsSync(DIST)) {
    console.log("FAILED:csp-probe dist/ is missing -- run `npm run build` first");
    process.exit(2);
  }
  const selfTestMode = process.argv.includes("--self-test");
  console.log(selfTestMode ? "MODE: detector control (--self-test)\n" : "MODE: verify shipped policy\n");
  if (selfTestMode) await selfTest();
  else await verifyShippedPolicy();

  const failed = checks.filter((c) => !c.ok);
  console.log();
  const label = selfTestMode ? "csp-probe-selftest" : "csp-probe";
  if (failed.length) {
    console.log(`FAILED:${label} ${failed.length}/${checks.length} checks failed`);
    process.exit(1);
  }
  console.log(`SUCCESS:${label} ${checks.length}/${checks.length} checks passed`);
};

main().catch((e) => {
  console.log(`FAILED:csp-probe ${e.message}`);
  process.exit(2);
});
