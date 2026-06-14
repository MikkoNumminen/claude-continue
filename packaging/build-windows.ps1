<#
Build a standalone, double-clickable claude-continue.exe (no Python required to run).

Uses PyInstaller in a throwaway virtualenv so it never touches your system
Python. Output: dist\claude-continue.exe (a single file, --windowed so no
console window flashes up behind the Tk toggle).

Usage (from the repo root):
    powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1

This is the Windows counterpart of the macOS build (packaging/build-macos.sh);
both bundle the same packaging/claude_continue_app.py entry point.

Requirements to BUILD: Python >= 3.9 on PATH (the `py` launcher or `python`).
Note: the bundled exe still shells out to `npx ccusage` at runtime for reset
detection (Node is not bundled), and the Windows action paths (`--exec`
headless, or `--keystroke` via PowerShell) remain system dependencies - same
as the CLI. Only Python + the package are baked into the exe.
#>

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

Write-Host "==> creating a clean build venv at $Venv"
& $PyCmd.Source -m venv --clear $Venv
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

Write-Host "==> building $AppName.exe"
# --onefile             : one self-contained .exe (vs the macOS .app bundle) - easiest to hand to someone.
# --windowed            : GUI subsystem, so no console window appears behind the Tk toggle.
# --collect-submodules  : belt-and-suspenders so every claude_continue.* module is bundled even if
#                         only reached via a lazy intra-package import (e.g. `from . import action`).
# Run PyInstaller as a module so we don't depend on the Scripts dir being on PATH.
& $VenvPython -m PyInstaller `
    --noconfirm --clean --windowed --onefile `
    --name $AppName `
    --collect-submodules claude_continue `
    (Join-Path "packaging" "claude_continue_app.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed (exit $LASTEXITCODE)" }

$Exe = Join-Path $Repo (Join-Path "dist" "$AppName.exe")
if (-not (Test-Path $Exe)) { throw "build reported success but $Exe is missing" }

Write-Host ""
Write-Host "==> built: $Exe"
Write-Host "    run it:   .\dist\$AppName.exe   (or double-click it in Explorer)"
Write-Host ""
Write-Host "    Note: the entry point routes through the CLI, so the exe is also a full"
Write-Host "    claude-continue (double-click -> GUI). But --windowed exes don't attach"
Write-Host "    to a console, so for CLI output (status/doctor/watch) use the pip install."
