/**
 * Does the app BOOT, and does the reader open?
 *
 * tsc and vitest between them do not answer this. `requireEl("reader")` throws at runtime if
 * the element is missing from index.html, and no type or unit test sees index.html at all --
 * this project's vitest has no DOM. The whole "built but never wired" family of defects in
 * this codebase lives in exactly that gap: every surface green, the seam between them
 * untested.
 *
 * Usage: node smoke_boot.mjs <vite-url>
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

  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForSelector("#app");
  // The app boots asynchronously (corpus restore, then a discovery scan).
  await page.waitForTimeout(1500);

  check("no uncaught page errors during boot", errors.length === 0, errors.slice(0, 2).join(" | "));
  check("the reader element exists", await page.locator("#reader").count() === 1);
  check("the reader is hidden at boot", await page.locator("#reader").isHidden());
  check("the provider filter element exists", await page.locator("#search-provider").count() === 1);

  // Drive the one journey that had no code path at all until now: search -> Read.
  await page.fill("#search-input", "a");
  await page.waitForTimeout(600);
  const hits = await page.locator(".hit-row").count();
  check("search returned at least one row to act on", hits > 0, `${hits} rows`);

  if (hits > 0) {
    check("each hit row offers a Read control", await page.locator(".hit-read").count() === hits);
    await page.locator(".hit-read").first().click();
    await page.waitForTimeout(900);
    const open = await page.locator("#reader").isVisible();
    check("clicking Read OPENS the reader", open);
    if (open) {
      const title = (await page.locator("#reader-title").textContent()) ?? "";
      check("the reader shows a heading, not a spinner", title.trim() !== "" && title !== "Loading…", title.slice(0, 40));
      check("the reader is a modal dialog for a11y",
        (await page.locator("#reader").getAttribute("role")) === "dialog"
        && (await page.locator("#reader").getAttribute("aria-modal")) === "true");
      const blocks = await page.locator(".reader-block, .reader-stub").count();
      check("the reader rendered content (turns or an honest stub)", blocks > 0, `${blocks} nodes`);
      await page.keyboard.press("Escape");
      await page.waitForTimeout(300);
      check("Escape CLOSES the reader", await page.locator("#reader").isHidden());
    }
  }

  await browser.close();
  const failed = checks.filter((c) => !c.ok);
  console.log();
  if (failed.length) {
    console.log(`FAILED:smoke-boot ${failed.length}/${checks.length} checks failed`);
    process.exit(1);
  }
  console.log(`SUCCESS:smoke-boot ${checks.length}/${checks.length} checks passed`);
};

main().catch((e) => {
  console.log(`FAILED:smoke-boot ${e.message}`);
  process.exit(2);
});
