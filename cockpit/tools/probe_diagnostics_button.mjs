/**
 * CF-19: is the "Copy diagnostics" button REACHABLE, and does clicking it produce a real bundle?
 *
 * `mountDiagnosticsButton` was fully implemented and fully unit-tested with ZERO production
 * callers for its whole life. Unit tests could not have caught that: they construct the thing
 * themselves, so they pass identically whether or not the app ever calls it. vitest here also
 * cannot see `index.html`, so nothing in the suite knows `#topbar` exists. That gap is the
 * defect, which means the fix has to be measured at the DOM of a real boot.
 *
 * ASSERTS THE ARTIFACT, NOT THE STATUS LINE. A status message only proves a handler ran. The
 * clipboard TEXT proves the click reached `ipc.appInfo()`, got an answer, and folded it into a
 * bundle with the live UI numbers -- so the whole chain is exercised rather than its first
 * link. Clipboard permissions are granted explicitly below; without them
 * `systemClipboardWrite` rejects and the app (correctly) reports a copy failure instead.
 *
 * WHAT IT CANNOT ANSWER: outside the Tauri webview `ipc/index.ts` selects the MOCK, so the
 * version and engine numbers are the mock's. This proves the wire and the bundle assembly, NOT
 * that the Rust `app_info` returns sensible values on a given host -- `cargo test` owns that.
 *
 * Usage: node probe_diagnostics_button.mjs <vite-url>
 */
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { homedir } from "node:os";
import { join } from "node:path";

const SKILL = join(homedir(), ".claude", "skills", "ui-audit", "package.json");
const requireFromSkill = createRequire(pathToFileURL(SKILL));
const url = process.argv[2] ?? "http://localhost:5199";

const checks = [];
const check = (name, ok, detail = "") => {
  checks.push({ name, ok, detail });
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${name}${detail ? `  -- ${detail}` : ""}`);
};

const main = async () => {
  const { chromium } = requireFromSkill("playwright");
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: url });
  const page = await context.newPage();

  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });

  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForSelector("#app");
  await page.waitForTimeout(1500); // boot is async: corpus restore, then the discovery scan

  const button = page.locator("#btn-diagnostics");
  check("no uncaught page errors during boot", errors.length === 0, errors.slice(0, 2).join(" | "));
  check("the button EXISTS at all", (await button.count()) === 1, "(0 = never mounted)");
  check("it is inside the topbar, not orphaned", (await page.locator("#topbar #btn-diagnostics").count()) === 1);
  check("it is visible to a user", await button.isVisible());
  check("its status line is a polite live region",
    (await page.locator("#diagnostics-status").getAttribute("role")) === "status");

  await button.click();
  await page.waitForFunction(
    () => (document.getElementById("diagnostics-status")?.textContent ?? "") !== "Collecting…",
    undefined,
    { timeout: 5000 },
  );
  const status = (await page.locator("#diagnostics-status").textContent()) ?? "";
  check("clicking it reports success rather than a failure", /copied/i.test(status), status);

  const copied = await page.evaluate(() => navigator.clipboard.readText());
  check("the clipboard holds a bundle, not an empty string", copied.length > 0, `${copied.length} chars`);
  check("the bundle carries the app version from the client", /app version: (?!\(unknown\))/.test(copied),
    (copied.match(/app version: .*/) ?? ["(absent)"])[0]);
  // THESE TWO ARE THE app.ts CHANGE, and they are asserted against the NULL rendering rather
  // than against a bare `engine: ` / `index: ` prefix, which both states print. Before CF-19
  // `loadHealth`/`loadStats` reduced each answer straight to topbar text and kept nothing, so
  // a bundle could only ever have said "(not reached)" and "(no stats". A weaker assertion
  // here would pass against exactly the defect being fixed.
  check("the bundle carries LIVE health, not the not-reached fallback",
    /engine: .+\(IR .+\), .*corpus/.test(copied) && !copied.includes("(not reached"),
    (copied.match(/engine: .*/) ?? ["(absent)"])[0]);
  check("the bundle carries LIVE stats, not the no-stats fallback",
    /index: \d+ conversations/.test(copied) && !copied.includes("(no stats"),
    (copied.match(/index: .*/) ?? ["(absent)"])[0]);
  check("the bundle carries the privacy note", /paste/i.test(copied) || copied.includes("diagnostics"));
  // The one field that could carry a username. `indexLabel` must have reduced it already.
  check("the bundle names an index by BASENAME only", !/[\\/]Users[\\/]/.test(copied),
    "(a full home path here would be a leak)");

  await browser.close();
  const failed = checks.filter((c) => !c.ok);
  console.log(
    failed.length === 0
      ? "SUCCESS:cf19-diagnostics-button"
      : `FAILED:cf19-diagnostics-button ${failed.map((c) => c.name).join("; ")}`,
  );
  process.exit(failed.length === 0 ? 0 : 1);
};

main().catch((err) => {
  console.error(`FAILED:cf19-diagnostics-button ${err}`);
  process.exit(1);
});
