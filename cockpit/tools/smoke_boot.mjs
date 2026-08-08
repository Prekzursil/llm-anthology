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

  // ---------------------------------------------------------------------------
  // The three workspace panels.
  //
  // These exist for exactly the reason this file does. All three shipped fully built and
  // fully unit-tested, with no import and no container -- 168 tests over code no user could
  // reach. tsc cannot see that (nothing was type-wrong) and vitest cannot see it (the tests
  // construct the panels themselves), so the ONLY way to know a panel is reachable is to
  // open it in a browser and look at what it painted.
  //
  // Every assertion below therefore checks CONTENT, not the existence of a container: a row
  // count off the engine, a phrase the engine chose, a refusal the safety model produced.
  // "The div is there" is precisely the claim that was already true and already worthless.
  // ---------------------------------------------------------------------------

  // -- the nav is operable by keyboard ---------------------------------------
  // This repo has shipped a change that broke Tab traversal before, which is why
  // probe_keyboard_reach.mjs exists. That probe walks the virtualized list; nothing walked
  // the top bar, so this asserts the specific thing a `role="tablist"` would have cost:
  // that all three controls are reachable with Tab alone, no arrow keys.
  await page.locator("#topbar h1").click(); // drop focus out of the search field
  const focusWalk = [];
  for (let i = 0; i < 14; i += 1) {
    await page.keyboard.press("Tab");
    focusWalk.push(await page.evaluate(() => document.activeElement?.id ?? ""));
  }
  check("every workspace nav button is reachable by Tab alone",
    ["btn-dedup", "btn-metadata", "btn-maintenance"].every((id) => focusWalk.includes(id)),
    focusWalk.filter(Boolean).slice(0, 6).join(" -> "));

  // -- Duplicates -------------------------------------------------------------
  // Opened from the KEYBOARD, so the first panel gesture in this probe is the one a
  // pointer-free user has to make. `locator.press` focuses and presses atomically; the
  // focused id is reported either way, because "the button did nothing" and "the key never
  // reached the button" are different faults and the detail line has to tell them apart.
  const dedupNav = page.locator("#btn-dedup");
  await dedupNav.focus();
  const focusedBefore = await page.evaluate(() => document.activeElement?.id ?? "(none)");
  await dedupNav.press("Enter");
  await page.waitForTimeout(500);
  check("pressing Enter on Duplicates OPENS the workspace",
    await page.locator("#workspace").isVisible(), `focus was #${focusedBefore}`);
  check("the Duplicates pane is the one showing",
    (await page.locator("#dedup-panel").isVisible())
    && (await page.locator("#btn-dedup").getAttribute("aria-expanded")) === "true");

  // The never-scanned reading, not a blank panel: this panel must never imply it has looked.
  const neverScanned = (await page.locator(".dedup-empty-never-scanned").textContent()) ?? "";
  check("the panel says it has NOT scanned yet", neverScanned.trim().length > 20,
    neverScanned.trim().slice(0, 64));

  // A candidate proves `discover.sources` -> `codexHomeCandidates` ran end to end.
  const candidateCount = await page.locator(".dedup-candidate").count();
  const homePath = ((await page.locator(".dedup-candidate-name").first().textContent()) ?? "").trim();
  check("a Codex home was offered from discovery, as a real absolute path",
    candidateCount > 0 && /^[A-Za-z]:\\/.test(homePath),
    `${candidateCount} candidate(s), first = ${homePath}`);

  await page.locator(".dedup-scan").first().click();
  await page.waitForTimeout(800);
  const dedupRows = await page.locator(".dedup-row").count();
  // Read from the FIRST row, which `renderBody` guarantees is a duplicate group (the
  // unidentified rows are appended after their own heading).
  const copyPaths = (
    await page.locator(".dedup-row").first().locator(".dedup-copy-path").allTextContents()
  ).map((p) => p.trim());
  check("scanning RENDERED duplicate groups from the engine", dedupRows > 0, `${dedupRows} rows`);
  check("a duplicate group names two or more real files on disk",
    copyPaths.length >= 2 && copyPaths.every((p) => p.includes("\\")),
    copyPaths.map((p) => p.split("\\").pop()).join(" + "));

  // -- Annotations ------------------------------------------------------------
  await page.click("#btn-metadata");
  await page.waitForTimeout(500);
  check("clicking Annotations swaps the pane",
    (await page.locator("#metadata-panel").isVisible())
    && (await page.locator("#dedup-panel").isHidden()));
  const scopeNote = (await page.locator(".metadata-scope").textContent()) ?? "";
  check("the editor rendered its three fields and the scope note",
    (await page.locator(".metadata-alias").count()) === 1
    && (await page.locator(".metadata-tags").count()) === 1
    && (await page.locator(".metadata-notes").count()) === 1
    && /not the message text/i.test(scopeNote),
    scopeNote.trim().slice(0, 48));

  // Searching the seeded annotations: rows here can only come from `metadata.search`.
  await page.fill(".metadata-q", "needle");
  await page.waitForTimeout(600);
  const annRows = await page.locator(".metadata-row").count();
  const annStatus = ((await page.locator(".metadata-status").textContent()) ?? "").trim();
  check("searching annotations RENDERED matching rows", annRows > 0, `${annRows} rows`);
  check("the status line reports hits rather than the idle prompt",
    /annotated conversation/.test(annStatus), annStatus);

  // The seam back out: an annotation row opens that conversation's transcript.
  await page.locator(".metadata-row").first().click();
  await page.waitForTimeout(900);
  check("picking an annotation OPENS that transcript in the reader",
    await page.locator("#reader").isVisible());
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  // Both listen for Escape on `document`; the reader is on top and must be the only one
  // that closes, or one press would throw the user out of the panel they were working in.
  check("Escape closes the reader and LEAVES the workspace open",
    (await page.locator("#reader").isHidden())
    && (await page.locator("#workspace").isVisible()));

  // -- Maintenance ------------------------------------------------------------
  await page.click("#btn-maintenance");
  await page.waitForTimeout(400);
  const idleText = ((await page.locator(".maintenance-output").textContent()) ?? "").trim();
  check("clicking Maintenance shows that pane with its idle statement",
    (await page.locator("#maintenance-panel").isVisible())
    && /Nothing will be touched/i.test(idleText),
    idleText.slice(0, 56));

  // Plan over the very files the Duplicates panel just found, with the store root it
  // offered -- both read out of the app's own DOM rather than hardcoded here, so this
  // probe cannot pass against a fixture the app is not actually showing.
  await page.selectOption(".maintenance-action", "delete");
  await page.fill(".maintenance-store-root", homePath);
  await page.fill(".maintenance-checkpoint-root", `${homePath}\\checkpoints`);
  await page.fill(".maintenance-targets", copyPaths.join("\n"));
  await page.click(".maintenance-plan");
  await page.waitForTimeout(800);

  // The live store is a PROTECTED path, so the canonical copy is refused and the backup is
  // not. Derived from the paths actually submitted, so the expectation does not depend on
  // which fixture row happened to render first.
  const refused = copyPaths.filter((p) => /\\\.codex\\sessions\\/i.test(p)).length;
  const allowed = copyPaths.length - refused;
  const phrase = `DELETE ${allowed} ${allowed === 1 ? "FILE" : "FILES"}`;
  const planText = (await page.locator(".maintenance-output").textContent()) ?? "";
  check("planning RENDERED the engine's plan for those files",
    planText.includes(`Delete ${allowed} file`) && planText.includes(`Type ${phrase} to confirm.`),
    planText.split("\n")[0]);
  check("the plan shows what the safety model REFUSED",
    refused === 0 || /refused: .*protected/i.test(planText),
    `${refused} protected target(s) submitted`);
  check("a refused target marks the plan as needing attention",
    refused === 0
    || (await page.locator(".maintenance-output").getAttribute("data-attention")) === "true");

  check("Execute is refused before anything is typed",
    await page.locator(".maintenance-execute").isDisabled());
  await page.fill(".maintenance-confirmation", phrase.toLowerCase());
  check("a wrong-case phrase does NOT unlock Execute",
    (await page.locator(".maintenance-execute").isDisabled())
    && (await page.locator(".maintenance-confirm").getAttribute("data-confirm")) === "mismatch");
  await page.fill(".maintenance-confirmation", phrase);
  check("the EXACT phrase unlocks Execute",
    (await page.locator(".maintenance-execute").isEnabled())
    && (await page.locator(".maintenance-confirm").getAttribute("data-confirm")) === "match");
  // Deliberately NOT clicked. The mock performs no filesystem operation, so pressing it
  // would be safe here -- but a probe that ends in a destructive verb is one environment
  // switch away from being pointed at the real engine. The gate is what was unreachable.

  await page.click("#workspace-close");
  await page.waitForTimeout(300);
  check("Close hides the workspace and the graph pane returns",
    (await page.locator("#workspace").isHidden())
    && (await page.locator("#graph-pane").isVisible())
    && (await page.locator("#btn-maintenance").getAttribute("aria-expanded")) === "false");

  check("no uncaught page errors across the whole journey", errors.length === 0,
    errors.slice(0, 2).join(" | "));

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
