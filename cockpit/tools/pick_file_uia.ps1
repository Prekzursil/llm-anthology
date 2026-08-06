# Drive a native Win32 file-open dialog via UI Automation.
#
# Why not SendKeys: Windows refuses SetForegroundWindow from a process that does not own the
# foreground, so keystrokes land in whatever window actually has focus — measured here, they
# went to an unrelated terminal. UIA manipulates the control tree directly and needs no
# focus, so it cannot mis-deliver input to another application. That matters when the
# alternative is typing a path into a random window and pressing Enter.
param(
  [Parameter(Mandatory = $true)][string]$DialogTitle,
  [Parameter(Mandatory = $true)][string]$Path
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

Add-Type @'
using System; using System.Runtime.InteropServices; using System.Text; using System.Collections.Generic;
public class Win32 {
  public delegate bool Cb(IntPtr h, IntPtr p);
  [DllImport("user32.dll")] public static extern bool EnumWindows(Cb cb, IntPtr p);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int m);
  public static IntPtr ByTitle(string title){
    IntPtr found = IntPtr.Zero;
    EnumWindows((h,p)=>{ if(IsWindowVisible(h)){ var t=new StringBuilder(500); GetWindowText(h,t,500);
      if(t.ToString() == title){ found = h; return false; } } return true; }, IntPtr.Zero);
    return found;
  }
}
'@

$hwnd = [Win32]::ByTitle($DialogTitle)
if ($hwnd -eq [IntPtr]::Zero) { Write-Output "FAILED:pick dialog '$DialogTitle' not found"; exit 1 }
Write-Output "dialog hwnd = $($hwnd.ToInt64())"

$root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
if ($null -eq $root) { Write-Output "FAILED:pick no automation element for the dialog"; exit 1 }

# --- the filename edit ------------------------------------------------------------------
$editCond = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
  [System.Windows.Automation.ControlType]::Edit)
$edits = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $editCond)
Write-Output "found $($edits.Count) edit control(s)"
if ($edits.Count -eq 0) { Write-Output "FAILED:pick no edit control in the dialog"; exit 1 }

# DANGER — READ BEFORE CHANGING THIS SELECTION LOGIC.
#
# "the first Edit that exposes ValuePattern" is WRONG and is actively unsafe. A Win32 open
# dialog contains ~49 Edit elements, and the ones belonging to the FILE LIST expose
# ValuePattern too: each list row has an inline-rename editor with
# AutomationId 'System.ItemNameDisplay'. Measured 2026-08-06: that naive selection picked a
# list row and SetValue() started renaming a folder in the user's Documents. It only failed
# because a full path contains '\' and ':', which Windows rejects as filename characters —
# i.e. the accident was prevented by input validation, not by the script.
#
# So: EXCLUDE the item-name editors explicitly, and PREFER the field the dialog labels as the
# file-name box. Never fall back to "any edit with ValuePattern".
$BANNED_IDS = @('System.ItemNameDisplay')

$target = $null; $valuePattern = $null
# Pass 1: the properly-labelled file-name field.
foreach ($e in $edits) {
  if ($BANNED_IDS -contains $e.Current.AutomationId) { continue }
  if ($e.Current.Name -notmatch 'File name|Filename|File_name') { continue }
  $vp = $null
  if ($e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
    $target = $e; $valuePattern = $vp; break
  }
}
# Pass 2: AutomationId 1148 is the classic file-name edit in the Win32 common dialog.
if ($null -eq $target) {
  foreach ($e in $edits) {
    if ($BANNED_IDS -contains $e.Current.AutomationId) { continue }
    if ($e.Current.AutomationId -ne '1148') { continue }
    $vp = $null
    if ($e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
      $target = $e; $valuePattern = $vp; break
    }
  }
}
# Pass 3: in the modern Windows dialog the file-name field is a COMBOBOX, not a descendant
# Edit. Measured: FindAll over Edit descendants returns 49 elements that are ALL file-list
# columns (Name/Date modified/Type/Size per row) plus 'SearchEditBox' — the file-name field is
# not among them. So query ComboBox and use its own ValuePattern.
if ($null -eq $target) {
  $cbCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::ComboBox)
  $combos = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cbCond)
  Write-Output "pass3: $($combos.Count) combobox(es)"
  foreach ($cb in $combos) {
    Write-Output "  combo '$($cb.Current.Name)' id='$($cb.Current.AutomationId)'"
    # 1148 is the classic file-name control id; the label is localised, the id is not.
    if ($cb.Current.Name -notmatch 'File name|Filename' -and $cb.Current.AutomationId -ne '1148') { continue }
    $vp = $null
    if ($cb.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
      $target = $cb; $valuePattern = $vp; break
    }
    # Some shells expose the value on the combo's child Edit instead of the combo.
    $inner = $cb.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $editCond)
    if ($inner) {
      $vp2 = $null
      if ($inner.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp2)) {
        $target = $inner; $valuePattern = $vp2; break
      }
    }
  }
}
if ($null -eq $target) {
  # REFUSE rather than guess. Guessing here renames a user's files.
  Write-Output "FAILED:pick could not identify the file-name field; refusing to guess"
  Write-Output "  candidate edits (name | automationId):"
  foreach ($e in $edits) { Write-Output "    '$($e.Current.Name)' | '$($e.Current.AutomationId)'" }
  exit 1
}
Write-Output "using edit: name='$($target.Current.Name)' id='$($target.Current.AutomationId)'"
if ($BANNED_IDS -contains $target.Current.AutomationId) {
  Write-Output "FAILED:pick refusing to type into a list-row rename editor"; exit 1
}

$valuePattern.SetValue($Path)
Start-Sleep -Milliseconds 500
$readBack = $valuePattern.Current.Value
Write-Output "set value; read back = '$readBack'"
if ($readBack -ne $Path) {
  # Not fatal on every shell, but say so rather than pretending it worked.
  Write-Output "WARNING: read-back differs from what was set"
}

# --- the Open button --------------------------------------------------------------------
$btnCond = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
  [System.Windows.Automation.ControlType]::Button)
$buttons = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $btnCond)
$names = @(); foreach ($b in $buttons) { $names += $b.Current.Name }
Write-Output "buttons: $($names -join ', ')"

$open = $null
foreach ($b in $buttons) {
  if ($b.Current.Name -match '^(&?Open|&?OK)$') { $open = $b; break }
}
if ($null -eq $open) { Write-Output "FAILED:pick no Open/OK button found"; exit 1 }

$ip = $null
if (-not $open.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ip)) {
  Write-Output "FAILED:pick Open button does not support InvokePattern"; exit 1
}
$ip.Invoke()
Write-Output "invoked '$($open.Current.Name)'"

# Confirm the dialog actually went away — an Open that silently failed would leave it up,
# and reporting success while it is still open would be a false green.
Start-Sleep -Seconds 2
$still = [Win32]::ByTitle($DialogTitle)
if ($still -ne [IntPtr]::Zero) {
  Write-Output "FAILED:pick dialog is STILL open after Invoke — the path was likely rejected"
  exit 1
}
Write-Output "dialog closed"
Write-Output "SUCCESS:pick"
