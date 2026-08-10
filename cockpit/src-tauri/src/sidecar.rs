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
//!   correlation is trivially satisfied. That sequencing is ENFORCED, not conventional:
//!   `call` takes `&mut self`, so the borrow checker rejects two overlapping calls on
//!   one client at compile time, and the `Arc<Mutex<..>>` in `lib.rs` serialises the
//!   commands that share one.
//!
//! THE CHILD MUST NOT BE ABLE TO WEDGE OR EXHAUST THE PARENT. Two properties carry that,
//! and both are load-bearing rather than defensive:
//!
//! * stderr is DRAINED, never merely held ([`drain_stderr`]). An undrained pipe blocks
//!   the child once the small OS buffer fills, and since the parent is meanwhile blocked
//!   reading stdout — holding the engine mutex, with no timeout on the path — one flood
//!   wedges the app permanently. The drained bytes are no longer THROWN AWAY: they land in
//!   a bounded, privacy-scrubbed ring plus a capped crash file ([`Diagnostics`], DECISION
//!   G-8), because a traceback is the only artifact that explains why an adapter failed.
//! * a response line is BOUNDED ([`MAX_RESPONSE_LINE_BYTES`]). `read_line` grows without
//!   limit, and response size is governed by session-file content the engine does not cap.
//!
//! What is NOT here, deliberately: there is no read TIMEOUT. A child that accepts a
//! request and simply never answers blocks its caller forever. Adding one means a reader
//! thread or an async runtime — a redesign of this module, not an edit to it.

use std::collections::VecDeque;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};

use serde_json::{json, Value};

/// Ceiling on ONE response line, in bytes.
///
/// `BufRead::read_line` is UNBOUNDED — it grows its buffer until it meets a `\n`, so a
/// child that emits a colossal line, or never terminates one at all, makes the parent
/// allocate without limit. That is not only a hostile-input concern: `conversation.get`
/// returns a whole transcript re-parsed on demand from a session file the user's OTHER
/// tools wrote, and the engine applies no size limit of its own (there is no truncation
/// anywhere in `llm_anthology/sidecar.py`'s response path), so response size is governed
/// by untrusted file content by construction. An allocation failure ABORTS the process —
/// Rust cannot unwind from OOM — which loses the whole app rather than the one call. A
/// ceiling turns that into an ordinary `Err` the UI can render.
///
/// 256 MiB is chosen to be far above any transcript a human could read (the renderer
/// would be unusable long before it) while still bounding the allocation. It is a
/// JUDGEMENT, not a measured maximum: no legitimate upper bound exists to measure,
/// because the engine imposes none. Tune it here if a real corpus ever trips it — the
/// error names the ceiling explicitly so such a report is unambiguous.
const MAX_RESPONSE_LINE_BYTES: u64 = 256 * 1024 * 1024;

// -- DECISION G-8: engine stderr is EVIDENCE, and it is kept in the PARENT --------------
//
// THE MEASURED PROBLEM. `drain_stderr` used to read the child's stderr into an 8 KiB
// throwaway array and drop every byte on the floor. Draining is mandatory (an undrained
// pipe deadlocks the child — see `drain_stderr`), but discarding is a separate decision,
// and it was the wrong one: a Python traceback is the ONLY artifact that explains why an
// adapter failed, and with six providers across eight adapter modules over formats this
// project does not control, an adapter meeting an unknown shape is the steady state rather
// than the edge case. The engine has no logging of its own either, so the traceback was
// the entire evidence surface and it was destroyed by design.
//
// TWO SURFACES, and the owner asked for BOTH knowingly:
//
//   1. A bounded in-memory RING ([`STDERR_RING_BYTES`]) in the parent. It lives in the
//      parent on purpose: the dominant failure is the ENGINE dying, and a buffer inside the
//      engine dies with it. Keeps the LAST N bytes, because what happened immediately
//      before a failure is what explains it.
//   2. A size-capped CRASH FILE on disk ([`CRASH_FILE_MAX_BYTES`]), for the case the ring
//      cannot survive: the SHELL itself dying. Keeps the FIRST N bytes of a run, which is
//      the deliberate complement of the ring — the first traceback names the root cause
//      while later output is usually cascade noise, so between the two surfaces a reporter
//      has both ends of the run.
//
// THE PRIVACY CONTRACT, which is what makes an on-disk surface acceptable at all. This
// corpus holds private conversations, and a traceback carries absolute paths (so, the
// username) and often the very data value that caused the failure. So NOTHING is retained
// raw. Every line is scrubbed ONCE, at ingest, before it reaches either surface, by an
// ALLOWLIST of structural shapes ([`scrub_line`]):
//
//   * the fixed traceback scaffolding lines, verbatim;
//   * a `File "<path>", line N, in <name>` frame, with the path home-relativized. A frame
//     path is always a SOURCE file — CPython builds it from `code.co_filename` — so it
//     cannot be a data path;
//   * an exception line reduced to its TYPE (`KeyError: <redacted 9 chars>`). The MESSAGE
//     is always redacted, and that is the deliberate cost of this design: `KeyError:
//     'mapping'` would have named the missing field, which is real debugging value, but the
//     same slot is where a parser puts the offending VALUE. Type + frame list is what
//     survives, and it is enough to locate the failure.
//   * ANYTHING else — including the source-echo line under a frame and any free-text
//     print — becomes `<redacted N chars>`. The echo is engine source and would have been
//     safe, but keeping it needs cross-line state, and stateless is what makes the scrubber
//     provable (and idempotent, which the previous-run file re-read relies on).
//
// So the residual leak surface is: an exception TYPE name, engine source paths with `~` in
// place of the home directory, and line numbers. UNVERIFIED that no adapter can put a
// conversation token into an exception TYPE name (a dynamically-constructed exception class
// named from data would do it); the settling experiment is `grep -rn "type(.*Error"
// llm_anthology/` plus a check that no adapter calls `type()` to build an exception class.
// Nothing in the tests below asserts that, because it is a property of the engine rather
// than of this module.

/// Scrubbed engine-stderr bytes the PARENT retains in memory.
///
/// 64 KiB is roughly a dozen full tracebacks after scrubbing, which is well past the point
/// where more output stops adding information — a cascade repeats itself. It is a JUDGEMENT
/// and not a measured optimum; the number that matters is that it is BOUNDED, because the
/// producer is a child process this code does not control.
const STDERR_RING_BYTES: usize = 64 * 1024;

/// Hard ceiling on ONE generation of the on-disk crash file.
///
/// The whole on-disk policy, stated in one place because the owner accepted this surface
/// deliberately and it should not have to be reverse-engineered: ONE fixed file per run at
/// `<logs_dir>/engine-stderr.log`, capped at 256 KiB, written already-scrubbed; at app start
/// the previous run's file is renamed to `engine-stderr.prev.log` and a fresh one begins, so
/// there are EXACTLY TWO generations and never more. Worst case on disk is therefore
/// 512 KiB, forever, with no rotation scheme to go wrong and nothing to prune. Deleting the
/// app's data folder removes both. The previous generation exists because the crash case is
/// specifically "the app died and the user restarted it" — truncating on start would destroy
/// the evidence at exactly the moment it was about to be read.
const CRASH_FILE_MAX_BYTES: usize = 256 * 1024;

/// Ceiling on an UNTERMINATED stderr line held in the line-assembly buffer.
///
/// Same class of hazard as [`MAX_RESPONSE_LINE_BYTES`]: a child that writes forever without
/// a `\n` would grow this buffer without limit. At the ceiling the partial line is flushed
/// as though it had ended, which the allowlist then almost certainly redacts — the safe
/// direction.
const MAX_PENDING_LINE_BYTES: usize = 8 * 1024;

/// The current run's crash file, inside the app's logs directory.
const CRASH_FILE_NAME: &str = "engine-stderr.log";

/// The PREVIOUS run's crash file — see [`CRASH_FILE_MAX_BYTES`] for why exactly two.
const CRASH_FILE_PREV_NAME: &str = "engine-stderr.prev.log";

/// Traceback scaffolding that is a FIXED string and therefore carries no data.
const TRACEBACK_HEADERS: [&str; 3] = [
    "Traceback (most recent call last):",
    "During handling of the above exception, another exception occurred:",
    "The above exception was the direct cause of the following exception:",
];

/// Suffixes an exception CLASS name must end in for its line to be recognised.
///
/// Without this the matcher accepted any capitalised dotted word before a colon, which a
/// conversation line like `Amoxicillin: 500mg` satisfies — the exact leak this unit exists
/// to prevent. Every Python exception name ends in one of these (`KeyError`,
/// `sqlite3.OperationalError`, `KeyboardInterrupt`, `SystemExit`, `GeneratorExit`,
/// `StopIteration`, `BaseExceptionGroup`, `DeprecationWarning`), so the cost of the
/// tightening is nil and the failure direction is "redact something that was safe".
const EXCEPTION_SUFFIXES: [&str; 8] = [
    "Error",
    "Exception",
    "Warning",
    "Exit",
    "Interrupt",
    "StopIteration",
    "StopAsyncIteration",
    "ExceptionGroup",
];

/// What replaces a line the allowlist does not recognise. The COUNT is kept because "how
/// much was dropped" is itself diagnostic, and a count cannot carry content.
fn redaction_marker(chars: usize) -> String {
    format!("<redacted {chars} chars>")
}

/// True for a line this module already produced.
///
/// Load-bearing for IDEMPOTENCE, not cosmetic: the previous run's crash file is read back
/// and re-scrubbed on its way into a bundle, so without this a marker would be re-wrapped
/// into a marker about a marker on every pass.
fn is_redaction_marker(line: &str) -> bool {
    let Some(rest) = line.strip_prefix("<redacted ") else {
        return false;
    };
    let Some(digits) = rest.strip_suffix(" chars>") else {
        return false;
    };
    !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit())
}

/// Split a leading run of ASCII digits off `s`, or `None` if there is not at least one.
fn split_digits(s: &str) -> Option<(&str, &str)> {
    let end = s.find(|c: char| !c.is_ascii_digit()).unwrap_or(s.len());
    (end > 0).then(|| s.split_at(end))
}

/// Rewrite a traceback FRAME line with its path home-relativized, or `None` if the line is
/// not one.
///
/// Rebuilt from its parsed parts rather than patched in place, so a line that merely
/// resembles a frame cannot smuggle a tail through. The `, in <name>` tail is a
/// `code.co_name` — `<module>`, `<lambda>`, `<listcomp>` are all real — so it is accepted
/// only as an identifier-shaped token of sane length and dropped otherwise.
fn scrub_frame(line: &str, paths: &PathScrubber) -> Option<String> {
    let indent_len = line.len() - line.trim_start().len();
    let (indent, rest) = line.split_at(indent_len);
    let after = rest.strip_prefix("File \"")?;
    let close = after.rfind('"')?;
    let (path, tail) = (&after[..close], &after[close + 1..]);
    let (number, tail) = split_digits(tail.strip_prefix(", line ")?)?;
    let name = match tail.strip_prefix(", in ") {
        None if tail.is_empty() => String::new(),
        None => return None,
        Some(name)
            if !name.is_empty()
                && name.len() <= 120
                && name
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '<' | '>')) =>
        {
            format!(", in {name}")
        }
        // A tail that is not identifier-shaped is dropped, not carried.
        Some(_) => String::new(),
    };
    Some(format!(
        "{indent}File \"{}\", line {number}{name}",
        paths.apply(path)
    ))
}

/// Reduce an exception line to `Type: <redacted N chars>`, or `None` if it is not one.
fn scrub_exception(line: &str) -> Option<String> {
    if line.starts_with(char::is_whitespace) {
        return None;
    }
    let (head, message) = line.split_once(':')?;
    if head.is_empty() || head.len() > 80 {
        return None;
    }
    if !head
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.'))
    {
        return None;
    }
    let last = head.rsplit('.').next().unwrap_or(head);
    if !last.starts_with(|c: char| c.is_ascii_uppercase()) {
        return None;
    }
    if !EXCEPTION_SUFFIXES.iter().any(|s| last.ends_with(s)) {
        return None;
    }
    let message = message.trim();
    Some(if message.is_empty() {
        format!("{head}:")
    } else if is_redaction_marker(message) {
        // ALREADY scrubbed. Measured by `scrubbing_an_already_scrubbed_line_changes_nothing`
        // before this arm existed: `KeyError: 'mapping'` scrubbed to
        // `KeyError: <redacted 9 chars>`, and scrubbing THAT produced
        // `KeyError: <redacted 18 chars>` — a marker describing a marker, growing on every
        // pass. The previous run's crash file is read back and re-scrubbed on its way into
        // a bundle, so that is a live path, not a hypothetical.
        format!("{head}: {message}")
    } else {
        format!("{head}: {}", redaction_marker(message.chars().count()))
    })
}

/// The ALLOWLIST. One stderr line in, one retention-safe line out.
///
/// Stateless by design — every decision is a property of the line itself — which is what
/// makes it idempotent and testable without reconstructing a stream.
fn scrub_line(line: &str, paths: &PathScrubber) -> String {
    let line = line.trim_end();
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return String::new();
    }
    if TRACEBACK_HEADERS.contains(&trimmed) || is_redaction_marker(trimmed) {
        return line.to_string();
    }
    if let Some(frame) = scrub_frame(line, paths) {
        return frame;
    }
    if let Some(exception) = scrub_exception(line) {
        return exception;
    }
    redaction_marker(line.chars().count())
}

/// Case-insensitive substring replacement.
///
/// Windows paths are case-insensitive and the spelling comes from whoever set the
/// environment variable, so `c:\users\tester` must scrub identically to `C:\Users\Tester`.
/// Byte indices from the lowercased mirror are valid in the original because
/// `to_ascii_lowercase` maps only `A-Z`, leaving byte length and char boundaries intact.
fn replace_ignore_ascii_case(haystack: &str, needle: &str, with: &str) -> String {
    if needle.is_empty() {
        return haystack.to_string();
    }
    let hay = haystack.to_ascii_lowercase();
    let pin = needle.to_ascii_lowercase();
    let mut out = String::with_capacity(haystack.len());
    let mut cursor = 0usize;
    while let Some(hit) = hay[cursor..].find(&pin) {
        let at = cursor + hit;
        out.push_str(&haystack[cursor..at]);
        out.push_str(with);
        cursor = at + needle.len();
    }
    out.push_str(&haystack[cursor..]);
    out
}

/// The final path segment of `path`, for either separator.
fn last_path_segment(path: &str) -> Option<&str> {
    let trimmed = path.trim().trim_end_matches(['\\', '/']);
    let cut = trimmed.rfind(['\\', '/']).map_or(0, |i| i + 1);
    let segment = &trimmed[cut..];
    (!segment.is_empty()).then_some(segment)
}

/// Home-path relativization: `C:\Users\tester\...` -> `~\...`, and the bare username ->
/// `<user>`.
///
/// This is the same rule the shareable graph export applies for the same reason — the
/// engine reduces a filesystem path to a basename precisely because "it embeds the owner's
/// username" (`llm_anthology/sidecar.py`, `_build_error`). A diagnostics bundle is a SHARED
/// artifact, so without this the support channel becomes the identification channel.
///
/// Cited BY NAME rather than by line: that anchor was written as `sidecar.py:543` and was
/// already stale within the hour, because the engine is under concurrent edit and every
/// insertion above shifts it. `tests/test_citation_anchors.py` pins `mock.ts`/`types.ts`
/// citations only and says so explicitly — `.rs` anchors are NOT covered — so a line number
/// here rots silently, which is precisely the decay that test exists to stop elsewhere.
///
/// Both are INJECTED rather than read from the environment here, matching
/// `base_dirs_with`'s contract in `lib.rs`: the rule is then testable without depending on
/// whose machine ran the test.
pub(crate) struct PathScrubber {
    /// Every spelling of the home directory, longest first so the fullest match wins.
    needles: Vec<String>,
    user: Option<String>,
}

impl PathScrubber {
    pub(crate) fn new(home: Option<&str>, user: Option<&str>) -> Self {
        let mut needles: Vec<String> = Vec::new();
        if let Some(home) = home.map(str::trim).filter(|h| h.len() >= 3) {
            let home = home.trim_end_matches(['\\', '/']);
            // Both separator spellings, because a Python traceback on Windows prints
            // whichever one the import machinery happened to build the path with.
            for spelling in [
                home.to_string(),
                home.replace('\\', "/"),
                home.replace('/', "\\"),
            ] {
                if spelling.len() >= 3 && !needles.contains(&spelling) {
                    needles.push(spelling);
                }
            }
            needles.sort_by_key(|n| std::cmp::Reverse(n.len()));
        }
        // A username shorter than 3 characters is dropped rather than replaced: a 1-2
        // character needle matches inside ordinary words and would corrupt every line it
        // touched, which is worse than leaving a short name in.
        let user = user
            .map(str::trim)
            .filter(|u| !u.is_empty())
            .map(str::to_string)
            .or_else(|| home.and_then(last_path_segment).map(str::to_string))
            .filter(|u| u.len() >= 3);
        Self { needles, user }
    }

    /// Build from the real environment. `USERPROFILE`/`HOME` and `USERNAME`/`USER` cover
    /// both platforms this crate is compiled for.
    pub(crate) fn from_env(var: impl Fn(&str) -> Option<String>) -> Self {
        let present = |name: &str| {
            var(name)
                .map(|v| v.trim().to_string())
                .filter(|v| !v.is_empty())
        };
        let home = present("USERPROFILE").or_else(|| present("HOME"));
        let user = present("USERNAME").or_else(|| present("USER"));
        Self::new(home.as_deref(), user.as_deref())
    }

    pub(crate) fn apply(&self, text: &str) -> String {
        let mut out = text.to_string();
        for needle in &self.needles {
            out = replace_ignore_ascii_case(&out, needle, "~");
        }
        match &self.user {
            Some(user) => replace_ignore_ascii_case(&out, user, "<user>"),
            None => out,
        }
    }
}

/// The on-disk half. An UNBUFFERED [`std::fs::File`], deliberately: the whole point is that
/// the bytes are already on disk when the process dies, so a `BufWriter` would defeat it.
struct CrashLog {
    file: std::fs::File,
    path: PathBuf,
    written: usize,
    cap: usize,
}

impl CrashLog {
    /// Append one already-scrubbed line, KEEP-FIRST: once `cap` is reached nothing more is
    /// written. See [`CRASH_FILE_MAX_BYTES`] for why first rather than last.
    fn append(&mut self, line: &str) {
        if self.written >= self.cap {
            return;
        }
        let mut bytes = Vec::with_capacity(line.len() + 1);
        bytes.extend_from_slice(line.as_bytes());
        bytes.push(b'\n');
        bytes.truncate(self.cap - self.written);
        if self.file.write_all(&bytes).is_ok() {
            self.written += bytes.len();
        } else {
            // A write failure (full disk, revoked ACL) stops the attempt for the rest of
            // the run rather than retrying once per line for the life of the app.
            self.written = self.cap;
        }
    }
}

/// Open this run's crash file, rotating the previous run's to `.prev.log`.
///
/// Returns `None` on any filesystem refusal — diagnostics are a convenience and must never
/// be the reason the app fails to start.
fn open_crash_log(dir: &Path) -> Option<CrashLog> {
    std::fs::create_dir_all(dir).ok()?;
    let path = dir.join(CRASH_FILE_NAME);
    if path.is_file() {
        // Best-effort: if the rename fails the previous generation is simply lost, which is
        // strictly better than refusing to record the current one.
        let _ = std::fs::rename(&path, dir.join(CRASH_FILE_PREV_NAME));
    }
    let file = std::fs::File::create(&path).ok()?;
    Some(CrashLog {
        file,
        path,
        written: 0,
        cap: CRASH_FILE_MAX_BYTES,
    })
}

/// Read at most `max_bytes` from the END of `path`, re-scrubbed, plus the file's real size.
///
/// The file was written scrubbed, so this pass is belt-and-braces — and it is safe to run
/// because [`scrub_line`] is idempotent (see [`is_redaction_marker`]).
fn read_tail_scrubbed(path: &Path, max_bytes: usize, paths: &PathScrubber) -> (String, u64) {
    let size = std::fs::metadata(path).map(|m| m.len()).unwrap_or(0);
    let Ok(bytes) = std::fs::read(path) else {
        return (String::new(), size);
    };
    let start = bytes.len().saturating_sub(max_bytes);
    let text = String::from_utf8_lossy(&bytes[start..]);
    let scrubbed: Vec<String> = text.lines().map(|l| scrub_line(l, paths)).collect();
    (scrubbed.join("\n"), size)
}

#[derive(Default)]
struct SinkState {
    /// The partial line assembled across chunk boundaries, bounded by
    /// [`MAX_PENDING_LINE_BYTES`].
    pending: Vec<u8>,
    /// Scrubbed lines, KEEP-LAST, bounded by [`STDERR_RING_BYTES`].
    lines: VecDeque<String>,
    ring_bytes: usize,
    crash: Option<CrashLog>,
}

/// The process-wide stderr sink: the ring, the crash file, and the scrubber that guards
/// both.
///
/// PROCESS-WIDE rather than per-client on purpose. Three separate engines are spawned over
/// an app's life — the managed one plus the throwaway index-less ones behind
/// `create_corpus` / `discover_sources` — and a first-run DISCOVERY failure is exactly the
/// report this exists to serve. A per-client buffer would die with the throwaway.
///
/// THE COST OF THAT CHOICE, stated because it is real: the line-assembly buffer is shared, so
/// if two engines write to stderr at the SAME moment their bytes interleave and the resulting
/// lines are garbled. UNVERIFIED how often that happens in practice — the throwaway engines
/// are short-lived and the UI drives them one command at a time, so the window is small; the
/// settling experiment is to run `discover_sources` and a `corpus.build` concurrently with
/// both engines failing. It is accepted rather than fixed because the failure direction is
/// SAFE: a garbled line matches nothing on the allowlist and is redacted whole, so
/// interleaving costs evidence, never privacy. A per-engine assembly buffer feeding one
/// shared ring is the fix if it ever bites.
pub struct Diagnostics {
    paths: PathScrubber,
    state: Mutex<SinkState>,
}

impl Diagnostics {
    fn new(paths: PathScrubber, crash: Option<CrashLog>) -> Self {
        Self {
            paths,
            state: Mutex::new(SinkState {
                crash,
                ..SinkState::default()
            }),
        }
    }

    /// A panicking writer must not silently end diagnostics for the rest of the run, so the
    /// poison is recovered rather than propagated. There is no invariant a partial write
    /// could have broken: every field is independently consistent.
    fn lock(&self) -> std::sync::MutexGuard<'_, SinkState> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Feed raw child stderr. Splits into lines, scrubs ONCE, then fans out to both
    /// surfaces — so neither the ring nor the file ever holds an unscrubbed byte.
    fn ingest(&self, chunk: &[u8]) {
        let mut state = self.lock();
        for &byte in chunk {
            match byte {
                b'\n' => {
                    let line = std::mem::take(&mut state.pending);
                    self.emit(&mut state, &line);
                }
                b'\r' => {}
                _ => {
                    state.pending.push(byte);
                    if state.pending.len() >= MAX_PENDING_LINE_BYTES {
                        let line = std::mem::take(&mut state.pending);
                        self.emit(&mut state, &line);
                    }
                }
            }
        }
    }

    /// Flush a trailing line the child never terminated (it died mid-write, which is the
    /// interesting case).
    fn finish(&self) {
        let mut state = self.lock();
        if !state.pending.is_empty() {
            let line = std::mem::take(&mut state.pending);
            self.emit(&mut state, &line);
        }
    }

    fn emit(&self, state: &mut SinkState, raw: &[u8]) {
        let scrubbed = scrub_line(&String::from_utf8_lossy(raw), &self.paths);
        if let Some(log) = state.crash.as_mut() {
            log.append(&scrubbed);
        }
        state.ring_bytes += scrubbed.len() + 1;
        state.lines.push_back(scrubbed);
        // `len() > 1` keeps the ring non-empty even if one line alone exceeds the budget,
        // so the most recent line is always readable.
        while state.ring_bytes > STDERR_RING_BYTES && state.lines.len() > 1 {
            if let Some(front) = state.lines.pop_front() {
                state.ring_bytes -= front.len() + 1;
            }
        }
    }

    /// The retained stderr tail, as text.
    pub fn snapshot(&self) -> String {
        let state = self.lock();
        state
            .lines
            .iter()
            .cloned()
            .collect::<Vec<String>>()
            .join("\n")
    }

    /// Everything the parent process knows that a bug report needs, minus the engine-side
    /// numbers (index stats) the UI already holds.
    pub fn bundle(&self) -> Value {
        // Two SEQUENTIAL locks, never nested: `snapshot` takes the lock itself and a
        // `Mutex` is not reentrant, so calling it from inside the block below would
        // deadlock the caller. A line arriving between the two is of no consequence to a
        // diagnostics read.
        let stderr = self.snapshot();
        let (crash_path, crash_bytes) = {
            let state = self.lock();
            match state.crash.as_ref() {
                Some(log) => (Some(log.path.clone()), log.written),
                None => (None, 0),
            }
        };
        let previous = match crash_path.as_ref().and_then(|p| p.parent()) {
            Some(dir) => {
                let prev = dir.join(CRASH_FILE_PREV_NAME);
                let (tail, bytes) = read_tail_scrubbed(&prev, STDERR_RING_BYTES, &self.paths);
                json!({
                    "path": self.paths.apply(&prev.to_string_lossy()),
                    "bytes": bytes,
                    "tail": tail,
                })
            }
            None => Value::Null,
        };
        json!({
            "platform": {
                "os": std::env::consts::OS,
                "arch": std::env::consts::ARCH,
                "family": std::env::consts::FAMILY,
            },
            "engine_stderr": stderr,
            "engine_stderr_cap_bytes": STDERR_RING_BYTES,
            "crash_file": match crash_path {
                Some(path) => json!({
                    "path": self.paths.apply(&path.to_string_lossy()),
                    "bytes": crash_bytes,
                    "cap_bytes": CRASH_FILE_MAX_BYTES,
                }),
                None => Value::Null,
            },
            "previous_run": previous,
        })
    }
}

/// The one sink for this process. See [`Diagnostics`] for why it is process-wide.
static DIAGNOSTICS: OnceLock<Arc<Diagnostics>> = OnceLock::new();

/// The process-wide sink, created on first use.
///
/// The crash file is deliberately NOT opened under `cfg(test)`: `cargo test` spawns real
/// engines, and rotating the developer's REAL `%LOCALAPPDATA%\LLM Anthology\logs` as a side
/// effect of running the suite is not something a test may do. The file logic is covered
/// directly against a temp directory instead (`the_crash_file_is_capped_and_keeps_the_first_bytes`,
/// `a_second_run_rotates_the_crash_file_to_exactly_one_previous_generation`). What is
/// therefore UNVERIFIED is only the JOIN — that the shipped app really opens a file under
/// `AppLocations::logs_dir`; the settling experiment is to run the installed app with a
/// deliberately broken engine and check that `engine-stderr.log` appears there.
pub fn diagnostics() -> &'static Arc<Diagnostics> {
    DIAGNOSTICS.get_or_init(|| {
        let crash = if cfg!(test) {
            None
        } else {
            crate::app_locations()
                .ok()
                .and_then(|locations| open_crash_log(&locations.logs_dir))
        };
        Arc::new(Diagnostics::new(
            PathScrubber::from_env(|name| std::env::var(name).ok()),
            crash,
        ))
    })
}

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
    jsonrpc_roundtrip_bounded(writer, reader, id, method, params, MAX_RESPONSE_LINE_BYTES)
}

/// [`jsonrpc_roundtrip`] with the response-line ceiling supplied explicitly.
///
/// Split out ONLY so the ceiling is testable: proving it against the production
/// [`MAX_RESPONSE_LINE_BYTES`] would mean allocating 256 MiB in CI, so the test drives
/// this form with a tiny cap instead. Production always goes through the wrapper.
fn jsonrpc_roundtrip_bounded(
    writer: &mut dyn Write,
    reader: &mut dyn BufRead,
    id: u64,
    method: &str,
    params: &Value,
    max_line_bytes: u64,
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

    // --- read exactly one response line, BOUNDED (see MAX_RESPONSE_LINE_BYTES) ---
    // `read_until` over a `take`-limited reader rather than `read_line`, so the buffer
    // cannot grow past the ceiling. Reading one byte OVER the ceiling is what proves the
    // line did not fit: at exactly the ceiling the line may still have ended in `\n`.
    let mut raw: Vec<u8> = Vec::new();
    let n = (&mut *reader)
        .take(max_line_bytes.saturating_add(1))
        .read_until(b'\n', &mut raw)
        .map_err(|e| format!("read response: {e}"))?;
    if n == 0 {
        return Err("sidecar closed stdout (EOF) before responding".to_string());
    }
    if n as u64 > max_line_bytes {
        // The rest of the line is still queued, so the stream is now mid-frame and
        // nothing after this can be correlated. Say so, rather than let the caller
        // retry into a permanently skewed stream.
        return Err(format!(
            "response line exceeds the {max_line_bytes}-byte ceiling; the transport is \
             now out of sync and this engine must be restarted"
        ));
    }
    // `read_line` would have done this validation implicitly, but on failure it leaves
    // the buffer unspecified AND the bytes consumed; doing it here names the fault.
    let buf =
        String::from_utf8(raw).map_err(|e| format!("response is not valid UTF-8: {e}"))?;
    let trimmed = buf.trim_end();
    let response: Value =
        serde_json::from_str(trimmed).map_err(|e| format!("parse response {trimmed:?}: {e}"))?;

    // --- correlate by id: the transport is strictly sequential (one request, one
    // response line), so the reply id must equal the request id.
    //
    // A NULL id is tolerated ONLY alongside an `error`. That allowance exists because a
    // parse error cannot echo an id it never managed to read
    // (`_error_response(None, ..)`, llm_anthology/sidecar.py:656,662) — and that is its
    // whole extent. A null id beside a RESULT has no legitimate producer, since every
    // result echoes the numeric request id (`:673`); accepting one hands the caller a
    // payload belonging to some other request as though it answered this one. Silent
    // wrong data is strictly worse than a loud refusal, so it is refused.
    let reply_id = response.get("id");
    let carries_error = response.get("error").is_some();
    let id_ok = matches!(reply_id, Some(v) if v.as_u64() == Some(id))
        || (carries_error && reply_id == Some(&Value::Null));
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

/// Continuously drain a child's stderr to EOF on a background thread.
///
/// AN UNDRAINED STDERR PIPE IS A DEADLOCK, NOT AN INEFFICIENCY. The OS pipe buffer is
/// small — measured on this Windows host, a child writing 1 KiB of stderr still answers,
/// but at 8 KiB it never does — and once the buffer fills the child BLOCKS inside its
/// stderr write and never reaches the stdout response. The parent is meanwhile blocked in
/// the response read, holding the engine mutex across it (`lib.rs:39-46`), with no
/// timeout anywhere on the path. So ONE flood wedges that call and every command after it
/// for the life of the app.
///
/// The prior contract — "the sidecar keeps stderr near-empty, so a plain pipe is safe" —
/// holds only while nothing writes there, and the engine cannot promise that: `corpus.build`
/// runs its ingest on a BACKGROUND THREAD (`llm_anthology/sidecar.py:20-21`) over session
/// files the user's other tools wrote, and an unhandled exception on a Python thread prints
/// a full traceback to stderr through `threading.excepthook`. A couple of tracebacks clear
/// 8 KiB. Nothing in the engine's own code has to be wrong for this to fire.
///
/// Bytes are RETAINED now rather than discarded — see the DECISION G-8 block above. Draining
/// and keeping are separate concerns and this function does both: the drain is what keeps the
/// child able to make progress, the sink is what keeps the traceback that explains a failure.
/// Nothing about the deadlock property changes, because the read loop is unchanged.
fn drain_stderr<R: Read + Send + 'static>(mut stderr: R, sink: Arc<Diagnostics>) {
    std::thread::spawn(move || {
        let mut buf = [0u8; 8192];
        // Stop on EOF (child gone) or on any read error — either way the pipe can no
        // longer block the child, which is the whole purpose.
        loop {
            match stderr.read(&mut buf) {
                Ok(0) | Err(_) => break,
                Ok(n) => sink.ingest(&buf[..n]),
            }
        }
        // The child is gone; flush whatever it was mid-way through writing when it died.
        sink.finish();
    });
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
        // The drainer thread now OWNS the parent end of stderr: it keeps the handle
        // alive exactly as the old `_stderr` field did, and additionally keeps the pipe
        // from ever filling. See `drain_stderr` for why holding it un-read deadlocks.
        drain_stderr(stderr, Arc::clone(diagnostics()));
        Ok(Self {
            stdin: Box::new(stdin),
            stdout: Box::new(BufReader::new(stdout)),
            _reaper: reaper,
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
            // stderr is piped per the transport contract, and DRAINED below — an
            // undrained pipe blocks the child once it fills. See `drain_stderr`.
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
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| "sidecar stderr was not piped".to_string())?;
        drain_stderr(stderr, Arc::clone(diagnostics()));
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
    use super::{
        diagnostics, drain_stderr, engine_python_in, jsonrpc_roundtrip,
        jsonrpc_roundtrip_bounded, open_crash_log, scrub_line, CrashLog, Diagnostics,
        PathScrubber, CRASH_FILE_MAX_BYTES, CRASH_FILE_NAME, CRASH_FILE_PREV_NAME,
        MAX_PENDING_LINE_BYTES, STDERR_RING_BYTES,
    };
    use serde_json::{json, Value};
    use std::io::Cursor;
    use std::path::PathBuf;
    use std::sync::Arc;

    // === DECISION G-8: engine stderr is EVIDENCE, kept in the parent =================

    /// Distinctive tokens standing in for the private material this corpus really holds.
    ///
    /// SYNTHETIC — invented for this test, never read from any corpus. Shaped like the
    /// medical/pharmaceutical content the owner's archive contains, because that is the
    /// material a leak would expose, and a canary that does not resemble the real risk
    /// tests the wrong thing.
    const CANARY_TOKENS: [&str; 3] = ["Amoxicillin", "clavulanate", "SYNTHETIC-CANARY-9f3a"];

    fn scratch_dir(tag: &str) -> PathBuf {
        use std::time::{SystemTime, UNIX_EPOCH};
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let dir = std::env::temp_dir()
            .join(format!("llm_anthology_diag_{tag}_{}_{nanos}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("create the scratch dir");
        dir
    }

    /// An in-memory sink with no scrubbing needles and no crash file — for the tests whose
    /// subject is the RING, not the filesystem or the relativization.
    fn ring_only() -> Diagnostics {
        Diagnostics::new(PathScrubber::new(None, None), None)
    }

    /// RETENTION **and** BOUND, both states, because either alone is a different bug.
    ///
    /// Before this unit `drain_stderr` read stderr into a throwaway array, so the retention
    /// half is the whole feature. The bound half is what keeps the fix from becoming the
    /// next defect: the producer is a child process this code does not control, and an
    /// unbounded buffer fed by an untrusted producer is the same allocation hazard
    /// `MAX_RESPONSE_LINE_BYTES` exists for.
    #[test]
    fn stderr_is_retained_in_a_bounded_ring_instead_of_being_discarded() {
        let sink = ring_only();

        // RETAINED: the structural shape of a traceback survives.
        sink.ingest(b"Traceback (most recent call last):\nValueError: boom\n");
        let kept = sink.snapshot();
        assert!(
            kept.contains("Traceback (most recent call last):"),
            "the traceback header must survive: {kept:?}"
        );
        assert!(
            kept.contains("ValueError: <redacted 4 chars>"),
            "the exception TYPE must survive and its message must not: {kept:?}"
        );

        // BOUNDED: ~180 KiB of retained text into a 64 KiB ring. Frame lines are used
        // because they carry a distinguishable payload through the allowlist — an exception
        // message would be redacted to the same marker on every line and could not tell the
        // oldest from the newest.
        for i in 0..4096 {
            sink.ingest(format!("  File \"/src/mod_{i}.py\", line {i}, in fn_{i}\n").as_bytes());
        }
        let kept = sink.snapshot();
        assert!(
            kept.len() <= STDERR_RING_BYTES,
            "the ring must stay under its {STDERR_RING_BYTES}-byte cap, got {}",
            kept.len()
        );
        assert!(
            kept.contains("mod_4095.py"),
            "keep-LAST: what happened just before the failure is what explains it: {}",
            &kept[kept.len().saturating_sub(200)..]
        );
        assert!(
            !kept.contains("mod_0.py"),
            "the oldest line must have been evicted, not merely joined by newer ones"
        );
        assert!(
            !kept.contains("Traceback (most recent call last):"),
            "eviction must reach the very first line too"
        );
    }

    /// THE PRIVACY GATE. Synthetic conversation content driven through a real-shaped failure
    /// must reach NEITHER surface — not the in-memory ring, not the file on disk.
    ///
    /// This is the test the unit exists to pass. A traceback carries the offending VALUE in
    /// its exception message, the surrounding source line, and whatever the failing code
    /// printed; all three are populated here with the canary. Without the allowlist the
    /// support channel becomes the leak channel — worse than the one two sibling units just
    /// closed, because a bug report is voluntarily pasted into a public issue tracker.
    ///
    /// DETECTOR CONTROL FIRST: the canary is asserted PRESENT in the input, and the
    /// structural parts are asserted PRESENT in the output. A scrubber that ate everything
    /// would pass a leak-only assertion while destroying the feature.
    #[test]
    fn synthetic_conversation_content_never_reaches_either_diagnostics_surface() {
        let dir = scratch_dir("privacy");
        let log = open_crash_log(&dir).expect("open the crash file");
        let sink = Diagnostics::new(
            PathScrubber::new(Some(r"C:\Users\tester"), Some("tester")),
            Some(log),
        );

        // A traceback of the shape `llm_anthology`'s adapters really produce, with the canary in
        // (a) the source echo, (b) the exception message, (c) a bare print.
        let stderr = concat!(
            "Traceback (most recent call last):\n",
            "  File \"C:\\Users\\tester\\AppData\\Local\\LLM Anthology\\engine\\Lib\\llm_anthology\\adapters\\chatgpt.py\", line 214, in _walk\n",
            "    raise KeyError(node[\"Amoxicillin and clavulanate 875mg\"])\n",
            "KeyError: 'Amoxicillin and clavulanate 875mg / SYNTHETIC-CANARY-9f3a'\n",
            "patient reported clavulanate side effects on 2026-03-02\n",
        );
        for token in CANARY_TOKENS {
            assert!(
                stderr.contains(token),
                "detector control: the input must actually carry {token:?}, or this test \
                 cannot fail"
            );
        }
        sink.ingest(stderr.as_bytes());
        sink.finish();

        let kept = sink.snapshot();
        let on_disk = std::fs::read_to_string(dir.join(CRASH_FILE_NAME)).expect("read crash file");

        // 1. The structural evidence SURVIVES — otherwise the scrubber is just a delete.
        assert!(
            kept.contains("Traceback (most recent call last):"),
            "{kept:?}"
        );
        assert!(
            kept.contains(
                "File \"~\\AppData\\Local\\LLM Anthology\\engine\\Lib\\llm_anthology\\adapters\\chatgpt.py\", line 214, in _walk"
            ),
            "the frame must survive WITH its home relativized: {kept:?}"
        );
        assert!(
            kept.contains("KeyError: <redacted"),
            "the exception TYPE is the diagnostic value and must survive: {kept:?}"
        );
        assert!(
            on_disk.contains("KeyError: <redacted"),
            "the on-disk surface carries the same evidence: {on_disk:?}"
        );

        // 2. NOTHING of the conversation survives, on either surface.
        for token in CANARY_TOKENS {
            assert!(
                !kept.contains(token),
                "{token:?} leaked into the in-memory bundle: {kept:?}"
            );
            assert!(
                !on_disk.contains(token),
                "{token:?} leaked onto disk: {on_disk:?}"
            );
        }
        // 3. Nor does the username, which identifies the reporter even without content.
        for surface in [&kept, &on_disk] {
            assert!(
                !surface.contains("tester"),
                "the username must be relativized away: {surface:?}"
            );
        }

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Home-path relativization — the same rule the shareable export applies, for the same
    /// reason.
    ///
    /// Both separator spellings and case-insensitively, because Windows paths are
    /// case-insensitive and a Python traceback prints whichever separator the import
    /// machinery built the path with. The two NEGATIVE arms matter as much: with no home
    /// known nothing may be invented, and a 2-character needle must be refused outright
    /// rather than replacing fragments of ordinary words.
    #[test]
    fn home_paths_and_the_username_are_relativized_before_anything_is_retained() {
        // The username is DERIVED from the home directory when not supplied separately.
        let paths = PathScrubber::new(Some(r"C:\Users\tester"), None);
        let out = scrub_line(r#"  File "c:/Users/Tester/app/x.py", line 3, in f"#, &paths);
        assert_eq!(out, r#"  File "~/app/x.py", line 3, in f"#, "got {out:?}");
        assert_eq!(
            paths.apply("hello tester"),
            "hello <user>",
            "a bare username identifies the reporter even outside a path"
        );

        let unknown = PathScrubber::new(None, None);
        assert_eq!(
            unknown.apply(r"C:\Users\tester\x"),
            r"C:\Users\tester\x",
            "with no home known, nothing may be invented"
        );

        let tiny = PathScrubber::new(Some("C:"), Some("ab"));
        assert_eq!(
            tiny.apply("C: is a drive and ab is a syllable"),
            "C: is a drive and ab is a syllable",
            "a 1-2 character needle matches inside ordinary words and must be refused"
        );
    }

    /// Scrubbing is IDEMPOTENT, which the previous-run file re-read depends on: that file is
    /// already scrubbed, and it is scrubbed again on its way into a bundle.
    #[test]
    fn scrubbing_an_already_scrubbed_line_changes_nothing() {
        let paths = PathScrubber::new(Some(r"C:\Users\tester"), Some("tester"));
        for line in [
            "Traceback (most recent call last):",
            r#"  File "C:\Users\tester\app\adapters\grok.py", line 9, in load"#,
            "KeyError: 'mapping'",
            "PermissionError:",
            "some free text nobody allowlisted",
            "",
            "   ",
        ] {
            let once = scrub_line(line, &paths);
            let twice = scrub_line(&once, &paths);
            assert_eq!(once, twice, "not idempotent for {line:?}");
        }
    }

    /// A capitalised word before a colon is NOT an exception line.
    ///
    /// Regression pin on a real hole in the first draft: the matcher accepted any
    /// capitalised dotted token, which `Amoxicillin: 500mg` satisfies — so a single line of
    /// conversation would have walked straight through the allowlist. The suffix list
    /// ([`super::EXCEPTION_SUFFIXES`]) is what closed it.
    #[test]
    fn a_capitalised_word_before_a_colon_is_not_mistaken_for_an_exception() {
        let paths = PathScrubber::new(None, None);
        for leak in [
            "Amoxicillin: 500mg twice daily",
            "Diagnosis: nothing to see here",
            "Patient: anonymous",
        ] {
            let out = scrub_line(leak, &paths);
            assert!(
                out.starts_with("<redacted "),
                "{leak:?} must be redacted whole, got {out:?}"
            );
        }
        // Real exception names still pass, including dotted and non-`Error` ones.
        for real in [
            "KeyError: 'mapping'",
            "sqlite3.OperationalError: database is locked",
            "KeyboardInterrupt: ",
            "SystemExit: 1",
            "DeprecationWarning: stop it",
        ] {
            let out = scrub_line(real, &paths);
            assert!(
                !out.starts_with("<redacted "),
                "{real:?} is a real exception line and must keep its type, got {out:?}"
            );
        }
    }

    /// An UNTERMINATED line cannot grow the assembly buffer without bound.
    #[test]
    fn an_unterminated_stderr_line_cannot_grow_the_buffer_without_bound() {
        let sink = ring_only();
        // Four ceilings' worth of bytes and not one newline.
        sink.ingest(&vec![b'z'; MAX_PENDING_LINE_BYTES * 4]);
        assert!(
            sink.lock().pending.len() < MAX_PENDING_LINE_BYTES,
            "the buffer must FLUSH at the ceiling rather than hoard the whole stream"
        );
        let kept = sink.snapshot();
        assert_eq!(
            kept.lines().count(),
            4,
            "one flushed line per ceiling-worth of bytes: {kept:?}"
        );
        assert!(
            kept.starts_with("<redacted 8192 chars>"),
            "a forced flush is not allowlisted, so it is redacted: {kept:?}"
        );
    }

    /// The crash file is HARD-capped and KEEPS-FIRST, and the ring keeps-last — the two
    /// surfaces are deliberate complements, not redundant copies.
    ///
    /// Driven with a tiny cap on purpose: proving the production 256 KiB would mean writing
    /// 256 KiB in CI, exactly as `a_response_line_over_the_ceiling_is_refused_not_allocated`
    /// drives its own ceiling small.
    #[test]
    fn the_crash_file_is_capped_and_keeps_the_first_bytes() {
        let dir = scratch_dir("cap");
        let path = dir.join(CRASH_FILE_NAME);
        let log = CrashLog {
            file: std::fs::File::create(&path).expect("create the crash file"),
            path: path.clone(),
            written: 0,
            cap: 64,
        };
        let sink = Diagnostics::new(PathScrubber::new(None, None), Some(log));

        sink.ingest(b"  File \"/a.py\", line 1, in first\n");
        for i in 0..200 {
            sink.ingest(format!("  File \"/b.py\", line {i}, in later\n").as_bytes());
        }

        let on_disk = std::fs::read_to_string(&path).expect("read the crash file");
        assert_eq!(
            on_disk.len(),
            64,
            "the cap is a HARD ceiling, not a target: {on_disk:?}"
        );
        assert!(
            on_disk.starts_with("  File \"/a.py\", line 1, in first\n"),
            "keep-FIRST: the first traceback names the root cause: {on_disk:?}"
        );
        assert!(
            !on_disk.contains("line 199"),
            "past the cap nothing more may be written: {on_disk:?}"
        );
        assert!(
            sink.snapshot().contains("line 199, in later"),
            "the RING meanwhile keeps the last lines — that complement is the design"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// EXACTLY TWO generations on disk, ever.
    ///
    /// The previous generation is not tidiness: the crash case is "the app died and the user
    /// restarted it", so truncating on start would destroy the evidence at the moment it was
    /// about to be read. A third run must DISCARD the oldest rather than accumulate — an
    /// unbounded rotation scheme on a user's disk is the failure being avoided.
    #[test]
    fn a_second_run_rotates_the_crash_file_to_exactly_one_previous_generation() {
        let dir = scratch_dir("rotate");
        for run in 1..=2 {
            let mut log = open_crash_log(&dir).expect("open the crash file");
            log.append(&format!("  File \"/run{run}.py\", line {run}, in only"));
        }

        let current = std::fs::read_to_string(dir.join(CRASH_FILE_NAME)).expect("current");
        let previous = std::fs::read_to_string(dir.join(CRASH_FILE_PREV_NAME)).expect("previous");
        assert!(current.contains("/run2.py"), "got {current:?}");
        assert!(
            previous.contains("/run1.py"),
            "the run BEFORE the restart is the one worth keeping: {previous:?}"
        );

        let mut names: Vec<String> = std::fs::read_dir(&dir)
            .expect("list the logs dir")
            .map(|e| {
                e.expect("dir entry")
                    .file_name()
                    .to_string_lossy()
                    .into_owned()
            })
            .collect();
        names.sort();
        assert_eq!(
            names,
            vec![CRASH_FILE_NAME.to_string(), CRASH_FILE_PREV_NAME.to_string()],
            "two files and no more"
        );

        let mut third = open_crash_log(&dir).expect("third run");
        third.append("  File \"/run3.py\", line 3, in only");
        let previous = std::fs::read_to_string(dir.join(CRASH_FILE_PREV_NAME)).expect("previous");
        assert!(
            previous.contains("/run2.py") && !previous.contains("/run1.py"),
            "the oldest generation must be discarded, not kept: {previous:?}"
        );
        assert_eq!(
            std::fs::read_dir(&dir).expect("list").count(),
            2,
            "still exactly two"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The bundle the UI copies: platform, both caps, both crash generations — and every
    /// path in it relativized, because a bundle is a SHARED artifact by definition.
    #[test]
    fn the_bundle_names_the_platform_the_caps_and_both_crash_generations() {
        let dir = scratch_dir("bundle");
        {
            let mut previous = open_crash_log(&dir).expect("first run");
            previous.append("PermissionError:");
        }
        let log = open_crash_log(&dir).expect("second run");
        // Home is pointed AT the scratch dir so the relativization assertion below is
        // deterministic on both CI legs rather than depending on the real profile.
        let sink = Diagnostics::new(
            PathScrubber::new(dir.to_str(), Some("tester")),
            Some(log),
        );
        sink.ingest(b"RuntimeError: boom\n");

        let bundle = sink.bundle();
        assert_eq!(bundle["platform"]["os"], json!(std::env::consts::OS));
        assert_eq!(bundle["platform"]["arch"], json!(std::env::consts::ARCH));
        assert!(
            bundle["engine_stderr"]
                .as_str()
                .is_some_and(|s| s.contains("RuntimeError: <redacted 4 chars>")),
            "bundle: {bundle}"
        );
        assert_eq!(bundle["engine_stderr_cap_bytes"], json!(STDERR_RING_BYTES));
        assert_eq!(bundle["crash_file"]["cap_bytes"], json!(CRASH_FILE_MAX_BYTES));
        let path = bundle["crash_file"]["path"]
            .as_str()
            .expect("a crash path");
        assert!(
            path.starts_with('~'),
            "the crash path must be home-relative: {path}"
        );
        assert!(
            bundle["previous_run"]["tail"]
                .as_str()
                .is_some_and(|s| s.contains("PermissionError:")),
            "the previous run's evidence is the whole point of the on-disk half: {bundle}"
        );
        assert!(
            bundle["previous_run"]["bytes"].as_u64().is_some_and(|n| n > 0),
            "bundle: {bundle}"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The process-wide sink is created once, and under `cfg(test)` it must NOT open a file
    /// in the developer's real `%LOCALAPPDATA%\LLM Anthology\logs`.
    #[test]
    fn the_process_wide_sink_is_created_once_and_writes_no_file_under_test() {
        assert!(
            Arc::ptr_eq(diagnostics(), diagnostics()),
            "three engines share ONE sink; a per-client buffer dies with the throwaway"
        );
        assert_eq!(
            diagnostics().bundle()["crash_file"],
            Value::Null,
            "running the suite must not rotate the developer's real crash log"
        );
    }

    /// THE WIRE: a real child's real traceback, through the real `drain_stderr`, lands in the
    /// ring already scrubbed.
    ///
    /// The unit tests above drive `ingest` directly; this one proves the join — that the
    /// production drainer is what feeds the sink — across a genuine OS pipe. That
    /// `spawn_platform` calls `drain_stderr` is a compiler-checked citation rather than
    /// something measured here.
    #[test]
    fn a_real_child_traceback_reaches_the_ring_through_the_production_drainer() {
        use std::process::{Command, Stdio};
        use std::time::{Duration, Instant};

        const BOOM: &str = concat!(
            "import sys\n",
            "sys.stderr.write('Traceback (most recent call last):\\n')\n",
            "sys.stderr.write('  File \"/x/adapters/grok.py\", line 7, in load\\n')\n",
            "sys.stderr.write(\"KeyError: 'SYNTHETIC-CANARY-9f3a'\\n\")\n",
            "sys.stderr.flush()\n",
        );

        let sink = Arc::new(ring_only());
        let mut child = Command::new("python")
            .arg("-c")
            .arg(BOOM)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .expect("spawn the traceback-emitting child (python on PATH?)");
        let stderr = child.stderr.take().expect("stderr piped");
        drain_stderr(stderr, Arc::clone(&sink));
        let _ = child.wait();

        // The drainer owns its own thread, so WAIT for the bytes instead of assuming them.
        let deadline = Instant::now() + Duration::from_secs(10);
        while !sink.snapshot().contains("grok.py") && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }

        let kept = sink.snapshot();
        assert!(
            kept.contains("Traceback (most recent call last):"),
            "the drainer must feed the ring: {kept:?}"
        );
        assert!(kept.contains("line 7, in load"), "{kept:?}");
        assert!(kept.contains("KeyError: <redacted"), "{kept:?}");
        assert!(
            !kept.contains("SYNTHETIC-CANARY-9f3a"),
            "scrubbing happens at INGEST, so nothing unscrubbed is ever retained: {kept:?}"
        );
    }

    // === transport robustness: the child cannot wedge or exhaust the parent ==========

    /// STDERR FLOOD, BOTH STATES — an undrained stderr pipe DEADLOCKS the round trip.
    ///
    /// The child writes 256 KiB to stderr BEFORE reading its request, so it sits blocked
    /// inside that write and never reaches its stdout reply. Both states are asserted
    /// because one alone proves nothing: WITHOUT the drainer the round trip never returns
    /// — and in the app that means the engine mutex is held across it (`lib.rs:39-46`)
    /// with no timeout anywhere, so every later command is wedged too — WITH the
    /// production `drain_stderr` the same child answers at once.
    ///
    /// Measured on this host by an independent `subprocess` probe: 1 KiB of undrained
    /// stderr still answers, 8 KiB never does. The ceiling is the OS pipe buffer (~4 KiB
    /// for a Windows `CreatePipe(.., 0)`), which two Python tracebacks clear.
    ///
    /// SCOPE OF THE PROOF: this drives the real `jsonrpc_roundtrip` over a child spawned
    /// with the same piped topology production uses, and calls the same `drain_stderr`.
    /// That `spawn_platform` calls it is a compiler-checked code citation, not something
    /// measured here.
    #[test]
    fn stderr_flood_deadlocks_the_round_trip_unless_the_pipe_is_drained() {
        assert!(
            !stderr_flood_answers(false, 3),
            "an UNDRAINED stderr pipe must leave the child blocked mid-write; if this \
             arm passes, the host buffered all 256 KiB and the test proves nothing"
        );
        assert!(
            stderr_flood_answers(true, 30),
            "WITH drain_stderr the SAME child must answer — that difference is what \
             makes the drainer load-bearing rather than decorative"
        );
    }

    /// Spawn a child that floods stderr and then replies on stdout; report whether ONE
    /// round trip completes within `secs`. `drain` selects the production drainer versus
    /// the previous behaviour (hold the handle open, never read it).
    fn stderr_flood_answers(drain: bool, secs: u64) -> bool {
        use std::io::BufReader;
        use std::process::{ChildStderr, Command, Stdio};
        use std::sync::mpsc;
        use std::time::Duration;

        const FLOOD: &str = concat!(
            "import sys\n",
            "sys.stderr.write('x' * 262144)\n",
            "sys.stderr.flush()\n",
            "sys.stdin.readline()\n",
            "sys.stdout.write('{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"ok\":true}}\\n')\n",
            "sys.stdout.flush()\n",
        );

        let mut child = Command::new("python")
            .arg("-c")
            .arg(FLOOD)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("spawn the stderr-flooding child (python on PATH?)");
        let mut stdin = child.stdin.take().expect("stdin piped");
        let stdout = child.stdout.take().expect("stdout piped");
        let stderr = child.stderr.take().expect("stderr piped");

        // The undrained arm must HOLD the handle open. Dropping it closes the parent's
        // read end, the child's write then fails instead of blocking, and the deadlock
        // silently fails to reproduce — the test would look green and prove nothing.
        let _held: Option<ChildStderr> = if drain {
            drain_stderr(stderr, Arc::new(ring_only()));
            None
        } else {
            Some(stderr)
        };

        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let got = jsonrpc_roundtrip(&mut stdin, &mut reader, 1, "health.ping", &json!({}));
            let _ = tx.send(got.is_ok());
        });
        let answered = rx.recv_timeout(Duration::from_secs(secs)).unwrap_or(false);

        // Kill either way: in the blocked arm this is what releases both the worker
        // thread and the child, so a PROVEN deadlock does not leak out of the test.
        let _ = child.kill();
        let _ = child.wait();
        answered
    }

    /// RESPONSE CEILING, BOTH STATES — a line within the cap parses; one past it is
    /// refused rather than allocated.
    ///
    /// `read_line` grows without limit and the engine caps nothing: `conversation.get`
    /// returns a whole transcript re-parsed from a session file the user's OTHER tools
    /// wrote. Unbounded, an over-large line ends in an allocation failure, which ABORTS
    /// the process — Rust cannot unwind from OOM — losing the app rather than the call.
    ///
    /// Driven through `jsonrpc_roundtrip_bounded` with a tiny cap on purpose: asserting
    /// the production 256 MiB constant would mean allocating 256 MiB in CI. The wrapper
    /// differs only in which number it passes.
    #[test]
    fn a_response_line_over_the_ceiling_is_refused_not_allocated() {
        const CAP: u64 = 200;

        // UNDER the ceiling: parses exactly as before.
        let small = br#"{"jsonrpc":"2.0","id":1,"result":{"ok":true}}"#.to_vec();
        assert!(small.len() as u64 <= CAP, "fixture must sit under the cap");
        let mut line = small.clone();
        line.push(b'\n');
        let mut written: Vec<u8> = Vec::new();
        let mut reader = Cursor::new(line);
        let out =
            jsonrpc_roundtrip_bounded(&mut written, &mut reader, 1, "h", &json!({}), CAP)
                .expect("a line within the ceiling must parse");
        assert_eq!(out, json!({ "ok": true }));

        // OVER the ceiling: the same shape, padded past it.
        let pad = "z".repeat(CAP as usize * 2);
        let big = format!("{{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{{\"p\":\"{pad}\"}}}}\n");
        assert!(big.len() as u64 > CAP, "fixture must exceed the cap");
        let mut written: Vec<u8> = Vec::new();
        let mut reader = Cursor::new(big.into_bytes());
        let err = jsonrpc_roundtrip_bounded(&mut written, &mut reader, 1, "h", &json!({}), CAP)
            .expect_err("a line past the ceiling must be refused, not allocated");
        assert!(err.contains("exceeds the 200-byte ceiling"), "got: {err}");
        assert!(
            err.contains("out of sync"),
            "the refusal must say the stream is unusable, so the caller does not retry \
             into a mid-frame stream: {err}"
        );
    }

    /// A NULL-id reply may carry an ERROR (a parse error cannot echo an id it failed to
    /// read), but never a RESULT — that would be uncorrelated data returned as this
    /// call's answer.
    ///
    /// Measured before the fix: `id:null` + `result` returned `Ok("other")` for request
    /// 42. No legitimate producer exists — the engine's only null-id replies are
    /// `_error_response(None, ..)` on a parse / invalid-request line
    /// (`llm_anthology/sidecar.py:656,662`), while every `result` echoes the numeric
    /// request id (`:673`) — so the tolerance is narrowed to exactly its stated purpose.
    #[test]
    fn a_null_id_reply_is_tolerated_only_when_it_carries_an_error() {
        // ERROR + null id: still tolerated, and surfaced as the error it is.
        let mut written: Vec<u8> = Vec::new();
        let mut reader = Cursor::new(
            b"{\"jsonrpc\":\"2.0\",\"id\":null,\"error\":{\"code\":-32700,\"message\":\"Parse error\"}}\n"
                .to_vec(),
        );
        let err = jsonrpc_roundtrip(&mut written, &mut reader, 42, "h", &json!({}))
            .expect_err("a null-id error envelope must map to Err");
        assert!(err.contains("Parse error"), "got: {err}");
        assert!(!err.contains("mismatch"), "it must surface AS the error, not as: {err}");

        // RESULT + null id: refused. Accepting it hands the caller another request's
        // payload as though it were the answer to this one.
        let mut written: Vec<u8> = Vec::new();
        let mut reader =
            Cursor::new(b"{\"jsonrpc\":\"2.0\",\"id\":null,\"result\":\"other\"}\n".to_vec());
        let err = jsonrpc_roundtrip(&mut written, &mut reader, 42, "h", &json!({}))
            .expect_err("an uncorrelated null-id RESULT must not be returned as ours");
        assert!(err.contains("mismatch"), "got: {err}");
    }

    /// Invalid UTF-8 is named as such rather than surfacing as a generic read failure.
    ///
    /// `read_line` returned `InvalidData` here and left its buffer unspecified with the
    /// bytes already consumed; reading raw and validating explicitly says what went wrong
    /// and keeps it distinguishable from a JSON parse error.
    #[test]
    fn a_response_line_that_is_not_utf8_is_named_as_such() {
        let mut written: Vec<u8> = Vec::new();
        let mut reader = Cursor::new(b"{\"id\":1,\"result\":\"\xff\xfe\"}\n".to_vec());
        let err = jsonrpc_roundtrip(&mut written, &mut reader, 1, "health.ping", &json!({}))
            .expect_err("invalid UTF-8 must map to Err");
        assert!(err.contains("not valid UTF-8"), "got: {err}");
    }

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
