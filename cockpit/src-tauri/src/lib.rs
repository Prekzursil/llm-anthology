mod sidecar;

use std::sync::{Arc, Mutex};

use serde_json::{json, Value};
use tauri::State;

use sidecar::SidecarClient;

/// Return basic cockpit metadata for the frontend status line.
///
/// This reports STATIC build metadata; the live engine connection state is queried
/// separately via `health_ping` once a corpus is attached with `open_corpus`. The
/// `engine` field stays "not-wired" here because a bare launch has no sidecar spawned
/// until the user opens an index.
#[tauri::command]
fn app_info() -> Value {
    json!({
        "name": "Cockpit",
        "version": env!("CARGO_PKG_VERSION"),
        "engine": "not-wired",
    })
}

/// Shared engine handle held in Tauri state: the (optionally) running sidecar client
/// behind an `Arc<Mutex<_>>` so every command serialises its request/response round
/// trip against the single stdio transport. `None` until `open_corpus` spawns one.
#[derive(Default)]
struct EngineState {
    client: Arc<Mutex<Option<SidecarClient>>>,
}

/// Forward one JSON-RPC call to the attached sidecar, or a typed error if none is
/// attached. Serialises on the state mutex.
fn forward(state: &EngineState, method: &str, params: Value) -> Result<Value, String> {
    // A no-arg method may arrive with a null/absent params; the sidecar requires an
    // object, so normalise null to an empty object.
    let params = if params.is_null() { json!({}) } else { params };
    let mut guard = state
        .client
        .lock()
        .map_err(|_| "engine mutex poisoned".to_string())?;
    match guard.as_mut() {
        Some(client) => client.call(method, &params),
        None => Err("no corpus attached: call open_corpus first".to_string()),
    }
}

/// Reject a UNC / network path spelling, judged on the path AS WRITTEN.
///
/// MEASURED, not assumed (this box, Windows 11, rustc 1.91.0), with a throwaway probe outside
/// this repo that replicates the exact `is_file()` + `is_dir()` pair `validate_index_path` runs
/// below. On `\\192.0.2.1\share\index.db` that pair took **272,061 ms** — four and a half
/// minutes. The identical pair on a missing LOCAL path took **0 ms**. While it was blocked the
/// TCP table held outbound `SynSent` sockets to `192.0.2.1:445` (SMB) and `:111` (NFS
/// portmapper), owned by PID 4 — the kernel redirector, not this process.
///
/// Two instruments agree and only one of them is a clock, so this is not a timing artefact: a
/// `stat` on a caller-named UNC path IS an outbound connection to a host the caller chose,
/// which is the Windows SMB/NTLM hash-leak vector. Had a host answered, the redirector would
/// have gone on to negotiate. (The connection ATTEMPT is measured. The credential offer is NOT
/// — nothing answered, by the deliberate choice of a non-routable RFC 5737 address.)
///
/// The 272 seconds are a second, independent reason to refuse: this runs on the thread serving
/// a `#[tauri::command]`, so before this guard existed one bad path froze the app's primary
/// action for minutes with no cancel and no timeout to bound it.
///
/// ORDER IS LOAD-BEARING, and this mirrors the engine's `_reject_unc_spelling`
/// (`llm_anthology/sidecar.py:484-497`) deliberately, including its reason for existing as a
/// separate step: the check must run on the RAW string, before any resolution. A caller that
/// canonicalises first destroys the evidence on POSIX, where `\\host\share` contains no
/// separator at all and so reads as an ordinary relative filename that `abspath` happily turns
/// into `/cwd/\\host\share` — no longer UNC-shaped, and absolute, so both guards then pass
/// something they exist to refuse. The engine learned that from its Linux and macOS CI legs
/// after a Windows-only check of the same ordering pronounced it safe; this file is compiled
/// and tested on a windows + linux matrix, so it inherits the same exposure.
///
/// Normalising `/` to `\` before the test is what makes ONE comparison cover every spelling:
/// `\\host\share`, the protocol-relative `//host/share`, the mixed `\/host\share`, the WebDAV
/// `\\host@SSL\DavWWWRoot\...`, and the extended-length `\\?\UNC\host\share`. `\\?\C:\...` is
/// caught too — that one is a legitimate local path, but it is not a spelling anything in this
/// app produces, and the engine refuses it identically.
fn reject_unc_spelling(index_path: &str) -> Result<(), String> {
    if index_path.replace('/', "\\").starts_with("\\\\") {
        return Err(format!(
            "{index_path} is a network path; a corpus index must be a local file"
        ));
    }
    Ok(())
}

/// Reject a path that RESOLVES to a network location without being SPELLED as one.
///
/// `reject_unc_spelling` judges the string as written, which is what makes it safe to run before
/// any filesystem call — and also what makes it blind to indirection. MEASURED on this box
/// (Windows 11, rustc 1.91.0) against `\\localhost\C$` so the target file is identical in all
/// three spellings:
///
/// | input | old verdict | `fs::canonicalize` says |
/// |---|---|---|
/// | `\\localhost\C$\Windows\explorer.exe` | REJECT (0 ms, lexical) | `\\?\UNC\localhost\C$\...` |
/// | `Z:\Windows\explorer.exe` (`net use Z: \\localhost\C$`) | **ACCEPT** | `\\?\UNC\localhost\C$\...` |
/// | `<dir-symlink>\Windows\explorer.exe` -> `\\localhost\C$` | **ACCEPT** | `\\?\UNC\localhost\C$\...` |
///
/// Same file, same share, two of the three accepted. So the drive-letter and symlink forms are
/// the residual of a spelling-only guard, and `Z:` is not exotic — a mapped network drive is an
/// ordinary corporate configuration, which also makes this the likelier real-world bite: an
/// index on a DISCONNECTED mapping puts the multi-minute stat described above on the thread
/// serving a `#[tauri::command]`.
///
/// The prefix is matched as a TYPED `Prefix`, not by string comparison, because a canonicalized
/// LOCAL path on Windows also begins `\\` — `\\?\C:\...` — so reusing `reject_unc_spelling` here
/// would refuse every legitimate file on the machine. `VerbatimDisk`/`Disk` is the local case;
/// `UNC`/`VerbatimUNC` is the remote one; `DeviceNS`/`Verbatim` is neither and is not a corpus.
///
/// SCOPE, stated plainly because it is narrow:
/// - This runs AFTER `is_file()`, since `canonicalize` needs the file to exist. It therefore
///   prevents ATTACHING a corpus over SMB — which also protects sqlite, whose locking is
///   unreliable on a network share — but it does NOT prevent the outbound connection
///   `is_file()` already made. Blocking the connection itself needs a check that runs BEFORE
///   the probe: `GetDriveTypeW` == `DRIVE_REMOTE` for the mapped-drive case (a local MUP
///   lookup, no network round trip) plus a `symlink_metadata` ancestor walk for the link case.
///   `GetDriveTypeW` needs the `Win32_Storage_FileSystem` feature added to `windows-sys` in
///   Cargo.toml, so it is deliberately NOT done here.
/// - On Linux and macOS this is INERT: a canonicalized path on an NFS or cifs mount is
///   `/mnt/share/index.db`, indistinguishable from a local path without consulting the mount
///   table. The raw-spelling guard still covers `//host/share` there.
fn reject_unc_resolution(
    index_path: &str,
    resolved: std::io::Result<std::path::PathBuf>,
) -> Result<(), String> {
    use std::path::{Component, Prefix};

    // The CALLER performs the resolution and this function judges the result, which keeps the
    // rule pure. A test can therefore hand it a recorded `\\?\UNC\...` directly, instead of
    // mounting a share with `net use` — that would need privileges, would mutate the machine,
    // and could never run on the CI matrix.
    let resolved = resolved.map_err(|e| {
        // Fail CLOSED. We only get here having already proved the path is a file, so a
        // resolution failure is an anomaly, not a routine miss.
        format!("cannot determine whether {index_path} is local: {e}")
    })?;

    let remote = match resolved.components().next() {
        Some(Component::Prefix(prefix)) => {
            !matches!(prefix.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_))
        }
        // No prefix at all: POSIX, where this check cannot speak. The raw-spelling guard ran.
        _ => false,
    };
    if remote {
        return Err(format!(
            "{index_path} resolves to {}, which is not a local disk; a corpus index must be a \
             local file",
            resolved.display()
        ));
    }
    Ok(())
}

/// Reject an `index_path` that is not an existing LOCAL file. Split out from the command so the
/// rule is unit-testable without constructing a Tauri `State`.
///
/// The two lexical guards run FIRST and in this order, because `is_file()` is itself the
/// dangerous operation — see `reject_unc_spelling` for the measurement. Requiring an absolute
/// path is the second half of the engine's `_reject_nonlocal_path` (`sidecar.py:500-514`) and
/// costs nothing legitimate: every path that reaches `open_corpus` is either a native
/// file-picker result, an `os.path`-derived finding from `sources.discover`, or a previously
/// accepted path echoed back out of Web Storage — all absolute. It is marginally STRICTER than
/// the engine, since `ntpath.isabs` accepts a drive-less `\Users\x` that Rust's `is_absolute`
/// rejects for having no prefix; a drive-ambiguous path is not something to resolve on the
/// user's behalf, and stricter-at-the-edge is the safe direction.
///
/// SCOPE, honestly: this closes the route that reaches `is_file()` with an arbitrary string —
/// `CorpusBarController.restore()` replaying a remembered path out of `localStorage`
/// (`cockpit/src/ui/corpusBar.ts:313-317`), with no dialog in between. It does NOT make the
/// native picker safe: a user who types a UNC path into the OS dialog's filename box has
/// already caused the SHELL to touch that host before Tauri returns a string to us. That is the
/// dialog's network contact, not this app's, and it is not fixable from here.
fn validate_index_path(index_path: &str) -> Result<(), String> {
    reject_unc_spelling(index_path)?;
    let path = std::path::Path::new(index_path);
    if !path.is_absolute() {
        return Err(format!(
            "{index_path} is not a full path; name the corpus index by its absolute path"
        ));
    }
    if path.is_file() {
        // It exists and it is a file — but "file" is not yet "local file". A mapped network
        // drive or a symlink reaches a share without ever being spelled `\\host\...`, so the
        // spelling guard above cannot see it. See `reject_unc_resolution`.
        return reject_unc_resolution(index_path, std::fs::canonicalize(path));
    }
    // Distinguish the two reasons, because "it's a folder" and "it's not there" send the
    // user to completely different next actions.
    Err(if path.is_dir() {
        format!("{index_path} is a folder, not a corpus index file")
    } else {
        format!("no corpus index at {index_path}")
    })
}

/// (Re)spawn the engine sidecar pointed at `index_path`. Replacing the previous
/// client drops it, which reaps the old process (best-effort — see `SidecarClient`).
///
/// REFUSES a path that is not an existing file, and that refusal is load-bearing rather
/// than defensive politeness. The engine opens an index with `corpus.open_index`, which
/// documents itself as "Open (creating if absent)" — it is `sqlite3.connect` plus a schema
/// init. The sidecar then reports `corpus_ready` as simply `self.conn is not None`
/// (`llm_anthology/sidecar.py`). So WITHOUT this guard, naming a path that does not exist
/// silently CREATES an empty index and reports success: a corpus the user has moved or
/// deleted would be resurrected as an empty file, and the UI would show zero conversations
/// with no error to explain why. "Open" must not be a create. Building a NEW index is the
/// CLI's `index` command, which owns the create verb deliberately.
#[tauri::command]
fn open_corpus(state: State<'_, EngineState>, index_path: String) -> Result<Value, String> {
    validate_index_path(&index_path)?;
    // Spawn BEFORE taking the lock's contents so a spawn failure leaves any existing
    // engine intact rather than tearing it down for a replacement that never arrived.
    let client = SidecarClient::spawn(&index_path)?;
    let mut guard = state
        .client
        .lock()
        .map_err(|_| "engine mutex poisoned".to_string())?;
    *guard = Some(client); // assigning here drops + reaps any previous client
    Ok(json!({ "ok": true, "index": index_path }))
}

/// Create an EMPTY corpus index at `index_path`, then leave it for `open_corpus` to attach.
///
/// Runs against a SHORT-LIVED, index-less engine rather than the managed one, because a user
/// making their first corpus has no engine attached — and `forward` requires one. Spawning a
/// throwaway also means a create can never disturb an engine the user already has open: a
/// corpus they are working in stays attached whatever happens here.
///
/// Refusing to clobber is the engine's job (`corpus.create` returns CORPUS_EXISTS), which is
/// the mirror of `open_corpus` refusing to create. Both halves of that split are enforced on
/// the side that owns the verb, so neither can be bypassed from here.
#[tauri::command]
fn create_corpus(index_path: String) -> Result<Value, String> {
    let mut client = SidecarClient::spawn_without_index()?;
    // `client` drops when this returns, reaping the throwaway engine. The call is the tail
    // expression rather than a `let` binding: the binding was not load-bearing for drop order
    // (`client` drops at end of scope either way) and clippy's `let_and_return` fires on it,
    // which under the CI job's `-D warnings` is a hard failure.
    client.call("corpus.create", &json!({ "index_path": index_path }))
}

/// Find AI session data already on this machine.
///
/// Runs on a THROWAWAY index-less engine for the same reason `create_corpus` does: this is the
/// first-run call, so by definition there is no corpus attached and `forward` would refuse it.
/// Using a separate process also means a scan can never disturb a corpus the user already has
/// open.
///
/// Takes no arguments on purpose. The engine exposes no `roots` parameter — accepting one
/// would turn autodetection into a directory-enumeration primitive against any path the engine
/// can read — and there is nothing for this layer to pass through.
#[tauri::command]
fn discover_sources() -> Result<Value, String> {
    let mut client = SidecarClient::spawn_without_index()?;
    // `client` drops when this returns, reaping the throwaway engine. See `create_corpus`
    // for why this is a tail expression and not a `let` binding.
    client.call("sources.discover", &json!({}))
}

#[tauri::command]
fn corpus_build(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "corpus.build", params.unwrap_or_else(|| json!({})))
}

/// Poll an ingest. Safe to call before any build — the engine answers `{"state":"idle"}`
/// rather than erroring, so the UI can render this unconditionally.
#[tauri::command]
fn corpus_build_status(
    state: State<'_, EngineState>,
    params: Option<Value>,
) -> Result<Value, String> {
    forward(
        state.inner(),
        "corpus.build_status",
        params.unwrap_or_else(|| json!({})),
    )
}

#[tauri::command]
fn health_ping(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "health.ping", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn corpus_stats(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "corpus.stats", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn graph_roots(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "graph.roots", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn graph_children(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "graph.children", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn graph_subtree(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "graph.subtree", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn search_query(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "search.query", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn thread_get(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "thread.get", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn graph_ancestors(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "graph.ancestors", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn conversation_get(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "conversation.get", params.unwrap_or_else(|| json!({})))
}

// -- Phase-3 time-travel + export surface -------------------------------------------
// Each mirrors the `forward(state, method, params)` pattern above, proxying one
// `graph.*` / `export.*` JSON-RPC method the sidecar (`llm_anthology/sidecar.py`) serves. The
// TS `real.ts` adapter targets these command names.
#[tauri::command]
fn graph_rollup(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "graph.rollup", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn graph_timeline(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "graph.timeline", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn graph_at(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "graph.at", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn graph_diff(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "graph.diff", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn export_plan(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "export.plan", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn export_run(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "export.run", params.unwrap_or_else(|| json!({})))
}

// -- dedup / maintenance / metadata / research ---------------------------------------
//
// 11 of the 13 RPCs that had no command at all. They were orphaned: the methods existed
// and were tested on the Python side, but with no `#[tauri::command]` here the panels that
// use them had no route to the engine. A command that compiles but is missing from
// `invoke_handler` is equally invisible, so every one below is registered there too.
//
// The other 2 (`research.*`) stay unwired ON PURPOSE — see the note where they would go.
//
// NAMING IS A CONTRACT, not a convention: RPC `a.b` -> command `a_b`. The TypeScript
// `invoke` call sites are written against it, and a mismatch is invisible to both tsc and
// vitest — it throws only when a user presses the button.
//
// All 13 forward through `forward`, so all 13 require an attached corpus (verified: each
// handler calls `_require_corpus`, llm_anthology/sidecar.py:1262-1611). That is why they
// take `state`, unlike `create_corpus` / `discover_sources`, which run on a throwaway
// index-less engine precisely because no corpus exists yet.

#[tauri::command]
fn dedup_scan(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "dedup.scan", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn dedup_sessions(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "dedup.sessions", params.unwrap_or_else(|| json!({})))
}

// THE DESTRUCTIVE PLANE. These four archive, quarantine and restore the owner's real
// session files, so params are forwarded VERBATIM — no defaulted root, no filled-in
// action, no convenience fallback. Every safety decision belongs to the engine's planner,
// which refuses a protected target, a protected destination, an unsafe root and a
// colliding restore, and an omitted field must surface as the engine's own refusal rather
// than be silently supplied here. `params.unwrap_or_else(|| json!({}))` is the same
// null-normalisation every command above does and widens nothing: an empty object leaves
// each required field missing, which `_req_str` rejects, and `apply` absent means DRY RUN.
#[tauri::command]
fn maintenance_plan(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "maintenance.plan", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn maintenance_execute(
    state: State<'_, EngineState>,
    params: Option<Value>,
) -> Result<Value, String> {
    forward(
        state.inner(),
        "maintenance.execute",
        params.unwrap_or_else(|| json!({})),
    )
}

#[tauri::command]
fn maintenance_restore(
    state: State<'_, EngineState>,
    params: Option<Value>,
) -> Result<Value, String> {
    forward(
        state.inner(),
        "maintenance.restore",
        params.unwrap_or_else(|| json!({})),
    )
}

#[tauri::command]
fn maintenance_runs(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "maintenance.runs", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn metadata_get(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "metadata.get", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn metadata_set(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "metadata.set", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn metadata_clear(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "metadata.clear", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn metadata_search(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "metadata.search", params.unwrap_or_else(|| json!({})))
}

#[tauri::command]
fn metadata_tags(state: State<'_, EngineState>, params: Option<Value>) -> Result<Value, String> {
    forward(state.inner(), "metadata.tags", params.unwrap_or_else(|| json!({})))
}

// `research.synthesize` and `research.extract_entities` are DELIBERATELY not wired, and
// the omission is enforced by `every_engine_rpc_has_a_registered_tauri_command` rather than
// left to memory. Both return empty output in the shipped app BY CONSTRUCTION: `main` builds
// `Sidecar(conn)` with no backend arguments (llm_anthology/sidecar.py:1933, the module's only
// construction site), so `research_backend` and `local_backend` both fall back to
// `research.MockBackend()` (`:587-590`), whose `synthesize` returns its `response` — default
// `""` (llm_anthology/research.py:88-97). The only two `synthesize` implementations in the
// package are that mock and the Protocol stub (`research.py:76`).
//
// Note this holds for BOTH methods, including extraction: `_research_extract_entities` is not
// a local deterministic pass that would work without a model — it routes through the same
// `self.research_backend` (`sidecar.py:1286`).
//
// A command here would therefore make a guaranteed-empty feature reachable. The owner
// deferred the feature rather than wire a backend, because it touches a standing privacy
// rule about this corpus — so wiring it is a decision about that rule, not a gap to fill.

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        // The corpus index is a file on disk, and a webview `<input type="file">` yields
        // no filesystem path in Tauri v2 — so a native picker is the only way the user can
        // name an index for `open_corpus`. Without it the app has no route to its primary
        // action and boots into a dead state.
        .plugin(tauri_plugin_dialog::init())
        .manage(EngineState::default())
        .invoke_handler(tauri::generate_handler![
            app_info,
            open_corpus,
            create_corpus,
            discover_sources,
            corpus_build,
            corpus_build_status,
            health_ping,
            corpus_stats,
            graph_roots,
            graph_children,
            graph_subtree,
            graph_ancestors,
            search_query,
            thread_get,
            conversation_get,
            graph_rollup,
            graph_timeline,
            graph_at,
            graph_diff,
            export_plan,
            export_run,
            dedup_scan,
            dedup_sessions,
            maintenance_plan,
            maintenance_execute,
            maintenance_restore,
            maintenance_runs,
            metadata_get,
            metadata_set,
            metadata_clear,
            metadata_search,
            metadata_tags,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::{app_info, reject_unc_resolution, validate_index_path};

    /// BOTH-STATES test for the open-vs-create guard. The passing case alone would not
    /// prove anything: the guard's whole purpose is to FAIL on a path the Python layer
    /// would otherwise happily create, so the rejecting cases are the real assertions.
    #[test]
    fn validate_index_path_accepts_a_real_file_and_rejects_everything_else() {
        // A file that certainly exists: this source file.
        let real = concat!(env!("CARGO_MANIFEST_DIR"), "/src/lib.rs");
        assert_eq!(validate_index_path(real), Ok(()), "an existing file must pass");

        // A path that does not exist. Without the guard the engine would CREATE this as an
        // empty index and report corpus_ready = true.
        let missing = concat!(env!("CARGO_MANIFEST_DIR"), "/src/definitely-not-here.db");
        let err = validate_index_path(missing).expect_err("a missing path must be rejected");
        assert!(
            err.contains("no corpus index at"),
            "error must name the missing-file reason, got: {err}"
        );

        // A directory. Distinguished from "missing" because it needs a different fix.
        let dir = concat!(env!("CARGO_MANIFEST_DIR"), "/src");
        let err = validate_index_path(dir).expect_err("a directory must be rejected");
        assert!(
            err.contains("is a folder"),
            "error must name the folder reason, got: {err}"
        );

        // Empty string — the shape a cleared persisted setting would take.
        assert!(
            validate_index_path("").is_err(),
            "an empty path must be rejected, not treated as the cwd"
        );
    }

    /// The UNC guard, over every spelling AND over the ORDERING that makes it a guard.
    ///
    /// The assertion is on WHICH refusal comes back, not merely that one does, and that is the
    /// whole point. Every path below is also missing, so a version that stat-ed first would
    /// still return `Err` — a test asserting only `is_err()` would stay green while the
    /// outbound SMB connection this exists to prevent went out on every single call. The
    /// `no corpus index at` / `is a folder` wordings are reachable ONLY from the two filesystem
    /// probes, so their ABSENCE is the evidence that no probe ran.
    ///
    /// Cross-platform is not incidental here. `//host/share/index.db` is absolute on Linux, so
    /// on the CI Linux leg the absolute-path check cannot catch it and only the raw-spelling
    /// check can; `\\host\share\index.db` is the reverse. Both must fail on both legs.
    #[test]
    fn validate_index_path_refuses_every_unc_spelling_before_any_filesystem_call() {
        for spelling in [
            r"\\host\share\index.db",          // canonical Windows UNC
            "//host/share/index.db",           // protocol-relative, and absolute on POSIX
            r"\/host\share\index.db",          // mixed separators
            r"\\host@SSL\DavWWWRoot\index.db", // WebDAV-over-HTTPS spelling
            r"\\?\UNC\host\share\index.db",    // extended-length UNC
            r"\\192.0.2.1\share\index.db",     // bare IP — reaches a host with no DNS at all
        ] {
            let err =
                validate_index_path(spelling).expect_err("a UNC/network path must be refused");
            assert!(
                err.contains("is a network path"),
                "wrong refusal for {spelling}, expected the network-path one: {err}"
            );
            assert!(
                !err.contains("no corpus index at") && !err.contains("is a folder"),
                "{spelling} was STAT-ED before it was refused — the filesystem answered first, \
                 and on Windows that stat is an outbound SMB/NFS connection to a caller-chosen \
                 host. The guard must run on the raw string, before any probe. Got: {err}"
            );
        }
    }

    /// The absolute-path half of the guard, plus its counterpart: a legitimate local path is
    /// still accepted. That passing half is load-bearing — a `validate_index_path` that refused
    /// everything would satisfy every rejection assertion in this module and ship a dead app.
    #[test]
    fn validate_index_path_requires_an_absolute_path_and_still_accepts_a_real_local_file() {
        for relative in [
            "index.db",
            "corpora/index.db",
            r"corpora\index.db",
            "./index.db",
            "C:index.db", // drive-RELATIVE on Windows: a real path, just not to a known place
        ] {
            let err = validate_index_path(relative).expect_err("a relative path must be refused");
            assert!(
                err.contains("is not a full path"),
                "wrong refusal for {relative}, expected the not-absolute one: {err}"
            );
        }

        // Every real caller supplies an absolute path and all of them must still pass — in BOTH
        // separator styles, because the native picker returns native `\` on Windows while the
        // engine's `sources.discover` findings and this crate's own `CARGO_MANIFEST_DIR`
        // concatenations use `/`.
        let real = concat!(env!("CARGO_MANIFEST_DIR"), "/src/lib.rs");
        assert_eq!(
            validate_index_path(real),
            Ok(()),
            "an absolute local file must pass"
        );
        let native = real.replace('/', std::path::MAIN_SEPARATOR_STR);
        assert_eq!(
            validate_index_path(&native),
            Ok(()),
            "the same file in native separators must pass: {native}"
        );
    }

    /// The spellings the enumeration above did NOT cover, kept separate so the reason each one
    /// is here stays legible. All were MEASURED against a replica of the guard before being
    /// written down; two of them refuted a guess made while enumerating:
    /// `C:\NUL` is NOT accepted (the Win32 device namespace does not make it stat as a file),
    /// and an embedded `\\` mid-path does NOT synthesise a UNC.
    #[test]
    fn validate_index_path_refuses_the_remaining_network_and_device_spellings() {
        for (spelling, why) in [
            ("///host/share/index.db", "3 slashes: the form a naive join produces"),
            ("////host/share/index.db", "4 slashes"),
            (r"/\host\share\index.db", "mixed, leading forward slash"),
            (r"\\.\C:\Windows\explorer.exe", "Win32 device namespace, local target"),
            (r"\\.\PhysicalDrive0", "raw device"),
            (r"\\?\C:\Windows\explorer.exe", "verbatim local: legitimate, but not a spelling this app produces"),
            (r"\\", "the degenerate UNC prefix alone"),
        ] {
            let err = validate_index_path(spelling)
                .expect_err(&format!("{spelling} must be refused ({why})"));
            assert!(
                err.contains("is a network path"),
                "{spelling} ({why}) must be refused by the raw-spelling guard, not by a \
                 filesystem probe — the probe is the outbound connection. Got: {err}"
            );
        }
    }

    /// The RESOLUTION guard: a path that reaches a share WITHOUT being spelled as one.
    ///
    /// MEASURED on this box before this test existed, with `net use Z: \\localhost\C$` and a
    /// directory symlink to the same share: `\\localhost\C$\Windows\explorer.exe` was refused
    /// in 0 ms, while `Z:\Windows\explorer.exe` and `<symlink>\Windows\explorer.exe` — the
    /// SAME file — were both ACCEPTED, and `fs::canonicalize` reported all three as
    /// `\\?\UNC\localhost\C$\Windows\explorer.exe`.
    ///
    /// The resolution is handed IN rather than mounting a share, because a test that shelled
    /// out to `net use` would need privileges, would mutate the machine, and could not run on
    /// the CI matrix. The canonical strings below are verbatim copies of what the real
    /// `fs::canonicalize` returned in that measurement, so they are a recording, not an
    /// invention.
    #[test]
    fn reject_unc_resolution_refuses_a_path_that_resolves_off_the_local_disk() {
        let resolving_to = |canonical: &str| Ok(std::path::PathBuf::from(canonical));

        // The two shapes measured as ACCEPTED before this guard existed.
        for canonical in [
            r"\\?\UNC\localhost\C$\Windows\explorer.exe", // via `net use Z:` and via a symlink
            r"\\?\UNC\evil.example\share\index.db",       // the same shape, attacker-named host
        ] {
            let err = reject_unc_resolution(r"Z:\index.db", resolving_to(canonical))
                .expect_err("a path resolving to a UNC must be refused");
            assert!(
                err.contains("not a local disk"),
                "expected the non-local refusal, got: {err}"
            );
        }

        // The over-rejection trap this guard must NOT fall into: a canonicalized LOCAL path on
        // Windows also begins with `\\`. Matching the raw string would refuse every real file.
        for canonical in [r"\\?\C:\Windows\explorer.exe", r"C:\Windows\explorer.exe"] {
            assert_eq!(
                reject_unc_resolution(r"C:\Windows\explorer.exe", resolving_to(canonical)),
                Ok(()),
                "a local disk path must still pass when canonicalized as {canonical}"
            );
        }

        // Fail CLOSED: we reach this guard only having proved the path IS a file, so a
        // resolution failure is an anomaly and must not be read as permission.
        let err = reject_unc_resolution(
            r"C:\x\index.db",
            Err(std::io::Error::from(std::io::ErrorKind::PermissionDenied)),
        )
        .expect_err("an unresolvable path must be refused, not waved through");
        assert!(
            err.contains("cannot determine whether"),
            "expected the fail-closed refusal, got: {err}"
        );
    }

    /// Every engine RPC must be EITHER a registered Tauri command spelled by the contract,
    /// OR explicitly declared deferred with a reason. Silence is not an option.
    ///
    /// This is the gate for the failure that produced the 11 commands above: those methods
    /// were implemented and tested on the Python side and simply had no command here, so
    /// finished panels had no route to the engine at all. Nothing caught it — a missing
    /// command is not a compile error, and a command that exists but is absent from
    /// `invoke_handler` is equally invisible. Only the registration list is searched here,
    /// for that reason: a name appearing in a comment proves nothing.
    ///
    /// It also pins the NAMING CONTRACT (`a.b` -> `a_b`). The TypeScript `invoke` call
    /// sites are written against that mapping, and a deviation type-checks, passes vitest,
    /// and throws only when a user presses the button — so a compile-time-invisible typo is
    /// exactly the class of bug this has to catch.
    #[test]
    fn every_engine_rpc_has_a_registered_tauri_command() {
        // The two commands that deliberately do NOT follow `a.b` -> `a_b`. Both run on a
        // throwaway index-less engine rather than the managed one, because they are the
        // first-run verbs — a user with no corpus has no engine to forward to — and both
        // are named verb-first to say so. Listed explicitly so a THIRD exception has to be
        // justified here rather than quietly added.
        const NAMED_EXCEPTIONS: [(&str, &str); 2] = [
            ("corpus.create", "create_corpus"),
            ("sources.discover", "discover_sources"),
        ];

        // RPCs deliberately left unreachable, with the reason. Both research methods return
        // empty output by construction — the shipped engine's backends are `MockBackend`,
        // whose `synthesize` returns "" — so a command would only make an empty feature
        // reachable. Checked in BOTH directions below: a name here that IS registered means
        // this list went stale, which matters because the entry doubles as the record of a
        // deferred product decision. Deleting an entry is how you un-defer it.
        const DEFERRED: [(&str, &str); 2] = [
            ("research.synthesize", "no LLM backend; MockBackend returns \"\""),
            ("research.extract_entities", "no LLM backend; MockBackend returns \"\""),
        ];

        let engine = std::fs::read_to_string(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("..")
                .join("..")
                .join("llm_anthology")
                .join("sidecar.py"),
        )
        .expect("read the engine's dispatch table");

        // Dispatch-table entries look like `"a.b": self._handler,`.
        let methods: Vec<&str> = engine
            .lines()
            .filter_map(|line| {
                let rest = line.trim().strip_prefix('"')?;
                let (name, tail) = rest.split_once('"')?;
                if !tail.trim_start().starts_with(": self._") || !name.contains('.') {
                    return None;
                }
                Some(name)
            })
            .collect();
        // Guard the DETECTOR before trusting its verdict: if the scrape silently stopped
        // matching, an empty method list would make this test vacuously green — the same
        // no-op-gate shape it exists to prevent.
        //
        // Anchored on NAMES rather than on a count, because a count guard is pinned to the
        // present. The scrape finds exactly 32 today, so the previous `>= 32` had ZERO margin:
        // deleting one dispatch entry — actually removing `research.synthesize` rather than
        // leaving it registered, say — failed with "found only 31 methods, so this gate is not
        // actually checking anything" and sent the reader hunting a parser bug that did not
        // exist. Equality-to-a-pinned-count cannot serve as a safety check: legitimate work
        // moves the count, and every move then looks like sabotage. A confidently wrong
        // diagnosis costs more than a plain failure, because it spends the time in the wrong
        // place.
        //
        // These three span three namespaces AND three positions in the table (`corpus.*` near
        // the top, `maintenance.*` at the very bottom), so a parser that stopped matching, that
        // only ever matched one entry shape, or that truncated part-way fails on a name it can
        // point at. Losing one of them for real is a breaking change to the data surface and
        // SHOULD require editing this list on purpose.
        const SCRAPE_ANCHORS: [&str; 3] = ["corpus.stats", "graph.roots", "maintenance.plan"];
        for anchor in SCRAPE_ANCHORS {
            assert!(
                methods.contains(&anchor),
                "the dispatch-table scrape did not find `{anchor}`. EITHER the parser has \
                 stopped matching the shape of sidecar.py's dispatch table, OR `{anchor}` was \
                 genuinely removed from the engine — grep sidecar.py for it to tell which. If \
                 it is really gone, update SCRAPE_ANCHORS deliberately. The scrape found {} \
                 methods: {methods:?}",
                methods.len()
            );
        }
        // Belt and braces, and deliberately LOOSE — a handful, not the present count. This can
        // only fire on a scrape that collapsed, never on ordinary add-or-remove work, so it
        // never needs re-baselining.
        assert!(
            methods.len() >= 8,
            "the dispatch-table scrape found only {} methods, far below any plausible engine \
             surface — the parser is broken rather than the engine being small: {methods:?}",
            methods.len()
        );

        let me = include_str!("lib.rs");
        let start = me
            .find("tauri::generate_handler![")
            .expect("the invoke_handler list must be findable");
        let end = me[start..].find("])").expect("the handler list must terminate");
        let registered: Vec<&str> = me[start..start + end]
            .lines()
            .map(|l| l.trim().trim_end_matches(','))
            .collect();

        let command_for = |method: &str| -> String {
            NAMED_EXCEPTIONS
                .iter()
                .find(|(rpc, _)| *rpc == method)
                .map(|(_, cmd)| (*cmd).to_string())
                .unwrap_or_else(|| method.replace('.', "_"))
        };

        // (1) Every non-deferred RPC must be reachable.
        let missing: Vec<String> = methods
            .iter()
            .filter(|m| !DEFERRED.iter().any(|(rpc, _)| rpc == *m))
            .filter(|m| !registered.contains(&command_for(m).as_str()))
            .map(|m| format!("{m} -> expected command `{}`", command_for(m)))
            .collect();
        assert!(
            missing.is_empty(),
            "engine RPCs with no registered Tauri command and no DEFERRED entry — the UI \
             cannot reach these, and nothing records that as intentional: {missing:#?}"
        );

        // (2) Every deferred RPC must ACTUALLY be unreachable, and must still exist in the
        //     engine. A deferred name that is registered, or that the engine no longer
        //     serves, means this list is lying about the shipped surface.
        let stale: Vec<String> = DEFERRED
            .iter()
            .filter_map(|(rpc, why)| {
                if registered.contains(&command_for(rpc).as_str()) {
                    Some(format!("{rpc} is DEFERRED ({why}) but IS registered"))
                } else if !methods.contains(rpc) {
                    Some(format!("{rpc} is DEFERRED but the engine no longer serves it"))
                } else {
                    None
                }
            })
            .collect();
        assert!(stale.is_empty(), "the DEFERRED list is out of date: {stale:#?}");
    }

    #[test]
    fn app_info_reports_name_version_and_deferred_engine() {
        let info = app_info();
        assert_eq!(info["name"], "Cockpit");
        assert_eq!(info["engine"], "not-wired");
        assert!(
            info["version"].as_str().is_some_and(|v| !v.is_empty()),
            "version must be a non-empty string, got {:?}",
            info["version"]
        );
    }
}
