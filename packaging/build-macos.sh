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
PYINSTALLER_VERSION="6.21.0"  # pinned for reproducible builds

echo "==> creating a clean build venv at ${VENV}"
python3 -m venv --clear "$VENV"
"$VENV/bin/pip" install --upgrade pip >/dev/null

echo "==> installing PyInstaller ${PYINSTALLER_VERSION} + the package"
"$VENV/bin/pip" install "pyinstaller==${PYINSTALLER_VERSION}" .

echo "==> building ${APP_NAME}.app"
# --collect-submodules guarantees every claude_continue submodule is bundled,
# so lazily-imported modules (e.g. action, imported inside a click handler)
# can't go missing from the frozen app.
# --noupx: never UPX-compress. UPX isn't applied today (not on the runner), but UPX is known to
# CORRUPT macOS binaries (and breaks code signing), so we lock it off defensively - a future build
# box with upx on PATH must never silently pack the .app. No effect on the current output.
"$VENV/bin/pyinstaller" \
  --noconfirm --clean --noupx --windowed \
  --name "$APP_NAME" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --collect-submodules claude_continue \
  packaging/claude_continue_app.py

# Stamp the bundle's version. A versioned Info.plist looks legitimate to Gatekeeper
# and 3rd-party scanners (it won't substitute for notarization, but it's free) and
# keeps "Get Info" honest. Generated from the package so it never drifts.
VERSION="$("$VENV/bin/python" -c 'import claude_continue, sys; sys.stdout.write(claude_continue.__version__)')"
PLIST="dist/${APP_NAME}.app/Contents/Info.plist"
if [ -n "$VERSION" ] && [ -f "$PLIST" ]; then
  for key in CFBundleShortVersionString CFBundleVersion; do
    /usr/libexec/PlistBuddy -c "Set :$key $VERSION" "$PLIST" 2>/dev/null \
      || /usr/libexec/PlistBuddy -c "Add :$key string $VERSION" "$PLIST"
  done
  echo "==> stamped Info.plist version: $VERSION"
fi

echo
echo "==> built: ${REPO}/dist/${APP_NAME}.app"
echo "    run it:   open dist/${APP_NAME}.app"
echo "    install:  cp -R dist/${APP_NAME}.app /Applications/"
