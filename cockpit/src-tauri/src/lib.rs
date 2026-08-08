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
        assert!(
            methods.len() >= 32,
            "the dispatch-table scrape found only {} methods, so this gate is not actually \
             checking anything: {methods:?}",
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
