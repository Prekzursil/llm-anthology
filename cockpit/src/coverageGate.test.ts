/**
 * The cockpit coverage gate must actually MEASURE something.
 *
 * ── THE BUG THIS FILE EXISTS TO CATCH ─────────────────────────────────────────────────
 *
 * `cockpit/package.json`'s `test` script was `vitest run` — no `--coverage` — and
 * `vitest.config.ts` carried no `coverage` block at all. `.github/workflows/ci.yml:273`
 * runs exactly `npm test`, so the blocking `cockpit` CI job measured **zero** coverage and
 * reported green for it. Nothing regressed; nothing was ever watched.
 *
 * ── THE STRUCTURAL REASON THE TWO RAILS DISAGREE ──────────────────────────────────────
 *
 * This is not a spelling mistake, it is two tools with OPPOSITE defaults, and the Python
 * rail's default is the safe one:
 *
 *   - Python: `pyproject.toml:107-109` sets `[tool.coverage.run] source = ["llm_anthology"]`.
 *     `source` is a PACKAGE root, so coverage.py walks it on disk and every module in the
 *     package lands in the denominator — an unimported module reports 0% and DRAGS THE
 *     TOTAL DOWN. A dark module cannot hide.
 *   - vitest/v8: with no `coverage.include`, the denominator is only the files the test run
 *     actually LOADED. An unimported module is not 0%, it is ABSENT — so adding a dark
 *     module RAISES the reported percentage. The default silently rewards not testing.
 *
 * Measured on this repo, same suite, same commit, isolated report dirs:
 *
 *   | coverage.include        | statements                  | files |
 *   |-------------------------|-----------------------------|-------|
 *   | (vitest default)        | 99.68%  (2863/2872)         | 29    |
 *   | ["src/**\/*.ts"]        | 71.71%  (2863/3992)         | 34    |
 *
 * Identical numerator (2863), denominator +1120. That 1120 is EXACTLY the sum of the eight
 * modules no test imports: `app.ts` 352, `graph/canvas.ts` 241, `ui/maintenanceShell.ts`
 * 198, `ui/reader.ts` 157, `ui/search.ts` 85, `ui/workspace.ts` 58, `graph/elkLayout.ts` 19,
 * `main.ts` 10. So the 28-point gap is not noise or instrumentation overhead — it is the
 * dark set, named and added up.
 *
 * `js/vitest.config.ts:6-20` is the honest twin already in this repo: it pins
 * `include: ["src/**\/*.ts"]` and gates on thresholds. This file holds the cockpit to the
 * same contract.
 *
 * ── WHY THE THRESHOLDS ARE NOT 100 ────────────────────────────────────────────────────
 *
 * They are pinned at the MEASURED values, so the gate is a ratchet that can only be
 * raised. An unreachable 100 would be red on the very first CI run, and the cheapest way
 * out of a permanently-red required check is to delete the gate — which is how the repo
 * arrived at zero measurement in the first place. Raising these numbers is the job of the
 * units that add the missing DOM tests; this unit only makes the number exist.
 *
 * The `toBeGreaterThanOrEqual` assertions below are deliberately one-directional: they let
 * a future agent RAISE a threshold freely, and go red if one is LOWERED to bury a
 * regression.
 */
import { describe, expect, it } from "vitest";

import packageJson from "../package.json";
import vitestConfig from "../vitest.config";

/**
 * The floors measured 2026-08-12 against 1101 passing tests in 33 files, via
 * `npx vitest run --coverage --coverage.include='src/**\/*.ts'` with the two excludes
 * below and an isolated `--coverage.reportsDirectory` (other agents share this tree, and a
 * shared report dir is how two runs quietly overwrite each other's answer).
 *
 * These are the values v8's reporter PRINTS, which it derives by truncating at two
 * decimals rather than rounding — e.g. statements are really 71.7184% and print as 71.71,
 * functions really 77.1953% and print as 77.19. Truncation means every printed value sits
 * at or just below its raw value, so pinning the printed number leaves a hair of margin
 * instead of balancing on a knife edge.
 */
const MEASURED = {
  statements: 71.71,
  branches: 80.67,
  functions: 77.19,
  lines: 70.95,
} as const;

/**
 * `vitestConfig.test.coverage` is typed as a union across the v8 / istanbul / custom
 * providers, and `include`/`exclude`/`thresholds` are not all present on every arm of it.
 * The assertions below are about RUNTIME values, so the union is narrowed once, here,
 * through a structural shape rather than sprinkling casts through the cases.
 */
interface CoverageShape {
  provider?: string;
  include?: string[];
  exclude?: string[];
  thresholds?: Record<string, number>;
}

const coverage = vitestConfig.test?.coverage as CoverageShape | undefined;

describe("cockpit coverage gate", () => {
  it("runs coverage in the script CI actually invokes", () => {
    // `.github/workflows/ci.yml:273` is `npm test` and nothing else, so this string is
    // the whole difference between a measured gate and a decorative one.
    expect(packageJson.scripts.test).toContain("--coverage");
  });

  it("declares a coverage block", () => {
    // The control for every case below: without this, each one would be asserting
    // `undefined?.x === undefined` and would pass while measuring nothing.
    expect(coverage).toBeDefined();
  });

  it("uses the v8 provider that is actually installed", () => {
    // `@vitest/coverage-v8` is the devDependency present in package.json; naming
    // `istanbul` here would fail at run time with a missing-package error.
    expect(coverage?.provider).toBe("v8");
  });

  it("puts every src module in the denominator, not just the imported ones", () => {
    // The heart of the fix. Dropping this line restores the 99.68% fiction.
    expect(coverage?.include).toEqual(["src/**/*.ts"]);
  });

  it("keeps the test files and the ambient type declaration out of the denominator", () => {
    // `include: src/**/*.ts` sweeps in `*.test.ts` and `vite-env.d.ts` too, and setting
    // `exclude` REPLACES vitest's defaults rather than adding to them — so both have to
    // be named explicitly or the gate measures its own test suite.
    const exclude = coverage?.exclude ?? [];
    expect(exclude).toContain("src/**/*.test.ts");
    expect(exclude).toContain("src/vite-env.d.ts");
  });

  it("gates on all four metrics at or above the measured floor", () => {
    const thresholds = coverage?.thresholds ?? {};
    for (const [metric, floor] of Object.entries(MEASURED)) {
      expect(thresholds[metric]).toBeGreaterThanOrEqual(floor);
      // A threshold above 100 can never be met, which is the disable-the-gate trap
      // described in the docblock.
      expect(thresholds[metric]).toBeLessThanOrEqual(100);
    }
  });

  it("still collects the same test files as before", () => {
    // Guards the blast radius: this unit was scoped to ADD measurement, not to change
    // which tests run. If `test.include` drifts, the coverage numerator moves for a
    // reason that has nothing to do with coverage.
    expect(vitestConfig.test?.include).toEqual(["src/**/*.test.ts"]);
    expect(vitestConfig.test?.environment).toBe("node");
  });
});
