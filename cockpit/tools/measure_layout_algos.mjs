/**
 * Is the graph-layout timeout an ARCHITECTURE problem or a CONFIG problem?
 *
 * Established by `measure_layout_scale.mjs`: the real corpus shape (wide, shallow, depth 3,
 * with a 4,844-child hub) blows the app's 8s guard, while the same node count as a deep chain
 * lays out in ~2.8s. Before anyone rewrites the renderer, it is worth knowing whether a
 * different ELK algorithm or a cheaper option set already fits inside the budget — that is the
 * difference between a one-line change and a rebuild.
 *
 * ELK's `layered` pipeline spends most of its time in crossing minimisation and node placement,
 * both of which scale badly with layer WIDTH — which is exactly this corpus's shape. Those are
 * the knobs tested here, alongside `mrtree`, which is purpose-built for trees.
 *
 * Runs against the app's own `buildElkGraph` output so the node sizes and edge set are real;
 * only the layout options differ. Same-load comparison, so the RANKING is meaningful even
 * though absolute milliseconds are not.
 *
 * Usage: node measure_layout_algos.mjs <vite-url> [nodeCount] [hubSize]
 */
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { homedir } from "node:os";
import { join } from "node:path";

const SKILL = join(homedir(), ".claude", "skills", "ui-audit", "package.json");
const requireFromSkill = createRequire(pathToFileURL(SKILL));

const url = process.argv[2] ?? "http://localhost:5199";
const NODES = Number(process.argv[3] ?? 12791);
const HUB = Number(process.argv[4] ?? 4844);

/**
 * Candidate configurations, cheapest-looking last so an early win is obvious.
 * `null` options means "whatever buildElkGraph already sets" — the current behaviour.
 */
const CONFIGS = [
  { name: "current (layered default)", opts: null },
  { name: "layered thoroughness=1", opts: { "elk.layered.thoroughness": "1" } },
  { name: "layered xing=NONE", opts: { "elk.layered.crossingMinimization.strategy": "NONE" } },
  { name: "layered place=SIMPLE", opts: { "elk.layered.nodePlacement.strategy": "SIMPLE" } },
  {
    name: "layered xing=NONE+SIMPLE",
    opts: {
      "elk.layered.crossingMinimization.strategy": "NONE",
      "elk.layered.nodePlacement.strategy": "SIMPLE",
      "elk.layered.thoroughness": "1",
    },
  },
  { name: "mrtree", opts: { "elk.algorithm": "mrtree" } },
  { name: "radial", opts: { "elk.algorithm": "radial" } },
  { name: "rectpacking", opts: { "elk.algorithm": "rectpacking" } },
  { name: "box", opts: { "elk.algorithm": "box" } },
];

const RUN = async ({ nodes, hub, opts, budgetMs }) => {
  const layoutMod = await import("/src/graph/layout.ts");
  // The app's OWN engine, not a bare `elkjs` import — vite does not resolve a bare specifier
  // for a dynamic import from page context, and an earlier version of this file failed every
  // case with "Failed to resolve" while still printing a verdict about layout performance.
  // Its constructor takes the timeout, so the budget is set here rather than raced separately.
  const elkMod = await import("/src/graph/elkLayout.ts");

  const ns = [];
  for (let i = 0; i < nodes; i++) {
    ns.push({
      id: `n${i}`,
      title: `synthetic session ${i} doing some work`,
      provider: i % 2 === 0 ? "codex" : "claude",
      created_at_ms: 1767600000000 + i * 1000,
      child_count: 0,
      depth: 0,
    });
  }
  const es = [];
  for (let i = 1; i <= hub && i < nodes; i++) es.push({ parent: "n0", child: `n${i}` });
  const MAX_DEPTH = 3;
  const rest = [];
  for (let i = hub + 1; i < nodes; i++) rest.push(i);
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

  const graph = layoutMod.buildElkGraph({ nodes: ns, edges: es });
  if (opts) graph.layoutOptions = { ...graph.layoutOptions, ...opts };

  const engine = new elkMod.ElkLayoutEngine(budgetMs);
  const t = performance.now();
  let error = null;
  let placed = 0;
  try {
    const res = await engine.layout(graph);
    placed = (res.children || []).length;
  } catch (e) {
    error = `${e && e.name}`.includes("Timeout") ? `>${budgetMs}ms` : String(e && e.message).slice(0, 60);
  }
  try {
    engine.terminate();
  } catch {
    /* the guard may already have terminated it */
  }
  return { ms: Math.round(performance.now() - t), placed, error, edges: es.length };
};

const main = async () => {
  const { chromium } = requireFromSkill("playwright");
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForSelector("#app");

  console.log(`  graph: ${NODES} nodes, hub of ${HUB}, depth 3 (the measured corpus shape)`);
  console.log(`  the app's own guard is 8000ms; a config is only useful well under it\n`);
  console.log("  configuration                 layout    placed   verdict");
  console.log("  --------------------------- --------- --------- ------------------");

  const wins = [];
  const results = [];
  for (const c of CONFIGS) {
    let r;
    try {
      r = await page.evaluate(RUN, { nodes: NODES, hub: HUB, opts: c.opts, budgetMs: 20000 });
    } catch (e) {
      r = { ms: -1, placed: 0, error: String(e).slice(0, 50), edges: -1 };
    }
    results.push(r);
    const ok = !r.error && r.ms < 8000;
    if (ok) wins.push({ name: c.name, ms: r.ms });
    const verdict = r.error ? `FAILED ${r.error}` : ok ? "WITHIN BUDGET" : "over the 8s guard";
    console.log(
      `  ${c.name.padEnd(27)} ${String(r.ms).padStart(6)}ms ${String(r.placed).padStart(9)}   ${verdict}`,
    );
  }

  await browser.close();
  console.log();

  // A verdict about LAYOUT requires that layout actually ran. An earlier version of this file
  // failed every case on a module-resolution error and still printed "the fix is
  // architectural" — a conclusion drawn entirely from its own broken instrument. If nothing
  // timed out and nothing succeeded, the harness is what failed, and it must say so.
  const timedOut = results.filter((r) => r.error && r.error.startsWith(">")).length;
  const succeeded = results.filter((r) => !r.error).length;
  if (succeeded === 0 && timedOut === 0) {
    console.log("  every case failed WITHOUT running a layout — this is a harness failure.");
    console.log(`  first error: ${results[0] && results[0].error}`);
    console.log("FAILED:layout-algos the harness is broken; no conclusion about layout is available");
    process.exit(2);
  }

  if (wins.length) {
    wins.sort((a, b) => a.ms - b.ms);
    console.log(`  FASTEST WITHIN BUDGET: ${wins[0].name} at ${wins[0].ms}ms`);
    console.log("SUCCESS:layout-algos a configuration exists that fits the current guard");
  } else {
    console.log("  NO configuration fitted the 8s guard at this scale.");
    console.log("FAILED:layout-algos config alone does not solve it — the fix is architectural");
    process.exit(1);
  }
};

main().catch((e) => {
  console.log(`FAILED:layout-algos ${e.message}`);
  process.exit(2);
});
