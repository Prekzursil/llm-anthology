//! Sidecar transport: spawn the Python analysis engine and speak stdio NDJSON
//! JSON-RPC 2.0 to it.
//!
//! The engine is the committed `aisr.sidecar` module — launched as
//! `python -m aisr.sidecar --index <path>` — which reads one compact JSON object
//! per line off stdin and writes one flushed JSON object per line to stdout (NOT
//! HTTP, NOT a socket). This module owns two pieces:
//!
//! * [`jsonrpc_roundtrip`] — the pure framing function: serialize a request, write
//!   it `\n`-terminated + flushed, read exactly ONE response line, parse it, and
//!   correlate by id. It is generic over any [`Write`] + [`BufRead`] so the framing
//!   is exercised by unit tests against in-memory mocks, with no real process.
//! * [`SidecarClient`] — owns the child process plus its piped stdin/stdout and a
//!   monotonic request-id counter; its [`SidecarClient::call`] delegates to the
//!   framing function over the real pipes. A single mutex-guarded client (held in
//!   Tauri state) makes every request/response strictly sequential, so id
//!   correlation is trivially satisfied.

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use serde_json::{json, Value};

/// Perform ONE JSON-RPC 2.0 request/response round trip over an NDJSON stream pair.
///
/// Writes `{"jsonrpc":"2.0","id":..,"method":..,"params":..}\n` (flushed) to
/// `writer`, then reads exactly one line from `reader` and parses it as a JSON-RPC
/// response, returning the `result` value or a stringified error. Generic over the
/// stream types so the framing can be tested against in-memory buffers.
fn jsonrpc_roundtrip<W, R>(
    writer: &mut W,
    reader: &mut R,
    id: u64,
    method: &str,
    params: &Value,
) -> Result<Value, String>
where
    W: Write,
    R: BufRead,
{
    // --- frame + write the request: one compact line, \n-terminated, flushed ---
    let request = json!({
        "jsonrpc": "2.0",
        "id": id,
        "method": method,
        "params": params,
    });
    let mut line =
        serde_json::to_string(&request).map_err(|e| format!("serialize request: {e}"))?;
    line.push('\n');
    writer
        .write_all(line.as_bytes())
        .map_err(|e| format!("write request: {e}"))?;
    writer.flush().map_err(|e| format!("flush request: {e}"))?;

    // --- read exactly one response line ---
    let mut buf = String::new();
    let n = reader
        .read_line(&mut buf)
        .map_err(|e| format!("read response: {e}"))?;
    if n == 0 {
        return Err("sidecar closed stdout (EOF) before responding".to_string());
    }
    let trimmed = buf.trim_end();
    let response: Value =
        serde_json::from_str(trimmed).map_err(|e| format!("parse response {trimmed:?}: {e}"))?;

    // --- correlate by id: the transport is strictly sequential (one request, one
    // response line), so the reply id must equal the request id. A JSON-RPC parse
    // error carries a null id, which we tolerate here and surface via `error` below.
    let reply_id = response.get("id");
    let id_ok = matches!(reply_id, Some(v) if v.as_u64() == Some(id))
        || reply_id == Some(&Value::Null);
    if !id_ok {
        return Err(format!(
            "response id mismatch: expected {id}, got {reply_id:?}"
        ));
    }

    // --- result | error ---
    if let Some(err) = response.get("error") {
        return Err(format!("rpc error (id {id}): {err}"));
    }
    response
        .get("result")
        .cloned()
        .ok_or_else(|| format!("malformed response (neither result nor error): {trimmed}"))
}

/// A spawned engine sidecar and its stdio JSON-RPC transport.
pub struct SidecarClient {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    next_id: u64,
}

impl SidecarClient {
    /// Spawn `python -m aisr.sidecar --index <index_path>` with stdin/stdout/stderr
    /// piped, ready to answer JSON-RPC requests.
    pub fn spawn(index_path: &str) -> Result<Self, String> {
        let mut command = Command::new("python");
        command
            .arg("-m")
            .arg("aisr.sidecar")
            .arg("--index")
            .arg(index_path)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            // stderr is piped per the transport contract. It is NOT drained here; the
            // sidecar reports operational failures as JSON-RPC error responses on
            // stdout and keeps stderr near-empty, so a plain pipe is safe for this
            // basic transport. A stderr drain thread is left to the e2e/hardening bite.
            .stderr(Stdio::piped());

        // Suppress the transient console window the child would otherwise flash on
        // Windows. This is the ONLY platform-specific spawn hardening here.
        //
        // TODO(hardening): BASIC spawn only. A Windows Job Object (kill-on-close, so
        // no orphaned engine can outlive the app) and an AppContainer sandbox
        // membrane around the engine process are BOTH a DEFERRED hardening spike —
        // see `cockpit/src-tauri/binaries/README.md` ("Lifecycle + isolation") and
        // the SOTA-DECISIONS record. Do not implement them in this transport.
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.creation_flags(CREATE_NO_WINDOW);
        }

        let mut child = command
            .spawn()
            .map_err(|e| format!("failed to spawn sidecar (python -m aisr.sidecar): {e}"))?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| "sidecar stdin was not piped".to_string())?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "sidecar stdout was not piped".to_string())?;
        Ok(Self {
            child,
            stdin,
            stdout: BufReader::new(stdout),
            next_id: 1,
        })
    }

    /// Send one JSON-RPC request and block for its response, returning the `result`
    /// value (or a stringified error). Ids are assigned monotonically.
    pub fn call(&mut self, method: &str, params: &Value) -> Result<Value, String> {
        let id = self.next_id;
        self.next_id = self.next_id.wrapping_add(1);
        jsonrpc_roundtrip(&mut self.stdin, &mut self.stdout, id, method, params)
    }
}

impl Drop for SidecarClient {
    fn drop(&mut self) {
        // Best-effort reap so a replaced/closed client does not leak the engine
        // process. NOTE: this is a plain kill, NOT the guaranteed kill-on-close of a
        // Windows Job Object — see the TODO(hardening) in `spawn`.
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[cfg(test)]
mod tests {
    use super::jsonrpc_roundtrip;
    use serde_json::{json, Value};
    use std::io::Cursor;

    #[test]
    fn roundtrip_frames_one_newline_terminated_line_and_returns_result() {
        let mut written: Vec<u8> = Vec::new();
        let mut reader = Cursor::new(b"{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"ok\":true}}\n".to_vec());

        let out = jsonrpc_roundtrip(&mut written, &mut reader, 1, "health.ping", &json!({}))
            .expect("happy-path round trip");
        assert_eq!(out, json!({ "ok": true }));

        // Framing: the request must be exactly ONE line, newline-terminated.
        let text = String::from_utf8(written).unwrap();
        assert!(text.ends_with('\n'), "request must be newline-terminated: {text:?}");
        assert_eq!(
            text.matches('\n').count(),
            1,
            "request must be exactly one line: {text:?}"
        );

        // Structure survives serialization (order-independent assertion).
        let sent: Value = serde_json::from_str(text.trim_end()).unwrap();
        assert_eq!(sent["jsonrpc"], "2.0");
        assert_eq!(sent["id"], 1);
        assert_eq!(sent["method"], "health.ping");
        assert_eq!(sent["params"], json!({}));
    }

    #[test]
    fn roundtrip_forwards_params_verbatim() {
        let mut written: Vec<u8> = Vec::new();
        let mut reader = Cursor::new(b"{\"jsonrpc\":\"2.0\",\"id\":7,\"result\":[]}\n".to_vec());

        let params = json!({ "thread_id": "t-42", "depth": 3 });
        jsonrpc_roundtrip(&mut written, &mut reader, 7, "graph.subtree", &params)
            .expect("round trip");

        let sent: Value = serde_json::from_str(String::from_utf8(written).unwrap().trim_end()).unwrap();
        assert_eq!(sent["method"], "graph.subtree");
        assert_eq!(sent["params"], params);
    }

    #[test]
    fn roundtrip_surfaces_error_envelope() {
        let mut written: Vec<u8> = Vec::new();
        let mut reader = Cursor::new(
            b"{\"jsonrpc\":\"2.0\",\"id\":1,\"error\":{\"code\":-32000,\"message\":\"corpus not indexed\"}}\n"
                .to_vec(),
        );

        let err = jsonrpc_roundtrip(&mut written, &mut reader, 1, "corpus.stats", &json!({}))
            .expect_err("error envelope must map to Err");
        assert!(err.contains("corpus not indexed"), "got: {err}");
        assert!(err.contains("-32000"), "got: {err}");
    }

    #[test]
    fn roundtrip_reports_eof_when_stream_closed() {
        let mut written: Vec<u8> = Vec::new();
        let mut reader = Cursor::new(Vec::<u8>::new());

        let err = jsonrpc_roundtrip(&mut written, &mut reader, 1, "health.ping", &json!({}))
            .expect_err("empty stream must map to Err");
        assert!(err.to_lowercase().contains("eof"), "got: {err}");
    }

    #[test]
    fn roundtrip_rejects_id_mismatch() {
        let mut written: Vec<u8> = Vec::new();
        let mut reader = Cursor::new(b"{\"jsonrpc\":\"2.0\",\"id\":99,\"result\":{}}\n".to_vec());

        let err = jsonrpc_roundtrip(&mut written, &mut reader, 1, "health.ping", &json!({}))
            .expect_err("a mismatched reply id must map to Err");
        assert!(err.contains("mismatch"), "got: {err}");
    }

    #[test]
    fn roundtrip_rejects_malformed_line() {
        let mut written: Vec<u8> = Vec::new();
        let mut reader = Cursor::new(b"this is not json\n".to_vec());

        let err = jsonrpc_roundtrip(&mut written, &mut reader, 1, "health.ping", &json!({}))
            .expect_err("a non-JSON line must map to Err");
        assert!(err.contains("parse response"), "got: {err}");
    }

    #[test]
    fn sequential_roundtrips_frame_independent_lines() {
        let mut written: Vec<u8> = Vec::new();
        let mut reader = Cursor::new(
            b"{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":1}\n{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":2}\n"
                .to_vec(),
        );

        // Two calls over the SAME buffers: each writes its own line and consumes
        // exactly one response line (proving the reader stops at the first `\n`).
        let a = jsonrpc_roundtrip(&mut written, &mut reader, 1, "graph.roots", &json!({ "limit": 10 }))
            .expect("first round trip");
        let b = jsonrpc_roundtrip(&mut written, &mut reader, 2, "graph.roots", &json!({ "offset": 5 }))
            .expect("second round trip");
        assert_eq!(a, json!(1));
        assert_eq!(b, json!(2));

        let text = String::from_utf8(written).unwrap();
        assert_eq!(
            text.matches('\n').count(),
            2,
            "two requests must frame two independent lines: {text:?}"
        );
        for l in text.lines() {
            let v: Value = serde_json::from_str(l).expect("each framed line parses independently");
            assert_eq!(v["jsonrpc"], "2.0");
        }
    }

    /// END-TO-END over the REAL wire: build a SYNTHETIC corpus index with a committed
    /// Python fixture, then spawn the ACTUAL `python -m aisr.sidecar --index <path>`
    /// process through the production [`SidecarClient`] and round-trip health.ping +
    /// corpus.stats + graph.roots over genuine OS stdio pipes. This is the Rust<->Python
    /// proof the unit tests above (in-memory `Cursor` mocks) cannot give: it exercises
    /// process spawn, real NDJSON framing across a pipe, and the sidecar's own engine.
    ///
    /// `aisr` is a source tree at the repo root (not pip-installed), so the repo root is
    /// put on PYTHONPATH for both the builder and the spawned sidecar. Assertions match
    /// the fixture's known corpus EXACTLY.
    #[test]
    fn e2e_roundtrip_real_python_sidecar() {
        use super::SidecarClient;
        use std::path::PathBuf;
        use std::process::Command;
        use std::time::{SystemTime, UNIX_EPOCH};

        // <manifest> = cockpit/src-tauri  ->  up two  ->  repo root (holds `aisr/`).
        let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let repo_root = manifest
            .parent()
            .and_then(|p| p.parent())
            .expect("repo root is two levels above the crate manifest")
            .to_path_buf();
        // Both subprocesses need `import aisr` to resolve. `Command` inherits this.
        // (`std::env::set_var` is safe under this crate's 2021 edition.)
        std::env::set_var("PYTHONPATH", &repo_root);

        // A unique temp index so parallel/repeat runs never collide.
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let index_path =
            std::env::temp_dir().join(format!("aisr_e2e_{}_{}.db", std::process::id(), nanos));

        // Build the KNOWN synthetic corpus (3 convs / 3 threads / 2 edges / 1 root).
        let fixture = manifest
            .join("tests")
            .join("fixtures")
            .join("build_synth_index.py");
        let built = Command::new("python")
            .arg(&fixture)
            .arg(&index_path)
            .output()
            .expect("failed to launch python to build the synthetic index (python on PATH?)");
        assert!(
            built.status.success(),
            "synthetic-index builder failed: status={:?}\nstdout={}\nstderr={}",
            built.status,
            String::from_utf8_lossy(&built.stdout),
            String::from_utf8_lossy(&built.stderr)
        );
        assert!(index_path.exists(), "builder did not create {index_path:?}");

        let index_str = index_path.to_str().expect("temp index path is valid UTF-8");
        let mut client = SidecarClient::spawn(index_str).expect("spawn real python sidecar");

        // 1) health.ping — the corpus IS attached, so corpus_ready must be true.
        let health = client.call("health.ping", &json!({})).expect("health.ping call");
        assert_eq!(health["ok"], json!(true), "health payload: {health}");
        assert_eq!(health["corpus_ready"], json!(true), "corpus must be ready: {health}");
        assert!(
            health["engine_version"].as_str().is_some_and(|s| !s.is_empty()),
            "engine_version must be a non-empty string: {health}"
        );
        assert!(health["ir_version"].is_number(), "ir_version must be numeric: {health}");

        // 2) corpus.stats — must equal the fixture's known aggregates.
        let stats = client.call("corpus.stats", &json!({})).expect("corpus.stats call");
        assert_eq!(stats["conversations"], json!(3), "stats: {stats}");
        assert_eq!(stats["threads"], json!(3), "stats: {stats}");
        assert_eq!(stats["edges"], json!(2), "stats: {stats}");
        assert_eq!(stats["records"], json!(6), "records = SUM(turn_count): {stats}");
        assert_eq!(stats["providers"]["codex"], json!(2), "stats: {stats}");
        assert_eq!(stats["providers"]["claude"], json!(1), "stats: {stats}");

        // 3) graph.roots — exactly one root (root-a), fan-out 2, provider codex.
        let roots = client.call("graph.roots", &json!({})).expect("graph.roots call");
        let arr = roots.as_array().expect("graph.roots returns an array");
        assert_eq!(arr.len(), 1, "exactly one root expected: {roots}");
        assert_eq!(arr[0]["id"], json!("root-a"), "roots: {roots}");
        assert_eq!(arr[0]["child_count"], json!(2), "roots: {roots}");
        assert_eq!(arr[0]["provider"], json!("codex"), "roots: {roots}");

        // Dropping the client kills+reaps the process; then best-effort clean the temp.
        drop(client);
        let _ = std::fs::remove_file(&index_path);
    }
}
