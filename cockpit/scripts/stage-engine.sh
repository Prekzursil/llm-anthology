#!/usr/bin/env bash
# Stage a relocatable CPython + the llm-anthology engine into
# cockpit/src-tauri/resources/engine  --  POSIX counterpart of stage-engine.ps1.
#
# WHY THIS EXISTS
#   stage-engine.ps1 is Windows-only, so `tauri build` on linux/macOS had no way to produce a
#   self-contained bundle: an INSTALLED cockpit would resolve `python -m llm_anthology.sidecar`
#   from PATH and require the user to already have Python and the package.
#
#   Same strategy as the PowerShell script: python-build-standalone via uv, i.e. a real
#   relocatable CPython rather than a frozen bundle, so the engine keeps normal import
#   semantics and the sidecar module loads exactly as it does in development.
#
#   The staged tree is a BUILD ARTIFACT and is gitignored -- never commit ~65 MB of interpreter.
#
# THIS IS NOT A PATH-SEPARATOR TRANSLATION OF THE .ps1. Three things genuinely differ, and
# getting any of them wrong produces a staged tree that looks right and does not run:
#
#   1. LAYOUT. python-build-standalone puts the stdlib at `lib/pythonX.Y/` on POSIX and at
#      `Lib/` on Windows, and the interpreter at `bin/python3` rather than a root `python.exe`.
#      So the slim list is a DIFFERENT SET OF PATHS, not the same paths with slashes flipped.
#   2. TRIPLE. uv's directory is `cpython-<ver>-linux-x86_64-gnu` / `...-macos-aarch64-none`,
#      versus `...-windows-x86_64-none`. Both OS and arch have to be detected.
#   3. THE PEP 668 MARKER MOVES. On Windows it sits at the root; on POSIX it is inside
#      `lib/pythonX.Y/`. A root-only search silently finds nothing and the later pip install
#      fails with an externally-managed-environment error that reads like a uv bug.
#
# Rust side: src/sidecar.rs `engine_python_in()` currently prefers <exe_dir>/engine/python.exe.
# THAT IS WINDOWS-ONLY AND STILL NEEDS A POSIX BRANCH looking for <exe_dir>/engine/bin/python3;
# until it has one, a staged POSIX tree will not be picked up by an installed app and the
# sidecar falls back to PATH. Staging works; resolution does not. Tracked as its own work item.

set -euo pipefail

PYTHON_VERSION="${1:-3.12}"
SLIM=1
for arg in "$@"; do
    [ "$arg" = "--no-slim" ] && SLIM=0
done

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
dest="$repo/cockpit/src-tauri/resources/engine"

echo "repo   : $repo"
echo "dest   : $dest"

# --- detect the python-build-standalone triple --------------------------------------
case "$(uname -s)" in
    Linux)  os="linux";  libc="gnu"  ;;
    Darwin) os="macos";  libc="none" ;;
    *) echo "FAILED:stage-engine unsupported OS $(uname -s) -- use stage-engine.ps1 on Windows" >&2
       exit 1 ;;
esac
case "$(uname -m)" in
    x86_64|amd64) arch="x86_64"  ;;
    aarch64|arm64) arch="aarch64" ;;
    *) echo "FAILED:stage-engine unsupported arch $(uname -m)" >&2; exit 1 ;;
esac
echo "triple : cpython-$PYTHON_VERSION-$os-$arch-$libc"

# --- locate the uv-managed standalone interpreter -----------------------------------
uv python install "$PYTHON_VERSION" >/dev/null
uv_dir="$(uv python dir)"

src=""
for d in "$uv_dir"/cpython-"$PYTHON_VERSION"*"$os"-"$arch"*; do
    # The interpreter must actually be there. uv keeps download stubs and partially-removed
    # versions in the same directory, so the name matching alone is not evidence.
    if [ -x "$d/bin/python3" ]; then src="$d"; break; fi
done
if [ -z "$src" ]; then
    echo "FAILED:stage-engine no uv-managed standalone CPython $PYTHON_VERSION with bin/python3 under $uv_dir" >&2
    exit 1
fi
echo "source : $src"

# --- stage --------------------------------------------------------------------------
if [ -d "$dest" ]; then
    echo "cleaning previous staging..."
    rm -rf "$dest"
fi
mkdir -p "$dest"
echo "copying interpreter..."
# -a preserves the symlinks python-build-standalone ships (bin/python -> python3.12). Copying
# them as regular files would triple the bin/ size and, worse, break the version-suffixed
# lookups some stdlib paths perform.
cp -a "$src"/. "$dest"/

# --- slim ----------------------------------------------------------------------------
if [ "$SLIM" = "1" ]; then
    # A headless JSON-RPC engine has no GUI and never runs CPython's own test suite.
    # NOTE the POSIX layout: lib/pythonX.Y/... , not Lib/... (see header note 1).
    pylib="$(ls -d "$dest"/lib/python* 2>/dev/null | head -1 || true)"
    if [ -n "$pylib" ]; then
        for sub in test idlelib tkinter turtledemo lib2to3 ensurepip; do
            if [ -d "$pylib/$sub" ]; then
                rm -rf "$pylib/$sub"
                echo "  dropped ${pylib#$dest}/$sub"
            fi
        done
    fi
    # Tcl/Tk data lives beside the stdlib on POSIX rather than in a root `tcl` directory.
    for tcl in "$dest"/lib/tcl* "$dest"/lib/tk*; do
        [ -d "$tcl" ] && rm -rf "$tcl" && echo "  dropped ${tcl#$dest}"
    done
    # Static archives are for embedders; nothing here links against libpython.
    find "$dest" -type f -name "*.a" -delete 2>/dev/null || true
    find "$dest" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
fi

# --- drop uv's PEP 668 marker from OUR copy -------------------------------------------
# uv marks its managed interpreters EXTERNALLY-MANAGED so a stray `pip install` cannot corrupt
# the shared toolchain install. That is correct for uv's copy and meaningless for this one:
# $dest is a private, disposable build artifact that exists precisely to have the engine
# installed into it. Removing the marker states that intent honestly, where
# `pip --break-system-packages` would assert something false about a managed environment.
# On POSIX the marker is under lib/pythonX.Y/, NOT at the root (see header note 3).
while IFS= read -r m; do
    rm -f "$m"
    echo "  removed PEP 668 marker ${m#$dest}"
done < <(find "$dest" -type f -name "EXTERNALLY-MANAGED" 2>/dev/null || true)

# --- install the engine INTO the staged interpreter ----------------------------------
py="$dest/bin/python3"
echo "installing llm-anthology into the staged interpreter..."
"$py" -m pip install --quiet --disable-pip-version-check "$repo"

# --- verify: the staged interpreter must serve the sidecar, standalone ----------------
# Run from a cwd with no repo on it, so nothing resolves by accident. This is the check that
# distinguishes "the files were copied" from "the engine actually runs out of them".
probe="$(cd "${TMPDIR:-/tmp}" && "$py" -c 'import llm_anthology, os; print(os.path.dirname(llm_anthology.__file__))')"
echo "  engine importable at: $probe"
(cd "${TMPDIR:-/tmp}" && "$py" -m llm_anthology.sidecar --help >/dev/null)

size_mb="$(du -sm "$dest" | cut -f1)"
echo "staged OK: ${size_mb} MB at $dest"
echo "SUCCESS:stage-engine"
