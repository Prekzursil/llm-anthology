mod sidecar;

use std::path::{Path, PathBuf};
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
///
/// It ALSO carries the DECISION G-10 default locations, and that placement is deliberate
/// rather than convenient. They are static per-install metadata, exactly like the version;
/// and putting them on an already-registered command means the resolution has a production
/// caller — run against the REAL environment on every status query — without adding a new
/// Tauri command, whose TypeScript binding lives in files this work unit does not own.
/// (`cockpit/src/ipc/real.test.ts` pins `app_info` as the one registered command the
/// adapter never calls, so a new unbound command would fail that suite.)
///
/// `locations` and `locations_error` are ALWAYS both present and exactly one is null, so a
/// consumer branches on shape rather than on a missing key. Resolution can genuinely fail
/// — a stripped environment with no `%USERPROFILE%` / `$HOME` — and when it does the status
/// line must still answer, degraded and saying why, rather than take the app down with it.
#[tauri::command]
fn app_info() -> Value {
    let (locations, locations_error) = match app_locations() {
        Ok(resolved) => (resolved.to_json(), Value::Null),
        Err(why) => (Value::Null, Value::String(why)),
    };
    json!({
        "name": "Cockpit",
        "version": env!("CARGO_PKG_VERSION"),
        "engine": "not-wired",
        "locations": locations,
        "locations_error": locations_error,
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
///   `is_file()` already made. The MAPPED-DRIVE half of that is now closed upstream by
///   `reject_remote_drive`, which asks `GetDriveTypeW` before any filesystem call (this
///   paragraph used to say that check was "deliberately NOT done here" pending a
///   `Win32_Storage_FileSystem` feature; the feature is in Cargo.toml and the check is wired).
///   The SYMLINK half is still open: a link has no remote drive type, so it is caught only
///   here, after the probe. Closing it needs a `symlink_metadata` ancestor walk.
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

/// `DRIVE_REMOTE` from `GetDriveTypeW`. Named rather than inlined so the test reads as the
/// Windows contract it is, not as a magic 4.
const DRIVE_REMOTE: u32 = 4;

/// Judge a drive type. PURE — the caller does the syscall, exactly as `reject_unc_resolution`
/// has the caller do the resolution, so the rejecting branch is testable without mounting a
/// share.
///
/// `None` means "could not ask" (a non-drive-letter path, or a non-Windows build) and is
/// deliberately NOT a rejection: this guard is an early-out for a case the later
/// `reject_unc_resolution` still catches, so failing open here loses nothing. Failing CLOSED
/// would refuse ordinary POSIX paths, where the question cannot even be posed.
fn reject_remote_drive(index_path: &str, drive_type: Option<u32>) -> Result<(), String> {
    if drive_type == Some(DRIVE_REMOTE) {
        return Err(format!(
            "{index_path} is on a mapped network drive; a corpus index must be a local file"
        ));
    }
    Ok(())
}

/// The drive type of a path's ROOT, or `None` when the question does not apply.
///
/// WHY THIS RUNS BEFORE `is_file()`. `reject_unc_resolution` judges the CANONICALIZED path, so
/// it only fires after the filesystem has already been touched — it stops the corpus being
/// ATTACHED over SMB but not the outbound connection that answering `is_file()` already made.
/// Two costs follow from that, and this closes both:
///
/// - AVAILABILITY, the one actually likely to bite. A DISCONNECTED mapping puts a multi-minute
///   blocking stat on the `#[tauri::command]` thread with no cancel and no timeout — the UI
///   simply stops. Measured on this repo's own UNC test: 12.85s and 13.18s against an
///   unreachable host with the guard neutered, versus 0.00s with it in place, and a prior
///   measurement recorded 272,061 ms against a black-holed address.
/// - SECURITY. That outbound stat is the SMB/NTLM handshake, so preventing it — rather than
///   refusing afterwards — is the difference between not attaching and not connecting.
///
/// `GetDriveTypeW` is a local mount-table lookup: no network round trip, so asking is cheap
/// even for a dead mapping. It takes the drive ROOT (`Z:\`), not the full path.
#[cfg(windows)]
fn drive_type_of(index_path: &str) -> Option<u32> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::GetDriveTypeW;

    // Only a drive-letter path has a drive type to ask about. A UNC spelling never reaches
    // here (the lexical guard runs first) and a relative path has no root.
    let bytes = index_path.as_bytes();
    if bytes.len() < 3 || bytes[1] != b':' || !bytes[0].is_ascii_alphabetic() {
        return None;
    }
    let root: Vec<u16> = std::ffi::OsStr::new(&format!("{}:\\", bytes[0] as char))
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    // SAFETY: `root` is a NUL-terminated UTF-16 buffer that outlives the call, which is the
    // whole of GetDriveTypeW's contract. It cannot fail — an unknown root answers
    // DRIVE_UNKNOWN/DRIVE_NO_ROOT_DIR, both of which fall through as "not remote".
    Some(unsafe { GetDriveTypeW(root.as_ptr()) })
}

#[cfg(not(windows))]
fn drive_type_of(_index_path: &str) -> Option<u32> {
    // Inert by design: there are no drive letters, and a cifs/NFS mount is indistinguishable
    // from a local path without reading the mount table. `reject_unc_resolution` documents the
    // same limitation for the same reason.
    None
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
    validate_index_path_with(index_path, drive_type_of)
}

/// The rule, with the drive-type lookup INJECTED.
///
/// Split out purely so the ORDERING is testable. The whole value of the remote-drive check is
/// that it runs before `is_file()`; a test of `reject_remote_drive` alone would still pass with
/// the call deleted, or moved after the filesystem touch, which is exactly the mistake worth
/// catching. With the lookup injected, a test can hand it DRIVE_REMOTE for a path that does not
/// exist and prove the remote refusal WINS over the not-found one — which can only happen if
/// the check ran first.
fn validate_index_path_with(
    index_path: &str,
    drive_type: impl Fn(&str) -> Option<u32>,
) -> Result<(), String> {
    reject_unc_spelling(index_path)?;
    let path = std::path::Path::new(index_path);
    if !path.is_absolute() {
        return Err(format!(
            "{index_path} is not a full path; name the corpus index by its absolute path"
        ));
    }
    // BEFORE any filesystem call. A mapped network drive answers `is_file()` by going out on
    // the wire, so asking first is what keeps a dead mapping from freezing the command thread
    // and a live one from completing an SMB handshake we are about to refuse anyway.
    reject_remote_drive(index_path, drive_type(index_path))?;
    if path.is_file() {
        // It exists and it is a file — but "file" is not yet "local file". A SYMLINK reaches a
        // share without ever being spelled `\\host\...` and without a remote drive type, so
        // neither guard above can see it. See `reject_unc_resolution`.
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

// -- DECISION G-10: standard OS app-data locations ------------------------------------
//
// The app writes to exactly FOUR places, and every one of them is derived here. That is
// not tidiness: `sidecar.rs`'s `Membrane::AppContainer` is implemented and tested but
// INACTIVE, because a sandboxed engine can only touch paths explicitly granted to its
// package SID — so the membrane needs a path set that is fixed, small, and enumerable.
// `AppLocations::grant_roots` is that enumeration.
//
// WHERE, and why:
//   - machine data (index, logs, cache) -> the per-machine app-data root. On Windows that
//     is `%LOCALAPPDATA%`, NEVER `%APPDATA%`: the latter is `...\AppData\Roaming`, which a
//     domain-joined machine copies to a server at logon, so a 55-75 MB corpus index placed
//     there is dragged over the network on every login. `reject_roaming` enforces that.
//   - the default export destination -> `Documents\LLM Anthology`, because an export is a
//     document the human goes looking for.
//
// The index path remains USER-OVERRIDABLE. Nothing here constrains `open_corpus`, which
// still accepts any absolute local file — a growing archive belongs on whatever drive the
// user wants, and `a_user_chosen_index_outside_the_app_data_root_is_still_accepted` pins
// that this resolution never became a jail.

/// The product's folder name as a human reads it. Used for every USER-VISIBLE folder.
const APP_DISPLAY_NAME: &str = "LLM Anthology";

/// The same product, spelled for a POSIX dotfile root. `~/.local/share/llm-anthology`
/// matches what every other application there looks like; `~/.local/share/LLM Anthology`
/// does not, and a space in a path that shell scripts and XDG tooling handle is a
/// gratuitous hazard. Windows has the opposite convention — `%LOCALAPPDATA%` is full of
/// display-cased, space-bearing folder names — so the machine folder's leaf name is
/// platform-dependent while the Documents folder's is not.
#[cfg(not(windows))]
const APP_SLUG: &str = "llm-anthology";

/// The corpus index filename inside the app's data folder. Keeps the name the owner's
/// existing corpus already uses, so only the DIRECTORY changes.
const INDEX_FILE_NAME: &str = "anthology.sqlite";

/// The leaf name of the machine-data folder. See [`APP_SLUG`] for why it differs.
#[cfg(windows)]
fn machine_dir_name() -> &'static str {
    APP_DISPLAY_NAME
}

#[cfg(not(windows))]
fn machine_dir_name() -> &'static str {
    APP_SLUG
}

/// The platform base directories every default path is derived from.
///
/// Handed IN rather than read from the environment inside the resolver, for the same
/// reason `validate_index_path_with` takes its drive-type lookup as a parameter: the rule
/// is then testable without depending on whose machine ran the test, and the windows and
/// ubuntu CI legs can each assert the layout they actually have.
#[derive(Debug)]
pub struct BaseDirs {
    /// Per-machine (non-roaming) application data. Windows: `%LOCALAPPDATA%`.
    /// POSIX: `$XDG_DATA_HOME`, default `$HOME/.local/share`.
    pub local_data: PathBuf,
    /// Discardable cache root. Windows: the SAME directory as `local_data` — Windows has
    /// no separate cache base, so the cache lives inside the app's own folder.
    /// POSIX: `$XDG_CACHE_HOME`, default `$HOME/.cache`, which is deliberately outside the
    /// data root so a backup tool can skip it.
    pub cache: PathBuf,
    /// The user's documents folder, parent of the default export destination.
    pub documents: PathBuf,
}

/// Every path the app writes to by default.
#[derive(Debug)]
pub struct AppLocations {
    /// The one machine-data folder. On Windows everything below lives inside it.
    pub data_dir: PathBuf,
    /// The DEFAULT corpus index. A default, not a requirement — see the module note.
    pub index_path: PathBuf,
    pub logs_dir: PathBuf,
    pub cache_dir: PathBuf,
    /// Default destination for `export.run` artifacts.
    pub exports_dir: PathBuf,
}

impl AppLocations {
    /// The MINIMAL set of directory roots that must be granted to the AppContainer
    /// package SID before `Membrane::AppContainer` can be switched on.
    ///
    /// Minimal because each grant is a real `icacls` walk — the membrane test in
    /// `sidecar.rs` grants the CPython prefix (~8k files) rather than a whole profile
    /// (~130k) for precisely that reason — and because granting a directory already
    /// covers everything beneath it. Any candidate that is inside another is therefore
    /// dropped rather than listed twice. On Windows that folds logs and cache into
    /// `data_dir` and yields two roots; under XDG the cache has its own base and yields
    /// three.
    pub fn grant_roots(&self) -> Vec<&Path> {
        let mut roots: Vec<&Path> = Vec::new();
        for candidate in [
            self.data_dir.as_path(),
            self.cache_dir.as_path(),
            self.exports_dir.as_path(),
        ] {
            // `starts_with` is component-wise, and true for equal paths — which is what
            // makes this also the de-duplicator when two bases coincide.
            if roots.iter().any(|kept| candidate.starts_with(kept)) {
                continue;
            }
            roots.retain(|kept| !kept.starts_with(candidate));
            roots.push(candidate);
        }
        roots
    }

    /// The set as JSON, for the frontend and for a later grant step to enumerate.
    fn to_json(&self) -> Value {
        json!({
            "data_dir": self.data_dir.to_string_lossy(),
            "index_path": self.index_path.to_string_lossy(),
            "logs_dir": self.logs_dir.to_string_lossy(),
            "cache_dir": self.cache_dir.to_string_lossy(),
            "exports_dir": self.exports_dir.to_string_lossy(),
            "grant_roots": self
                .grant_roots()
                .iter()
                .map(|p| p.to_string_lossy())
                .collect::<Vec<_>>(),
        })
    }
}

/// Refuse a base that resolves under a ROAMING profile.
///
/// This is the load-bearing half of "use `%LOCALAPPDATA%`". The failure it guards is not
/// hypothetical: `%LOCALAPPDATA%` can be absent from a stripped or service environment,
/// and the obvious-looking fallback is `%APPDATA%` — which is the roaming half of the same
/// `AppData` folder. A domain-joined machine synchronises that to a server at logon, so an
/// index there is copied over the network every login. `base_dirs_with` never reads
/// `APPDATA` at all; this check is the belt to that braces, and it also catches a base
/// handed in by a caller (or a future settings file) that points at the roaming profile.
///
/// Compared case-INSENSITIVELY because Windows paths are, and the spelling comes from
/// whatever set the variable. Runs on POSIX too — a roaming profile has no meaning there,
/// so the check is inert in practice, but one code path means the ubuntu CI leg still
/// exercises it and a POSIX directory literally named `Roaming` is refused in the safe
/// direction.
fn reject_roaming(what: &str, base: &Path) -> Result<(), String> {
    if base
        .components()
        .any(|c| c.as_os_str().eq_ignore_ascii_case("Roaming"))
    {
        return Err(format!(
            "{what} ({}) is inside a roaming profile; a corpus index and its cache must \
             live in per-machine storage or a domain logon will copy them over the network",
            base.display()
        ));
    }
    Ok(())
}

/// Windows keeps no separate cache base, so `base.cache` is normally the very same
/// directory as `base.local_data` and this lands at `%LOCALAPPDATA%\LLM Anthology\cache`:
/// one folder to grant, one folder to delete.
///
/// Derived from `base.cache` rather than from the already-built `data_dir`, even though
/// the two produce an identical answer for every real input. A field that one platform
/// reads and the other silently ignores cannot be VALIDATED on the platform that ignores
/// it, and a caller who set it would be overruled without being told. The first draft did
/// read `data_dir` here, and `the_default_index_path_passes_every_pre_filesystem_guard_in_
/// the_open_corpus_chain` caught it by poisoning each base in turn: a UNC cache base was
/// discarded instead of refused, which also made the roaming-cache assertion in
/// `a_roaming_base_is_refused_and_a_local_one_is_accepted` a check on an unreachable value.
#[cfg(windows)]
fn cache_dir_for(base: &BaseDirs) -> PathBuf {
    base.cache.join(machine_dir_name()).join("cache")
}

/// XDG gives the cache its OWN base so that backup and sync tools can skip it, so on POSIX
/// the cache is deliberately NOT inside the data directory.
#[cfg(not(windows))]
fn cache_dir_for(base: &BaseDirs) -> PathBuf {
    base.cache.join(APP_SLUG)
}

/// Apply the PRE-FILESYSTEM half of `open_corpus`'s guard chain to a default path.
///
/// Same functions, same order — `reject_unc_spelling` on the raw string, then
/// `is_absolute`, then `reject_remote_drive` before anything touches the disk. Reusing
/// them rather than restating the rules is the point: a default that the real chain would
/// refuse must fail HERE, at resolution, instead of being discovered when the user presses
/// Open. The two probes that chain ends with (`is_file` / `canonicalize`) are deliberately
/// NOT run — a default index does not exist yet on a fresh machine, and a default export
/// folder has not been created either.
///
/// The guard's own wording is preserved inside the message so a caller (and the tests) can
/// still tell WHICH guard fired; `what` only adds the context of which path it was.
fn reject_nonlocal_default(
    what: &str,
    path: &Path,
    drive_type: &impl Fn(&str) -> Option<u32>,
) -> Result<(), String> {
    let text = path.to_str().ok_or_else(|| {
        format!(
            "{what} ({}) is not valid UTF-8, so it cannot be handed to the engine",
            path.display()
        )
    })?;
    let context = |cause: String| format!("{what} is unusable: {cause}");
    reject_unc_spelling(text).map_err(context)?;
    if !path.is_absolute() {
        return Err(context(format!(
            "{text} is not a full path; the app-data base must be absolute"
        )));
    }
    reject_remote_drive(text, drive_type(text)).map_err(context)
}

/// Resolve every default path from INJECTED base directories.
///
/// Fails rather than returning a path the app could not then use: a roaming base, a UNC
/// base, a relative base, or a base on a mapped network drive are all refused here.
pub fn app_locations_from(
    base: &BaseDirs,
    drive_type: impl Fn(&str) -> Option<u32>,
) -> Result<AppLocations, String> {
    reject_roaming("the app-data folder", &base.local_data)?;
    reject_roaming("the cache folder", &base.cache)?;

    let data_dir = base.local_data.join(machine_dir_name());
    let locations = AppLocations {
        index_path: data_dir.join(INDEX_FILE_NAME),
        logs_dir: data_dir.join("logs"),
        cache_dir: cache_dir_for(base),
        exports_dir: base.documents.join(APP_DISPLAY_NAME),
        data_dir,
    };

    // Every root the app will WRITE to has to survive the same chain `open_corpus`
    // applies, because the same SMB/NTLM exposure applies to an export destination on a
    // share as to an index on one.
    reject_nonlocal_default(
        "the default corpus index",
        &locations.index_path,
        &drive_type,
    )?;
    reject_nonlocal_default("the cache folder", &locations.cache_dir, &drive_type)?;
    reject_nonlocal_default(
        "the default export destination",
        &locations.exports_dir,
        &drive_type,
    )?;
    Ok(locations)
}

/// The Windows base directories, with the environment lookup INJECTED.
///
/// `std::env::var` is not called here so that a test can drive every branch: mutating the
/// real environment is process-global, `unsafe` from the 2024 edition on, and would race
/// the other tests in this binary, which cargo runs on parallel threads.
///
/// `%APPDATA%` is never consulted, by design — see [`reject_roaming`]. When
/// `%LOCALAPPDATA%` is missing the fallback is derived from `%USERPROFILE%`, which is the
/// same directory Windows itself would have named.
///
/// LIMITATION, stated inline because it is a real one and not measured here: `Documents`
/// is taken as `%USERPROFILE%\Documents`, which is wrong for a user whose Documents folder
/// has been REDIRECTED (OneDrive backup, or a roamed folder on a domain). The correct
/// answer is `SHGetKnownFolderPath(FOLDERID_Documents)`. UNVERIFIED whether this box's
/// Documents is redirected; the settling experiment is to call that API and compare its
/// answer with `%USERPROFILE%\Documents`. It is left for a follow-up because it needs a
/// `Win32_UI_Shell` + `Win32_System_Com` feature and a `CoTaskMemFree`-owning wrapper, and
/// because the failure mode is a benign one: exports land in a real, writable folder that
/// is simply not the one the shell shows as Documents.
#[cfg(windows)]
fn base_dirs_with(var: impl Fn(&str) -> Option<String>) -> Result<BaseDirs, String> {
    // A variable that is present but blank is NOT a value: used verbatim it would make
    // every derived path relative, which the guard chain then refuses.
    let present = |name: &str| var(name).filter(|v| !v.trim().is_empty());
    let profile = present("USERPROFILE").map(PathBuf::from);
    let local = present("LOCALAPPDATA")
        .map(PathBuf::from)
        .or_else(|| profile.as_ref().map(|p| p.join("AppData").join("Local")))
        .ok_or_else(|| {
            "neither LOCALAPPDATA nor USERPROFILE is set, so there is no per-machine \
             app-data folder to use"
                .to_string()
        })?;
    let documents = profile.map(|p| p.join("Documents")).ok_or_else(|| {
        "USERPROFILE is not set, so the Documents folder cannot be located".to_string()
    })?;
    Ok(BaseDirs {
        // Windows has no separate cache base; the app's own folder holds it.
        cache: local.clone(),
        local_data: local,
        documents,
    })
}

/// The POSIX base directories, per the XDG Base Directory specification — the real
/// counterpart to the Windows branch rather than a platform this feature skips.
///
/// `XDG_DOCUMENTS_DIR` belongs to xdg-user-dirs rather than the base-directory spec and is
/// usually NOT exported into the environment, so `$HOME/Documents` is the normal answer;
/// it is honoured when present because a user who did set it meant it.
#[cfg(not(windows))]
fn base_dirs_with(var: impl Fn(&str) -> Option<String>) -> Result<BaseDirs, String> {
    let present = |name: &str| var(name).filter(|v| !v.trim().is_empty());
    let home = present("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| "HOME is not set, so no XDG base directory can be resolved".to_string())?;
    // The spec: "If an implementation encounters a relative path in any of these variables
    // it should consider the path invalid and ignore it." Ignoring it also keeps a
    // relative value from defeating the absolute-path guard downstream.
    let xdg = |name: &str, fallback: &[&str]| -> PathBuf {
        match present(name).map(PathBuf::from).filter(|p| p.is_absolute()) {
            Some(absolute) => absolute,
            None => fallback.iter().fold(home.clone(), |acc, seg| acc.join(seg)),
        }
    };
    Ok(BaseDirs {
        local_data: xdg("XDG_DATA_HOME", &[".local", "share"]),
        cache: xdg("XDG_CACHE_HOME", &[".cache"]),
        documents: xdg("XDG_DOCUMENTS_DIR", &["Documents"]),
    })
}

/// The default locations for THIS process, resolved from the real environment.
///
/// The only place the environment is read. `drive_type_of` is the same lookup
/// `validate_index_path` uses, so a profile on a mapped network drive is refused here for
/// the same reason and by the same code.
pub fn app_locations() -> Result<AppLocations, String> {
    let base = base_dirs_with(|name| std::env::var(name).ok())?;
    app_locations_from(&base, drive_type_of)
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
    #[cfg(windows)]
    use super::drive_type_of;
    use super::{
        app_info, app_locations_from, base_dirs_with, reject_remote_drive,
        reject_unc_resolution, validate_index_path, validate_index_path_with, BaseDirs,
        APP_DISPLAY_NAME, DRIVE_REMOTE, INDEX_FILE_NAME,
    };
    use std::path::{Path, PathBuf};
    // Used only by the POSIX leg; importing it unconditionally is an unused import
    // on Windows, which `-D warnings` correctly refuses.
    #[cfg(not(windows))]
    use super::reject_unc_spelling;

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
    fn reject_remote_drive_refuses_a_mapped_network_drive_before_touching_it() {
        // DRIVE_REMOTE is what `GetDriveTypeW` answers for a `net use` mapping. The syscall
        // is the caller's, exactly as the resolution is in the sibling below, so the refusing
        // branch is testable without mounting a share — which needs privileges, mutates the
        // machine, and could never run on the CI matrix.
        let err = reject_remote_drive(r"Z:\index.db", Some(DRIVE_REMOTE)).unwrap_err();
        assert!(
            err.contains("mapped network drive"),
            "wrong refusal for a remote drive: {err}"
        );

        // The literal, because every assertion above refers to the constant SYMBOLICALLY and
        // would therefore follow it anywhere. Mutation testing caught exactly that: changing
        // `DRIVE_REMOTE` from 4 to 99 left the whole suite GREEN while a real mapped drive —
        // which Windows reports as 4 — sailed straight through the guard. This is a Win32 API
        // contract value, not a choice, so it is pinned as one.
        assert_eq!(DRIVE_REMOTE, 4, "GetDriveTypeW reports DRIVE_REMOTE as 4");
    }

    #[test]
    fn reject_remote_drive_lets_every_other_answer_through() {
        // Fails OPEN on purpose, and the control matters: a guard that refused anything it
        // could not classify would reject ordinary local paths (DRIVE_FIXED), removable media,
        // and EVERY path on Linux and macOS, where `None` is the only possible answer. This is
        // an early-out for one case; `reject_unc_resolution` still catches what it misses.
        for answer in [None, Some(0u32), Some(2), Some(3), Some(5), Some(6)] {
            assert!(
                reject_remote_drive(r"C:\index.db", answer).is_ok(),
                "a non-remote drive type {answer:?} must not be refused"
            );
        }
    }

    #[test]
    fn the_remote_drive_check_runs_before_the_filesystem_is_touched() {
        // The ordering IS the fix. `reject_remote_drive` passing in isolation would survive
        // the call being deleted, or moved below `is_file()` — and moved below is the subtly
        // wrong version, because by then the multi-minute stat has already happened and the
        // SMB handshake with it.
        //
        // Proven without mounting a share: hand it DRIVE_REMOTE for a path that does NOT
        // exist. If the check runs first the answer is the remote refusal; if it runs after
        // `is_file()`, the answer is "no corpus index at". Only one ordering produces this.
        //
        // PLATFORM-SPLIT, and it is not a formality. `Z:\...` is absolute on Windows and
        // RELATIVE on POSIX, so on Linux the absolute-path guard answers first and this
        // asserted the wrong string entirely. That is what CI's ubuntu leg caught after a
        // fully green Windows run — the exact shape of cross-platform coverage theatre, and
        // the reason the assertion is split rather than the test being cfg'd away: each
        // platform still verifies the contract it actually has.
        let posix_shaped = if cfg!(windows) {
            r"Z:\nonexistent\index.db"
        } else {
            "/nonexistent/index.db"
        };
        let err = validate_index_path_with(posix_shaped, |_| Some(DRIVE_REMOTE)).unwrap_err();
        assert!(
            err.contains("mapped network drive"),
            "the remote-drive refusal must win over not-found, or the check ran too late: {err}"
        );

        // Control: the same non-existent path with a LOCAL drive type falls through to the
        // ordinary not-found message, so the test above is measuring the drive type rather
        // than just the path being absent.
        let local = validate_index_path_with(posix_shaped, |_| Some(3)).unwrap_err();
        assert!(
            local.contains("no corpus index at"),
            "a local drive type must not be refused as remote: {local}"
        );
    }

    #[test]
    #[cfg(windows)]
    fn drive_type_of_asks_only_about_drive_letter_paths() {
        // The adapter's own contract, which is where a wrong answer would be silent: anything
        // without a drive letter has no drive type, so it must return None rather than
        // stumble into a syscall with a malformed root.
        assert_eq!(drive_type_of("relative\\index.db"), None);
        assert_eq!(drive_type_of(r"\\host\share\index.db"), None);
        assert_eq!(drive_type_of("C"), None);
        assert_eq!(
            drive_type_of("4:\\index.db"),
            None,
            "a digit is not a drive letter"
        );
        // The system drive exists and is local, so this both proves the syscall is reached
        // and pins that it does not answer DRIVE_REMOTE for it.
        assert!(matches!(drive_type_of(r"C:\Windows"), Some(t) if t != DRIVE_REMOTE));
    }

    #[test]
    #[cfg(windows)]
    fn reject_unc_resolution_refuses_a_path_that_resolves_off_the_local_disk() {
        // WINDOWS-ONLY, because the rule itself is. `Component::Prefix` exists only on
        // Windows: on POSIX a `\\?\UNC\...` string parses as one ordinary relative filename
        // with no prefix, so the guard correctly answers Ok and `expect_err` panics. The
        // function's own docstring already says it is INERT off Windows — the test simply
        // did not say so too, and CI's ubuntu leg found that after a fully green Windows
        // run. The POSIX contract is asserted separately below, so nothing is lost by
        // scoping this one.
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

    #[test]
    #[cfg(not(windows))]
    fn on_posix_the_resolution_guard_is_inert_and_the_spelling_guard_carries_it() {
        // The POSIX half of the pair above, so the ubuntu CI leg still verifies this
        // function instead of merely skipping it. Asserting the inertness is the point: a
        // reader who sees only a `#[cfg(windows)]` test cannot tell whether POSIX is
        // covered by something else or simply forgotten.
        //
        // `Component::Prefix` does not exist off Windows, so a canonicalized `\\?\UNC\...`
        // string parses as ONE ordinary relative filename and the guard answers Ok. That is
        // correct and documented — a cifs mount canonicalizes to `/mnt/share/x`, which is
        // genuinely indistinguishable from a local path without reading the mount table.
        let resolving_to = |c: &str| Ok(std::path::PathBuf::from(c));
        assert_eq!(
            reject_unc_resolution("/mnt/share/index.db", resolving_to(r"\\?\UNC\host\share\i.db")),
            Ok(()),
            "off Windows this guard cannot speak, and must not pretend to"
        );

        // So the raw-SPELLING guard is what actually protects POSIX, and it still does —
        // which is exactly why `reject_unc_spelling` runs on the unresolved string rather
        // than after canonicalization, where the evidence would already be destroyed.
        for spelling in [r"\\host\share\index.db", "//host/share/index.db"] {
            assert!(
                reject_unc_spelling(spelling).is_err(),
                "the spelling guard is POSIX's only cover and must refuse {spelling}"
            );
        }
        // Fail-closed still holds on both platforms.
        assert!(reject_unc_resolution(
            "/x/index.db",
            Err(std::io::Error::from(std::io::ErrorKind::PermissionDenied))
        )
        .is_err());
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

    // -- DECISION G-10: standard OS app-data locations --------------------------------

    /// A synthetic profile, per platform. The bases are HANDED IN exactly as
    /// `validate_index_path_with` has the drive-type lookup handed in: reading the real
    /// environment inside a unit test would make the assertions depend on whose machine
    /// ran them, and on CI the answer differs between the windows and ubuntu legs.
    ///
    /// The two shapes are not interchangeable and that is the point — `Path::is_absolute`
    /// disagrees about both of them, so a single hardcoded spelling would test the guard
    /// on one leg and something else entirely on the other. Same lesson as
    /// `the_remote_drive_check_runs_before_the_filesystem_is_touched`.
    fn synthetic_base() -> BaseDirs {
        if cfg!(windows) {
            BaseDirs {
                local_data: PathBuf::from(r"C:\Users\tester\AppData\Local"),
                // Windows has no separate cache root; %LOCALAPPDATA% is it.
                cache: PathBuf::from(r"C:\Users\tester\AppData\Local"),
                documents: PathBuf::from(r"C:\Users\tester\Documents"),
            }
        } else {
            BaseDirs {
                local_data: PathBuf::from("/home/tester/.local/share"),
                cache: PathBuf::from("/home/tester/.cache"),
                documents: PathBuf::from("/home/tester/Documents"),
            }
        }
    }

    /// The synthetic profile root, i.e. the WRONG place for a 55-75 MB index.
    fn synthetic_home() -> PathBuf {
        PathBuf::from(if cfg!(windows) {
            r"C:\Users\tester"
        } else {
            "/home/tester"
        })
    }

    /// A local drive type, so the remote-drive guard inside the resolver falls through.
    /// `Some(3)` is DRIVE_FIXED; the constant is not re-exported, and `reject_remote_drive`
    /// already has its own both-states coverage above.
    fn local_drive(_: &str) -> Option<u32> {
        Some(3)
    }

    /// THE LAYOUT the owner locked: machine data under the per-machine app-data root,
    /// exports under Documents, and — the actual regression — the index NOT at the
    /// profile root.
    #[test]
    fn the_default_index_lives_under_the_app_data_root_and_never_at_the_profile_root() {
        let base = synthetic_base();
        let loc = app_locations_from(&base, local_drive).expect("a normal profile must resolve");

        // The bug this unit exists to fix. The shipped example index sat at
        // `C:/Users/<user>/anthology.sqlite` — the profile ROOT, which no installed app
        // should write to. Asserted as a literal comparison against that exact shape so
        // the test names the defect rather than merely describing a preference.
        assert_ne!(
            loc.index_path,
            synthetic_home().join(INDEX_FILE_NAME),
            "the default index must not sit at the profile root"
        );

        // Every machine-owned path is inside ONE folder, which is what makes the set
        // enumerable for the AppContainer grant a later unit must perform.
        assert!(
            loc.index_path.starts_with(&loc.data_dir),
            "index {:?} must live inside the data dir {:?}",
            loc.index_path,
            loc.data_dir
        );
        assert!(loc.logs_dir.starts_with(&loc.data_dir), "logs: {:?}", loc.logs_dir);
        assert_eq!(loc.index_path.file_name().and_then(|n| n.to_str()), Some(INDEX_FILE_NAME));

        // The data dir is derived from the LOCAL app-data base, not from Documents and
        // not from the profile root.
        assert!(
            loc.data_dir.starts_with(&base.local_data),
            "data dir {:?} must derive from the local app-data base {:?}",
            loc.data_dir,
            base.local_data
        );

        // Exports go to a folder the human browses, so they use the DISPLAY name on both
        // platforms — unlike the machine folder, whose leaf follows platform convention
        // (`LLM Anthology` in %LOCALAPPDATA%, `llm-anthology` under ~/.local/share).
        assert_eq!(loc.exports_dir, base.documents.join(APP_DISPLAY_NAME));
        assert_ne!(
            loc.exports_dir, base.documents,
            "exports must go to a named subfolder, not scatter into Documents itself"
        );

        // Per-platform spellings, pinned. Written out in full because a relative
        // assertion ("contains the app name") would pass for a wrong parent.
        if cfg!(windows) {
            assert_eq!(
                loc.data_dir,
                PathBuf::from(r"C:\Users\tester\AppData\Local\LLM Anthology")
            );
            assert_eq!(loc.cache_dir, loc.data_dir.join("cache"));
        } else {
            assert_eq!(loc.data_dir, PathBuf::from("/home/tester/.local/share/llm-anthology"));
            // XDG keeps the cache under its OWN root so a backup tool can skip it.
            assert_eq!(loc.cache_dir, PathBuf::from("/home/tester/.cache/llm-anthology"));
            assert!(
                !loc.cache_dir.starts_with(&loc.data_dir),
                "on POSIX the cache must NOT be inside the data dir: {:?}",
                loc.cache_dir
            );
        }
    }

    /// ROAMING IS THE REFUSAL, and it is the whole reason G-10 names `%LOCALAPPDATA%`
    /// explicitly. `%APPDATA%` is `...\AppData\Roaming`, which a domain-joined machine
    /// synchronises to a server at logon — so a 55-75 MB corpus index placed there is
    /// copied over the network on every login, which is hostile rather than merely untidy.
    ///
    /// BOTH STATES: the same resolver must accept the Local base and refuse the Roaming
    /// one. Asserting only the refusal would be satisfied by a resolver that refuses
    /// everything.
    #[test]
    fn a_roaming_base_is_refused_and_a_local_one_is_accepted() {
        let local = synthetic_base();
        assert!(
            app_locations_from(&local, local_drive).is_ok(),
            "the Local base is the correct one and must be accepted"
        );

        // Case-insensitively, because Windows paths are, and `%APPDATA%` is spelled by
        // whatever set it.
        for spelling in ["Roaming", "roaming", "ROAMING"] {
            let roaming = BaseDirs {
                local_data: synthetic_home().join("AppData").join(spelling),
                cache: local.cache.clone(),
                documents: local.documents.clone(),
            };
            let err = app_locations_from(&roaming, local_drive)
                .expect_err("a roaming base must be refused");
            assert!(
                err.contains("roaming"),
                "expected the roaming refusal for {spelling}, got: {err}"
            );
        }

        // The CACHE base too: a synced cache is the same defect with a different name.
        let roaming_cache = BaseDirs {
            local_data: local.local_data.clone(),
            cache: synthetic_home().join("AppData").join("Roaming"),
            documents: local.documents.clone(),
        };
        assert!(
            app_locations_from(&roaming_cache, local_drive).is_err(),
            "a roaming CACHE base must be refused too"
        );
    }

    /// THE DEFAULT MUST SURVIVE `open_corpus`'s GUARD CHAIN. That chain
    /// (`reject_unc_spelling` -> `is_absolute` -> `reject_remote_drive` -> `is_file` ->
    /// `reject_unc_resolution`) is a real SMB/NTLM defence, and a default path that it
    /// refuses would produce an app whose out-of-the-box index cannot be opened.
    ///
    /// The assertion is on WHICH refusal comes back. The default index does not exist on
    /// a fresh machine, so `validate_index_path` MUST answer "no corpus index at" — the
    /// message reachable only from the final filesystem probe. Any other refusal means a
    /// lexical guard fired, i.e. the default is UNC-shaped, relative, or on a remote
    /// drive. A bare `is_err()` here would pass for all four and prove nothing.
    #[test]
    fn the_default_index_path_passes_every_pre_filesystem_guard_in_the_open_corpus_chain() {
        let loc = app_locations_from(&synthetic_base(), local_drive).expect("resolve");
        let index = loc.index_path.to_str().expect("the default index path is UTF-8");

        let err = validate_index_path_with(index, local_drive)
            .expect_err("the default index does not exist yet, so open must still refuse it");
        assert!(
            err.contains("no corpus index at"),
            "the ONLY refusal a fresh default may attract is not-found — anything else \
             means a lexical guard rejected the default itself. Got: {err}"
        );
        // Named individually so a failure says which guard fired.
        assert!(!err.contains("is a network path"), "default is UNC-shaped: {err}");
        assert!(!err.contains("is not a full path"), "default is not absolute: {err}");
        assert!(!err.contains("mapped network drive"), "default is on a remote drive: {err}");

        // And the resolver REFUSES to hand back a default that the chain would reject,
        // rather than leaving that to be discovered at open time.
        //
        // EVERY base, not just the first. Only three paths are checked explicitly
        // (`index_path`, `cache_dir`, `exports_dir`) because the other two live inside
        // `data_dir` and so share its prefix — but that means a forgotten check would be
        // INVISIBLE unless each base is poisoned in turn. So each one is, and the poison
        // is a UNC spelling because that is the guard whose absence carries the SMB/NTLM
        // exposure. `//server/...` is used for the POSIX leg's benefit: `\\server\...`
        // contains no separator at all there and would read as one relative filename,
        // which the absolute-path guard would then catch for the WRONG reason.
        let unc_root = if cfg!(windows) {
            PathBuf::from(r"\\server\profiles\tester")
        } else {
            PathBuf::from("//server/profiles/tester")
        };
        for (which, poisoned) in [
            (
                "app-data",
                BaseDirs {
                    local_data: unc_root.clone(),
                    cache: synthetic_base().cache,
                    documents: synthetic_base().documents,
                },
            ),
            (
                "cache",
                BaseDirs {
                    local_data: synthetic_base().local_data,
                    cache: unc_root.clone(),
                    documents: synthetic_base().documents,
                },
            ),
            (
                "documents",
                BaseDirs {
                    local_data: synthetic_base().local_data,
                    cache: synthetic_base().cache,
                    documents: unc_root.clone(),
                },
            ),
        ] {
            let err = app_locations_from(&poisoned, local_drive)
                .expect_err(&format!("a UNC {which} base must be refused"));
            assert!(
                err.contains("is a network path"),
                "the UNC {which} base must be refused by the raw-SPELLING guard, not by \
                 something downstream of a filesystem touch. Got: {err}"
            );
        }

        let relative = BaseDirs {
            local_data: PathBuf::from("AppData/Local"),
            cache: synthetic_base().cache,
            documents: synthetic_base().documents,
        };
        assert!(
            app_locations_from(&relative, local_drive).is_err(),
            "a relative app-data base must be refused"
        );

        // A mapped network drive, via the SAME injected seam `validate_index_path_with`
        // uses. This is the ordering-sensitive one: the resolver must ASK before it
        // hands the path out.
        let err = app_locations_from(&synthetic_base(), |_| Some(DRIVE_REMOTE))
            .expect_err("an app-data base on a mapped network drive must be refused");
        assert!(
            err.contains("mapped network drive"),
            "expected the remote-drive refusal, got: {err}"
        );
    }

    /// THE INDEX PATH STAYS USER-OVERRIDABLE — a growing archive belongs on whatever
    /// drive the user wants, so resolving a default must not turn into a jail. Nothing
    /// in `open_corpus` may require the index to be under `data_dir`.
    #[test]
    fn a_user_chosen_index_outside_the_app_data_root_is_still_accepted() {
        let loc = app_locations_from(&synthetic_base(), local_drive).expect("resolve");
        // A real file that is definitely NOT under the app-data root: this source file.
        let elsewhere = concat!(env!("CARGO_MANIFEST_DIR"), "/src/lib.rs");
        assert!(
            !Path::new(elsewhere).starts_with(&loc.data_dir),
            "precondition: the override must be outside the app-data root"
        );
        assert_eq!(
            validate_index_path(elsewhere),
            Ok(()),
            "a user-chosen index outside the default folder must still open"
        );
    }

    /// THE GRANT SET, which is why this unit blocks the membrane one.
    /// `Membrane::AppContainer` is implemented and tested in `sidecar.rs` but inactive,
    /// because a sandboxed engine can only write where the package SID has been granted
    /// access — so activating it needs a small, fixed, enumerable set of roots. This is
    /// that set, and it must be MINIMAL: granting a directory already covers everything
    /// beneath it, and each redundant `icacls` grant is a real cost (the sibling
    /// membrane test grants ~8k files rather than ~130k for exactly this reason).
    #[test]
    fn grant_roots_are_minimal_and_still_cover_every_default_path() {
        let loc = app_locations_from(&synthetic_base(), local_drive).expect("resolve");
        let roots = loc.grant_roots();

        // COVERAGE: every default path must be under some granted root, or a sandboxed
        // engine would fail to write it.
        for path in [&loc.data_dir, &loc.index_path, &loc.logs_dir, &loc.cache_dir, &loc.exports_dir] {
            assert!(
                roots.iter().any(|root| path.starts_with(root)),
                "{path:?} is not covered by any grant root: {roots:?}"
            );
        }

        // MINIMALITY: no root may be inside another.
        for (i, a) in roots.iter().enumerate() {
            for (j, b) in roots.iter().enumerate() {
                assert!(
                    i == j || !a.starts_with(b),
                    "grant root {a:?} is redundant — it is already covered by {b:?}"
                );
            }
        }

        // The COUNT is platform-specific and pinned, because "minimal" is only meaningful
        // against a known layout. Windows folds logs+cache inside the one %LOCALAPPDATA%
        // folder, so two roots suffice; XDG puts the cache under its own root, so three.
        assert_eq!(roots.len(), if cfg!(windows) { 2 } else { 3 }, "roots: {roots:?}");
        assert!(roots.contains(&loc.data_dir.as_path()));
        assert!(roots.contains(&loc.exports_dir.as_path()));
    }

    /// The ENVIRONMENT half, on Windows. Injected `var` lookup rather than
    /// `std::env::set_var` — that is process-global, `unsafe` as of the 2024 edition, and
    /// would race the other tests in this binary, which cargo runs on parallel threads.
    #[test]
    #[cfg(windows)]
    fn windows_base_dirs_prefer_localappdata_and_never_fall_back_to_roaming() {
        let env = |pairs: &'static [(&'static str, &'static str)]| {
            move |name: &str| {
                pairs
                    .iter()
                    .find(|(k, _)| *k == name)
                    .map(|(_, v)| (*v).to_string())
            }
        };

        // 1. The normal case: %LOCALAPPDATA% is set and is used verbatim.
        let base = base_dirs_with(env(&[
            ("LOCALAPPDATA", r"C:\Users\tester\AppData\Local"),
            ("APPDATA", r"C:\Users\tester\AppData\Roaming"),
            ("USERPROFILE", r"C:\Users\tester"),
        ]))
        .expect("a normal Windows environment must resolve");
        assert_eq!(base.local_data, PathBuf::from(r"C:\Users\tester\AppData\Local"));
        assert_eq!(base.documents, PathBuf::from(r"C:\Users\tester\Documents"));

        // 2. %LOCALAPPDATA% ABSENT — the case that produces the defect. %APPDATA% is
        //    present and points at Roaming, and it must be ignored: the fallback is
        //    derived from the profile, not from the roaming variable.
        let base = base_dirs_with(env(&[
            ("APPDATA", r"C:\Users\tester\AppData\Roaming"),
            ("USERPROFILE", r"C:\Users\tester"),
        ]))
        .expect("USERPROFILE alone must be enough");
        assert_eq!(base.local_data, PathBuf::from(r"C:\Users\tester\AppData\Local"));
        assert!(
            !base.local_data.to_string_lossy().to_lowercase().contains("roaming"),
            "APPDATA (Roaming) must never become the app-data base: {:?}",
            base.local_data
        );

        // 3. An EMPTY variable is not a value. A blank %LOCALAPPDATA% used verbatim would
        //    make every default path relative, which the guard chain then refuses.
        let base = base_dirs_with(env(&[
            ("LOCALAPPDATA", "   "),
            ("USERPROFILE", r"C:\Users\tester"),
        ]))
        .expect("a blank LOCALAPPDATA must fall back, not be used");
        assert_eq!(base.local_data, PathBuf::from(r"C:\Users\tester\AppData\Local"));

        // 4. Nothing to go on: fail LOUDLY rather than invent a relative default.
        let err = base_dirs_with(env(&[("APPDATA", r"C:\Users\tester\AppData\Roaming")]))
            .expect_err("with no LOCALAPPDATA and no USERPROFILE there is no honest answer");
        assert!(err.contains("LOCALAPPDATA"), "the error must name what is missing: {err}");
    }

    /// The ENVIRONMENT half, on POSIX — the real counterpart, not a skipped test.
    /// `%LOCALAPPDATA%` has no meaning here; the equivalent contract is the XDG Base
    /// Directory specification, so that is what is asserted.
    #[test]
    #[cfg(not(windows))]
    fn posix_base_dirs_follow_the_xdg_base_directory_spec() {
        let env = |pairs: &'static [(&'static str, &'static str)]| {
            move |name: &str| {
                pairs
                    .iter()
                    .find(|(k, _)| *k == name)
                    .map(|(_, v)| (*v).to_string())
            }
        };

        // 1. XDG variables set: honoured verbatim.
        let base = base_dirs_with(env(&[
            ("HOME", "/home/tester"),
            ("XDG_DATA_HOME", "/data/tester"),
            ("XDG_CACHE_HOME", "/scratch/tester"),
        ]))
        .expect("a normal POSIX environment must resolve");
        assert_eq!(base.local_data, PathBuf::from("/data/tester"));
        assert_eq!(base.cache, PathBuf::from("/scratch/tester"));
        assert_eq!(base.documents, PathBuf::from("/home/tester/Documents"));

        // 2. Unset: the spec's defaults, which are NOT the home root.
        let base = base_dirs_with(env(&[("HOME", "/home/tester")])).expect("HOME alone resolves");
        assert_eq!(base.local_data, PathBuf::from("/home/tester/.local/share"));
        assert_eq!(base.cache, PathBuf::from("/home/tester/.cache"));

        // 3. "If an implementation encounters a relative path in any of these variables
        //    it should consider the path invalid and ignore it." Honoured, because a
        //    relative base would otherwise defeat the absolute-path guard downstream.
        let base = base_dirs_with(env(&[
            ("HOME", "/home/tester"),
            ("XDG_DATA_HOME", "relative/share"),
            ("XDG_CACHE_HOME", ""),
        ]))
        .expect("a relative XDG value must fall back, not be used");
        assert_eq!(base.local_data, PathBuf::from("/home/tester/.local/share"));
        assert_eq!(base.cache, PathBuf::from("/home/tester/.cache"));

        // 4. No HOME: fail loudly rather than resolve to a relative default.
        let err = base_dirs_with(env(&[("XDG_DATA_HOME", "/data/tester")]))
            .expect_err("without HOME there is no honest answer for Documents");
        assert!(err.contains("HOME"), "the error must name what is missing: {err}");
    }

    /// The resolution is REACHABLE, and reachable against the REAL environment — which
    /// is the half a fully-injected test can never prove. `app_info` is the app's static
    /// metadata command and is already registered, so wiring the locations there gives
    /// the resolver a production caller without adding a command whose TypeScript
    /// binding lives outside this unit's file scope.
    #[test]
    fn app_info_reports_the_default_locations_resolved_from_the_real_environment() {
        let info = app_info();
        // Both keys ALWAYS present, exactly one of them null, so a consumer can branch on
        // shape rather than on absence.
        assert!(
            info.get("locations").is_some() && info.get("locations_error").is_some(),
            "app_info must always carry both keys: {info}"
        );

        match info["locations"].as_object() {
            Some(loc) => {
                assert!(info["locations_error"].is_null(), "{info}");
                for key in ["data_dir", "index_path", "logs_dir", "cache_dir", "exports_dir"] {
                    let value = loc.get(key).and_then(|v| v.as_str()).unwrap_or_default();
                    assert!(!value.is_empty(), "{key} must be a non-empty string: {info}");
                    assert!(
                        Path::new(value).is_absolute(),
                        "{key} must be absolute on this platform, got {value:?}"
                    );
                }
                assert!(
                    loc.get("grant_roots").and_then(|v| v.as_array()).is_some_and(|a| !a.is_empty()),
                    "grant_roots must be a non-empty array so a later unit can enumerate it: {info}"
                );
            }
            // Resolution CAN fail (a stripped environment with no HOME/USERPROFILE), and
            // when it does the status line must still work — degraded, and saying so.
            None => {
                assert!(info["locations"].is_null(), "{info}");
                assert!(
                    info["locations_error"].as_str().is_some_and(|e| !e.is_empty()),
                    "a failed resolution must carry a reason: {info}"
                );
            }
        }
    }
}
