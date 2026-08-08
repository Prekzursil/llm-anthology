/**
 * The dev server must not serve anything above `cockpit/`.
 *
 * Why this needs a test: the repo root holds a gitignored `.scratch/` working tree
 * (synthetic fixtures, probe scripts, notes) next to the engine and `.git`. Vite's dev
 * server exposes any allow-listed file over `/@fs/<abs-path>`, so the width of
 * `server.fs.allow` is the whole boundary.
 *
 * Today that boundary holds *by default*, but only as a side effect of a heuristic:
 * Vite's default is `searchForWorkspaceRoot(root)`, which walks upward looking for
 * `pnpm-workspace.yaml`, `lerna.json`, or a `package.json` carrying a `workspaces` key —
 * and deliberately NOT for `.git`. Add any one of those at the repo root and serving
 * silently widens to the entire repo with no edit to `vite.config.ts` and no warning.
 * That is the regression this file exists to catch; `vite.config.ts` pins `fs.allow`
 * explicitly so the boundary is stated rather than inferred.
 *
 * This asserts through `isFileServingAllowed`, the same predicate the static middleware
 * calls, so it tracks real serving behaviour rather than re-implementing the check. It is
 * a pure path decision — no file needs to exist — so the `.scratch/` cases hold on CI,
 * where `.scratch/` is absent.
 */
import { describe, expect, it } from "vitest";
import { isFileServingAllowed, resolveConfig } from "vite";

/**
 * Anchored to this file rather than to `process.cwd()`, so the test does not depend on
 * which directory the runner was started from — and because the cockpit tsconfig is
 * browser-only with no `@types/node`, ruling out `node:path`/`node:url`.
 * `new URL(".", …).pathname` yields `/C:/…/cockpit/src/` on Windows and `/…/cockpit/src/`
 * elsewhere, so the leading slash is dropped only when a drive letter follows it.
 */
const SRC_DIR = decodeURIComponent(new URL(".", import.meta.url).pathname).replace(
  /^\/(?=[A-Za-z]:)/,
  "",
);
const COCKPIT_DIR = SRC_DIR.replace(/\/src\/$/, "");
const REPO_ROOT = COCKPIT_DIR.slice(0, COCKPIT_DIR.lastIndexOf("/"));

/** `/@fs/` is the URL prefix Vite uses to reach an absolute path outside the root. */
function fsUrl(absolutePath: string): string {
  return `/@fs/${absolutePath}`;
}

/**
 * Resolved ONCE and shared by every case below.
 *
 * `resolveConfig` loads and esbuild-transpiles `vite.config.ts`, and this file asked for
 * it ten times — four tests plus six `it.each` rows — for one immutable answer. Alone
 * that is affordable (10/10 pass in 4.17s); inside the full 27-file parallel run one of
 * the ten exceeded vitest's default 5000ms and the file went red at 5017ms. So this
 * security-boundary test failed for a reason with nothing to do with the boundary, and
 * a flaky security test is worse than a slow one: the reflex is to stop believing it.
 *
 * Memoised rather than given a longer timeout, because the ten redundant transpiles were
 * the cause and a bigger timeout only tolerates them. Sharing is safe — every case reads
 * the config and none mutates it.
 */
let devConfig: ReturnType<typeof resolveConfig> | null = null;

function resolveDevConfig() {
  devConfig ??= resolveConfig(
    { configFile: `${COCKPIT_DIR}/vite.config.ts`, root: COCKPIT_DIR },
    "serve",
  );
  return devConfig;
}

describe("dev server file-serving boundary", () => {
  it("derives the cockpit and repo paths this test reasons about", async () => {
    // Guards the path arithmetic above: if it ever produces something other than the
    // real cockpit root, every other assertion here would be checking a fiction.
    const config = await resolveDevConfig();
    expect(config.root).toBe(COCKPIT_DIR);
    expect(REPO_ROOT.length).toBeLessThan(COCKPIT_DIR.length);
  });

  it("allows nothing above the cockpit directory", async () => {
    const config = await resolveDevConfig();
    for (const allowed of config.server.fs.allow) {
      // Equal to the cockpit root, or nested inside it. A parent would mean the repo
      // root (or higher) is reachable.
      expect(
        allowed === COCKPIT_DIR || allowed.startsWith(`${COCKPIT_DIR}/`),
      ).toBe(true);
    }
  });

  it("keeps fs.strict on, without which the allow list is inert", async () => {
    // `isFileLoadingAllowed` returns true immediately when strict is off, so a false
    // here would silently defeat every other assertion in this file.
    const config = await resolveDevConfig();
    expect(config.server.fs.strict).toBe(true);
  });

  it("still serves the app's own sources", async () => {
    // The control. Without it, a config that refused *everything* would pass the
    // refusal cases below while breaking the dev server outright.
    const config = await resolveDevConfig();
    expect(isFileServingAllowed(config, fsUrl(`${COCKPIT_DIR}/src/main.ts`))).toBe(true);
    expect(isFileServingAllowed(config, fsUrl(`${COCKPIT_DIR}/index.html`))).toBe(true);
  });

  it.each([
    [".scratch working tree", ".scratch/OPEN-ITEMS.md"],
    [".scratch probe script", ".scratch/probe.py"],
    ["repo README", "README.md"],
    ["engine source", "llm_anthology/discover.py"],
    ["git internals", ".git/config"],
    ["dotenv", ".env"],
  ])("refuses %s above the cockpit root", async (_label, relativePath) => {
    const config = await resolveDevConfig();
    expect(isFileServingAllowed(config, fsUrl(`${REPO_ROOT}/${relativePath}`))).toBe(
      false,
    );
  });
});
