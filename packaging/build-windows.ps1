# Build a standalone claude-continue.exe (no Python required to run it).
#
# Uses PyInstaller in a throwaway venv so it never touches your system Python.
# Output: dist\claude-continue.exe  (a single, double-clickable GUI exe)
#
# Usage (from the repo root, in PowerShell):
#   .\packaging\build-windows.ps1
#
# Requirements to BUILD: Python 3.9+ on PATH (the `py` launcher or `python`).
# Requirements to RUN: the exe still shells out to `npx ccusage` for reset
# detection (Node is not bundled); the optional --keystroke action uses
# PowerShell SendKeys. Those remain runtime dependencies, same as the CLI.

$ErrorActionPreference = "Stop"

$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo

$Venv = Join-Path $Repo ".build-venv"
$AppName = "claude-continue"
$PyInstallerVersion = "6.21.0"   # pinned for reproducible builds (matches build-macos.sh)

Write-Host "==> creating a clean build venv at $Venv"
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m venv --clear $Venv
} else {
    & python -m venv --clear $Venv
}

$VenvPy = Join-Path $Venv "Scripts\python.exe"

Write-Host "==> installing PyInstaller $PyInstallerVersion + the package"
& $VenvPy -m pip install --upgrade pip | Out-Null
& $VenvPy -m pip install "pyinstaller==$PyInstallerVersion" .

Write-Host "==> building $AppName.exe"
# --windowed: no console window for the GUI.
# --onefile: a single, easy-to-hand-around .exe.
# --collect-submodules: guarantee every claude_continue module is bundled, so a
#   lazily-imported one (e.g. action, imported inside a click handler) can't go
#   missing from the frozen exe.
& $VenvPy -m PyInstaller `
    --noconfirm --clean --windowed --onefile `
    --name $AppName `
    --collect-submodules claude_continue `
    packaging\claude_continue_app.py

if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

Write-Host ""
Write-Host "==> built: $Repo\dist\$AppName.exe"
Write-Host "    run it:  double-click dist\$AppName.exe   (or: .\dist\$AppName.exe)"
Write-Host ""
Write-Host "    Note: the inner binary is also the full CLI, but --windowed exes"
Write-Host "    don't attach to a console. For CLI use (status/doctor/watch) run"
Write-Host "    the pip install instead, or rebuild without --windowed."
