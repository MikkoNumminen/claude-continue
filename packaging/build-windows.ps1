#Requires -Version 5.1

<#
Build a standalone, double-clickable claude-continue.exe (no Python required to run).

Uses PyInstaller in a throwaway virtualenv so it never touches your system
Python. Output: dist\claude-continue.exe (a single file, --windowed so no
console window flashes up behind the Tk toggle).

Usage (from the repo root):
    powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1
    powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1 -OneDir

-OneDir builds a dist\claude-continue\ folder instead of one .exe: faster startup
and fewer antivirus false-positives, at the cost of being a directory not a file.

This is the Windows counterpart of the macOS build (packaging/build-macos.sh);
both bundle the same packaging/claude_continue_app.py entry point.

Requirements to BUILD: Python >= 3.9 on PATH (the `py` launcher or `python`).
Note: the bundled exe still shells out to `npx ccusage` at runtime for reset
detection (Node is not bundled), and the Windows action paths (`--exec`
headless, or `--keystroke` via PowerShell) remain system dependencies - same
as the CLI. Only Python + the package are baked into the exe.
#>

[CmdletBinding()]
param(
    [switch]$OneDir
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Repo root = the parent of this script's directory, regardless of where it's run from.
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo

$Venv               = Join-Path $Repo ".build-venv"
$AppName            = "claude-continue"
$PyInstallerVersion = "6.21.0"   # pinned for reproducible builds

# Find a REAL interpreter, preferring `python` then the `py` launcher. On stock
# Windows, ...\Microsoft\WindowsApps\python.exe is a 0-byte "App Execution Alias"
# stub that just opens the Store when Python isn't installed; Get-Command reports
# it as a valid Application, so a plain null check would let it through and bypass
# the clear "install Python" guidance below. Filter those stubs out by hand.
function Find-RealInterpreter([string[]] $Names) {
    foreach ($name in $Names) {
        $cmd = Get-Command $name -All -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Source -and
                $_.Source -notmatch '\\Microsoft\\WindowsApps\\' -and
                (Test-Path $_.Source) -and ((Get-Item $_.Source).Length -gt 0)
            } | Select-Object -First 1
        if ($cmd) { return $cmd }
    }
    return $null
}

$PyCmd = Find-RealInterpreter @("python", "py")
if (-not $PyCmd) {
    throw "No Python found on PATH (the Microsoft Store python.exe stub does not count). Install Python >= 3.9 from python.org (tick 'Add to PATH') and re-run."
}

# If we resolved the `py` launcher (not python.exe), force Python 3 so it can't
# default to a co-installed Python 2 (which has no venv module). `python.exe`
# rejects -3, so the flag is conditional; pyproject's requires-python >= 3.9
# still backstops a stale 3.7/3.8 default.
$PyArgs = @()
if ($PyCmd.Source -match '\\py\.exe$') { $PyArgs = @("-3") }

Write-Host "==> creating a clean build venv at $Venv"
& $PyCmd.Source @PyArgs -m venv --clear $Venv
if ($LASTEXITCODE -ne 0) { throw "venv creation failed (exit $LASTEXITCODE)" }

# Drive the build through the venv's own interpreter so it's fully isolated.
$VenvPython = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "venv creation produced no interpreter at $VenvPython - is '$($PyCmd.Source)' a real Python (not the Microsoft Store stub)?"
}

Write-Host "==> upgrading pip"
& $VenvPython -m pip install --upgrade pip | Out-Null
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed (exit $LASTEXITCODE)" }

Write-Host "==> installing PyInstaller $PyInstallerVersion + the package"
& $VenvPython -m pip install "pyinstaller==$PyInstallerVersion" .
if ($LASTEXITCODE -ne 0) { throw "dependency install failed (exit $LASTEXITCODE)" }

# Embed a Windows version resource. A bare unsigned exe with NO version/company
# metadata is exactly what heuristic AV (SmartScreen, IPVanish-style scanners)
# distrusts most; legitimate VERSIONINFO won't make it signed, but it measurably
# lowers false positives. Generated from the package version so it never drifts.
Write-Host "==> generating Windows version metadata"
$Version = (& $VenvPython -c "import claude_continue, sys; sys.stdout.write(claude_continue.__version__)")
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($Version)) { throw "couldn't read claude_continue.__version__" }
# filevers/prodvers want a 4-int tuple. Strip any pre-release/build tail FIRST
# (e.g. 0.9.0-rc1 / 1.2.3+build) so a suffix can't corrupt the numeric fields,
# then split on dots and pad to 4 ints.
$core = ($Version -split '[-+]', 2)[0]
$nums = @($core -split '\.' | ForEach-Object { [int]($_ -replace '\D', '') })
while ($nums.Count -lt 4) { $nums += 0 }
$nums = $nums[0..3]
$vtuple = ($nums -join ', ')
$vstr = ($nums -join '.')
$VerFile = Join-Path $Repo (Join-Path "build" "win-version-info.txt")
New-Item -ItemType Directory -Force (Split-Path $VerFile) | Out-Null
@"
# Auto-generated by build-windows.ps1 - DO NOT EDIT.
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($vtuple), prodvers=($vtuple),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Mikko Numminen'),
      StringStruct('FileDescription', 'claude-continue'),
      StringStruct('FileVersion', '$vstr'),
      StringStruct('InternalName', 'claude-continue'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', 'claude-continue.exe'),
      StringStruct('ProductName', 'claude-continue'),
      StringStruct('ProductVersion', '$vstr')])]),
    VarFileInfo([VarStruct('Translation', [0x0409, 0x04B0])])])
"@ | Set-Content -Path $VerFile -Encoding ascii

Write-Host "==> building $AppName.exe"
# --onefile (default)   : one self-contained .exe (vs the macOS .app bundle) - easiest to hand to someone.
#   (-OneDir switch)    : a dist\<name>\ folder instead - faster startup, fewer AV false-positives.
# --windowed            : GUI subsystem, so no console window appears behind the Tk toggle.
# --collect-submodules  : belt-and-suspenders so every claude_continue.* module is bundled even if
#                         only reached via a lazy intra-package import (e.g. `from . import action`).
# --noupx               : never UPX-compress. UPX isn't on the CI runner today, so the shipped exe
#                         isn't packed (PyInstaller silently skips it) - but if a future build box
#                         has upx on PATH, PyInstaller's upx=True default would pack it, which only
#                         worsens antivirus false-positives. Lock it off so that can't sneak in.
# --version-file        : embed the VERSIONINFO generated above (see the AV note there).
# Run PyInstaller as a module so we don't depend on the Scripts dir being on PATH.
# Releases use -OneDir (release.yml): a one-dir build doesn't re-unpack python311.dll
# into %TEMP% on every launch, the behaviour AV heuristics (e.g. IPVanish Threat
# Protection) flagged on the old one-file exe. The default stays one-file for a
# hand-it-to-someone single exe.
$ModeFlag = if ($OneDir) { "--onedir" } else { "--onefile" }
& $VenvPython -m PyInstaller `
    --noconfirm --clean --noupx --windowed $ModeFlag `
    --name $AppName `
    --version-file $VerFile `
    --collect-submodules claude_continue `
    (Join-Path "packaging" "claude_continue_app.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed (exit $LASTEXITCODE)" }

$Exe = if ($OneDir) {
    Join-Path $Repo (Join-Path "dist" (Join-Path $AppName "$AppName.exe"))
} else {
    Join-Path $Repo (Join-Path "dist" "$AppName.exe")
}
if (-not (Test-Path $Exe)) { throw "build reported success but $Exe is missing" }

Write-Host ""
Write-Host "==> built: $Exe"
Write-Host "    run it:   $Exe   (or double-click it in Explorer)"
Write-Host ""
Write-Host "    Note: the entry point routes through the CLI, so the exe is also a full"
Write-Host "    claude-continue (double-click -> GUI). But --windowed exes don't attach"
Write-Host "    to a console, so for CLI output (status/doctor/watch) use the pip install."
