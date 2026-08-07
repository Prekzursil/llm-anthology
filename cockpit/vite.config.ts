import { defineConfig } from "vite";

// @ts-expect-error process is a nodejs global
const host = process.env.TAURI_DEV_HOST;

// https://vite.dev/config/
export default defineConfig(async () => ({

  // Vite options tailored for Tauri development and only applied in `tauri dev` or `tauri build`
  //
  // 1. prevent Vite from obscuring rust errors
  clearScreen: false,
  // 2. tauri expects a fixed port, fail if that port is not available
  server: {
    port: 1420,
    strictPort: true,
    host: host || false,
    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 1421,
        }
      : undefined,
    watch: {
      // 3. tell Vite to ignore watching `src-tauri`
      ignored: ["**/src-tauri/**"],
    },
    // 4. confine file serving to this directory. `.` resolves against Vite's `root`,
    //    i.e. `cockpit/`, so nothing above it is reachable over `/@fs/<abs-path>`.
    //
    //    This is what Vite's default already produces here, but only as a side effect of
    //    a heuristic: the default is `searchForWorkspaceRoot(root)`, which walks upward
    //    looking for `pnpm-workspace.yaml`, `lerna.json`, or a `package.json` carrying a
    //    `workspaces` key — and deliberately NOT for `.git`. Adding any one of those at
    //    the repo root would silently widen serving to the whole repo, which holds the
    //    gitignored `.scratch/` working tree, with no edit here and no warning. Stating
    //    the boundary makes it an invariant instead of an inference.
    //
    //    Nothing above `cockpit/` is needed: `index.html` pulls only `/src/*`,
    //    `node_modules` lives inside `cockpit/`, and the single import that crosses the
    //    boundary (`graph/palette.test.ts` -> `llm_anthology/discover.py?raw`) is a TEST,
    //    resolved by the standalone `vitest.config.ts`, which never loads this file.
    //
    //    `devServerFsAllow.test.ts` locks this, including `fs.strict`, without which the
    //    allow list is skipped entirely.
    fs: { allow: ["."] },
  },
}));
