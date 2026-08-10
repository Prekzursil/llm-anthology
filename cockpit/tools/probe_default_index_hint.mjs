/**
 * CF-1: does the DEFAULT INDEX PATH actually reach the screen?
 *
 * The unit tests prove `CorpusBarController` puts the hint in its view, and tsc proves the
 * types line up. Neither can see `index.html`, so neither can answer the only question that
 * made CF-1 a defect in the first place: whether anything the user can look at ends up
 * carrying the value. `app_info` was registered in Rust, resolved on every call, and read by
 * nobody -- a wire that stops one element short is the same defect one layer higher.
 *
 * So this asserts on the DOM attribute, on a real boot, with no corpus attached: the app is
 * loaded with `localStorage` cleared, so `restore()` takes the nothing-remembered branch.
 *
 * WHAT IT CANNOT ANSWER: outside the Tauri webview `ipc/index.ts` selects the MOCK, so the
 * path shown is the mock's synthetic one. This proves the value crosses every seam from the
 * adapter to a rendered attribute; it does NOT prove the Rust resolver returns a sensible
 * path on this host. That needs a Tauri build, and `cargo test` covers the resolver itself
 * (`lib.rs` app_locations_from tests).
 *
 * Usage: node probe_default_index_hint.mjs <vite-url>
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
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });

  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });

  // A REMEMBERED corpus would take the other branch and there would be no hint to find, so
  // the storage is cleared before the app script runs, not after.
  await page.addInitScript(() => window.localStorage.clear());
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForSelector("#corpus-current");
  await page.waitForTimeout(1500); // boot is async: restore, then the discovery scan

  const label = page.locator("#corpus-current");
  const title = (await label.getAttribute("title")) ?? "";
  const text = (await label.textContent()) ?? "";

  check("no uncaught page errors during boot", errors.length === 0, errors.slice(0, 2).join(" | "));
  check("nothing is attached, so this is the first-run branch", text.includes("No corpus"), text);
  check("the label carries a title attribute at all", title !== "", "(empty = the wire is dead)");
  check("the title names a concrete index file", /\.sqlite\b/.test(title), title);
  check(
    "the title names the app data folder, not just any path",
    /anthology/i.test(title),
    title,
  );

  await browser.close();
  const failed = checks.filter((c) => !c.ok);
  console.log(
    failed.length === 0
      ? "SUCCESS:cf1-default-index-hint"
      : `FAILED:cf1-default-index-hint ${failed.map((c) => c.name).join("; ")}`,
  );
  process.exit(failed.length === 0 ? 0 : 1);
};

main().catch((err) => {
  console.error(`FAILED:cf1-default-index-hint ${err}`);
  process.exit(1);
});
