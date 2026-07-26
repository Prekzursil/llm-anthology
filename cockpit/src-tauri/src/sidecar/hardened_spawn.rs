//! Windows-only hardened process spawn for the engine sidecar.
//!
//! This replaces the plain `std::process::Command` spawn with ONE raw
//! `CreateProcessW` call (via `windows-sys`) that does TRIPLE duty:
//!
//! 1. **REAP** — the child is created under a Windows **Job Object** whose
//!    `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` flag guarantees the whole sidecar
//!    subtree dies the moment the *last handle to the job closes*. Because the
//!    only handle to the job lives in this process, an ABRUPT death of the app
//!    (force-kill / `tauri dev` SIGKILL / updater exit) — which never runs a
//!    Rust `Drop` — still closes the job handle via the OS handle-table
//!    teardown, so no orphaned engine can outlive the app. The plain
//!    `Drop::kill` it replaces MISSES exactly that abrupt-death case.
//!
//! 2. **MEMBRANE** — when [`Membrane::AppContainer`] is requested the child is
//!    launched into a REGULAR AppContainer (NOT LPAC — LPAC locks CPython out of
//!    the registry/COM it needs) WITHOUT the `internetClient` capability, via
//!    `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`. The Windows Filtering
//!    Platform then kernel-blocks all outbound network at the built-in
//!    AppContainer default-block rule — NO admin firewall rule needed. The
//!    corpus index + interpreter directories are `icacls`-granted to the package
//!    SID so the sandboxed engine can still READ them, and the channel stays on
//!    STDIO pipes (an AppContainer's loopback filter would kill a localhost TCP
//!    socket; inherited stdio pipe handles are unaffected).
//!
//! 3. **CREATE_NO_WINDOW** — no console window flashes for the child.
//!
//! All of the `unsafe` FFI lives here; [`super::SidecarClient`] consumes the
//! safe [`HardenedSpawn`] it returns (owned `File` pipe ends + a `Reaper` that
//! holds the job handle). Non-Windows builds never compile this module.

use core::ffi::c_void;
use std::ffi::OsStr;
use std::fs::File;
use std::mem::{size_of, zeroed};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::{AsRawHandle, FromRawHandle, IntoRawHandle, OwnedHandle, RawHandle};
use std::path::{Path, PathBuf};
use std::ptr::{null, null_mut};

use windows_sys::Win32::Foundation::{GetLastError, LocalFree, SetHandleInformation};
use windows_sys::Win32::Security::Authorization::{
    ConvertSidToStringSidW, ConvertStringSecurityDescriptorToSecurityDescriptorW,
};
use windows_sys::Win32::Security::Isolation::{
    CreateAppContainerProfile, DeriveAppContainerSidFromAppContainerName,
};
use windows_sys::Win32::Security::{FreeSid, SECURITY_ATTRIBUTES, SECURITY_CAPABILITIES};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
};
use windows_sys::Win32::System::Pipes::CreatePipe;
use windows_sys::Win32::System::Threading::{
    CreateProcessW, DeleteProcThreadAttributeList, InitializeProcThreadAttributeList,
    ResumeThread, TerminateProcess, UpdateProcThreadAttribute, WaitForSingleObject,
    LPPROC_THREAD_ATTRIBUTE_LIST, PROCESS_INFORMATION, STARTUPINFOEXW,
};

// --- flags/consts defined locally so this module never depends on a specific
// windows-sys constant path (only the FUNCTIONS and STRUCTS are imported). ---
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const CREATE_SUSPENDED: u32 = 0x0000_0004;
const EXTENDED_STARTUPINFO_PRESENT: u32 = 0x0008_0000;
const STARTF_USESTDHANDLES: u32 = 0x0000_0100;
const HANDLE_FLAG_INHERIT: u32 = 0x0000_0001;
const PROC_THREAD_ATTRIBUTE_HANDLE_LIST: usize = 0x0002_0002;
const PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES: usize = 0x0002_0009;
const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x0000_2000;
const SDDL_REVISION_1: u32 = 1;
const TRUE: i32 = 1;
#[cfg(test)]
const WAIT_OBJECT_0: u32 = 0x0000_0000;

/// A DACL granting Everyone + ALL APPLICATION PACKAGES full access, applied to
/// the stdio pipes so an AppContainer (LowBox) child can use its inherited ends.
const PIPE_APPCONTAINER_SDDL: &str = "D:(A;;GA;;;WD)(A;;GA;;;AC)";

/// Which isolation membrane to wrap the child in.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
// `AppContainer` is currently constructed only by the membrane test and by an
// (opt-in) app caller of `spawn_membrane`, so a non-test lib build sees it as
// "never constructed" — the mode is real and tested, hence the allow.
#[allow(dead_code)]
pub(crate) enum Membrane {
    /// Job-object reap + CREATE_NO_WINDOW only. No network sandbox. This is the
    /// reliable core used by the default [`super::SidecarClient::spawn`].
    JobOnly,
    /// Regular AppContainer created with ZERO capabilities — in particular
    /// WITHOUT `internetClient` (SID S-1-15-3-1) — so the Windows Filtering
    /// Platform default rule kernel-blocks all outbound network. No admin
    /// firewall rule needed. (NOT LPAC: LPAC would lock CPython out of the
    /// registry/COM it needs; a regular AppContainer + FS grants lets it run.)
    AppContainer,
}

impl Membrane {
    fn is_appcontainer(self) -> bool {
        matches!(self, Membrane::AppContainer)
    }
    /// The stable AppContainer profile name (fixed, so repeated runs reuse ONE
    /// profile/SID instead of accumulating registry entries).
    fn profile_name(self) -> Option<&'static str> {
        match self {
            Membrane::JobOnly => None,
            Membrane::AppContainer => Some("llm-anthology-cockpit-sidecar"),
        }
    }
}

/// Options for a hardened spawn. Granting the AppContainer's package SID read
/// access to the interpreter/corpus dirs is a SEPARATE, one-time provisioning
/// step ([`ensure_container_sid`] + [`grant_read`]) rather than a per-spawn
/// concern, so this struct carries no grant list.
pub(crate) struct SpawnOpts {
    pub membrane: Membrane,
    /// Whether the job carries KILL_ON_JOB_CLOSE. Always `true` in production;
    /// a test flips it `false` to prove the flag is what reaps the child.
    pub kill_on_job_close: bool,
}

impl SpawnOpts {
    pub fn job_only() -> Self {
        SpawnOpts { membrane: Membrane::JobOnly, kill_on_job_close: true }
    }
}

/// A spawned, hardened child: owned parent-side pipe ends plus a [`Reaper`] that
/// keeps the job alive (and reaps on drop). `HardenedSpawn` itself has NO `Drop`,
/// so a caller may move the fields out and keep only the `reaper`.
pub(crate) struct HardenedSpawn {
    pub stdin: File,
    pub stdout: File,
    /// The child's stderr (parent read end). A caller SHOULD hold it so the
    /// child's stderr pipe never fills; tests read it for failure diagnostics.
    pub stderr: File,
    pub reaper: Reaper,
}

/// Owns the child process + job handles. Dropping it terminates the child (best
/// effort) and closes the job handle — the latter is what enforces
/// KILL_ON_JOB_CLOSE for any straggler in the subtree.
pub(crate) struct Reaper {
    process: OwnedHandle,
    /// `Some` in normal operation; a test takes it to simulate the app's job
    /// handle closing (the exact kernel event an abrupt app death triggers).
    /// Held for its Drop side-effect — closing it enforces KILL_ON_JOB_CLOSE.
    #[allow(dead_code)]
    job: Option<OwnedHandle>,
    #[allow(dead_code)]
    pid: u32,
}

impl Drop for Reaper {
    fn drop(&mut self) {
        // Best-effort immediate reap; the job close below is the hard guarantee.
        unsafe {
            TerminateProcess(self.process.as_raw_handle() as _, 1);
            WaitForSingleObject(self.process.as_raw_handle() as _, 2000);
        }
        // OwnedHandle drops (process, then job) close the handles; closing the
        // last job handle enforces KILL_ON_JOB_CLOSE on anything still alive.
    }
}

impl Reaper {
    #[cfg(test)]
    pub(crate) fn pid(&self) -> u32 {
        self.pid
    }
    /// True iff the child has exited within `timeout_ms`.
    #[cfg(test)]
    pub(crate) fn wait_exit(&self, timeout_ms: u32) -> bool {
        unsafe { WaitForSingleObject(self.process.as_raw_handle() as _, timeout_ms) == WAIT_OBJECT_0 }
    }
    /// Close the job handle NOW (drops it). This reproduces exactly what the OS
    /// does to the app's job handle when the app process dies abruptly.
    #[cfg(test)]
    pub(crate) fn close_job(&mut self) {
        self.job = None;
    }
}

// --------------------------------------------------------------- wide strings

fn to_wide(s: &OsStr) -> Vec<u16> {
    s.encode_wide().chain(std::iter::once(0)).collect()
}

fn wide(s: &str) -> Vec<u16> {
    to_wide(OsStr::new(s))
}

/// MSVC/`CommandLineToArgvW`-compatible argument quoting.
fn append_quoted(arg: &str, out: &mut String) {
    if !arg.is_empty() && !arg.contains([' ', '\t', '"', '\\']) {
        out.push_str(arg);
        return;
    }
    out.push('"');
    let mut backslashes = 0usize;
    for c in arg.chars() {
        match c {
            '\\' => backslashes += 1,
            '"' => {
                for _ in 0..(backslashes * 2 + 1) {
                    out.push('\\');
                }
                out.push('"');
                backslashes = 0;
            }
            _ => {
                for _ in 0..backslashes {
                    out.push('\\');
                }
                backslashes = 0;
                out.push(c);
            }
        }
    }
    for _ in 0..(backslashes * 2) {
        out.push('\\');
    }
    out.push('"');
}

/// Resolve `program` to a full path (searching PATH with a `.exe` fallback) so
/// `CreateProcessW` gets an explicit application name AND we know which
/// interpreter directory to grant the AppContainer.
fn resolve_program(program: &str) -> Result<PathBuf, String> {
    let p = Path::new(program);
    if (p.is_absolute() || program.contains(['\\', '/'])) && p.is_file() {
        return Ok(p.to_path_buf());
    }
    if let Some(paths) = std::env::var_os("PATH") {
        for dir in std::env::split_paths(&paths) {
            let exe = dir.join(format!("{program}.exe"));
            if exe.is_file() {
                return Ok(exe);
            }
            let bare = dir.join(program);
            if bare.is_file() {
                return Ok(bare);
            }
        }
    }
    Err(format!("could not resolve program on PATH: {program}"))
}

fn last_error(ctx: &str) -> String {
    format!("{ctx}: {}", std::io::Error::last_os_error())
}

// --------------------------------------------------------------- pipes

/// Create an anonymous pipe. When `sd` is set (AppContainer), both ends carry a
/// DACL usable by a LowBox child. Returns `(read, write)`, both inheritable.
unsafe fn create_pipe(sd: *mut c_void) -> Result<(OwnedHandle, OwnedHandle), String> {
    let mut sa: SECURITY_ATTRIBUTES = zeroed();
    sa.nLength = size_of::<SECURITY_ATTRIBUTES>() as u32;
    sa.bInheritHandle = TRUE;
    sa.lpSecurityDescriptor = sd;
    let mut read: windows_sys::Win32::Foundation::HANDLE = null_mut();
    let mut write: windows_sys::Win32::Foundation::HANDLE = null_mut();
    if CreatePipe(&mut read, &mut write, &sa, 0) == 0 {
        return Err(last_error("CreatePipe"));
    }
    Ok((
        OwnedHandle::from_raw_handle(read as RawHandle),
        OwnedHandle::from_raw_handle(write as RawHandle),
    ))
}

/// Clear HANDLE_FLAG_INHERIT on a parent-retained handle (defence-in-depth; the
/// PROC_THREAD_ATTRIBUTE_HANDLE_LIST already restricts inheritance to the child
/// ends, but the job handle etc. must never leak into the child).
unsafe fn clear_inherit(h: &OwnedHandle) {
    SetHandleInformation(h.as_raw_handle() as _, HANDLE_FLAG_INHERIT, 0);
}

// --------------------------------------------------------------- job object

unsafe fn create_job(kill_on_close: bool) -> Result<OwnedHandle, String> {
    let job = CreateJobObjectW(null(), null());
    if job.is_null() {
        return Err(last_error("CreateJobObjectW"));
    }
    let job = OwnedHandle::from_raw_handle(job as RawHandle);
    if kill_on_close {
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = zeroed();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            job.as_raw_handle() as _,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const c_void,
            size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        ) == 0
        {
            return Err(last_error("SetInformationJobObject(KILL_ON_JOB_CLOSE)"));
        }
    }
    Ok(job)
}

// --------------------------------------------------------------- AppContainer

/// A resolved AppContainer profile: the package SID (must be freed) + its string
/// form (for `icacls`). Frees the SID on drop.
struct AppContainer {
    sid: *mut c_void,
    sid_string: String,
}

impl Drop for AppContainer {
    fn drop(&mut self) {
        unsafe {
            FreeSid(self.sid);
        }
    }
}

/// Ensure the fixed-name AppContainer profile exists and return its package SID.
/// `CreateAppContainerProfile` creates-or-returns `ERROR_ALREADY_EXISTS`; on the
/// already-exists path we derive the SID by name.
unsafe fn ensure_appcontainer(name: &str) -> Result<AppContainer, String> {
    let wname = wide(name);
    let wdisp = wide("LLM Anthology Cockpit engine sidecar");
    let wdesc = wide("Sandboxed AI-session analysis engine (no internetClient)");
    let mut sid: *mut c_void = null_mut();
    let hr = CreateAppContainerProfile(
        wname.as_ptr(),
        wdisp.as_ptr(),
        wdesc.as_ptr(),
        null(),
        0,
        &mut sid,
    );
    // HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS) == 0x800700B7
    if hr != 0 {
        if hr == 0x8007_00B7u32 as i32 {
            if DeriveAppContainerSidFromAppContainerName(wname.as_ptr(), &mut sid) != 0 {
                return Err(format!(
                    "DeriveAppContainerSidFromAppContainerName: HRESULT 0x{:08X}",
                    GetLastError()
                ));
            }
        } else {
            return Err(format!("CreateAppContainerProfile: HRESULT 0x{hr:08X}"));
        }
    }
    if sid.is_null() {
        return Err("AppContainer SID resolved to null".to_string());
    }
    let sid_string = sid_to_string(sid)?;
    Ok(AppContainer { sid, sid_string })
}

/// Convert a PSID to its `S-1-...` string form.
unsafe fn sid_to_string(sid: *mut c_void) -> Result<String, String> {
    let mut out: *mut u16 = null_mut();
    if ConvertSidToStringSidW(sid, &mut out) == 0 {
        return Err(last_error("ConvertSidToStringSidW"));
    }
    // out is LocalAlloc'd; measure, copy, free.
    let mut len = 0usize;
    while *out.add(len) != 0 {
        len += 1;
    }
    let slice = std::slice::from_raw_parts(out, len);
    let s = String::from_utf16_lossy(slice);
    LocalFree(out as *mut c_void);
    Ok(s)
}

/// Ensure the fixed AppContainer profile for `membrane` exists and return its
/// package SID string (for `icacls`). Errors on the non-AppContainer membrane.
/// This is the provisioning entry point a caller uses ONCE (with [`grant_read`])
/// before spawning, so the slow FS grants are not repeated per launch.
#[allow(dead_code)] // provisioning API; exercised by the membrane test / an app opt-in.
pub(crate) fn ensure_container_sid(membrane: Membrane) -> Result<String, String> {
    let name = membrane
        .profile_name()
        .ok_or_else(|| "membrane has no AppContainer profile".to_string())?;
    // `AppContainer`'s Drop frees the SID; clone the string out before it drops.
    unsafe { Ok(ensure_appcontainer(name)?.sid_string.clone()) }
}

/// `icacls <path> /grant *<sid>:<perms>` — grant the package SID read+execute so
/// the sandboxed interpreter can read `path`. `tree` = recurse (`/T`, a dir
/// subtree) vs a single object (a file, a folder, or an `icacls` wildcard such as
/// `C:\Python314\*`). Best-effort: a non-zero exit (e.g. a locked file) is
/// logged, not fatal — the subsequent launch is the real proof the grants held.
#[allow(dead_code)] // provisioning API; exercised by the membrane test / an app opt-in.
pub(crate) fn grant_read(sid_string: &str, path: &str, tree: bool) {
    icacls_grant(sid_string, path, "(RX)", tree);
}

/// Like [`grant_read`] but grants MODIFY (read+write) — for the corpus-index dir,
/// where SQLite must create/write the WAL sidecars (`-wal`/`-shm`) even for read
/// queries, because `corpus.open_index` sets `PRAGMA journal_mode=WAL` on connect.
#[allow(dead_code)] // provisioning API; exercised by the membrane test / an app opt-in.
pub(crate) fn grant_modify(sid_string: &str, path: &str, tree: bool) {
    icacls_grant(sid_string, path, "(M)", tree);
}

fn icacls_grant(sid_string: &str, path: &str, perms: &str, tree: bool) {
    let spec = if tree {
        format!("*{sid_string}:(OI)(CI){perms}")
    } else {
        format!("*{sid_string}:{perms}")
    };
    let mut cmd = std::process::Command::new("icacls");
    cmd.arg(path).arg("/grant").arg(&spec);
    if tree {
        cmd.arg("/T");
    }
    cmd.arg("/C").arg("/Q");
    if let Ok(o) = cmd.output() {
        if !o.status.success() {
            eprintln!(
                "[hardened_spawn] icacls grant {perms} on {path} exit {:?}: {}",
                o.status.code(),
                String::from_utf8_lossy(&o.stderr).trim()
            );
        }
    }
}

/// Build a heap SECURITY_DESCRIPTOR from SDDL; caller `LocalFree`s it.
unsafe fn make_security_descriptor(sddl: &str) -> Result<*mut c_void, String> {
    let wsddl = wide(sddl);
    let mut sd: *mut c_void = null_mut();
    if ConvertStringSecurityDescriptorToSecurityDescriptorW(
        wsddl.as_ptr(),
        SDDL_REVISION_1,
        &mut sd,
        null_mut(),
    ) == 0
    {
        return Err(last_error("ConvertStringSecurityDescriptorToSecurityDescriptorW"));
    }
    Ok(sd)
}

// --------------------------------------------------------------- the spawn

/// Spawn `program args...` under the hardening described in [`SpawnOpts`].
pub(crate) fn spawn_hardened(
    program: &str,
    args: &[&str],
    opts: &SpawnOpts,
) -> Result<HardenedSpawn, String> {
    let exe = resolve_program(program)?;

    // Command line: quoted argv0 (the resolved exe) followed by the args.
    let mut cmdline = String::new();
    append_quoted(&exe.to_string_lossy(), &mut cmdline);
    for a in args {
        cmdline.push(' ');
        append_quoted(a, &mut cmdline);
    }

    unsafe {
        // --- AppContainer profile (membrane paths only) ----------------------
        // The profile must exist so its SID can go into the token's
        // SECURITY_CAPABILITIES. FS grants are provisioned SEPARATELY (once, via
        // ensure_container_sid + grant_read) — NOT per spawn — because granting a
        // large interpreter tree is slow and only needs doing once.
        let container = if opts.membrane.is_appcontainer() {
            let name = opts.membrane.profile_name().expect("appcontainer has a name");
            Some(ensure_appcontainer(name)?)
        } else {
            None
        };

        // --- pipes (child stdin=read, stdout/stderr=write) -------------------
        // AppContainer pipes carry an app-package-usable DACL.
        let pipe_sd = if opts.membrane.is_appcontainer() {
            Some(make_security_descriptor(PIPE_APPCONTAINER_SDDL)?)
        } else {
            None
        };
        let sd_ptr = pipe_sd.unwrap_or(null_mut());

        let (child_stdin, parent_stdin_w) = create_pipe(sd_ptr)?; // child reads, parent writes
        let (parent_stdout_r, child_stdout) = create_pipe(sd_ptr)?; // child writes, parent reads
        let (parent_stderr_r, child_stderr) = create_pipe(sd_ptr)?; // child writes, parent reads
        if let Some(sd) = pipe_sd {
            LocalFree(sd);
        }
        // Parent-retained ends must not be inherited by the child.
        clear_inherit(&parent_stdin_w);
        clear_inherit(&parent_stdout_r);
        clear_inherit(&parent_stderr_r);

        // The child's three inheritable ends, restricted via the handle list.
        let child_in_h = child_stdin.as_raw_handle() as windows_sys::Win32::Foundation::HANDLE;
        let child_out_h = child_stdout.as_raw_handle() as windows_sys::Win32::Foundation::HANDLE;
        let child_err_h = child_stderr.as_raw_handle() as windows_sys::Win32::Foundation::HANDLE;
        let handle_list = [child_in_h, child_out_h, child_err_h];

        // --- proc-thread attribute list (1 attr = handle list; +1 for caps) --
        let attr_count: u32 = if opts.membrane.is_appcontainer() { 2 } else { 1 };
        let mut attr_size: usize = 0;
        InitializeProcThreadAttributeList(null_mut(), attr_count, 0, &mut attr_size);
        let mut attr_buf = vec![0u8; attr_size];
        let attr_list = attr_buf.as_mut_ptr() as LPPROC_THREAD_ATTRIBUTE_LIST;
        if InitializeProcThreadAttributeList(attr_list, attr_count, 0, &mut attr_size) == 0 {
            return Err(last_error("InitializeProcThreadAttributeList"));
        }

        if UpdateProcThreadAttribute(
            attr_list,
            0,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            handle_list.as_ptr() as *const c_void,
            std::mem::size_of_val(&handle_list),
            null_mut(),
            null_mut(),
        ) == 0
        {
            DeleteProcThreadAttributeList(attr_list);
            return Err(last_error("UpdateProcThreadAttribute(HANDLE_LIST)"));
        }

        // SECURITY_CAPABILITIES must outlive CreateProcessW; keep it in this local.
        // ZERO capabilities => no `internetClient` => the WFP default rule blocks
        // all outbound network for this LowBox token, no admin firewall rule.
        let mut sec_caps: SECURITY_CAPABILITIES = zeroed();
        if let Some(c) = &container {
            sec_caps.AppContainerSid = c.sid;
            sec_caps.Capabilities = null_mut();
            sec_caps.CapabilityCount = 0;
            if UpdateProcThreadAttribute(
                attr_list,
                0,
                PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                &sec_caps as *const _ as *const c_void,
                size_of::<SECURITY_CAPABILITIES>(),
                null_mut(),
                null_mut(),
            ) == 0
            {
                DeleteProcThreadAttributeList(attr_list);
                return Err(last_error("UpdateProcThreadAttribute(SECURITY_CAPABILITIES)"));
            }
        }

        // --- STARTUPINFOEX + CreateProcessW ----------------------------------
        let mut si: STARTUPINFOEXW = zeroed();
        si.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        si.StartupInfo.hStdInput = child_in_h;
        si.StartupInfo.hStdOutput = child_out_h;
        si.StartupInfo.hStdError = child_err_h;
        si.lpAttributeList = attr_list;

        let mut pi: PROCESS_INFORMATION = zeroed();
        let wexe = to_wide(exe.as_os_str());
        let mut wcmd = wide(&cmdline);
        let flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW | CREATE_SUSPENDED;

        let ok = CreateProcessW(
            wexe.as_ptr(),
            wcmd.as_mut_ptr(),
            null(),
            null(),
            TRUE, // inherit handles (restricted to the handle list)
            flags,
            null(),
            null(),
            &si as *const STARTUPINFOEXW as *const _,
            &mut pi,
        );

        DeleteProcThreadAttributeList(attr_list);
        // The container SID has been copied into the child token by CreateProcessW
        // (or there was none on the JobOnly path); free it now.
        drop(container);

        if ok == 0 {
            let e = last_error("CreateProcessW");
            // child-end OwnedHandles drop here, closing them.
            return Err(e);
        }

        let process = OwnedHandle::from_raw_handle(pi.hProcess as RawHandle);
        let thread = OwnedHandle::from_raw_handle(pi.hThread as RawHandle);

        // --- job object: assign BEFORE resuming so the child cannot escape ----
        let job = match create_job(opts.kill_on_job_close) {
            Ok(j) => j,
            Err(e) => {
                TerminateProcess(process.as_raw_handle() as _, 1);
                return Err(e);
            }
        };
        if AssignProcessToJobObject(job.as_raw_handle() as _, process.as_raw_handle() as _) == 0 {
            let e = last_error("AssignProcessToJobObject");
            TerminateProcess(process.as_raw_handle() as _, 1);
            return Err(e);
        }

        if ResumeThread(thread.as_raw_handle() as _) == u32::MAX {
            let e = last_error("ResumeThread");
            TerminateProcess(process.as_raw_handle() as _, 1);
            return Err(e);
        }
        drop(thread); // no longer needed

        // Parent-side pipe ends become owned Files (their Drop closes them).
        let stdin = File::from_raw_handle(parent_stdin_w.into_raw_handle());
        let stdout = File::from_raw_handle(parent_stdout_r.into_raw_handle());
        let stderr = File::from_raw_handle(parent_stderr_r.into_raw_handle());
        // child-end handles (child_stdin/out/err) drop here → parent copies
        // close; the child keeps its own inherited copies.

        Ok(HardenedSpawn {
            stdin,
            stdout,
            stderr,
            reaper: Reaper {
                process,
                job: Some(job),
                pid: pi.dwProcessId,
            },
        })
    }
}
