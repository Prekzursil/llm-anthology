import { defineConfig } from "vitest/config";

/**
 * Standalone from vite.config.ts (which carries Tauri dev-server settings).
 *
 * ── DOM ENVIRONMENT: node by default, `happy-dom` per file ────────────────────────────
 *
 * The default stays `node`. Files that need a DOM opt in with a docblock on line 1:
 *
 *     // @vitest-environment happy-dom
 *
 * Three more modules (`app.ts`, `ui/reader.ts`, `ui/search.ts`) are expected to follow
 * that pattern, so the measurements behind it are recorded here rather than re-derived.
 * Measured 2026-08-08, vitest 4.1.10 / node 24.16.0 / happy-dom 20.11.2 / jsdom 30.0.1.
 *
 * WHY NOT A GLOBAL DOM ENVIRONMENT. Because it is not merely wasteful — it BREAKS this
 * suite. `--environment jsdom` and `--environment happy-dom` were each run against the
 * whole 756-test suite, and both took FOUR files down and dropped ~100 tests out of
 * collection entirely (jsdom 652 collected, happy-dom 662, against 756 under node):
 *
 *   - `graph/palette.test.ts`, `ipc/mock.test.ts` — `Error: Denied ID …/discover.py?raw`.
 *     These import `llm_anthology/*.py?raw`. Under a client (DOM) environment vite
 *     enforces `server.fs.allow`, which this config does not carry, so the import is
 *     refused. Under `node` the same import resolves.
 *   - `devServerFsAllow.test.ts` — `Invariant violation: "new TextEncoder().encode("")
 *     instanceof Uint8Array" is incorrectly false`. That test builds the vite config
 *     through esbuild, and a DOM environment swaps in its own realm's typed arrays.
 *   - `ui/corpusBar.test.ts` — has a test literally named "reads null under the node
 *     runner, where localStorage does not exist". A global DOM flip contradicts a
 *     premise the existing suite asserts on purpose.
 *
 * None of that is fixable from inside this file, so per-file opt-in is the only option
 * that does not require repairing four unrelated test files.
 *
 * WHY happy-dom OVER jsdom. Two measured reasons:
 *   - `ResizeObserver` is a global in happy-dom and ABSENT in jsdom (`ReferenceError`).
 *     `VirtualList`'s constructor and `graph/canvas.ts` both call `new ResizeObserver`
 *     unconditionally, so jsdom needs a stub before the class can even be built.
 *   - Per-file environment startup, 3 reps each, one test file in isolation:
 *     node 0ms · happy-dom 640/792/843ms · jsdom 1.10/2.09/2.19s. jsdom is ~2.6x
 *     happy-dom's setup cost for an identical probe.
 *
 * WHY NOT VITEST BROWSER MODE, despite being the only option with real layout: it needs
 * `@vitest/browser` + `playwright` + a downloaded browser binary, and the `cockpit` CI job
 * (.github/workflows/ci.yml) runs `npm ci → tsc → npm test → npm run build` with NO
 * `playwright install` step on either matrix leg, so `npm test` would fail on ubuntu AND
 * windows. Real-browser behaviour is already covered in this repo by the headless probes
 * in `cockpit/tools/` (`probe_focus_survives_repaint.mjs` covers exactly the VirtualList
 * repaint-focus path), which drive a real vite dev server. Browser mode would duplicate
 * that while adding a browser download to the unit gate.
 *
 * ── THE TRAP THAT MATTERS WHEN WRITING DOM TESTS ──────────────────────────────────────
 *
 * NEITHER happy-dom NOR jsdom DOES LAYOUT. Measured on a div with `style.height="300px"`
 * holding a 40000px child, identically in both:
 *
 *     clientHeight = 0 · getBoundingClientRect().height = 0 · scrollHeight = 0
 *
 * So any geometry-dependent assertion must INJECT its geometry
 * (`Object.defineProperty(el, "clientHeight", { value: 300, configurable: true })` —
 * verified to work in both) or it silently measures nothing. `VirtualList.paint` reads
 * `this.viewport.clientHeight || this.itemHeight`; left alone, a DOM test would always
 * take the fallback and a "virtualization works" assertion would prove only that the
 * fallback exists.
 *
 * `scrollTop` is the exception and is honest: assignment reads back verbatim in both
 * (`el.scrollTop = 500` → 500), though neither clamps it to the scrollable range the way
 * a browser would. `CSS.escape`, `closest`, `:not([disabled])` selectors,
 * `replaceChildren`, DocumentFragment, and range-input value sanitisation
 * (clamped to min/max) all behave identically in the two. `focus()` only moves
 * `document.activeElement` for an element ATTACHED to the document — a detached element
 * silently does not take focus in either, so DOM tests must append to `document.body`.
 *
 * ── COVERAGE: WHY `include` IS LOAD-BEARING AND NOT A TIDINESS PREFERENCE ─────────────
 *
 * This file carried NO `coverage` block, and `package.json`'s `test` script was
 * `vitest run` with no `--coverage`. Since `.github/workflows/ci.yml:273` runs exactly
 * `npm test`, the blocking `cockpit` job measured ZERO coverage and reported green for it.
 *
 * The reason that failure is silent rather than loud is that the two rails in this repo
 * have OPPOSITE defaults, and only the Python one is safe:
 *
 *   - Python (`pyproject.toml:107-109`) sets `[tool.coverage.run] source = ["llm_anthology"]`.
 *     `source` names a package ROOT, so coverage.py walks it on disk; a module no test
 *     imports still enters the denominator at 0% and drags the total DOWN.
 *   - vitest/v8 with no `coverage.include` builds the denominator from the files the run
 *     actually LOADED. A module no test imports is not 0%, it is ABSENT — so adding an
 *     untested module RAISES the reported percentage. The default rewards not testing.
 *
 * Measured 2026-08-12, same suite (1101 tests / 33 files), same commit, isolated
 * `--coverage.reportsDirectory` because concurrent agents share this tree:
 *
 *     (vitest default)  99.68% statements (2863/2872), 29 files
 *     include src ts    71.71% statements (2863/3992), 34 files
 *
 * Same numerator, denominator +1120 — and 1120 is EXACTLY the sum of the eight modules no
 * test imports: `app.ts` 352, `graph/canvas.ts` 241, `ui/maintenanceShell.ts` 198,
 * `ui/reader.ts` 157, `ui/search.ts` 85, `ui/workspace.ts` 58, `graph/elkLayout.ts` 19,
 * `main.ts` 10. The 28-point gap is that dark set, named and added up — not overhead.
 *
 * `js/vitest.config.ts:6-20` is the honest twin already in the repo and this mirrors it.
 * `src/coverageGate.test.ts` asserts every key below, so deleting one goes red.
 */
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
    coverage: {
      // `@vitest/coverage-v8` is the installed devDependency; `istanbul` would need a
      // package that is not here.
      provider: "v8",
      // The whole fix. Without this the denominator is only what the run imported, and
      // the eight dark modules above vanish from it instead of counting as 0%.
      include: ["src/**/*.ts"],
      // `include` above also sweeps in the test files and the ambient declaration, and
      // setting `exclude` REPLACES vitest's defaults rather than extending them — so both
      // must be named here or the gate measures its own suite.
      exclude: ["src/**/*.test.ts", "src/vite-env.d.ts"],
      // A RATCHET pinned at the values measured above, not an aspirational 100. An
      // unreachable threshold is red on its first CI run, and the cheapest escape from a
      // permanently-red required check is to delete the gate — which is precisely how
      // this repo ended up measuring nothing. These may only be raised: the units that
      // add the missing DOM tests own that, and `coverageGate.test.ts` fails if one is
      // lowered. The numbers are v8's PRINTED values, which it truncates at two decimals
      // (statements are really 71.7184%), so each floor sits a hair below its raw value
      // rather than balancing on a knife edge.
      thresholds: {
        statements: 71.71,
        branches: 80.67,
        functions: 77.19,
        lines: 70.95,
      },
      reporter: ["text-summary", "text"],
    },
  },
});
