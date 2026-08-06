# Capture a window's OWN content, without stealing focus or requiring it to be visible.
#
# Why not CopyFromScreen: it grabs screen PIXELS at the window's coordinates, so anything on
# top is captured instead. Measured — a fullscreen game in front of the app produced a
# screenshot of the game, which would have been read as "the app renders wrong". It also needs
# SetForegroundWindow, which yanks focus away from whatever the user is actually doing.
#
# PrintWindow with PW_RENDERFULLCONTENT (0x2) asks the window to render itself into a DC. That
# flag is what makes it work for a WebView2/DirectComposition surface; without it a Tauri
# window comes back blank or white.
param(
  [string]$ProcessName = 'LLM Anthology',
  [Parameter(Mandatory = $true)][string]$Out
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
# Only the P/Invoke declarations live in C#. Doing the Bitmap/Graphics work in PowerShell
# instead avoids needing an explicit System.Drawing.Common reference, which PowerShell 7's
# Add-Type requires and which fails with CS1069 otherwise.
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class Shooter {
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  public struct RECT { public int L, T, R, B; }
}
'@

function Get-WindowShot([IntPtr]$h) {
  $r = New-Object Shooter+RECT
  [void][Shooter]::GetWindowRect($h, [ref]$r)
  $w = $r.R - $r.L; $ht = $r.B - $r.T
  if ($w -le 0 -or $ht -le 0) { return $null }
  $bmp = New-Object System.Drawing.Bitmap $w, $ht
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $dc = $g.GetHdc()
  # PW_RENDERFULLCONTENT = 2 — required for a WebView2 / DirectComposition surface;
  # without it a Tauri window comes back blank.
  $ok = [Shooter]::PrintWindow($h, $dc, 2)
  $g.ReleaseHdc($dc)
  $g.Dispose()
  if (-not $ok) { $bmp.Dispose(); return $null }
  return $bmp
}

$proc = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $proc) { Write-Output "FAILED:shoot no window for '$ProcessName'"; exit 1 }
$h = $proc.MainWindowHandle

if ([Shooter]::IsIconic($h)) {
  # A minimized window has nothing to render. Say so rather than saving a blank frame and
  # letting it be mistaken for a rendering bug.
  Write-Output "FAILED:shoot window is minimized; PrintWindow would capture nothing"
  exit 1
}

$bmp = Get-WindowShot $h
if ($null -eq $bmp) { Write-Output "FAILED:shoot PrintWindow returned nothing"; exit 1 }

New-Item -ItemType Directory -Force (Split-Path $Out) | Out-Null
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)

# A capture that is a single flat colour means the surface did not render — report it instead
# of saving a blank PNG that reads as an app bug.
$distinct = @{}
for ($x = 0; $x -lt $bmp.Width; $x += 37) {
  for ($y = 0; $y -lt $bmp.Height; $y += 37) {
    $distinct[$bmp.GetPixel($x, $y).ToArgb()] = $true
    if ($distinct.Count -gt 8) { break }
  }
  if ($distinct.Count -gt 8) { break }
}
$w = $bmp.Width; $ht = $bmp.Height
$bmp.Dispose()
Write-Output "captured ${w}x${ht} -> $Out  (distinct sampled colours: $($distinct.Count))"
if ($distinct.Count -le 2) {
  Write-Output "FAILED:shoot capture is essentially blank — the surface did not render"
  exit 1
}
Write-Output "SUCCESS:shoot"
