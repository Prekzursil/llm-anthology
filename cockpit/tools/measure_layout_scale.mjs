/**
 * Can the spawn-graph layout survive a REAL corpus?
 *
 * Measured facts this exists to settle: the Claude Code adapter yields ~12,791 graph nodes with
 * ONE node having 4,844 children, and `ElkLayoutEngine` guards `elk.layout` with a hard
 * `DEFAULT_LAYOUT_TIMEOUT_MS = 8000` that throws `LayoutTimeoutError` on expiry. Whether the
 * app can render that corpus is therefore a single empirical question, and the owner has just
 * chosen a layout where the graph is permanently on screen — so it is not a corner they can
 * avoid by switching tabs.
 *
 * This drives the app's OWN `buildElkGraph` + `ElkLayoutEngine` inside a real browser (the
 * engine runs ELK in a Web Worker, which does not exist under node), against synthetic graphs
 * shaped like the real corpus. No app code is modified and no real data is read — the graphs
 * are generated here.
 *
 * Usage: node measure_layout_scale.mjs <vite-url>
 */
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { homedir } from "node:os";
import { join } from "node:path";

const SKILL = join(homedir(), ".claude", "skills", "ui-audit", "package.json");
const requireFromSkill = createRequire(pathToFileURL(SKILL));

const url = process.argv[2] ?? "http://localhost:5199";

/**
 * Shapes worth measuring, smallest first so a catastrophic case does not hide the threshold.
 * `hub` gives ONE node that many children — the 4,844 case is the real corpus's worst node.
 */
const CASES = [
  { name: "current mock", nodes: 16, hub: 0 },
  { name: "codex-real", nodes: 2043, hub: 0 },
  { name: "codex+grok", nodes: 2112, hub: 0 },
  // The DISCRIMINATOR. 4845-with-a-hub timed out while 2112-without one did not, which leaves
  // two very different causes: raw node COUNT, or the single 4,844-child FAN-OUT. They need
  // opposite fixes — general scaling versus hub-specific handling — so the same node counts are
  // run both with and without the hub. Whichever variable moves the result is the cause.
  { name: "4845 wide (no hub)", nodes: 4845, hub: 0 },
  { name: "4845 HUB", nodes: 4845, hub: 4844 },
  { name: "12791 wide (no hub)", nodes: 12791, hub: 0 },
  { name: "12791 + HUB (real)", nodes: 12791, hub: 4844 },
  // Where exactly does fan-out break? A threshold turns "the hub is too wide" into a number
  // an aggregation rule can be written against.
  { name: "hub 500", nodes: 2500, hub: 500 },
  { name: "hub 1000", nodes: 2500, hub: 1000 },
  { name: "hub 2000", nodes: 2500, hub: 2000 },
];

/**
 * Build a synthetic {nodes, edges} of a given size with an optional wide hub, then run the
 * app's real layout pipeline against it and time each stage separately — a slow BUILD and a
 * slow LAYOUT need different fixes.
 */
const RUN = async ({ nodes, hub }) => {
  const layoutMod = await import("/src/graph/layout.ts");
  const elkMod = await import("/src/graph/elkLayout.ts");

  const ns = [];
  for (let i = 0; i < nodes; i++) {
    ns.push({
      id: `n${i}`,
      // Deterministic, and long enough to exercise the label-width sizing.
      title: `synthetic session ${i} doing some work`,
      provider: i % 2 === 0 ? "codex" : "claude",
      created_at_ms: 1767600000000 + i * 1000,
      child_count: 0,
      depth: 0,
    });
  }
  const es = [];
  // The hub: one node with `hub` children — the shape the real corpus actually has.
  for (let i = 1; i <= hub && i < nodes; i++) es.push({ parent: "n0", child: `n${i}` });

  // The REST must be WIDE AND SHALLOW, not a chain. An earlier version of this generator
  // linked every remaining node to its predecessor, producing a graph thousands of levels
  // deep — and it duly reported "RangeError: Maximum call stack size exceeded", a finding
  // about a shape no real corpus has. The measured corpus is depth 3 (max over a 2000-node
  // sample), so a fan-out tree of that depth is the faithful shape and the earlier stack
  // overflow was an artifact of the harness, not a property of the app.
  const MAX_DEPTH = 3;
  const rest = [];
  for (let i = hub + 1; i < nodes; i++) rest.push(i);
  if (rest.length > 0) {
    // Spread `rest` over MAX_DEPTH levels, each node parented to one on the level above.
    const perLevel = Math.ceil(rest.length / MAX_DEPTH);
    let prevLevel = [0];
    let cursor = 0;
    for (let level = 0; level < MAX_DEPTH && cursor < rest.length; level++) {
      const thisLevel = rest.slice(cursor, cursor + perLevel);
      cursor += thisLevel.length;
      for (let k = 0; k < thisLevel.length; k++) {
        const parent = prevLevel[k % prevLevel.length];
        es.push({ parent: `n${parent}`, child: `n${thisLevel[k]}` });
      }
      prevLevel = thisLevel;
    }
  }

  const input = { nodes: ns, edges: es };

  const t0 = performance.now();
  const elkGraph = layoutMod.buildElkGraph(input);
  const tBuild = performance.now() - t0;

  // ONE engine, reused — matching the app, which constructs it once in CockpitApp's
  // constructor. A fresh engine per case spawns a fresh Web Worker, and measuring that
  // startup as though it were layout time produced a nonsensical 3772ms for SIXTEEN nodes.
  // The first call still pays worker spawn, so the harness warms it before timing.
  if (!globalThis.__scaleEngine) {
    globalThis.__scaleEngine = new elkMod.ElkLayoutEngine();
    const warm = layoutMod.buildElkGraph({
      nodes: [{ id: "w", title: "warm", provider: "codex", created_at_ms: 1, child_count: 0, depth: 0 }],
      edges: [],
    });
    try {
      await globalThis.__scaleEngine.layout(warm);
    } catch {
      /* a warm-up failure is reported by the real cases below */
    }
  }
  const engine = globalThis.__scaleEngine;

  const t1 = performance.now();
  let laidOut = null;
  let error = null;
  try {
    laidOut = await engine.layout(elkGraph);
  } catch (e) {
    error = `${e && e.name}: ${e && e.message}`;
  }
  const tLayout = performance.now() - t1;

  let tExtract = 0;
  let positioned = 0;
  if (laidOut) {
    const t2 = performance.now();
    const pg = layoutMod.extractLayout(laidOut, input);
    tExtract = performance.now() - t2;
    positioned = pg.nodes.length;
  }
  // A LayoutTimeoutError terminates the worker (see ElkLayoutEngine), so a later case would
  // silently run against a dead engine and report a misleading failure. Drop the cached one
  // after any error so the next case builds a fresh, live worker.
  if (error) {
    try {
      engine.terminate();
    } catch {
      /* the guard already terminated it */
    }
    globalThis.__scaleEngine = undefined;
  }

  return {
    edges: es.length,
    buildMs: Math.round(tBuild),
    layoutMs: Math.round(tLayout),
    extractMs: Math.round(tExtract),
    positioned,
    error,
  };
};

const main = async () => {
  const { chromium } = requireFromSkill("playwright");
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const consoleErrors = [];
  page.on("pageerror", (e) => consoleErrors.push(String(e)));
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForSelector("#app");

  console.log("  case                nodes    edges   build   layout  extract  result");
  console.log("  ----------------- ------- -------- ------- -------- -------- ------------------");
  let firstFailure = null;
  for (const c of CASES) {
    let r;
    try {
      // Generous per-case cap: the app's own guard is 8s, so anything beyond that is already a
      // failure for the app even if the browser would eventually finish.
      r = await page.evaluate(RUN, { nodes: c.nodes, hub: c.hub });
    } catch (e) {
      r = { edges: -1, buildMs: -1, layoutMs: -1, extractMs: -1, positioned: 0, error: String(e).slice(0, 90) };
    }
    const verdict = r.error ? `FAILED ${r.error}`.slice(0, 40) : `ok (${r.positioned} placed)`;
    console.log(
      `  ${c.name.padEnd(17)} ${String(c.nodes).padStart(7)} ${String(r.edges).padStart(8)} ` +
      `${String(r.buildMs).padStart(6)}ms ${String(r.layoutMs).padStart(6)}ms ` +
      `${String(r.extractMs).padStart(6)}ms  ${verdict}`,
    );
    if (r.error && !firstFailure) firstFailure = c.name;
  }

  if (consoleErrors.length) {
    console.log(`\n  page errors during the run: ${consoleErrors.length}`);
    for (const e of consoleErrors.slice(0, 3)) console.log(`    ${e.slice(0, 140)}`);
  }

  await browser.close();
  console.log();
  if (firstFailure) {
    console.log(`  FIRST FAILING SHAPE: ${firstFailure}`);
    console.log("FAILED:layout-scale the real corpus cannot be laid out by the current pipeline");
    process.exit(1);
  }
  console.log("SUCCESS:layout-scale every measured shape laid out within the app's own guard");
};

main().catch((e) => {
  console.log(`FAILED:layout-scale ${e.message}`);
  process.exit(2);
});
