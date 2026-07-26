# Cockpit

Tauri v2 desktop shell for **LLM Anthology** (`llm_anthology`) — a local, offline cockpit for
browsing an AI-session corpus: the cross-provider spawn tree, FTS5 search, time-travel, diff,
fidelity-gated export, and the absorbed session-management layer.

The analysis engine **is** wired: the Rust core spawns `python -m llm_anthology.sidecar` and
speaks **stdio NDJSON JSON-RPC 2.0** to it — deliberately not a localhost HTTP port, so no
other process on the machine can talk to the engine.

## Layout

- `index.html` — app entry document (the three-zone shell: sidebar / graph / detail).
- `src/graph/` — ELK-in-a-Worker layered layout + the canvas renderer + diff overlay.
- `src/ui/` — virtualized list, time scrubber, export panel, search.
- `src/ipc/` — the data surface: `types.ts` (the wire contract), `real.ts` (Tauri commands),
  `mock.ts` (an in-memory forest), `index.ts` (runtime adapter selection).
- `src-tauri/` — the Rust crate: the sidecar client, the hardened spawn, Tauri commands.
- `src-tauri/binaries/` — documented placeholder for a bundled engine (see the caveat below).

## Previewing the UI in a browser

`src/ipc/index.ts` picks its adapter from the ENVIRONMENT, not a compile-time flag: inside
Tauri it uses the real engine, anywhere else it falls back to the mock forest. So

    npm run dev

serves the full interface — populated spawn tree, thread list, scrubber — with no Rust build
and no corpus. That is what makes the UI screenshottable, auditable and design-reviewable.

This matters more than it sounds. The flag used to be hard-coded to the real adapter, so
opening the cockpit in a browser threw `TypeError: Cannot read properties of undefined
(reading 'invoke')` on first paint and every pane rendered dead — which is a large part of why
this UI went through four build phases without anyone ever looking at it.

## Toolchain prerequisites (Windows)

- Rust with the `x86_64-pc-windows-msvc` target.
- MSVC C++ build tools (VS 2022 Build Tools, `VC.Tools.x86.x64`) — the linker.
- WebView2 runtime (ships with modern Windows / Edge).
- Node.js >= 20 and npm.

## Build

    cd cockpit
    npm install
    npm run build          # tsc typecheck + vite build -> dist/
    npm run tauri build    # release exe + the NSIS installer

Output: `src-tauri/target/release/cockpit.exe` and
`src-tauri/target/release/bundle/nsis/LLM Anthology_<version>_x64-setup.exe`.

### Why the bundle target is NSIS and not `"all"`

`bundle.targets` is pinned to `["nsis"]`. `"all"` also builds the WiX/MSI target, which does
not build on this machine: `light.exe` aborts ICE validation with `LGHT0217` and Windows
Installer error **2738** — the ICE custom actions cannot reach the VBScript runtime. The
generated `main.wxs` itself is fine (warnings only, no errors), so this is an environment
fault rather than a packaging defect. The usual cause — an `HKCU` CLSID entry shadowing the
machine-wide VBScript/JScript registration — was checked and came back negative for both
CLSIDs, so the specific origin here is *not* established.

NSIS avoids ICE entirely and is the better fit regardless: a ~2 MB per-user installer that
needs no administrator.

(This rationale lives here because `tauri.conf.json` is strict JSON — it has no comments, and
Tauri's config schema hard-fails on an unknown field, so a `"//targets"` pseudo-comment breaks
the build.)

## Packaging — the engine IS bundled

    powershell -File scripts/stage-engine.ps1     # stage a relocatable CPython (~50 MB)
    npm run tauri build                            # -> ~14 MB NSIS installer

`scripts/stage-engine.ps1` stages a python-build-standalone CPython (via `uv`) into
`src-tauri/resources/engine/` with the `llm-anthology` package installed into it, and
`tauri.conf.json` maps that into the bundle. An installed app therefore carries its own
interpreter at `<install-dir>/engine/python.exe` and does **not** require the user to have
Python or the package.

`src/sidecar.rs::engine_python_in()` prefers the bundled interpreter and falls back to `python`
on `PATH`, so a dev build with nothing staged behaves exactly as before. Both branches are
unit-tested. Details and the reasoning for a resource rather than `externalBin`:
[`src-tauri/binaries/README.md`](src-tauri/binaries/README.md).

The staged tree is a build artifact and is gitignored.

## Run (dev)

    cd cockpit
    npm install
    npm run tauri dev

`@tauri-apps/cli` is a **per-project devDependency** (invoked via `npm run tauri`), never a
global install.

## Tests

    npx tsc --noEmit          # types
    npx vitest run            # UI logic (graph, ipc, ui modules)
    cd src-tauri && cargo test # Rust core, incl. e2e round-trips against the REAL sidecar

The Rust suite includes tests that spawn the actual Python engine over stdio, plus both-states
proofs for the Job-Object reap and the AppContainer egress membrane.
