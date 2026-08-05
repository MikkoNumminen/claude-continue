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

_BOOL_FIELDS = {"skip_busy", "all_sessions", "force", "keystroke", "keystroke_all", "tmux",
                "start_window", "require_limit"}
_INT_FIELDS = {
    "buffer",
    "reset_offset",
    "verify_delay",
    "poll_interval",
    "retry_interval",
    "retry_cap",
    "timeout",
}
_FLOAT_FIELDS = {"every_hours"}
_LIST_FIELDS = {"filter", "skip_dirs"}

# Timing values must be positive. For poll/retry/verify, a zero/negative value
# makes ``watch._sleep_until`` return "reached" immediately, turning the
# idle-poll and post-fire backoff into a tight loop that re-runs ccusage / the
# resume action every iteration. ``timeout`` is the ccusage subprocess timeout:
# it can't busy-loop, but a non-positive value makes every probe time out
# instantly, so auto-detect never works. Either way the value is invalid and a
# bad one can arrive from the config file or an env var (neither is
# bounds-checked), so floor them all here.
MIN_TIMING_SECONDS = 1
_TIMING_FLOORS = {
    "poll_interval": MIN_TIMING_SECONDS,
    "retry_interval": MIN_TIMING_SECONDS,
    "verify_delay": MIN_TIMING_SECONDS,
    "timeout": MIN_TIMING_SECONDS,
}


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
    keystroke_all: bool = False  # Windows: continue EVERY running Claude session via console-input injection, not one window
    # Windows continue-all: never send `continue` to a Claude session working in
    # one of these directories — "that terminal is doing its own thing". Entries
    # are full paths (match the dir and everything under it) or bare folder names
    # (match by basename, e.g. "HRManager"); see winterm.dir_skipped.
    skip_dirs: list = field(default_factory=list)
    # Only ever type into a session that Claude Code's own transcript shows parked on
    # a spent rate limit (see limits.py). ON by default because the opposite is
    # actively harmful: a blind fire lands `continue` in sessions that finished their
    # work, and Claude then goes off and does something nobody asked for. Turn it off
    # (--no-require-limit) to get the pre-0.14 fire-regardless behaviour.
    require_limit: bool = True
    tmux: bool = False  # resume via `tmux send-keys` — terminal-agnostic (any terminal, macOS/Linux)
    tmux_busy_pattern: str = "esc to interrupt"  # pane content marking a mid-turn (busy) session
    start_window: bool = False  # "quota mode": open a fresh window headlessly instead of resuming terminals
    window_cmd: str = 'claude -p "Reply with only: ok"'  # headless command that opens a window in quota mode

    # --- timing ---
    buffer: int = 90  # seconds past the reset before firing
    # Seconds to add to ccusage's estimated reset before firing — a correction for
    # a systematically-wrong estimate. ccusage floors the window start to the whole
    # hour, so its reset estimate runs EARLY by the first message's minutes-past-the
    # -hour; this offsets that. Set via the GUI "Fire at" field; 0 = trust the
    # estimate. May be negative (estimate late). NOT clamped (0 is the valid default).
    reset_offset: int = 0
    verify_delay: int = 90  # seconds to wait after firing before re-reading ccusage
    poll_interval: int = 600  # seconds between polls while idle (no active block)
    retry_interval: int = 120  # seconds between retries when the window hasn't rolled yet
    retry_cap: int = 30  # max retries; 30 * 2m ≈ 1h covers a worst-case-early ccusage estimate
    timeout: int = 30  # ccusage subprocess timeout (seconds)

    # --- fixed-schedule fallback (used when ccusage is unavailable, or by choice) ---
    at: str | None = None  # "HH:MM"
    every_hours: float | None = None
    anchor: str | None = None  # "HH:MM" anchor for every_hours

    # --- launchd ---
    node_path: str | None = None  # extra PATH dir so launchd can find npx/node
    log_path: str | None = None


def _load_file(path: Path = CONFIG_PATH) -> dict:
    # utf-8-sig, not the platform default: this file is meant to be hand-edited, and
    # on Windows the obvious editors (Notepad, `Out-File`, VS Code's "UTF-8 with
    # BOM") prepend a BOM. Read as cp1252 those three bytes become "ï»¿", json.load
    # raises, and the whole file is discarded — every setting in it silently reverts
    # to its default with nothing said. The sig variant strips a BOM when present
    # and is a plain utf-8 read when it isn't.
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        # UnicodeDecodeError matters because of the explicit encoding above: the
        # platform default this replaced was cp1252 on Windows, which decodes any
        # byte sequence, so a config saved in a legacy encoding used to load (or at
        # worst mojibake) and now raises. Uncaught that is a hard crash at startup —
        # strictly worse than the silent-default behaviour being fixed here.
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


def _coerce_file_list(value):
    """Shape a config-file value for a list field, or None when unusable.

    File values arrive as raw JSON with no coercion, and a list field fed a
    non-iterable (``"skip_dirs": true``) blows up wherever it's iterated — for
    skip_dirs that's inside the GUI's Tk refresh callback, which then never
    re-schedules itself: the whole app freezes with no visible error. A bare
    string is an equally plausible hand-edit (``"skip_dirs": "D:\\\\proj"``) and
    would silently char-iterate. So: a list passes through, a string becomes a
    one-entry list (NOT comma-split — a real path may contain commas; the env
    var is the comma-separated form), anything else is unusable and the caller
    keeps the default."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return None


def resolve(overrides: dict | None = None, *, config_path: Path = CONFIG_PATH) -> Config:
    """Build a Config from defaults, then file, then env, then explicit overrides.

    ``overrides`` is typically a dict assembled from parsed CLI args; keys whose
    value is ``None`` are ignored (i.e. "flag not given" never clobbers a lower
    layer).
    """
    cfg = Config()
    valid = {f.name for f in fields(Config)}

    for key, value in _load_file(config_path).items():
        if key not in valid:
            continue
        if key in _LIST_FIELDS:
            value = _coerce_file_list(value)
            if value is None:
                continue  # unusable shape (e.g. `true`) — keep the default
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

    # A blanked window_cmd would make quota mode try to run an empty command;
    # restore the default so --start-window always has something to open with.
    if not (cfg.window_cmd or "").strip():
        cfg.window_cmd = Config.window_cmd

    return cfg


def save_setting(name: str, value, *, config_path: Path = CONFIG_PATH) -> bool:
    """Persist one setting into the JSON config file. Returns True on success.

    The GUI is the only interface most people use, and until now it could only
    *read* config — so a mode chosen there was forgotten at every restart, silently
    reverting to the default. That is fine for a fire time you retype anyway, and
    not fine for a switch that decides which sessions get typed into.

    Merges rather than rewrites, so hand-edited keys survive, and writes through a
    temporary file in the same directory so an interrupted write cannot leave a
    truncated config that ``_load_file`` would then discard wholesale. Best-effort
    by contract: a read-only config dir must not stop the app, it just means the
    choice lasts for this session only.
    """
    if name not in {f.name for f in fields(Config)}:
        raise KeyError("unknown setting %r" % name)
    data = _load_file(config_path)
    data[name] = value
    tmp = config_path.with_name(config_path.name + ".tmp")
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, config_path)  # atomic on POSIX and on Windows
        return True
    except (OSError, TypeError, ValueError):
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def timing_issues(cfg: Config) -> list:
    """Return ``[(field, value, floor)]`` for every loop interval below its floor.

    Pure and non-mutating, so ``doctor`` can warn about a busy-loop-inducing
    value without changing the config the user is about to run with.
    """
    issues = []
    for name, floor in _TIMING_FLOORS.items():
        value = getattr(cfg, name)
        try:
            ok = value >= floor
        except TypeError:
            ok = False  # wrong type entirely (e.g. a string from the JSON file)
        if not ok:
            issues.append((name, value, floor))
    return issues


def clamp_timing(cfg: Config) -> list:
    """Floor non-positive timing values in place; return what was adjusted.

    The watch loop calls this at startup so a bad config degrades (clamped +
    logged) instead of busy-looping (poll/retry/verify) or making auto-detect
    fail outright (timeout).
    """
    issues = timing_issues(cfg)
    for name, _value, floor in issues:
        setattr(cfg, name, floor)
    return issues
