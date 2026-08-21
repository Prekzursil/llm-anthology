//! One-time membrane provisioning: the grants the AppContainer package SID needs
//! BEFORE the corpus-attached engine can be spawned inside the membrane at all.
//!
//! Two halves, deliberately split by cost and by failure policy:
//!
//! 1. **The engine tree** (interpreter + the packages its import graph reaches) —
//!    a large, slow `icacls` walk (~8k files for a dev CPython's stdlib), granted
//!    BEST-EFFORT via [`grant_read`]: a locked file inside the walk is survivable,
//!    and the spawn that follows is the real proof the grants held.
//! 2. **The app's own grant roots** (`AppLocations::grant_roots`: the data dir and
//!    the default export destination) — small, load-bearing, granted FAIL-FAST via
//!    [`try_grant`]: if these do not land the membrane cannot work, and the user
//!    must see a clear error instead of an opaque access-denied from inside the
//!    sandbox.
//!
//! Both run behind a MARKER FILE (`.membrane-provisioned-v1` in the data dir) whose
//! content is a stamp of everything the grant set depends on (package SID, resolved
//! interpreter, `PYTHONPATH`). Matching stamp → the whole walk is skipped, so the
//! per-spawn cost after first run is one small file read plus the (cheap, idempotent)
//! profile ensure. A changed interpreter, a moved dev tree, or a recreated
//! AppContainer profile changes the stamp and re-provisions.
//!
//! UNINSTALL / ROLLBACK (documented here because no uninstaller integration exists
//! yet): call [`deprovision`] to sweep the grant-root ACEs and the marker; the
//! interpreter-tree ACEs are additive entries for this app's package SID only and
//! can be swept with `icacls <python-home> /remove *<sid> /T`, and the profile
//! itself with `DeleteAppContainerProfile("llm-anthology-cockpit-sidecar")`.

use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use super::hardened_spawn::{ensure_container_sid, grant_read, revoke_grant, try_grant, Membrane};
use super::ResolvedEngine;
use crate::{app_locations, AppLocations};

/// Leaf name of the marker file recording a completed provisioning, kept in the
/// data dir so wiping the app's data also re-arms provisioning.
pub(crate) const PROVISION_MARKER_FILE: &str = ".membrane-provisioned-v1";

/// Run `grants` exactly once per `stamp`: skip when `marker` already records the
/// same stamp, re-run when it records a different one, and NEVER write the marker
/// when the grants failed — a marker over failed grants would convert every later
/// launch into the opaque in-sandbox failure this module exists to prevent.
///
/// Returns `Ok(true)` when the grants ran, `Ok(false)` when the marker skipped them.
pub(crate) fn provision_gate(
    marker: &Path,
    stamp: &str,
    grants: impl FnOnce() -> Result<(), String>,
) -> Result<bool, String> {
    if std::fs::read_to_string(marker).is_ok_and(|recorded| recorded == stamp) {
        return Ok(false);
    }
    grants()?;
    if let Some(parent) = marker.parent() {
        std::fs::create_dir_all(parent).map_err(|e| {
            format!(
                "provisioned, but could not create {}: {e}",
                parent.display()
            )
        })?;
    }
    std::fs::write(marker, stamp).map_err(|e| {
        format!(
            "provisioned, but could not record the marker at {}: {e}",
            marker.display()
        )
    })?;
    Ok(true)
}

/// Ensure the AppContainer profile exists and every provisioning grant is in place
/// for `engine`; returns the package SID string. Cached per process (the marker
/// file makes cross-process repeats cheap; the cache makes same-process repeats
/// free). A failure is cached too — deliberately: re-walking a failing grant on
/// every spawn attempt would turn one clear error into a stutter of slow ones.
pub(crate) fn ensure_provisioned(engine: &ResolvedEngine) -> Result<String, String> {
    static PROVISIONED: OnceLock<Result<String, String>> = OnceLock::new();
    PROVISIONED.get_or_init(|| provision_now(engine)).clone()
}

fn provision_now(engine: &ResolvedEngine) -> Result<String, String> {
    let locations = app_locations()
        .map_err(|e| format!("membrane provisioning needs the app locations: {e}"))?;
    let sid = ensure_container_sid(Membrane::AppContainer)?;
    let pythonpath = std::env::var("PYTHONPATH").unwrap_or_default();
    let stamp = format!("v1|{sid}|{}|{pythonpath}", engine.python);
    let marker = locations.data_dir.join(PROVISION_MARKER_FILE);
    provision_gate(&marker, &stamp, || {
        engine_grants(engine, &sid, &pythonpath)?;
        root_grants(&locations, &sid)
    })?;
    Ok(sid)
}

/// Read+execute on the interpreter tree the engine will load, keyed off the SAME
/// distinction `sidecar_argv` uses (the `bundled` flag):
///   * BUNDLED: one self-contained tree beside the app binary — grant it whole.
///   * DEV (`python` on PATH): the stdlib walk minus `site-packages` (plus the two
///     engine wheels), then every `PYTHONPATH` entry — the source tree the `-s`
///     spawn arm deliberately keeps reachable.
fn engine_grants(engine: &ResolvedEngine, sid: &str, pythonpath: &str) -> Result<(), String> {
    if engine.bundled {
        let home = Path::new(&engine.python).parent().ok_or_else(|| {
            format!(
                "bundled interpreter {} has no parent directory",
                engine.python
            )
        })?;
        grant_read(sid, &home.to_string_lossy(), true);
        return Ok(());
    }
    grant_stdlib_read(sid, &python_home_for(engine)?);
    for entry in std::env::split_paths(pythonpath) {
        if entry.as_os_str().is_empty() {
            continue;
        }
        // The entry itself: traverse + read its top level (packages resolve through
        // it); the engine package beneath it: the whole tree.
        grant_read(sid, &entry.to_string_lossy(), false);
        let pkg = entry.join("llm_anthology");
        if pkg.is_dir() {
            grant_read(sid, &pkg.to_string_lossy(), true);
        }
    }
    Ok(())
}

/// Modify on each of the app's own roots — created first, because on a fresh
/// machine none of them exists yet, and FAIL-FAST: these are the paths the whole
/// corpus/export design routes writes through.
fn root_grants(locations: &AppLocations, sid: &str) -> Result<(), String> {
    for root in locations.grant_roots() {
        std::fs::create_dir_all(root).map_err(|e| {
            format!(
                "membrane provisioning could not create {}: {e}",
                root.display()
            )
        })?;
        try_grant(sid, root, "(M)", true, true)
            .map_err(|e| format!("membrane provisioning failed: {e}"))?;
    }
    Ok(())
}

/// The directory CPython considers home for a DEV engine: where `engine.python`
/// itself lives if it is a real path, else the first `PATH` entry with `python.exe`.
fn python_home_for(engine: &ResolvedEngine) -> Result<PathBuf, String> {
    let direct = Path::new(&engine.python);
    if direct.is_file() {
        if let Some(home) = direct.parent() {
            return Ok(home.to_path_buf());
        }
    }
    if let Some(paths) = std::env::var_os("PATH") {
        for dir in std::env::split_paths(&paths) {
            if dir.join("python.exe").is_file() {
                return Ok(dir);
            }
        }
    }
    Err("membrane provisioning could not locate python.exe on PATH".to_string())
}

/// Grant the AppContainer package `sid` read+execute on JUST the CPython
/// standard library (root binaries, DLLs, and every `Lib` child EXCEPT the
/// huge `site-packages` tree — the sidecar imports only stdlib + llm_anthology,
/// plus the two [`ENGINE_WHEELS`]). This keeps the AppContainer grant fast
/// (~8k files, not ~130k).
pub(crate) fn grant_stdlib_read(sid: &str, python_home: &Path) {
    // The engine's third-party wheels are granted BEFORE the fast path, deliberately.
    // The sentinel below is `python.exe`, which a PRIOR run already granted — so an
    // early return here would skip the wheel grant forever on any machine that had run
    // this once, and the fix would be dead code that looks correct. It cost one
    // red build to learn that; the grant is two directories, so doing it unconditionally
    // is cheaper than a second sentinel that can drift out of step with ENGINE_WHEELS.
    grant_engine_wheels(sid, &python_home.join("Lib").join("site-packages"));

    // Fast path: grants persist for the FIXED container SID, so if a prior run
    // (or a production installer) already granted the interpreter, skip the slow
    // ~8k-file re-walk. `python.exe` is the sentinel.
    let sentinel = python_home.join("python.exe");
    if let Ok(o) = std::process::Command::new("icacls").arg(&sentinel).output() {
        if String::from_utf8_lossy(&o.stdout).contains(sid) {
            return;
        }
    }
    let ph = python_home.to_string_lossy();
    grant_read(sid, &format!("{ph}\\*"), false); // python.exe + *.dll + immediate folder objects
    grant_read(sid, &format!("{ph}\\DLLs"), true); // native ext modules (_socket, _sqlite3, ...)
    let lib = python_home.join("Lib");
    grant_read(sid, &format!("{}\\*", lib.to_string_lossy()), false); // Lib top-level .py + folders
    if let Ok(entries) = std::fs::read_dir(&lib) {
        for e in entries.flatten() {
            if e.file_name() == std::ffi::OsStr::new("site-packages") {
                grant_engine_wheels(sid, &e.path());
                continue;
            }
            if e.path().is_dir() {
                grant_read(sid, &e.path().to_string_lossy(), true);
            }
        }
    }
}

/// The third-party packages the ENGINE'S IMPORT GRAPH reaches, and nothing else.
///
/// WHY THIS EXISTS AT ALL. This grant's whole premise used to be "the sidecar is pure
/// stdlib + llm_anthology", which is why `site-packages` was skipped outright — granting
/// ~120k files would make the membrane meaningless as a statement about what the engine
/// may read. That premise was TRUE and is no longer: `07be166` put a top-level
/// `import ijson` on the ingest path and `0fc4463` added `from . import archive`, whose
/// `archive.py` does a top-level `import zstandard`. Both are now reachable from
/// `import llm_anthology.sidecar`, so a stdlib-only grant makes the engine unloadable.
///
/// The narrow grant is the honest fix. The membrane exists to block EGRESS, not to block
/// dependencies — nothing here opens a network path, and naming two packages keeps the
/// grant a meaningful assertion instead of a blanket one.
///
/// FAIL-CLOSED BY CONSTRUCTION: this list is not the source of truth for what the engine
/// needs — the membrane test is. If a future commit adds a third-party import the engine
/// reaches at module scope, that test goes RED with a `ModuleNotFoundError` naming the
/// package, and whoever added it decides deliberately whether it belongs in a sandboxed
/// engine. That is the same failure that surfaced ijson and zstandard, and it is why the
/// list should never be widened to a wildcard to make a red build go away.
const ENGINE_WHEELS: &[&str] = &["ijson", "zstandard"];

/// Grant read on just [`ENGINE_WHEELS`] inside `site-packages`.
///
/// Prefix-matched because a wheel ships more than its import name: `zstandard` carries a
/// native `backend_c` extension and `ijson` its `backends/` tree, and both sit under
/// directories whose names begin with the package name. Matching the prefix takes the
/// package and its siblings without taking the ~120k-file remainder.
fn grant_engine_wheels(sid: &str, site_packages: &Path) {
    let Ok(entries) = std::fs::read_dir(site_packages) else {
        return;
    };
    for e in entries.flatten() {
        let name = e.file_name();
        let name = name.to_string_lossy();
        if !ENGINE_WHEELS.iter().any(|w| name.starts_with(w)) {
            continue;
        }
        if e.path().is_dir() {
            grant_read(sid, &e.path().to_string_lossy(), true);
        } else {
            grant_read(sid, &e.path().to_string_lossy(), false);
        }
    }
}

/// Rollback: sweep the grant-root ACEs and the marker (see the module header for
/// the interpreter-tree and profile halves of a FULL uninstall). Not called by the
/// app yet — this is the documented cleanup entry point.
#[allow(dead_code)]
pub(crate) fn deprovision(locations: &AppLocations, sid: &str) -> Result<(), String> {
    for root in locations.grant_roots() {
        // Best-effort per root: an already-deleted export folder must not stop the
        // data-dir sweep behind it.
        if root.exists() {
            let _ = revoke_grant(sid, root, true);
        }
    }
    let marker = locations.data_dir.join(PROVISION_MARKER_FILE);
    if marker.exists() {
        std::fs::remove_file(&marker)
            .map_err(|e| format!("could not remove {}: {e}", marker.display()))?;
    }
    Ok(())
}
