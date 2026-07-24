import { invoke } from "@tauri-apps/api/core";

/** Shape returned by the Rust `app_info` command. */
interface AppInfo {
  name: string;
  version: string;
  engine: string;
}

/**
 * Fetch basic cockpit metadata from the Rust backend and render it into the
 * status line. The engine sidecar is not yet wired, so `engine` reports its
 * deferred state (see src-tauri/binaries/README.md).
 */
async function renderStatus(): Promise<void> {
  const statusEl = document.querySelector<HTMLElement>("#status");
  if (!statusEl) {
    return;
  }
  try {
    const info = await invoke<AppInfo>("app_info");
    statusEl.textContent = `${info.name} v${info.version} — engine: ${info.engine}`;
  } catch (err) {
    statusEl.textContent = `Cockpit (engine status unavailable: ${String(err)})`;
  }
}

window.addEventListener("DOMContentLoaded", () => {
  void renderStatus();
});
