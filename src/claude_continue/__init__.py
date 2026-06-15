"""claude-continue — keep Claude Code's 5-hour usage windows running back-to-back.

The instant a usage window resets, resume paused Claude sessions so quota is
never left idle between windows. See the README for the design and caveats.
"""

__version__ = "0.5.2"
