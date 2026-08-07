/**
 * Does COLLAPSING wide fan-outs actually make the real corpus layable? And at what threshold?
 *
 * Established already: the real corpus shape (12,791 nodes, one 4,844-child hub, depth 3) blows
 * the app's 8s guard under EVERY hierarchical ELK configuration, and the only algorithms that
 * finish are packing ones that discard the edge structure. So the fix has to reduce what is
 * handed to the layout. "Collapse a wide fan-out into one expandable placeholder" is the
 * obvious candidate — but obvious is not measured, and the whole point of the previous two
 * harnesses was that unmeasured beliefs about this layout were wrong three times.
 *
 * This applies exactly that transform at a range of thresholds and reports where `layered`
 * comes back inside budget. The output is a NUMBER an implementation can be written against,
 * not a direction.
 *
 * The transform mirrors what a real implementation would do: a node with more than `threshold`
 * children keeps the first `threshold` and gains ONE synthetic "+N more" child standing for the
 * remainder — so the parent still shows it is wide, and the user can expand.
 *
 * Usage: node measure_layout_collapse.mjs <vite-url>
 */
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { homedir } from "node:os";
import { join } from "node:path";

const SKILL = join(homedir(), ".claude", "skills", "ui-audit", "package.json");
const requireFromSkill = createRequire(pathToFileURL(SKILL));

const url = process.argv[2] ?? "http://localhost:5199";

/** null = no collapse (the baseline that is known to fail). */
/**
 * These double as the EXPANSION curve, which is the point most easily missed: showing N
 * children of one parent costs the same whether you arrived at N by collapsing down to it or by
 * expanding up to it. So this table answers "how far can a user expand a +N more affordance
 * before the graph dies", and 200-vs-500 is the interesting gap to close.
 */
const THRESHOLDS = [null, 500, 400, 300, 250, 200, 150, 100, 50, 25];

/**
 * Each threshold is measured REPEATS times and the MEDIAN reported. A single sample swung the
 * 200 case from 2130ms to 8337ms across two runs, so one number per threshold is not a
 * measurement — it is a sample from a noisy distribution, on a machine that also runs agent
 * workflows and a game.
 */
const REPEATS = 3;

const RUN = async ({ threshold, budgetMs }) => {
  const layoutMod = await import("/src/graph/layout.ts");
  const elkMod = await import("/src/graph/elkLayout.ts");

  const NODES = 12791;
  const HUB = 4844;
  const MAX_DEPTH = 3;

  const ns = [];
  for (let i = 0; i < NODES; i++) {
    ns.push({
      id: `n${i}`,
      title: `synthetic session ${i} doing some work`,
      provider: i % 2 === 0 ? "codex" : "claude",
      created_at_ms: 1767600000000 + i * 1000,
      child_count: 0,
      depth: 0,
    });
  }
  let es = [];
  for (let i = 1; i <= HUB && i < NODES; i++) es.push({ parent: "n0", child: `n${i}` });
  const rest = [];
  for (let i = HUB + 1; i < NODES; i++) rest.push(i);
  if (rest.length) {
    const perLevel = Math.ceil(rest.length / MAX_DEPTH);
    let prev = [0];
    let cur = 0;
    for (let lvl = 0; lvl < MAX_DEPTH && cur < rest.length; lvl++) {
      const here = rest.slice(cur, cur + perLevel);
      cur += here.length;
      for (let k = 0; k < here.length; k++) es.push({ parent: `n${prev[k % prev.length]}`, child: `n${here[k]}` });
      prev = here;
    }
  }

  // --- the transform under test -------------------------------------------------------
  let visibleNodes = ns;
  if (threshold !== null) {
    const childrenOf = new Map();
    for (const e of es) {
      if (!childrenOf.has(e.parent)) childrenOf.set(e.parent, []);
      childrenOf.get(e.parent).push(e.child);
    }
    const dropped = new Set();
    const kept = [];
    const extra = [];
    for (const [parent, kids] of childrenOf) {
      if (kids.length <= threshold) {
        for (const k of kids) kept.push({ parent, child: k });
        continue;
      }
      for (const k of kids.slice(0, threshold)) kept.push({ parent, child: k });
      for (const k of kids.slice(threshold)) dropped.add(k);
      const placeholder = `more__${parent}`;
      extra.push({ id: placeholder, title: `+${kids.length - threshold} more`, provider: "",
                   created_at_ms: null, child_count: 0, depth: 0 });
      kept.push({ parent, child: placeholder });
    }
    // A dropped node's own descendants go with it — that is what "collapsed" means.
    let changed = true;
    while (changed) {
      changed = false;
      for (const e of kept) {
        if (dropped.has(e.parent) && !dropped.has(e.child)) {
          dropped.add(e.child);
          changed = true;
        }
      }
    }
    es = kept.filter((e) => !dropped.has(e.parent) && !dropped.has(e.child));
    visibleNodes = ns.filter((n) => !dropped.has(n.id)).concat(extra);
  }

  const input = { nodes: visibleNodes, edges: es };
  const graph = layoutMod.buildElkGraph(input);

  // ONE warm engine, reused across cases — the same bug the sibling harness already had and
  // that I failed to carry across. A fresh ElkLayoutEngine per case spawns a fresh Web Worker,
  // and folding that startup into every measurement produced a NON-MONOTONIC table: 100
  // children at 1844ms but 50 at 3183ms and 25 at 3582ms. Smaller graphs taking longer is a
  // detector fault, and it also swung the 200 case from 2130ms to 8337ms between runs.
  if (!globalThis.__collapseEngine) {
    globalThis.__collapseEngine = new elkMod.ElkLayoutEngine(budgetMs);
    const warm = layoutMod.buildElkGraph({
      nodes: [{ id: "w", title: "warm", provider: "codex", created_at_ms: 1, child_count: 0, depth: 0 }],
      edges: [],
    });
    try {
      await globalThis.__collapseEngine.layout(warm);
    } catch {
      /* a warm-up failure shows up in the real cases below */
    }
  }
  const engine = globalThis.__collapseEngine;

  const t = performance.now();
  let error = null;
  let placed = 0;
  try {
    const res = await engine.layout(graph);
    placed = (res.children || []).length;
  } catch (e) {
    error = `${e && e.name}`.includes("Timeout") ? `>${budgetMs}ms` : String(e && e.message).slice(0, 50);
  }
  // A timeout terminates the worker, so a later case would run against a dead engine. Drop the
  // cached one after any error and let the next case build a live one.
  if (error) {
    try {
      engine.terminate();
    } catch {
      /* the guard already terminated it */
    }
    globalThis.__collapseEngine = undefined;
  }

  return {
    visible: visibleNodes.length,
    edges: es.length,
    ms: Math.round(performance.now() - t),
    placed,
    error,
  };
};

const main = async () => {
  const { chromium } = requireFromSkill("playwright");
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForSelector("#app");

  console.log("  corpus: 12,791 nodes, one 4,844-child hub, depth 3 — the measured real shape");
  console.log("  layout: the app's own `layered` config. Guard is 8000ms.\n");
  console.log(`  each threshold measured ${REPEATS}x; MEDIAN reported, spread shown\n`);
  console.log("  max children  visible   edges    median      spread     result");
  console.log("  ------------ --------- -------- --------- ------------- --------------------");

  const results = [];
  for (const threshold of THRESHOLDS) {
    const samples = [];
    let last = null;
    for (let i = 0; i < REPEATS; i++) {
      let r;
      try {
        r = await page.evaluate(RUN, { threshold, budgetMs: 20000 });
      } catch (e) {
        r = { visible: -1, edges: -1, ms: -1, placed: 0, error: String(e).slice(0, 45) };
      }
      last = r;
      samples.push(r);
    }
    const times = samples.map((s) => s.ms).sort((a, b) => a - b);
    const median = times[Math.floor(times.length / 2)];
    const errored = samples.filter((s) => s.error).length;
    const r = { ...last, ms: median };
    results.push({ threshold, ...r, errored });

    const label = threshold === null ? "none (base)" : String(threshold);
    // Only "ok" when EVERY repeat fit the guard — a threshold that passes 2 of 3 is not safe
    // to recommend, and reporting the median alone would hide that.
    const ok = errored === 0 && times[times.length - 1] < 8000;
    const verdict = errored === REPEATS ? `FAILED (${errored}/${REPEATS})`
      : errored > 0 ? `UNSTABLE (${errored}/${REPEATS} failed)`
        : ok ? `ok (${r.placed} placed)` : "over the 8s guard";
    console.log(
      `  ${label.padEnd(12)} ${String(r.visible).padStart(9)} ${String(r.edges).padStart(8)} ` +
      `${String(median).padStart(7)}ms  ${String(times[0]).padStart(5)}-${String(times[times.length - 1]).padEnd(6)} ${verdict}`,
    );
  }

  await browser.close();
  console.log();

  const ran = results.filter((r) => !r.error || r.error.startsWith(">"));
  if (ran.length === 0) {
    console.log(`  every case failed WITHOUT laying out — harness failure. First: ${results[0].error}`);
    console.log("FAILED:layout-collapse no conclusion available");
    process.exit(2);
  }

  // A winner must have passed EVERY repeat, not just the median one.
  const wins = results.filter((r) => r.errored === 0 && r.ms < 8000 && r.threshold !== null);
  if (!wins.length) {
    console.log("  collapsing fan-out did NOT bring layout inside the guard at any threshold.");
    console.log("FAILED:layout-collapse the proposed fix is insufficient on its own");
    process.exit(1);
  }
  // The most GENEROUS threshold that works — collapsing less means showing the user more.
  const best = wins.reduce((a, b) => (a.threshold > b.threshold ? a : b));
  console.log(`  HIGHEST WORKING THRESHOLD: ${best.threshold} children`);
  console.log(`  -> ${best.visible} visible nodes, laid out in ${best.ms}ms`);
  console.log("SUCCESS:layout-collapse collapsing wide fan-outs brings the real corpus in budget");
};

main().catch((e) => {
  console.log(`FAILED:layout-collapse ${e.message}`);
  process.exit(2);
});
