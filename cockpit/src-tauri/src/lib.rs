use serde_json::{json, Value};

/// Return basic cockpit metadata for the frontend status line.
///
/// The AI-session analysis engine (a python-build-standalone sidecar spoken to
/// over stdio NDJSON) is NOT yet wired — see `src-tauri/binaries/README.md`.
/// The `engine` field advertises that deferred state so the bare window is
/// honest about what is (not) connected.
#[tauri::command]
fn app_info() -> Value {
    json!({
        "name": "Cockpit",
        "version": env!("CARGO_PKG_VERSION"),
        "engine": "not-wired",
    })
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![app_info])
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
