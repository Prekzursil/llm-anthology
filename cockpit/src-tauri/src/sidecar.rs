//! Sidecar transport: spawn the Python analysis engine and speak stdio NDJSON
//! JSON-RPC 2.0 to it.
//!
//! The engine is the committed `llm_anthology.sidecar` module — launched as
//! `python -m llm_anthology.sidecar --index <path>` — which reads one compact JSON object
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
use std::path::Path;

use serde_json::{json, Value};

// Windows-only: the raw `CreateProcessW` hardening (KILL_ON_JOB_CLOSE Job Object
// reap + AppContainer network membrane + CREATE_NO_WINDOW) that backs
// `SidecarClient::spawn` on Windows. Non-Windows falls back to `std::process`.
#[cfg(windows)]
mod hardened_spawn;

/// Perform ONE JSON-RPC 2.0 request/response round trip over an NDJSON stream pair.
///
/// Writes `{"jsonrpc":"2.0","id":..,"method":..,"params":..}\n` (flushed) to
/// `writer`, then reads exactly one line from `reader` and parses it as a JSON-RPC
/// response, returning the `result` value or a stringified error. Generic over the
/// stream types so the framing can be tested against in-memory buffers.
fn jsonrpc_roundtrip(
    writer: &mut dyn Write,
    reader: &mut dyn BufRead,
    id: u64,
    method: &str,
    params: &Value,
) -> Result<Value, String> {
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
///
/// On Windows the child is launched through [`hardened_spawn`] — ONE raw
/// `CreateProcessW` that places the engine under a `KILL_ON_JOB_CLOSE` Job
/// Object (a guaranteed reap of the whole subtree even on an ABRUPT app death,
/// which the previous `Drop::kill` missed) with `CREATE_NO_WINDOW`, and
/// optionally inside an AppContainer network membrane. The transport itself is
/// unchanged across platforms: `stdin`/`stdout` are the parent-side pipe ends
/// held behind trait objects so the framing code is identical everywhere.
pub struct SidecarClient {
    stdin: Box<dyn Write + Send>,
    stdout: Box<dyn BufRead + Send>,
    /// Windows: holds the process + Job Object handles; dropping it reaps the
    /// engine (the Job Object close is the hard guarantee).
    #[cfg(windows)]
    _reaper: hardened_spawn::Reaper,
    /// Windows: the child's stderr pipe (parent end), held un-drained so it never
    /// fills — the sidecar keeps stderr near-empty (mirrors the prior transport).
    #[cfg(windows)]
    _stderr: std::fs::File,
    /// Non-Windows: the std child, reaped by [`SidecarClient`]'s `Drop`.
    #[cfg(not(windows))]
    _child: std::process::Child,
    next_id: u64,
}

/// Resolve the engine interpreter, given the directory holding the app executable.
///
/// A PACKAGED install ships a relocatable CPython (python-build-standalone) beside the
/// binary as a Tauri resource, with the engine package already installed into it — so the
/// app does not require the user to have Python at all. A DEV build ships none and falls
/// back to `python` on PATH, which is also what every test in this file relies on.
///
/// Pure and directory-taking, so BOTH branches are testable without an installed app —
/// the bundled branch is otherwise only reachable from a real installation, which is
/// exactly the kind of path that ships broken.
///
/// Note this returns a path, not a decision about hardening: the spawn flags (Job Object
/// reap, CREATE_NO_WINDOW, optional AppContainer membrane) are unchanged and apply
/// identically to the bundled interpreter.
pub(crate) fn engine_python_in(exe_dir: &Path) -> String {
    let bundled = if cfg!(windows) {
        exe_dir.join("engine").join("python.exe")
    } else {
        exe_dir.join("engine").join("bin").join("python3")
    };
    if bundled.is_file() {
        return bundled.to_string_lossy().into_owned();
    }
    "python".to_string()
}

/// The interpreter this process should spawn. Falls back to `python` if the executable's
/// own location cannot be determined, so a resolution failure degrades to today's
/// behaviour rather than to no engine at all.
fn engine_python() -> String {
    std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(engine_python_in))
        .unwrap_or_else(|| "python".to_string())
}

impl SidecarClient {
    /// Spawn `<engine-python> -m llm_anthology.sidecar --index <index_path>` with
    /// stdin/stdout/stderr piped, ready to answer JSON-RPC requests.
    ///
    /// Windows: raw `CreateProcessW` under a `KILL_ON_JOB_CLOSE` Job Object +
    /// `CREATE_NO_WINDOW` — the reliable reap + no-window core. The AppContainer
    /// network membrane is a first-class, tested spawn mode
    /// (via `spawn_hardened` with `Membrane::AppContainer`); it is left opt-in HERE because a
    /// sandboxed engine also needs its export DESTINATION granted to the package
    /// SID, an export-lifecycle concern rather than a fixed spawn concern.
    pub fn spawn(index_path: &str) -> Result<Self, String> {
        Self::spawn_platform(Some(index_path))
    }

    /// Spawn an engine with NO index attached.
    ///
    /// Needed to break a genuine chicken-and-egg between two individually-correct rules:
    /// `open_corpus` refuses a path that is not an existing file (so "open" can never
    /// resurrect a deleted corpus as a silently-empty one), while `corpus.create` — the verb
    /// that MAKES that file — is only reachable through a running engine. A user creating
    /// their first corpus has neither.
    ///
    /// The engine already supports this: its `main` treats a missing `--index` as "no corpus"
    /// (`conn = corpus.open_index(path) if path else None`), `health.ping` still answers with
    /// `corpus_ready` false, and `corpus.create` is deliberately the one data method that does
    /// not require an attached corpus. So a short-lived index-less engine can create the file,
    /// after which the normal `open_corpus` path takes over.
    ///
    /// Every hardening property of the normal spawn still applies — same `spawn_platform`,
    /// so the Job Object reap and `CREATE_NO_WINDOW` are unchanged.
    pub fn spawn_without_index() -> Result<Self, String> {
        Self::spawn_platform(None)
    }

    #[cfg(windows)]
    fn spawn_platform(index_path: Option<&str>) -> Result<Self, String> {
        use hardened_spawn::{spawn_hardened, HardenedSpawn, SpawnOpts};
        let mut args: Vec<&str> = vec!["-m", "llm_anthology.sidecar"];
        if let Some(p) = index_path {
            args.push("--index");
            args.push(p);
        }
        let python = engine_python();
        let HardenedSpawn { stdin, stdout, stderr, reaper } =
            spawn_hardened(&python, &args, &SpawnOpts::job_only())?;
        Ok(Self {
            stdin: Box::new(stdin),
            stdout: Box::new(BufReader::new(stdout)),
            _reaper: reaper,
            _stderr: stderr,
            next_id: 1,
        })
    }

    #[cfg(not(windows))]
    fn spawn_platform(index_path: Option<&str>) -> Result<Self, String> {
        use std::process::{Command, Stdio};
        let mut cmd = Command::new(engine_python());
        cmd.arg("-m").arg("llm_anthology.sidecar");
        if let Some(p) = index_path {
            cmd.arg("--index").arg(p);
        }
        let mut child = cmd
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            // stderr is piped per the transport contract. It is NOT drained here; the
            // sidecar reports operational failures as JSON-RPC error responses on
            // stdout and keeps stderr near-empty, so a plain pipe is safe here.
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| format!("failed to spawn sidecar (python -m llm_anthology.sidecar): {e}"))?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| "sidecar stdin was not piped".to_string())?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "sidecar stdout was not piped".to_string())?;
        Ok(Self {
            stdin: Box::new(stdin),
            stdout: Box::new(BufReader::new(stdout)),
            _child: child,
            next_id: 1,
        })
    }

    /// Send one JSON-RPC request and block for its response, returning the `result`
    /// value (or a stringified error). Ids are assigned monotonically.
    pub fn call(&mut self, method: &str, params: &Value) -> Result<Value, String> {
        let id = self.next_id;
        self.next_id = self.next_id.wrapping_add(1);
        jsonrpc_roundtrip(&mut *self.stdin, &mut *self.stdout, id, method, params)
    }
}

#[cfg(not(windows))]
impl Drop for SidecarClient {
    fn drop(&mut self) {
        // Non-Windows: a std `Child` does NOT reap on drop, so kill+wait explicitly.
        // (Windows reaping is owned by the Job Object held inside `Reaper`.)
        let _ = self._child.kill();
        let _ = self._child.wait();
    }
}

#[cfg(test)]
mod tests {
    use super::{engine_python_in, jsonrpc_roundtrip};
    use serde_json::{json, Value};
    use std::io::Cursor;

    /// A dev build (no bundled interpreter beside the exe) must fall back to PATH.
    #[test]
    fn engine_python_falls_back_to_path_when_nothing_is_bundled() {
        let dir = std::env::temp_dir().join(format!(
            "llm_anthology_enginepy_none_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        assert_eq!(engine_python_in(&dir), "python");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// A PACKAGED install ships a relocatable CPython beside the binary and must prefer it,
    /// so the app does not depend on the user having Python.
    ///
    /// This branch is otherwise reachable only from a real installation — exactly the kind
    /// of path that ships broken — so it is exercised here against a stand-in file.
    #[test]
    fn engine_python_prefers_a_bundled_interpreter() {
        let dir = std::env::temp_dir().join(format!(
            "llm_anthology_enginepy_bundled_{}", std::process::id()));
        let engine = dir.join("engine");
        std::fs::create_dir_all(&engine).unwrap();
        let exe = if cfg!(windows) {
            engine.join("python.exe")
        } else {
            let bin = engine.join("bin");
            std::fs::create_dir_all(&bin).unwrap();
            bin.join("python3")
        };
        std::fs::write(&exe, b"stand-in").unwrap();

        let resolved = engine_python_in(&dir);
        assert_ne!(resolved, "python", "a bundled interpreter must win over PATH");
        assert_eq!(std::path::Path::new(&resolved), exe.as_path());
        // An absolute path is what hardened_spawn::resolve_program accepts directly
        // (it short-circuits the PATH walk for an absolute, existing file).
        assert!(std::path::Path::new(&resolved).is_absolute());
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// A directory that merely CONTAINS an `engine` dir, with no interpreter in it, is not
    /// a packaged install — a half-staged bundle must not be mistaken for a complete one.
    #[test]
    fn engine_python_ignores_an_empty_engine_directory() {
        let dir = std::env::temp_dir().join(format!(
            "llm_anthology_enginepy_empty_{}", std::process::id()));
        std::fs::create_dir_all(dir.join("engine")).unwrap();
        assert_eq!(engine_python_in(&dir), "python");
        let _ = std::fs::remove_dir_all(&dir);
    }

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

    /// `sources.discover` over the real wire, with NO index attached.
    ///
    /// This is the first-run call, so the index-less path is the ONLY one it ever takes. A
    /// test that attached a corpus first would exercise a situation that never happens.
    /// Asserts shape rather than contents, since findings depend on the host machine — but a
    /// shape assertion over a real process still proves the thing worth proving: that the
    /// call reaches Python, scans, serialises, and comes back without a corpus.
    #[test]
    fn e2e_discover_sources_without_an_index() {
        use super::SidecarClient;
        use std::path::PathBuf;

        let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let repo_root = manifest
            .parent()
            .and_then(|p| p.parent())
            .expect("repo root is two levels above the crate manifest")
            .to_path_buf();
        std::env::set_var("PYTHONPATH", &repo_root);

        let mut engine = SidecarClient::spawn_without_index().expect("index-less spawn");
        let out = engine
            .call("sources.discover", &json!({}))
            .expect("sources.discover with no corpus attached");

        assert!(
            out["findings"].is_array(),
            "findings must be an array the UI can iterate: {out}"
        );
        // `roots_scanned` must be REPORTED, not necessarily non-zero. It counts only roots
        // whose base directory exists (`discover.py:528`), so on a bare CI runner with no
        // ~/.codex, ~/.grok, ~/.claude or ~/Downloads it is legitimately 0. The previous
        // `>= 1` therefore asserted a property of the DEVELOPER'S MACHINE, not of this code,
        // and was measured deterministically red in a clean Linux container — one
        // `mkdir ~/Downloads` flipped it green, which is the tell.
        //
        // This test's job is the WIRE: that the index-less engine answers `sources.discover`
        // with the shape the UI iterates. Whether discovery FINDS anything is behaviour of
        // `discover.py`, which is tested properly on the Python side with an injected `Roots`
        // pointing at a temp tree — exactly what that dataclass exists for. Asserting it here
        // conflated the two and bought nothing the Python tests do not already cover.
        assert!(
            out["stats"]["roots_scanned"].is_number(),
            "the scan must report how many roots it scanned: {out}"
        );
        // Bounded scanning is a hard requirement — an unbounded walk would hang the UI.
        assert!(
            out["stats"]["files_examined"].is_number(),
            "the scan must report what it examined: {out}"
        );
    }

    /// The CREATE-then-OPEN journey over the real wire — the chicken-and-egg proof.
    ///
    /// Two individually-correct rules deadlock a first-time user: `open_corpus` refuses a path
    /// that is not an existing file, and `corpus.create` (the verb that makes that file) is
    /// only reachable through a running engine. A user with no corpus has neither. This proves
    /// the index-less spawn breaks that, and — the part worth testing — that it does so
    /// WITHOUT weakening either rule: the second create must still refuse to clobber.
    ///
    /// A compile is not evidence for any of this: every assertion below crosses a real process
    /// boundary into Python.
    #[test]
    fn e2e_create_without_an_index_then_open_the_file_it_made() {
        use super::SidecarClient;
        use std::path::PathBuf;
        use std::time::{SystemTime, UNIX_EPOCH};

        let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let repo_root = manifest
            .parent()
            .and_then(|p| p.parent())
            .expect("repo root is two levels above the crate manifest")
            .to_path_buf();
        std::env::set_var("PYTHONPATH", &repo_root);

        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let index = std::env::temp_dir().join(format!("anth_create_{stamp}.sqlite"));
        let index_str = index.to_string_lossy().to_string();
        assert!(!index.exists(), "precondition: the index must not exist yet");

        // 1. An engine with NO index must still start and answer.
        let mut engine = SidecarClient::spawn_without_index().expect("index-less spawn");
        let health = engine
            .call("health.ping", &json!({}))
            .expect("health.ping with no corpus attached");
        assert_eq!(
            health["corpus_ready"],
            json!(false),
            "an index-less engine must report corpus_ready false: {health}"
        );

        // 2. It can create the file the user does not have yet.
        let created = engine
            .call("corpus.create", &json!({ "index_path": index_str }))
            .expect("corpus.create through an index-less engine");
        assert!(
            index.exists(),
            "corpus.create must leave a real file on disk: {created}"
        );

        // 3. Creating again must REFUSE — open-refuses-to-create is only half the guarantee;
        //    create-must-not-clobber is the other half, and it is what stops a stray second
        //    call from replacing a corpus the user already has with an empty one.
        let clobber = engine.call("corpus.create", &json!({ "index_path": index_str }));
        assert!(
            clobber.is_err(),
            "a second create must refuse to clobber, got: {clobber:?}"
        );
        drop(engine);

        // 4. The created file must satisfy the REAL open path — the whole point of creating it.
        let mut opened = SidecarClient::spawn(&index_str).expect("open the created index");
        let h2 = opened
            .call("health.ping", &json!({}))
            .expect("health.ping on the created index");
        assert_eq!(
            h2["corpus_ready"],
            json!(true),
            "the created index must attach: {h2}"
        );
        let stats = opened
            .call("corpus.stats", &json!({}))
            .expect("corpus.stats on the created index");
        assert_eq!(
            stats["conversations"],
            json!(0),
            "a freshly created corpus is EMPTY, not broken: {stats}"
        );
        drop(opened);

        let _ = std::fs::remove_file(&index);
    }

    /// END-TO-END over the REAL wire: build a SYNTHETIC corpus index with a committed
    /// Python fixture, then spawn the ACTUAL `python -m llm_anthology.sidecar --index <path>`
    /// process through the production [`SidecarClient`] and round-trip health.ping +
    /// corpus.stats + graph.roots over genuine OS stdio pipes. This is the Rust<->Python
    /// proof the unit tests above (in-memory `Cursor` mocks) cannot give: it exercises
    /// process spawn, real NDJSON framing across a pipe, and the sidecar's own engine.
    ///
    /// `llm-anthology` is a source tree at the repo root (not pip-installed), so the repo root is
    /// put on PYTHONPATH for both the builder and the spawned sidecar. Assertions match
    /// the fixture's known corpus EXACTLY.
    #[test]
    fn e2e_roundtrip_real_python_sidecar() {
        use super::SidecarClient;
        use std::path::PathBuf;
        use std::process::Command;
        use std::time::{SystemTime, UNIX_EPOCH};

        // <manifest> = cockpit/src-tauri  ->  up two  ->  repo root (holds `llm_anthology/`).
        let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let repo_root = manifest
            .parent()
            .and_then(|p| p.parent())
            .expect("repo root is two levels above the crate manifest")
            .to_path_buf();
        // Both subprocesses need `import llm_anthology` to resolve. `Command` inherits this.
        // (`std::env::set_var` is safe under this crate's 2021 edition.)
        std::env::set_var("PYTHONPATH", &repo_root);

        // A unique temp index so parallel/repeat runs never collide.
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let index_path =
            std::env::temp_dir().join(format!("llm_anthology_e2e_{}_{}.db", std::process::id(), nanos));

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

    /// END-TO-END over the REAL wire for the Phase-3 TIME-TRAVEL + EXPORT surface: build
    /// the same SYNTHETIC index (its thread births SPAN TIME and carry differing tokens),
    /// spawn the ACTUAL `python -m llm_anthology.sidecar` process through [`SidecarClient`], and
    /// round-trip the SIX new methods over genuine OS stdio NDJSON pipes —
    /// `graph.timeline`, `graph.at`, `graph.rollup`, `graph.diff` (the as-of DELTA form),
    /// `export.plan`, and a POSITIVE `export.run` that writes a real artifact to a temp
    /// path (asserted present on disk AND re-parsed to prove the graph round-trips).
    ///
    /// It also asserts the NEGATIVE path-guard end to end: an unsafe (relative / UNC)
    /// `export.run` dest is rejected over the wire with NO file written. The FIDELITY-gate
    /// block (ok:false + a diff report) is NOT exercised here on purpose: the graph
    /// serialize<->parse round-trip is faithful by construction and the sidecar bundles no
    /// transcripts, so a corpus alone can never trip the gate across the wire — that
    /// negative is proven at the dispatch/gate layer (tests/test_sidecar.py::
    /// test_e2e_export_run_negative_gate_* and tests/test_export.py).
    #[test]
    fn e2e_timetravel_export_roundtrip_real_python_sidecar() {
        use super::SidecarClient;
        use std::path::PathBuf;
        use std::process::Command;
        use std::time::{SystemTime, UNIX_EPOCH};

        let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let repo_root = manifest
            .parent()
            .and_then(|p| p.parent())
            .expect("repo root is two levels above the crate manifest")
            .to_path_buf();
        std::env::set_var("PYTHONPATH", &repo_root);

        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let index_path = std::env::temp_dir()
            .join(format!("llm_anthology_e2e_tt_{}_{}.db", std::process::id(), nanos));

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

        // 1) graph.timeline — the three dated births, their range, no undated threads.
        let tl = client
            .call("graph.timeline", &json!({}))
            .expect("graph.timeline call");
        assert_eq!(tl["events"], json!([1000, 1100, 1200]), "timeline: {tl}");
        assert_eq!(tl["min_ms"], json!(1000), "timeline: {tl}");
        assert_eq!(tl["max_ms"], json!(1200), "timeline: {tl}");
        assert_eq!(tl["undated_count"], json!(0), "timeline: {tl}");

        // 2) graph.at(1100) — a coherent snapshot: child-c (born 1200) is NOT yet present,
        //    and root-a's fan-out matches the edges visible as-of the moment (just child-b).
        let at = client
            .call("graph.at", &json!({ "as_of_ms": 1100 }))
            .expect("graph.at call");
        let at_nodes = at["nodes"].as_array().expect("graph.at nodes array");
        let at_ids: Vec<&str> = at_nodes
            .iter()
            .map(|n| n["id"].as_str().expect("node id"))
            .collect();
        assert_eq!(at_ids, vec!["child-b", "root-a"], "as-of 1100 nodes: {at}");
        assert_eq!(at_nodes[1]["id"], json!("root-a"), "at: {at}");
        assert_eq!(at_nodes[1]["child_count"], json!(1), "root-a fan-out as-of 1100: {at}");
        assert_eq!(at_nodes[1]["depth"], json!(0), "at: {at}");
        assert_eq!(at_nodes[0]["id"], json!("child-b"), "at: {at}");
        assert_eq!(at_nodes[0]["child_count"], json!(0), "at: {at}");
        assert_eq!(at_nodes[0]["depth"], json!(1), "at: {at}");
        assert_eq!(at_nodes[0]["tokens"], json!(20), "child-b tokens survive the wire: {at}");
        let at_edges = at["edges"].as_array().expect("graph.at edges array");
        assert_eq!(at_edges.len(), 1, "only root-a->child-b is born as-of 1100: {at}");
        assert_eq!(at_edges[0]["parent"], json!("root-a"), "at: {at}");
        assert_eq!(at_edges[0]["child"], json!("child-b"), "at: {at}");
        assert_eq!(at_edges[0]["status"], json!("completed"), "at: {at}");

        // 3) graph.rollup — the subtree SUM aggregates tokens up each branch: root-a's
        //    subtree total is 100 + 20 + 3 = 123 over 3 nodes, deduped and cycle-safe.
        let rollup = client
            .call("graph.rollup", &json!({}))
            .expect("graph.rollup call");
        assert_eq!(rollup["root-a"]["self_tokens"], json!(100), "rollup: {rollup}");
        assert_eq!(
            rollup["root-a"]["subtree_tokens"],
            json!(123),
            "root-a subtree sum = 100+20+3: {rollup}"
        );
        assert_eq!(rollup["root-a"]["subtree_count"], json!(3), "rollup: {rollup}");
        assert_eq!(rollup["root-a"]["max_depth"], json!(1), "rollup: {rollup}");
        assert_eq!(rollup["root-a"]["child_count"], json!(2), "rollup: {rollup}");
        assert_eq!(rollup["child-b"]["subtree_tokens"], json!(20), "rollup: {rollup}");
        assert_eq!(rollup["child-c"]["subtree_tokens"], json!(3), "rollup: {rollup}");

        // 4) graph.diff(as_of_a=1000, as_of_b=1100) — the time-travel DELTA: child-b was
        //    born (with its edge) between the two moments; nothing removed or changed.
        let diff = client
            .call("graph.diff", &json!({ "as_of_a": 1000, "as_of_b": 1100 }))
            .expect("graph.diff call");
        assert_eq!(diff["added_nodes"], json!(["child-b"]), "diff delta: {diff}");
        assert_eq!(diff["removed_nodes"], json!([]), "diff: {diff}");
        assert_eq!(
            diff["added_edges"],
            json!([{ "parent": "root-a", "child": "child-b" }]),
            "diff: {diff}"
        );
        assert_eq!(diff["removed_edges"], json!([]), "diff: {diff}");
        assert_eq!(diff["changed_nodes"], json!({}), "diff: {diff}");

        // 5) export.plan — a dry-run tally, no filesystem access.
        let plan = client
            .call("export.plan", &json!({}))
            .expect("export.plan call");
        assert_eq!(plan["node_count"], json!(3), "plan: {plan}");
        assert_eq!(plan["edge_count"], json!(2), "plan: {plan}");
        // export is GRAPH-ONLY (the sidecar bundles no transcripts — see the
        // export.run artifact assertion below and `Sidecar._export_plan`), so
        // conversation_count is 0 and est_bytes is the serialized-graph byte size,
        // NOT a transcript Σ(char_count). (Corrects a stale pre-existing assertion
        // that predated the graph-only export contract; cross-checked against the
        // artifact the run writes, further down.)
        assert_eq!(plan["conversation_count"], json!(0), "plan: {plan}");
        let plan_est_bytes = plan["est_bytes"].as_u64().expect("est_bytes must be numeric");
        assert!(plan_est_bytes > 0, "graph-only est_bytes is a positive size: {plan}");

        // 6a) export.run POSITIVE — writes a real artifact to a temp path and passes both
        //     gates; the file lands on disk and its graph re-parses to the three threads.
        let export_path = std::env::temp_dir()
            .join(format!("llm_anthology_e2e_export_{}_{}.json", std::process::id(), nanos));
        let export_str = export_path.to_str().expect("export path is valid UTF-8");
        let run = client
            .call("export.run", &json!({ "dest_path": export_str }))
            .expect("export.run call");
        assert_eq!(run["ok"], json!(true), "export.run positive: {run}");
        assert_eq!(run["graph_gate"], json!(true), "export.run: {run}");
        assert_eq!(run["transcript_gate"], json!(true), "export.run: {run}");
        assert_eq!(run["written_path"], json!(export_str), "written_path echoes dest: {run}");
        assert!(
            export_path.exists(),
            "export.run must write the artifact to disk: {export_path:?}"
        );
        let artifact = std::fs::read_to_string(&export_path).expect("read export artifact");
        let doc: Value = serde_json::from_str(&artifact).expect("artifact is valid JSON");
        assert_eq!(doc["llm_anthology_export_version"], json!(1), "artifact: {doc}");
        assert_eq!(doc["conversations"], json!([]), "graph-only export bundles no transcripts");
        let graph: Value = serde_json::from_str(
            doc["graph"].as_str().expect("artifact graph is a JSON string"),
        )
        .expect("artifact graph parses");
        // The dry-run est_bytes must equal the EXACT serialized-graph size the run
        // wrote (both sides call serialize_graph over the same corpus) — a stronger
        // check than the old hardcoded constant it replaces.
        assert_eq!(
            plan_est_bytes,
            doc["graph"].as_str().unwrap().len() as u64,
            "export.plan est_bytes must equal the serialized graph export.run writes"
        );
        let graph_ids: Vec<&str> = graph["nodes"]
            .as_array()
            .expect("artifact nodes array")
            .iter()
            .map(|n| n["id"].as_str().expect("artifact node id"))
            .collect();
        assert_eq!(
            graph_ids,
            vec!["child-b", "child-c", "root-a"],
            "the written artifact round-trips to the full spawn graph: {graph}"
        );

        // 6b) export.run NEGATIVE (path guard, over the real wire): an unsafe dest is
        //     rejected as a JSON-RPC error (Err) BEFORE any write, so no file is created.
        let rel = client.call("export.run", &json!({ "dest_path": "relative/out.json" }));
        assert!(
            rel.is_err(),
            "a relative dest_path must be rejected over the wire, got: {rel:?}"
        );
        let unc = client.call("export.run", &json!({ "dest_path": r"\\evil\share\out.json" }));
        assert!(
            unc.is_err(),
            "a UNC dest_path must be rejected over the wire, got: {unc:?}"
        );

        drop(client);
        let _ = std::fs::remove_file(&index_path);
        let _ = std::fs::remove_file(&export_path);
    }

    // === Windows hardening proofs (raw CreateProcessW spawn) =====================

    /// FORCE-KILL, BOTH STATES — proves the Job Object's `KILL_ON_JOB_CLOSE` flag is
    /// what reaps the engine subtree when the app dies abruptly. Closing the LAST
    /// job handle is the EXACT kernel event the OS triggers when the app process is
    /// force-killed (its handle table is torn down by the kernel, running no Rust
    /// `Drop`), so this reproduces an abrupt death faithfully WITHOUT the test
    /// killing itself. WITH the flag the sleeping child is reaped; WITHOUT it the
    /// child is orphaned and survives — silence in one state alone proves nothing,
    /// so both are asserted (the flag is shown to be load-bearing).
    #[cfg(windows)]
    #[test]
    fn job_object_kill_on_close_reaps_child_both_states() {
        use super::hardened_spawn::{spawn_hardened, Membrane, SpawnOpts};

        let sleeper = ["-c", "import time; time.sleep(120)"];

        // --- WITH KILL_ON_JOB_CLOSE: closing the job handle reaps the child. ---
        let opts_on = SpawnOpts {
            membrane: Membrane::JobOnly,
            kill_on_job_close: true,
        };
        let mut with_flag = spawn_hardened("python", &sleeper, &opts_on)
            .expect("spawn sleeper under kill-on-close job");
        assert!(
            !with_flag.reaper.wait_exit(500),
            "child must be alive before the job handle closes (pid {})",
            with_flag.reaper.pid()
        );
        with_flag.reaper.close_job(); // == the app process dying abruptly
        assert!(
            with_flag.reaper.wait_exit(5000),
            "WITH KILL_ON_JOB_CLOSE the child MUST be reaped when the last job handle \
             closes (pid {})",
            with_flag.reaper.pid()
        );

        // --- WITHOUT the flag: the SAME handle-close leaves the child ORPHANED. ---
        let opts_off = SpawnOpts {
            membrane: Membrane::JobOnly,
            kill_on_job_close: false,
        };
        let mut without_flag = spawn_hardened("python", &sleeper, &opts_off)
            .expect("spawn sleeper under plain job");
        assert!(
            !without_flag.reaper.wait_exit(500),
            "control child must be alive before the job handle closes"
        );
        without_flag.reaper.close_job();
        assert!(
            !without_flag.reaper.wait_exit(1500),
            "WITHOUT KILL_ON_JOB_CLOSE the child MUST survive the job-handle close \
             (this is what proves the flag — not the spawn — does the reaping)"
        );
        // Dropping `without_flag` now TerminateProcess-reaps the orphan (cleanup).
        drop(without_flag);
    }

    /// MEMBRANE, BOTH STATES — proves the AppContainer network membrane blocks the
    /// engine's outbound network at the WFP layer WITHOUT breaking the legitimate
    /// stdio + corpus-read channel. Two independent proofs:
    ///   (1) the REAL sidecar runs INSIDE the AppContainer and still answers
    ///       health.ping + corpus.stats over stdio (the membrane doesn't break the
    ///       wire, and CPython genuinely runs in a regular AppContainer here);
    ///   (2) a socket probe to a loopback listener CONNECTS when spawned normally
    ///       (JobOnly) but is BLOCKED when spawned in the AppContainer — both states,
    ///       so the block is attributable to the membrane and not a dead network.
    ///
    /// Grants the fixed container's package SID read+execute on the interpreter dir,
    /// the `llm-anthology` source tree, and a dedicated index/probe dir (idempotent, benign RX
    /// ACEs for one stable SID — exactly the icacls step a production install runs).
    /// If CPython cannot run in a regular AppContainer on this host, proof (1) FAILS
    /// LOUDLY rather than silently skipping.
    #[cfg(windows)]
    #[test]
    fn appcontainer_membrane_blocks_egress_but_not_stdio_or_corpus() {
        use super::hardened_spawn::{
            ensure_container_sid, grant_modify, grant_read, spawn_hardened, Membrane, SpawnOpts,
        };
        use super::jsonrpc_roundtrip;
        use std::io::{BufReader, Read};
        use std::path::PathBuf;
        use std::process::Command;
        use std::time::{SystemTime, UNIX_EPOCH};

        let python_home = probe_python_home();
        let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let repo_root = manifest
            .parent()
            .and_then(|p| p.parent())
            .expect("repo root two levels above crate")
            .to_path_buf();
        let llm_anthology_dir = repo_root.join("llm_anthology");
        std::env::set_var("PYTHONPATH", &repo_root);

        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let work = std::env::temp_dir()
            .join(format!("llm_anthology_membrane_{}_{}", std::process::id(), nanos));
        std::fs::create_dir_all(&work).expect("create membrane work dir");
        let index_path = work.join("index.db");

        // Build the KNOWN synthetic corpus (same fixture the e2e round-trip uses).
        let fixture = manifest.join("tests").join("fixtures").join("build_synth_index.py");
        let built = Command::new("python")
            .arg(&fixture)
            .arg(&index_path)
            .output()
            .expect("launch python to build the synth index");
        assert!(
            built.status.success(),
            "synth index build failed: {}",
            String::from_utf8_lossy(&built.stderr)
        );

        // --- ONE-TIME provisioning: grant the FIXED container SID read+execute on
        //     JUST the stdlib (NOT the ~120k-file site-packages — the sidecar is
        //     pure stdlib + llm_anthology), plus the llm_anthology tree, the repo root (traverse), and
        //     the dedicated work dir. Once (not per-spawn), so the test stays fast. ---
        let sid = ensure_container_sid(Membrane::AppContainer).expect("ensure AppContainer profile");
        grant_stdlib_read(&sid, &python_home);
        grant_read(&sid, &llm_anthology_dir.to_string_lossy(), true);
        grant_read(&sid, &repo_root.to_string_lossy(), false);
        // The index dir needs WRITE: SQLite's WAL journal (open_index sets
        // journal_mode=WAL) creates `-wal`/`-shm` sidecars even for read queries.
        grant_modify(&sid, &work.to_string_lossy(), true);

        let index_str = index_path.to_str().expect("index path is UTF-8");

        // (1) REAL sidecar INSIDE the AppContainer: stdio + corpus read must work.
        //     `-S` keeps the engine off site.py / site-packages entirely.
        {
            let opts = SpawnOpts { membrane: Membrane::AppContainer, kill_on_job_close: true };
            let mut sc = spawn_hardened(
                "python",
                &["-S", "-m", "llm_anthology.sidecar", "--index", index_str],
                &opts,
            )
            .expect("spawn the real sidecar inside a regular AppContainer");
            // ONE persistent reader so pipe buffering carries across both calls.
            let mut reader = BufReader::new(sc.stdout);
            let health = match jsonrpc_roundtrip(&mut sc.stdin, &mut reader, 1, "health.ping", &json!({})) {
                Ok(v) => v,
                Err(e) => {
                    let mut err = String::new();
                    let _ = sc.stderr.read_to_string(&mut err);
                    panic!(
                        "sidecar failed health.ping INSIDE the AppContainer ({e}) — CPython could \
                         not run/answer over stdio in a regular AppContainer here.\n\
                         --- child stderr ---\n{err}\n--- end child stderr ---"
                    );
                }
            };
            assert_eq!(health["ok"], json!(true), "health through membrane: {health}");
            assert_eq!(
                health["corpus_ready"],
                json!(true),
                "corpus must be readable through the membrane: {health}"
            );
            let stats = jsonrpc_roundtrip(&mut sc.stdin, &mut reader, 2, "corpus.stats", &json!({}))
                .expect("corpus.stats over stdio THROUGH the membrane");
            assert_eq!(stats["conversations"], json!(3), "corpus read through membrane: {stats}");
            assert_eq!(stats["threads"], json!(3), "corpus read through membrane: {stats}");
            assert_eq!(stats["edges"], json!(2), "corpus read through membrane: {stats}");
            // `reader` (stdout) then `sc` (stdin→EOF, stderr, reaper) drop here → reaped.
        }

        // (2) Egress both-states against a loopback listener we control.
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind loopback listener");
        let port = listener.local_addr().unwrap().port();

        let probe_py = work.join("probe.py");
        std::fs::write(&probe_py, PROBE_SRC).expect("write socket probe");
        let probe_path = probe_py.to_str().expect("probe path is UTF-8").to_string();
        let port_str = port.to_string();

        let read_probe = |membrane: Membrane| -> String {
            let opts = SpawnOpts { membrane, kill_on_job_close: true };
            let mut spawn = spawn_hardened("python", &["-S", &probe_path, &port_str], &opts)
                .expect("spawn the socket probe");
            let mut out = String::new();
            spawn.stdout.read_to_string(&mut out).ok(); // probe prints one line then exits → EOF
            out
        };

        // Control: NOT sandboxed → the probe CAN reach loopback.
        let normal = read_probe(Membrane::JobOnly);
        assert!(
            normal.contains("CONNECT_OK"),
            "un-sandboxed control probe must connect to loopback, got: {normal:?}"
        );

        // Membrane: AppContainer (same provisioned SID) → the SAME connect is BLOCKED.
        let sandboxed = read_probe(Membrane::AppContainer);
        eprintln!("[membrane] egress both-states: JobOnly={normal:?} AppContainer={sandboxed:?}");
        assert!(
            sandboxed.contains("CONNECT_FAIL"),
            "AppContainer probe must be network-BLOCKED (got: {sandboxed:?}); \
             un-sandboxed control was: {normal:?}"
        );
        assert!(
            !sandboxed.contains("CONNECT_OK"),
            "AppContainer probe must NOT connect, got: {sandboxed:?}"
        );

        drop(listener);
        let _ = std::fs::remove_dir_all(&work);
    }

    /// Grant the AppContainer package `sid` read+execute on JUST the CPython
    /// standard library (root binaries, DLLs, and every `Lib` child EXCEPT the
    /// huge `site-packages` tree — the sidecar imports only stdlib + llm_anthology). This
    /// keeps the AppContainer grant fast (~8k files, not ~130k).
    #[cfg(windows)]
    fn grant_stdlib_read(sid: &str, python_home: &std::path::Path) {
        use super::hardened_spawn::grant_read;
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
                    continue;
                }
                if e.path().is_dir() {
                    grant_read(sid, &e.path().to_string_lossy(), true);
                }
            }
        }
    }

    /// The PATH directory holding `python.exe` — its interpreter home, which the
    /// AppContainer must be `icacls`-granted so the sandboxed engine can load CPython.
    #[cfg(windows)]
    fn probe_python_home() -> std::path::PathBuf {
        if let Some(paths) = std::env::var_os("PATH") {
            for dir in std::env::split_paths(&paths) {
                if dir.join("python.exe").is_file() {
                    return dir;
                }
            }
        }
        panic!("python.exe not found on PATH");
    }

    /// A tiny script that attempts ONE outbound TCP connect and reports the outcome
    /// on stdout: `CONNECT_OK`, or `CONNECT_FAIL errno=.. winerror=..` (an AppContainer
    /// WFP block surfaces as WSAEACCES / winerror 10013).
    #[cfg(windows)]
    const PROBE_SRC: &str = r#"
import sys
port = int(sys.argv[1])
try:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect(("127.0.0.1", port))
        sys.stdout.write("CONNECT_OK\n")
    except OSError as e:
        sys.stdout.write("CONNECT_FAIL errno=%s winerror=%s\n" % (e.errno, getattr(e, "winerror", 0)))
    finally:
        s.close()
except Exception as e:
    sys.stdout.write("PROBE_ERR %r\n" % (e,))
sys.stdout.flush()
"#;
}
