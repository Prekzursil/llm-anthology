/**
 * Cockpit entry point. Boots the {@link CockpitApp} once the document is ready; any
 * boot failure is surfaced in the health line rather than swallowed.
 */

import { CockpitApp } from "./app";

function boot(): void {
  try {
    const app = new CockpitApp();
    void app.init();
  } catch (err) {
    const health = document.getElementById("health");
    if (health !== null) health.textContent = `cockpit failed to start: ${String(err)}`;
    // Re-throw so it also lands in the devtools console during development.
    throw err;
  }
}

if (document.readyState === "loading") {
  window.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
