/**
 * How far can a keyboard-only user get down a virtualized list?
 *
 * The claim under test: `VirtualList.paint()` runs on every scroll and starts with
 * `sizer.replaceChildren()`, which detaches the focused row. Focus falls to `<body>`, so the
 * next Tab restarts at the top of the document and the walk becomes a closed loop.
 *
 * It drives the REAL `/src/ui/virtualList.ts` with 1,000 synthetic rows in a fresh container
 * rather than the app's own lists: the mock IPC returns only ~13 search hits, which all fit
 * in one window, so no scroll-driven repaint ever happens and the probe would report a clean
 * bill of health for a broken component. Measuring the component directly is the only way to
 * reach the code path.
 *
 * Usage: node probe_keyboard_reach.mjs <vite-url>
 */
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { homedir } from "node:os";
import { join } from "node:path";

const SKILL = join(homedir(), ".claude", "skills", "ui-audit", "package.json");
const requireFromSkill = createRequire(pathToFileURL(SKILL));
const url = process.argv[2] ?? "http://127.0.0.1:5199";

const ITEMS = 1000;
const MAX_TABS = 160;
const TAB_DELAY_MS = Number(process.argv[3] ?? 0);

const SETUP = async ({ items }) => {
  const mod = await import("/src/ui/virtualList.ts");
  document.body.innerHTML = "";
  const before = document.createElement("button");
  before.id = "probe-before";
  before.textContent = "before";
  const box = document.createElement("div");
  box.id = "probe-list";
  box.style.height = "400px";
  box.style.width = "300px";
  document.body.append(before, box);

  const list = new mod.VirtualList(box, {
    itemHeight: 40,
    emptyLabel: "empty",
    renderRow: (item) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "probe-row";
      b.dataset.idx = String(item);
      b.textContent = `row ${item}`;
      return b;
    },
  });
  list.setItems(Array.from({ length: items }, (_, i) => i));
  return document.querySelectorAll(".probe-row").length;
};

const main = async () => {
  const { chromium } = requireFromSkill("playwright");
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForSelector("#app");

  const windowed = await page.evaluate(SETUP, { items: ITEMS });
  console.log(`  ${ITEMS} items backing the list, ${windowed} rendered in the window`);

  // CONTROL: the probe must be able to observe a healthy walk. A plain (non-virtualized)
  // list of the same size, driven identically, should reach far more rows. If it does not,
  // the harness is broken and its verdict on the real component means nothing.
  const control = await page.evaluate(async (n) => {
    document.body.innerHTML = "";
    const box = document.createElement("div");
    box.style.height = "400px";
    box.style.overflowY = "auto";
    for (let i = 0; i < n; i++) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "ctl-row";
      b.textContent = `row ${i}`;
      b.style.display = "block";
      b.style.height = "40px";
      box.appendChild(b);
    }
    document.body.appendChild(box);
    return document.querySelectorAll(".ctl-row").length;
  }, 200);
  await page.focus("body");
  const ctlReached = new Set();
  for (let i = 0; i < MAX_TABS; i++) {
    await page.keyboard.press("Tab");
    const t = await page.evaluate(() => {
      const a = document.activeElement;
      return a && a.classList && a.classList.contains("ctl-row") ? a.textContent : null;
    });
    if (t) ctlReached.add(t);
  }
  console.log(`  control (plain list, ${control} rows): reached ${ctlReached.size} in ${MAX_TABS} tabs`);
  if (ctlReached.size < 100) {
    console.log("  CONTROL FAILED -- the harness cannot observe a healthy walk; verdict void");
    await browser.close();
    process.exit(2);
  }

  // The real measurement.
  await page.evaluate(SETUP, { items: ITEMS });
  await page.focus("#probe-before");
  const reached = new Set();
  let fellToBody = 0;
  for (let i = 0; i < MAX_TABS; i++) {
    await page.keyboard.press("Tab");
    if (TAB_DELAY_MS) await page.waitForTimeout(TAB_DELAY_MS);
    const where = await page.evaluate(() => {
      const a = document.activeElement;
      if (!a || a === document.body) return { body: true };
      return { body: false, idx: a.dataset ? a.dataset.idx : undefined };
    });
    if (where.body) fellToBody++;
    else if (where.idx !== undefined) reached.add(where.idx);
  }

  console.log(`  virtualized: Tab x${MAX_TABS} -> ${reached.size} distinct rows reached, `
    + `focus fell to <body> ${fellToBody}x`);

  await browser.close();
  console.log();
  const ok = reached.size >= 100;
  if (!ok) {
    console.log(`FAILED:keyboard-reach ${reached.size}/${ITEMS} rows reachable `
      + `(control reached ${ctlReached.size}, so the harness works)`);
    process.exit(1);
  }
  console.log(`SUCCESS:keyboard-reach ${reached.size} rows reachable`);
};

main().catch((e) => {
  console.log(`FAILED:keyboard-reach ${e.message}`);
  process.exit(2);
});
