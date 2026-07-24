import { defineConfig } from "vitest/config";

// Standalone from vite.config.ts (which carries Tauri dev-server settings): the tests
// are pure data transforms over the layout mapping and the mock IPC, so they run in a
// plain node environment with no DOM/jsdom dependency.
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
