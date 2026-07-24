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

/// (Re)spawn the engine sidecar pointed at `index_path`. Replacing the previous
/// client drops it, which reaps the old process (best-effort — see `SidecarClient`).
#[tauri::command]
fn open_corpus(state: State<'_, EngineState>, index_path: String) -> Result<Value, String> {
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(EngineState::default())
        .invoke_handler(tauri::generate_handler![
            app_info,
            open_corpus,
            health_ping,
            corpus_stats,
            graph_roots,
            graph_children,
            graph_subtree,
            graph_ancestors,
            search_query,
            thread_get,
            conversation_get,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::app_info;

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
