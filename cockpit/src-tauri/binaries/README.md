# Cockpit engine sidecar — placeholder (NOT yet wired)

This directory is the Tauri **sidecar** location for the AI-session analysis
engine that the Cockpit desktop app will spawn. It is a documented placeholder
for the *next* work-unit; nothing here is wired yet, which is why the app still
builds today.

## What will live here

A self-contained Python engine binary, one per target triple, following Tauri's
`externalBin` naming convention (Tauri appends the triple at build time and
strips it at runtime, so the app always spawns `aisr-engine`):

    binaries/aisr-engine-x86_64-pc-windows-msvc.exe
    binaries/aisr-engine-x86_64-unknown-linux-gnu
    binaries/aisr-engine-aarch64-apple-darwin

## How it will be built (SOTA decision)

- Packaged with **python-build-standalone** via **uv** (uv 0.10.4) — a
  relocatable CPython plus the `aisr` package, produced as a single launchable
  engine binary. The build step (a future bite) emits the per-triple binary here.

## How it will be wired (deferred to the NEXT bite)

1. Declare it in `../tauri.conf.json` under `bundle.externalBin` (currently an
   empty `[]` placeholder), e.g.:

       "bundle": { "externalBin": ["binaries/aisr-engine"] }

2. Spawn it from Rust (tauri-plugin-shell sidecar API) and speak to it over
   **stdio using newline-delimited JSON (NDJSON)** — one JSON object per line,
   framed by `\n`. No HTTP, no localhost port.

3. Lifecycle + isolation, also deferred to that bite:
   - a Windows **Job Object** (kill-on-close) so no orphaned engine outlives the app;
   - an **AppContainer** sandbox membrane around the engine process.

## Why it is empty now

Wiring a not-yet-built sidecar into `externalBin` would break `tauri build`
(the bundler looks for the file on disk). Keeping `externalBin: []` plus this
README means the scaffold BUILDS today while documenting exactly where the
engine plugs in.
