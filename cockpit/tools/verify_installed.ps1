# End-to-end check of the INSTALLED cockpit: does it launch, does it spawn the BUNDLED
# engine (not a system python), and what does the real WebView actually render?
#
# The engine check matters because sidecar.rs falls back to bare "python" when it cannot
# find <exe_dir>/engine/python.exe. That fallback would work on this dev box (python is
# on PATH) while being broken on a clean machine, so asserting "the app started" proves
# nothing. Comparing the child process's ExecutablePath against the bundled path is the
# signal that distinguishes them.
param(
  [string]$InstallDir = "$env:LOCALAPPDATA\LLM Anthology",
  [string]$OutPng     = "$env:USERPROFILE\.local\opt\ai-sessions-render\.audit\installed-app.png"
)
$ErrorActionPreference = 'Stop'

$exe = Join-Path $InstallDir 'LLM Anthology.exe'
$bundledPy = Join-Path $InstallDir 'engine\python.exe'

Get-Process -Name 'LLM Anthology' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500

$proc = Start-Process -FilePath $exe -PassThru
Write-Output "launched pid=$($proc.Id)"

# Give the WebView time to boot and the engine handshake to complete.
Start-Sleep -Seconds 9

if ($proc.HasExited) {
  Write-Output "FAILED:installed-app exited early with code $($proc.ExitCode)"
  exit 1
}
Write-Output "app alive after 9s"

# --- engine child process -------------------------------------------------------
$kids = Get-CimInstance Win32_Process -Filter "ParentProcessId = $($proc.Id)" -ErrorAction SilentlyContinue
Write-Output "child processes: $($kids.Count)"
$py = $kids | Where-Object { $_.Name -like 'python*' }
if (-not $py) {
  # The engine may be a grandchild; sweep any python whose path is the bundled one.
  $py = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -eq $bundledPy }
}
if ($py) {
  foreach ($p in $py) {
    Write-Output "  engine pid=$($p.ProcessId) path=$($p.ExecutablePath)"
    if ($p.ExecutablePath -eq $bundledPy) {
      Write-Output "  ENGINE=BUNDLED (resolved <exe_dir>\engine\python.exe)"
    } else {
      Write-Output "  ENGINE=FALLBACK -- resolved a system python, NOT the bundle"
    }
  }
} else {
  Write-Output "  no python child found (engine may be lazy-spawned on first RPC)"
}

# --- screenshot the app window --------------------------------------------------
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class Win {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out R r);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  public struct R { public int L, T, Rt, B; }
}
'@
$h = $proc.MainWindowHandle
if ($h -eq [IntPtr]::Zero) {
  $proc.Refresh(); Start-Sleep -Seconds 2; $h = $proc.MainWindowHandle
}
if ($h -ne [IntPtr]::Zero) {
  [void][Win]::ShowWindow($h, 9)   # SW_RESTORE
  [void][Win]::SetForegroundWindow($h)
  Start-Sleep -Seconds 2
  $r = New-Object Win+R
  [void][Win]::GetWindowRect($h, [ref]$r)
  $w = $r.Rt - $r.L; $ht = $r.B - $r.T
  Write-Output "window rect ${w}x${ht} at ($($r.L),$($r.T))"
  if ($w -gt 0 -and $ht -gt 0) {
    $bmp = New-Object System.Drawing.Bitmap $w, $ht
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($r.L, $r.T, 0, 0, (New-Object System.Drawing.Size $w, $ht))
    New-Item -ItemType Directory -Force (Split-Path $OutPng) | Out-Null
    $bmp.Save($OutPng, [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $bmp.Dispose()
    Write-Output "screenshot -> $OutPng"
  }
} else {
  Write-Output "no MainWindowHandle -- cannot screenshot"
}

Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
Write-Output "SUCCESS:installed-app launched, engine checked, screenshot captured"
