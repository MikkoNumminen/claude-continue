"""Configuration with precedence: CLI flags > env vars > config file > defaults.

Config file is JSON (``~/.config/claude-continue/config.json``) — deliberately
not TOML, so we stay stdlib-only on the system Python 3.9 (``tomllib`` is 3.11+).
Env vars are ``CLAUDE_CONTINUE_<FIELD>`` (e.g. ``CLAUDE_CONTINUE_BUFFER=120``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "claude-continue" / "config.json"

# Default session name-substrings to target (matches the original script).
DEFAULT_FILTER = ["claude", "✳"]

_BOOL_FIELDS = {"skip_busy", "all_sessions", "force", "keystroke", "tmux"}
_INT_FIELDS = {
    "buffer",
    "verify_delay",
    "poll_interval",
    "retry_interval",
    "retry_cap",
    "timeout",
}
_FLOAT_FIELDS = {"every_hours"}
_LIST_FIELDS = {"filter"}


@dataclass
class Config:
    # --- action: what to do at a reset ---
    filter: list = field(default_factory=lambda: list(DEFAULT_FILTER))
    skip_busy: bool = True  # never type into a session that's mid-turn
    text: str = "continue"  # what to send to a resumed session
    exec_cmd: str | None = None  # if set, run this headless instead of iTerm broadcast
    session: str | None = None  # target a single session by name substring
    all_sessions: bool = False  # drop the name filter (skip_busy still applies)
    force: bool = False  # with all_sessions: also drop skip_busy
    keystroke: bool = False  # Windows/WSL: type `text` into a terminal window (opt-in)
    window_title: str = "Windows Terminal"  # window to target in keystroke mode
    tmux: bool = False  # resume via `tmux send-keys` — terminal-agnostic (any terminal, macOS/Linux)
    tmux_busy_pattern: str = "esc to interrupt"  # pane content marking a mid-turn (busy) session

    # --- timing ---
    buffer: int = 90  # seconds past the reset before firing
    verify_delay: int = 90  # seconds to wait after firing before re-reading ccusage
    poll_interval: int = 600  # seconds between polls while idle (no active block)
    retry_interval: int = 300  # seconds between retries when still limited after firing
    retry_cap: int = 6  # max retries (6 * 5m ≈ 30m of ccusage-was-early slack)
    timeout: int = 30  # ccusage subprocess timeout (seconds)

    # --- fixed-schedule fallback (used when ccusage is unavailable, or by choice) ---
    at: str | None = None  # "HH:MM"
    every_hours: float | None = None
    anchor: str | None = None  # "HH:MM" anchor for every_hours

    # --- launchd ---
    node_path: str | None = None  # extra PATH dir so launchd can find npx/node
    log_path: str | None = None


def _load_file(path: Path = CONFIG_PATH) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _coerce_env(name: str, raw: str):
    if name in _BOOL_FIELDS:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if name in _INT_FIELDS:
        return int(raw)
    if name in _FLOAT_FIELDS:
        return float(raw)
    if name in _LIST_FIELDS:
        return [p for p in (x.strip() for x in raw.split(",")) if p]
    return raw


def resolve(overrides: dict | None = None, *, config_path: Path = CONFIG_PATH) -> Config:
    """Build a Config from defaults, then file, then env, then explicit overrides.

    ``overrides`` is typically a dict assembled from parsed CLI args; keys whose
    value is ``None`` are ignored (i.e. "flag not given" never clobbers a lower
    layer).
    """
    cfg = Config()
    valid = {f.name for f in fields(Config)}

    for key, value in _load_file(config_path).items():
        if key in valid:
            setattr(cfg, key, value)

    for name in valid:
        env_key = "CLAUDE_CONTINUE_" + name.upper()
        if env_key in os.environ:
            setattr(cfg, name, _coerce_env(name, os.environ[env_key]))

    if overrides:
        for key, value in overrides.items():
            if key in valid and value is not None:
                setattr(cfg, key, value)

    # A blank exec_cmd is "unset", not "run nothing" — otherwise it's truthy and
    # would route the action to the (empty) exec path.
    if cfg.exec_cmd is not None and not cfg.exec_cmd.strip():
        cfg.exec_cmd = None

    return cfg
