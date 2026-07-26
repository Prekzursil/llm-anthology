# Cockpit engine sidecar — how the engine is shipped

This directory was the placeholder for a Tauri `externalBin` sidecar. **That route was not
taken.** The engine ships as a Tauri **resource** instead, and the packaging gap this file
used to document is closed.

## What actually ships

`cockpit/scripts/stage-engine.ps1` stages a **relocatable CPython**
(python-build-standalone, fetched via `uv`) into `../resources/engine/`, with the
`llm-anthology` package pip-installed into it. `tauri.conf.json` maps that tree into the
bundle:

    "bundle": { "resources": { "resources/engine": "engine" } }

so an installed app has its interpreter at `<install-dir>/engine/python.exe`.

`src/sidecar.rs::engine_python_in()` prefers that interpreter and falls back to `python` on
`PATH`:

- **packaged install** → the bundled CPython. The app does **not** require the user to have
  Python or the package installed.
- **dev build** (nothing staged) → `python` from `PATH`, exactly as before, which is also
  what the Rust test-suite's real-sidecar e2e tests use.

Both branches are unit-tested (`engine_python_prefers_a_bundled_interpreter`,
`engine_python_falls_back_to_path_when_nothing_is_bundled`,
`engine_python_ignores_an_empty_engine_directory`) — the bundled branch is otherwise only
reachable from a real installation, which is exactly the kind of path that ships broken.

## Why a resource, not `externalBin`

`externalBin` wants ONE executable per target triple. python-build-standalone is a *tree* — a
real interpreter plus its stdlib — not a single file. Freezing it into one binary
(PyInstaller/Nuitka) was the alternative and was rejected: a frozen bundle changes import
semantics, and the engine's value here is that the sidecar module loads in production exactly
as it does in development. Shipping the tree as a resource keeps one code path.

## Lifecycle and isolation are unchanged

Swapping the interpreter path changed nothing about how the process is created. The engine is
still spawned through one raw `CreateProcessW` that does three things at once:

- a **Job Object** with `KILL_ON_JOB_CLOSE`, so no engine outlives the app;
- `CREATE_NO_WINDOW`;
- optionally an **AppContainer** membrane with no `internetClient` capability (a first-class,
  tested spawn mode; opt-in at the production call site because a sandboxed engine also needs
  its export destination granted to the package SID — an export-lifecycle concern).

Transport is unchanged too: **stdio NDJSON JSON-RPC 2.0**, one object per line. No HTTP, no
localhost port.

## Rebuilding

    cd cockpit
    powershell -File scripts/stage-engine.ps1     # ~50 MB staged, gitignored
    npm run tauri build                            # -> ~14 MB NSIS installer

The staged tree is a build artifact and is gitignored — never commit the interpreter. The
staging script verifies its own output before finishing: it imports `llm_anthology` and runs
`python -m llm_anthology.sidecar --help` using the staged interpreter, from a cwd with no repo
on it, so a half-staged tree fails the script rather than the installer.

## Size

| Piece | Size |
|---|---|
| staged engine (slimmed: no CPython test suite, IDLE, Tk/Tcl) | ~50 MB |
| NSIS installer | ~14 MB |
| installed footprint | ~60 MB |

Pass `-NoSlim` to keep the full stdlib.
