"""Entry point for the bundled macOS .app.

Routes through the CLI so the frozen executable is a full `claude-continue` too
(`claude-continue.app/Contents/MacOS/claude-continue status`, etc.), and so
PyInstaller statically sees every feature module via the cli imports. With no
arguments — i.e. a double-click — it defaults to the GUI.
"""

import sys

from claude_continue.cli import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("gui")  # double-click → open the window
    sys.exit(main())
