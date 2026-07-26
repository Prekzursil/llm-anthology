# Cockpit

Tauri v2 desktop shell for **llm-anthology** (`llm_anthology`) — a local, offline
cockpit for browsing an AI-session corpus (rollouts, state graph, FTS5 search).

This is the **P2 scaffold**: a bare window that compiles on Windows. The analysis
engine (a python-build-standalone sidecar spoken to over stdio NDJSON) is **not
yet wired** — see [`src-tauri/binaries/README.md`](src-tauri/binaries/README.md).

## Layout

- `index.html` — app entry document.
- `src/` — minimal Vite + TypeScript frontend (the bare Cockpit window + a
  status line fed by the Rust `app_info` command).
- `src-tauri/` — the Rust crate (Tauri v2 app; one `app_info` command).
- `src-tauri/binaries/` — documented placeholder for the engine sidecar.

## Toolchain prerequisites (Windows)

- Rust with the `x86_64-pc-windows-msvc` target.
- MSVC C++ build tools (VS 2022 Build Tools, `VC.Tools.x86.x64`) — the linker.
- WebView2 runtime (ships with modern Windows / Edge).
- Node.js >= 20 and npm.

## Build

    cd cockpit
    npm install
    npm run build          # tsc typecheck + vite build -> dist/
    cd src-tauri
    cargo build            # compiles the Tauri app (first build is long)

## Run (dev)

    cd cockpit
    npm install
    npm run tauri dev

`@tauri-apps/cli` is a **per-project devDependency** (invoked via `npm run
tauri`), never a global install.
