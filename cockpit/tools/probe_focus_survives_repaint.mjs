/**
 * Does focus survive a repaint that is NOT driven by the user's own keystroke?
 *
 * Context. The survey reported "most rows are keyboard-unreachable", measured at 38/1000.
 * That number turned out to be an artifact of the probe pressing Tab faster than the browser
 * could dispatch a scroll event: at a 30ms cadence -- still ~7x faster than a human -- the
 * same list is fully traversable (160/160, zero focus drops). So the reachability blocker is
 * REFUTED.
 *
 * What remains real is narrower and worth measuring on its own: `paint()` calls
 * `sizer.replaceChildren()`, which detaches the focused row unconditionally. Any repaint that
 * is not the user's own Tab -- a window resize, the ResizeObserver firing, a layout shift
 * from a sibling pane -- therefore drops focus to `<body>` mid-interaction. That is not a
 * race; it is deterministic.
 *
 * This measures exactly that: focus a row, force a repaint via resize, ask where focus went.
 *
 * Usage: node probe_focus_survives_repaint.mjs <vite-url>
 */
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { homedir } from "node:os";
import { join } from "node:path";

const SKILL = join(homedir(), ".claude", "skills", "ui-audit", "package.json");
const requireFromSkill = createRequire(pathToFileURL(SKILL));
const url = process.argv[2] ?? "http://127.0.0.1:5199";

const main = async () => {
  const { chromium } = requireFromSkill("playwright");
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForSelector("#app");

  const result = await page.evaluate(async () => {
    const mod = await import("/src/ui/virtualList.ts");
    document.body.innerHTML = "";
    const box = document.createElement("div");
    box.id = "probe-list";
    box.style.height = "400px";
    box.style.width = "300px";
    document.body.appendChild(box);

    // Two focusables per row: the search hit row is a div holding "locate" and "read", so
    // restoring to the ROW instead of the control would silently move the user sideways.
    const list = new mod.VirtualList(box, {
      itemHeight: 40,
      emptyLabel: "empty",
      renderRow: (item) => {
        const wrap = document.createElement("div");
        const a = document.createElement("button");
        a.type = "button";
        a.textContent = `locate ${item}`;
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = `read ${item}`;
        wrap.append(a, b);
        return wrap;
      },
    });
    list.setItems(Array.from({ length: 500 }, (_, i) => i));

    const settle = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));

    // Focus the SECOND control of a mid-window row.
    const rows = [...box.querySelectorAll("[data-vl-index]")];
    const target = rows[5] ?? rows[0];
    const wanted = target ? target.querySelectorAll("button")[1] : null;
    if (!wanted) return { error: "probe could not find a row control to focus" };
    wanted.focus();
    const before = document.activeElement.textContent;

    // Force a repaint that is NOT a keystroke: resize the container.
    box.style.height = "360px";
    await settle();
    const afterResize = document.activeElement === document.body
      ? "(body)" : document.activeElement.textContent;

    // And one driven by a programmatic scroll, still not a keystroke.
    box.scrollTop = 200;
    await settle();
    const afterScroll = document.activeElement === document.body
      ? "(body)" : document.activeElement.textContent;

    return { before, afterResize, afterScroll };
  });

  await browser.close();

  if (result.error) {
    console.log(`FAILED:focus-repaint ${result.error}`);
    process.exit(2);
  }
  console.log(`  focused before repaint : ${result.before}`);
  console.log(`  after a RESIZE repaint : ${result.afterResize}`);
  console.log(`  after a SCROLL repaint : ${result.afterScroll}`);
  console.log();

  const survived = result.afterResize === result.before;
  if (!survived) {
    console.log("FAILED:focus-repaint a repaint the user did not cause drops focus to <body>");
    process.exit(1);
  }
  console.log("SUCCESS:focus-repaint focus survives a repaint, on the same control");
};

main().catch((e) => {
  console.log(`FAILED:focus-repaint ${e.message}`);
  process.exit(2);
});
