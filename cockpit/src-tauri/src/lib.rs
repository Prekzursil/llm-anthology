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

/// Reject an `index_path` that is not an existing file. Split out from the command so the
/// rule is unit-testable without constructing a Tauri `State`.
fn validate_index_path(index_path: &str) -> Result<(), String> {
    let path = std::path::Path::new(index_path);
    if path.is_file() {
        return Ok(());
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
    let result = client.call("corpus.create", &json!({ "index_path": index_path }));
    // `client` drops here, reaping the throwaway engine.
    result
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
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::{app_info, validate_index_path};

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
