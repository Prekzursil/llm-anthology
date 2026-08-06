# Drive the INSTALLED cockpit through its real UI and capture what it renders.
#
# Why UI automation rather than a unit test: the corpus-open path crosses a native file
# picker, the Tauri IPC boundary, a spawned Python process and a canvas renderer. No unit
# test in this repo can cross all four, so the only honest verification is to click the
# button and look at the result. Every defect found in this app so far was found this way.
#
# -Click x,y clicks (client-relative), -Type sends a string, -Enter presses Return, -Shot
# saves a PNG. Actions run in the order given.
param(
  [int[]]$Click,
  [string]$Type,
  [switch]$Enter,
  [switch]$Launch,
  [switch]$Keep,
  [string]$Shot = "$env:USERPROFILE\.local\opt\ai-sessions-render\.audit\installed.png",
  [int]$SettleMs = 9000
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class W {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out R r);
  [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr h, ref P p);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out R r);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, System.Text.StringBuilder s, int m);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint d, IntPtr e);
  public struct R { public int L, T, Rt, B; }
  public struct P { public int X, Y; }
  public static string Title(IntPtr h) {
    int n = GetWindowTextLength(h); if (n == 0) return "";
    var sb = new System.Text.StringBuilder(n + 1); GetWindowText(h, sb, sb.Capacity); return sb.ToString();
  }
}
'@
$exe = "$env:LOCALAPPDATA\LLM Anthology\LLM Anthology.exe"

if ($Launch) {
  Get-Process -Name 'LLM Anthology' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Milliseconds 600
  $null = Start-Process -FilePath $exe -PassThru
  Start-Sleep -Milliseconds $SettleMs
}

$proc = Get-Process -Name 'LLM Anthology' -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $proc) { Write-Output "FAILED:drive no app window found"; exit 1 }
$h = $proc.MainWindowHandle
[void][W]::ShowWindow($h, 9); [void][W]::SetForegroundWindow($h); Start-Sleep -Milliseconds 900

if ($Click) {
  # Convert client coords -> screen, so the numbers I read off a screenshot of the CLIENT
  # area are the numbers I can pass here.
  $p = New-Object W+P; $p.X = $Click[0]; $p.Y = $Click[1]
  [void][W]::ClientToScreen($h, [ref]$p)
  [System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point($p.X, $p.Y)
  Start-Sleep -Milliseconds 250
  [W]::mouse_event(0x0002, 0, 0, 0, [IntPtr]::Zero)   # LEFTDOWN
  [W]::mouse_event(0x0004, 0, 0, 0, [IntPtr]::Zero)   # LEFTUP
  Write-Output "clicked client($($Click[0]),$($Click[1])) = screen($($p.X),$($p.Y))"
  Start-Sleep -Milliseconds 1800
  Write-Output "foreground now: '$([W]::Title([W]::GetForegroundWindow()))'"
}

if ($Type) {
  # SendWait into whatever has focus (the native Open dialog's filename field).
  # Escape the SendKeys metacharacters + ( ) { } ^ % ~ so a path survives verbatim.
  $esc = $Type -replace '([+^%~(){}\[\]])', '{$1}'
  [System.Windows.Forms.SendKeys]::SendWait($esc)
  Write-Output "typed $($Type.Length) chars"
  Start-Sleep -Milliseconds 700
}

if ($Enter) {
  [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
  Write-Output "pressed ENTER"
  Start-Sleep -Milliseconds 4500   # let open_corpus spawn the engine + the UI refetch
}

# --- screenshot the CLIENT area (no titlebar), so coords match what I click ---
$proc.Refresh()
$h = $proc.MainWindowHandle
if ($h -eq [IntPtr]::Zero) { Write-Output "FAILED:drive window vanished"; exit 1 }
$cr = New-Object W+R; [void][W]::GetClientRect($h, [ref]$cr)
$o = New-Object W+P; $o.X = 0; $o.Y = 0; [void][W]::ClientToScreen($h, [ref]$o)
$w = $cr.Rt - $cr.L; $ht = $cr.B - $cr.T
if ($w -le 0 -or $ht -le 0) { Write-Output "FAILED:drive zero-size client rect"; exit 1 }
[void][W]::SetForegroundWindow($h); Start-Sleep -Milliseconds 500
$bmp = New-Object System.Drawing.Bitmap $w, $ht
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($o.X, $o.Y, 0, 0, (New-Object System.Drawing.Size $w, $ht))
New-Item -ItemType Directory -Force (Split-Path $Shot) | Out-Null
$bmp.Save($Shot, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Output "client ${w}x${ht}; shot -> $Shot"

# --- did the engine actually spawn? (independent of anything on screen) ---
$py = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -like '*llm_anthology.sidecar*' }
if ($py) {
  foreach ($q in $py) {
    $bundled = $q.ExecutablePath -like "*LLM Anthology\engine\python.exe"
    Write-Output "engine pid=$($q.ProcessId) bundled=$bundled"
    Write-Output "  cmd: $($q.CommandLine)"
  }
} else {
  Write-Output "engine: NO sidecar process running"
}

if (-not $Keep) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
Write-Output "SUCCESS:drive"
