<#
.SYNOPSIS
  Stage a relocatable CPython + the llm-anthology engine into cockpit/src-tauri/resources/engine.

.DESCRIPTION
  Closes the packaging gap documented in src-tauri/binaries/README.md: without this, an
  INSTALLED cockpit resolves `python -m llm_anthology.sidecar` from PATH and therefore
  requires the user to already have Python and the package. With it, the installer carries
  its own interpreter and the app is self-contained.

  Uses python-build-standalone via uv (the SOTA choice over PyInstaller: a real, relocatable
  CPython rather than a frozen bundle, so the engine keeps normal import semantics and the
  sidecar module loads exactly as it does in development).

  The staged tree is a BUILD ARTIFACT and is gitignored -- never commit ~65 MB of interpreter.

  Rust side: src/sidecar.rs `engine_python_in()` prefers <exe_dir>/engine/python.exe and
  falls back to PATH, so a dev build (nothing staged) behaves exactly as before. Both
  branches are unit-tested.

.PARAMETER PythonVersion
  The uv-managed CPython to stage. Must be a version the engine supports (>= 3.9).

.PARAMETER Slim
  Drop parts of the stdlib a headless JSON-RPC engine cannot need (the test suite, IDLE,
  Tk/Tcl). Roughly halves the staged size. On by default.
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.12",
    [switch]$NoSlim
)

$ErrorActionPreference = "Stop"

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$dest = Join-Path $repo "cockpit\src-tauri\resources\engine"

Write-Host "repo   : $repo"
Write-Host "dest   : $dest"

# --- locate the uv-managed standalone interpreter -----------------------------------
& uv python install $PythonVersion | Out-Null
$uvDir = (& uv python dir).Trim()
$src = Get-ChildItem $uvDir -Directory |
    Where-Object { $_.Name -like "cpython-$PythonVersion-windows-x86_64*" -and
                   (Test-Path (Join-Path $_.FullName "python.exe")) } |
    Select-Object -First 1
if (-not $src) {
    throw "no uv-managed standalone CPython $PythonVersion with a root python.exe under $uvDir"
}
Write-Host "source : $($src.FullName)"

# --- stage --------------------------------------------------------------------------
if (Test-Path $dest) {
    Write-Host "cleaning previous staging..."
    [System.IO.Directory]::Delete($dest, $true)
}
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Write-Host "copying interpreter..."
Copy-Item -Path (Join-Path $src.FullName "*") -Destination $dest -Recurse -Force

# --- slim ----------------------------------------------------------------------------
if (-not $NoSlim) {
    # A headless JSON-RPC engine has no GUI and never runs CPython's own test suite.
    $drop = @("Lib\test", "Lib\idlelib", "Lib\tkinter", "Lib\turtledemo", "tcl",
              "Lib\lib2to3", "Lib\ensurepip") |
        ForEach-Object { Join-Path $dest $_ } | Where-Object { Test-Path $_ }
    foreach ($d in $drop) {
        [System.IO.Directory]::Delete($d, $true)
        Write-Host "  dropped $($d.Replace($dest,''))"
    }
    Get-ChildItem $dest -Recurse -Include "*.pdb" -File -ErrorAction SilentlyContinue |
        ForEach-Object { [System.IO.File]::Delete($_.FullName) }
    Get-ChildItem $dest -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        ForEach-Object { if (Test-Path $_.FullName) { [System.IO.Directory]::Delete($_.FullName, $true) } }
}

# --- drop uv's PEP 668 marker from OUR copy -------------------------------------------
# uv marks its managed interpreters EXTERNALLY-MANAGED so a stray `pip install` cannot
# corrupt the shared toolchain install. That is correct for uv's copy and meaningless for
# this one: `$dest` is a private, disposable build artifact that exists precisely to have
# the engine installed into it. Removing the marker states that intent honestly, where
# `pip --break-system-packages` would assert something false about a managed environment.
$marker = Get-ChildItem $dest -Recurse -File -Filter "EXTERNALLY-MANAGED" -ErrorAction SilentlyContinue
foreach ($m in $marker) {
    [System.IO.File]::Delete($m.FullName)
    Write-Host "  removed PEP 668 marker $($m.FullName.Replace($dest,''))"
}

# --- install the engine INTO the staged interpreter ----------------------------------
$py = Join-Path $dest "python.exe"
Write-Host "installing llm-anthology into the staged interpreter..."
& $py -m pip install --quiet --disable-pip-version-check $repo
if ($LASTEXITCODE -ne 0) { throw "pip install into the staged interpreter failed" }

# --- verify: the staged interpreter must serve the sidecar, standalone ----------------
Push-Location $env:TEMP    # a cwd with no repo on it, so nothing resolves by accident
try {
    $probe = & $py -c "import llm_anthology, os; print(os.path.dirname(llm_anthology.__file__))"
    if ($LASTEXITCODE -ne 0) { throw "the staged interpreter cannot import llm_anthology" }
    Write-Host "  engine importable at: $probe"
    & $py -m llm_anthology.sidecar --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "the staged interpreter cannot run the sidecar module" }
} finally { Pop-Location }

$size = (Get-ChildItem $dest -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ("staged OK: {0:N1} MB at {1}" -f ($size / 1MB), $dest)
Write-Host "SUCCESS:stage-engine"
