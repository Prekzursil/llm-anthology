/**
 * Does the SHIPPED `capFanOut` actually make the real corpus renderable?
 *
 * `measure_layout_collapse.mjs` established the numbers, but it collapsed the graph with a
 * transform written INSIDE the harness. That answers "would a fix of this shape work", not
 * "does the fix I wrote work" — and the two differ in ways that matter (the shipped one counts
 * the placeholder inside the budget, drops subtrees by reachability rather than by a
 * fixed-point loop over kept edges, and keeps DAG children that survive via another parent).
 * Citing that table as verification of this code would be exactly the "compared it to itself"
 * mistake.
 *
 * So this imports `/src/graph/capFanOut.ts` — the module the app actually calls — and runs the
 * app's own `buildElkGraph` + `ElkLayoutEngine` over its output.
 *
 * It also runs the UNCAPPED case as a control. If the harness cannot make the corpus fail, its
 * success verdict measures nothing.
 *
 * Usage: node verify_capfanout.mjs <vite-url>
 */
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { homedir } from "node:os";
import { join } from "node:path";

const SKILL = join(homedir(), ".claude", "skills", "ui-audit", "package.json");
const requireFromSkill = createRequire(pathToFileURL(SKILL));

const url = process.argv[2] ?? "http://localhost:5199";

/** The app's own guard (`elkLayout.ts` DEFAULT_LAYOUT_TIMEOUT_MS). */
const GUARD_MS = 8000;
/** Measured with a longer budget so a failure reports HOW far over, not just "over". */
const BUDGET_MS = 20000;
const REPEATS = 3;

const RUN = async ({ cap, budgetMs }) => {
  const layoutMod = await import("/src/graph/layout.ts");
  const elkMod = await import("/src/graph/elkLayout.ts");
  const capMod = await import("/src/graph/capFanOut.ts");

  // The measured real shape: 12,791 nodes, one 4,844-child hub, max depth 3.
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
  const es = [];
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
      for (let k = 0; k < here.length; k++) {
        es.push({ parent: `n${prev[k % prev.length]}`, child: `n${here[k]}` });
      }
      prev = here;
    }
  }

  // --- the SHIPPED transform, not a local copy of it ---------------------------------
  const tCap = performance.now();
  const view = cap ? capMod.capFanOut({ nodes: ns, edges: es }) : { nodes: ns, edges: es };
  const capMs = Math.round(performance.now() - tCap);

  // The invariant the whole fix rests on, checked on the real output rather than assumed.
  const childCount = new Map();
  for (const e of view.edges) childCount.set(e.parent, (childCount.get(e.parent) ?? 0) + 1);
  const widest = childCount.size ? Math.max(...childCount.values()) : 0;

  if (!globalThis.__capEngine) {
    globalThis.__capEngine = new elkMod.ElkLayoutEngine(budgetMs);
    const warm = layoutMod.buildElkGraph({
      nodes: [{ id: "w", title: "warm", provider: "codex", created_at_ms: 1, child_count: 0, depth: 0 }],
      edges: [],
    });
    try {
      await globalThis.__capEngine.layout(warm);
    } catch {
      /* surfaces in the real cases below */
    }
  }
  const engine = globalThis.__capEngine;

  const graph = layoutMod.buildElkGraph(view);
  const t = performance.now();
  let error = null;
  let placed = 0;
  try {
    placed = ((await engine.layout(graph)).children || []).length;
  } catch (e) {
    error = `${e && e.name}`.includes("Timeout")
      ? `>${budgetMs}ms`
      : String(e && e.message).slice(0, 60);
  }
  if (error) {
    // A timeout terminates the worker; a cached dead engine would poison the next case.
    try {
      engine.terminate();
    } catch {
      /* the guard already terminated it */
    }
    globalThis.__capEngine = undefined;
  }

  return {
    visible: view.nodes.length,
    edges: view.edges.length,
    widest,
    threshold: capMod.DEFAULT_MAX_CHILDREN,
    capMs,
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

  console.log("  corpus: 12,791 nodes, one 4,844-child hub, depth 3 (the measured real shape)");
  console.log(`  transform: the SHIPPED src/graph/capFanOut.ts. Guard is ${GUARD_MS}ms.\n`);

  // CONTROL FIRST. If the uncapped corpus does not fail here, nothing this harness says
  // about the capped one means anything.
  const control = await page.evaluate(RUN, { cap: false, budgetMs: BUDGET_MS });
  const controlFailed = Boolean(control.error);
  console.log(`  control (no cap): ${control.visible} nodes, widest layer ${control.widest} `
    + `-> ${control.error ?? `${control.ms}ms, ${control.placed} placed`}`);
  if (!controlFailed) {
    console.log("\n  The UNCAPPED corpus laid out fine, so this harness cannot detect the");
    console.log("  problem the cap exists to solve. Its verdict on the capped case is void.");
    console.log("FAILED:verify-capfanout control did not fail; harness proves nothing");
    await browser.close();
    process.exit(2);
  }
  console.log("  -> control fails as expected, so a pass below is meaningful\n");

  const samples = [];
  for (let i = 0; i < REPEATS; i++) samples.push(await page.evaluate(RUN, { cap: true, budgetMs: BUDGET_MS }));
  await browser.close();

  const times = samples.map((s) => s.ms).sort((a, b) => a - b);
  const median = times[Math.floor(times.length / 2)];
  const errored = samples.filter((s) => s.error).length;
  const last = samples[samples.length - 1];

  console.log(`  capped at DEFAULT_MAX_CHILDREN=${last.threshold}`);
  console.log(`    visible nodes  ${last.visible}   edges ${last.edges}`);
  console.log(`    widest layer   ${last.widest}  (must be <= ${last.threshold})`);
  console.log(`    transform cost ${last.capMs}ms`);
  console.log(`    layout         median ${median}ms  spread ${times[0]}-${times[times.length - 1]}ms`);
  console.log(`    placed         ${last.placed}`);
  console.log(`    hidden         ${12791 - last.visible} nodes behind "+N more"\n`);

  const problems = [];
  if (errored) problems.push(`${errored}/${REPEATS} repeats failed to lay out`);
  if (times[times.length - 1] >= GUARD_MS) {
    problems.push(`slowest repeat ${times[times.length - 1]}ms is over the ${GUARD_MS}ms guard`);
  }
  if (last.widest > last.threshold) {
    problems.push(`widest layer ${last.widest} exceeds the cap of ${last.threshold}`);
  }
  if (last.placed !== last.visible) {
    problems.push(`placed ${last.placed} of ${last.visible} nodes`);
  }
  if (problems.length) {
    for (const p of problems) console.log(`  PROBLEM: ${p}`);
    console.log("FAILED:verify-capfanout the shipped transform does not bring the corpus in budget");
    process.exit(1);
  }
  console.log("SUCCESS:verify-capfanout shipped capFanOut renders the real corpus inside the guard");
};

main().catch((e) => {
  console.log(`FAILED:verify-capfanout ${e.message}`);
  process.exit(2);
});
