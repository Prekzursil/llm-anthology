/**
 * Per-Tab trace of a virtualized list: which row index has focus, and where the viewport is.
 *
 * The keyboard-reach probe says the walk stalls; it does not say WHERE or WHY. Guessing at a
 * fix from an aggregate count is how the first attempt made the number worse (38 -> 25).
 */
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { homedir } from "node:os";
import { join } from "node:path";

const SKILL = join(homedir(), ".claude", "skills", "ui-audit", "package.json");
const requireFromSkill = createRequire(pathToFileURL(SKILL));
const url = process.argv[2] ?? "http://127.0.0.1:5199";
const TABS = Number(process.argv[3] ?? 30);

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
  window.__paints = 0;
  box.addEventListener("scroll", () => { window.__paints++; }, { passive: true });
  const list = new mod.VirtualList(box, {
    itemHeight: 40,
    emptyLabel: "empty",
    renderRow: (item) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "probe-row";
      b.textContent = `row ${item}`;
      return b;
    },
  });
  list.setItems(Array.from({ length: items }, (_, i) => i));
};

const main = async () => {
  const { chromium } = requireFromSkill("playwright");
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const errs = [];
  page.on("pageerror", (e) => errs.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error") errs.push(m.text()); });
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForSelector("#app");
  await page.evaluate(SETUP, { items: 1000 });
  await page.focus("#probe-before");

  console.log("  tab | focus                | vlIndex | scrollTop | rendered window");
  console.log("  ----+----------------------+---------+-----------+----------------");
  for (let i = 1; i <= TABS; i++) {
    await page.keyboard.press("Tab");
    const s = await page.evaluate(() => {
      const a = document.activeElement;
      const box = document.getElementById("probe-list");
      const rows = [...document.querySelectorAll("[data-vl-index]")]
        .map((r) => Number(r.dataset.vlIndex));
      const win = rows.length ? `${Math.min(...rows)}..${Math.max(...rows)}` : "(none)";
      let who = "(body)";
      let idx = "";
      if (a && a !== document.body) {
        who = a.id || a.textContent || a.tagName;
        const row = a.closest ? a.closest("[data-vl-index]") : null;
        idx = row ? row.dataset.vlIndex : "-";
      }
      return { who: String(who).slice(0, 20), idx, top: box ? box.scrollTop : -1, win };
    });
    console.log(`  ${String(i).padStart(3)} | ${s.who.padEnd(20)} | ${String(s.idx).padStart(7)} `
      + `| ${String(s.top).padStart(9)} | ${s.win}`);
  }
  console.log();
  console.log("  page errors captured: " + errs.length);
  for (const e of [...new Set(errs)].slice(0, 3)) console.log("    " + e.slice(0, 220));
  await browser.close();
};

main().catch((e) => {
  console.log(`FAILED:trace-tab ${e.message}`);
  process.exit(2);
});
