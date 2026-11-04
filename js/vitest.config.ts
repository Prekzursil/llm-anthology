import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    coverage: {
      provider: "v8",
      // QZP lean gate, gate 3: 100% on all four metrics, the same bar the Python rail
      // enforces with --cov-fail-under=100. Every module under src/ is measured,
      // generated ones included (themes.ts carries a real `?? ""` branch).
      thresholds: { lines: 100, functions: 100, statements: 100, branches: 100 },
      include: ["src/**/*.ts"],
      // the text table is not truncated, so every uncovered line is named in the log
      reporter: ["text-summary", ["text", { maxCols: 1000 }], "json-summary"],
    },
  },
});
