@echo off
REM No-install launcher for Windows dev: run claude-continue straight from a
REM checkout, mirroring ./bin/claude-continue on macOS/Linux. Same command name
REM (claude-continue) on every platform -- add this bin\ dir to PATH to use it
REM bare, e.g. `claude-continue status`. Delegates to the Python shim alongside
REM it, which puts ../src on sys.path so no pip install is needed.
setlocal
set "CC_PY="
set "CC_ARG="
REM Prefer a REAL `py -3`, then `python`, skipping the 0-byte Microsoft Store
REM alias in WindowsApps (which `where` lists but which only opens the Store when
REM run). Quote the interpreter path -- it may live under "Program Files".
for /f "delims=" %%P in ('where py 2^>NUL') do if not defined CC_PY if /i "%%~dpP" neq "%LOCALAPPDATA%\Microsoft\WindowsApps\" ( set "CC_PY=%%P" & set "CC_ARG=-3" )
if not defined CC_PY for /f "delims=" %%P in ('where python 2^>NUL') do if not defined CC_PY if /i "%%~dpP" neq "%LOCALAPPDATA%\Microsoft\WindowsApps\" set "CC_PY=%%P"
if not defined CC_PY (
  echo claude-continue: no real Python 3 found on PATH ^(the Microsoft Store alias doesn't count^) -- install it from python.org and tick "Add to PATH".>&2
  exit /b 1
)
"%CC_PY%" %CC_ARG% "%~dp0claude-continue" %*
