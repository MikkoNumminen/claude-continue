"""Entry point for the bundled macOS .app — launches the GUI directly.

PyInstaller analyzes the imports from here, so importing claude_continue pulls
the whole package into the bundle. Double-clicking the .app runs this.
"""

from claude_continue.gui import run

if __name__ == "__main__":
    run()
