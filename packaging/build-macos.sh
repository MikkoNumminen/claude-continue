#!/usr/bin/env bash
# Build a standalone, double-clickable claude-continue.app (no Python required to run).
#
# Uses PyInstaller in a throwaway virtualenv so it never touches your system
# Python. Output: dist/claude-continue.app
#
# Usage:  ./packaging/build-macos.sh
#
# Note: the bundled app still shells out to `npx ccusage` at runtime for reset
# detection (Node is not bundled) and to `osascript`/iTerm2 for the action —
# those remain system dependencies, same as the CLI.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

VENV="${REPO}/.build-venv"
APP_NAME="claude-continue"
BUNDLE_ID="com.mikko.claude-continue"

echo "==> creating build venv at ${VENV}"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip >/dev/null

echo "==> installing PyInstaller + the package"
"$VENV/bin/pip" install pyinstaller .

echo "==> building ${APP_NAME}.app"
"$VENV/bin/pyinstaller" \
  --noconfirm --clean --windowed \
  --name "$APP_NAME" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  packaging/claude_continue_app.py

echo
echo "==> built: ${REPO}/dist/${APP_NAME}.app"
echo "    run it:   open dist/${APP_NAME}.app"
echo "    install:  cp -R dist/${APP_NAME}.app /Applications/"
