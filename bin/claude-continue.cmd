@echo off
REM No-install launcher for Windows dev: run claude-continue straight from a
REM checkout, mirroring ./bin/claude-continue on macOS/Linux. Same command name
REM (claude-continue) on every platform -- add this bin\ dir to PATH to use it
REM bare, e.g. `claude-continue status`. Delegates to the Python shim alongside
REM it, which puts ../src on sys.path so no pip install is needed.
setlocal
set "CC_PY=py -3"
where py >NUL 2>&1 || set "CC_PY=python"
%CC_PY% "%~dp0claude-continue" %*
