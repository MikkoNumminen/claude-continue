# Architecture

How `claude-continue` is put together. Pairs with `AGENTS.md` (conventions) and
the per-module docstrings (the authoritative detail).

## The idea in one paragraph

Claude Code enforces a rolling 5-hour usage window. When the quota is exhausted
mid-job the session pauses until someone nudges it. `claude-continue` runs a
long-lived **watch loop**: read the active window's reset time, sleep until
`reset + buffer`, perform an **action** (resume paused sessions, or open a fresh
window), then **verify** the window actually rolled and re-arm — so windows run
back-to-back with no idle gap.

## Layers

```
              cli.py  (argparse: status|doctor|watch|gui|once|fire|install|uninstall|update)
                 │ builds a Config (config.py: flags > env > JSON file > defaults)
                 ▼
   ┌── watch.run() ───────────────────────────────────────────────┐
   │  _next_plan ── ccusage.get_active_block ── model.Block        │  the loop
   │      │         (npx ccusage blocks --active --json --offline) │  (watch.py)
   │      ▼                                                        │
   │  schedule.next_target(block, buffer)  ── fixed_target(at/every)│
   │      │ sleep in ≤60s slices (survives Mac sleep)              │
   │      ▼                                                        │
   │  action.perform(cfg) ──┬─ exec_cmd?  → headless `claude -p`   │  the action
   │      │                 ├─ start_window? → headless window cmd │  (action.py)
   │      │                 ├─ tmux?       → tmux.broadcast        │
   │      │                 ├─ macOS       → iterm.broadcast       │
   │      │                 └─ Win/WSL     → winterm.send_keystroke│
   │      ▼                                                        │
   │  _verify_and_retry ── re-read ccusage; rolled? else retry     │
   └──────────────────────────────────────────────────────────────┘

   gui.py        a Tkinter window driving the same WatchController/watch.run
   update.py     self-update from GitHub releases (check → verify checksum → swap → relaunch)
   selfremove.py "remove completely": uninstall agent + delete config/logs + self-delete bundle
   launchd.py / tasksched.py (via scheduler.py)   install watch.run as an unattended agent
   osenv.py      platform detection + detached-Popen / pid-alive helpers used everywhere
```

## Key contracts

- **ccusage is the only local source of the reset time** (`ccusage.py`). Always
  `--offline` with a subprocess timeout; any failure → `CcusageUnavailable`,
  which callers treat as "no signal" (fall back to a fixed schedule or poll) —
  never a crash. `model.active_block_from_payload` returns the active, non-gap
  block or `None` (idle).
- **`endTime` is an estimate, not gospel.** It can be early. The correctness
  mechanism is `watch._verify_and_retry`: after firing, re-read ccusage; a real
  resume produces a *new* window with a later reset. Same/earlier/no-window all
  mean "didn't take" → retry (bounded). Quota mode opening from idle succeeds
  when *any* active window appears.
- **The action is pluggable** (`action.perform`): `exec_cmd` > `start_window`
  (quota) > `tmux` > macOS iTerm2 > Windows/WSL `--keystroke`. Each per-platform
  module raises a tame error wrapped as `ActionError`.
- **Skip-busy safety:** iTerm2 uses its `is processing` flag; tmux reads the pane
  for a "working" marker. We never type into a mid-turn session unless `--force`.
- **Self-update / self-remove** can't overwrite/delete a running bundle directly,
  so both spawn a **detached helper** that waits for this process to exit, then
  swaps/deletes the macOS `.app` bundle or the Windows one-dir install folder
  (`update.py`, `selfremove.py`).

## Ports & contracts

`watch.run` is the seam everything plugs into. Its injectable ports (all keyword
args, defaulting to the real implementations) and their contracts:

| Port | Signature | Contract |
| --- | --- | --- |
| `clock` | `() -> datetime` | tz-aware UTC "now". |
| `sleep` | `(seconds: float) -> None` | interruptible sleep (the real one is `Event.wait`). |
| `get_block` | `(timeout: float) -> Block \| None` | active block or `None` (idle); raises `ccusage.CcusageUnavailable` on failure (treated as "no signal", never fatal). |
| `perform` | `(cfg, dry_run=False) -> list[str]` | do the action; returns labels acted on; raises `action.ActionError` on failure (the loop logs + degrades). |
| `stop` | `() -> bool` | True when the loop should exit (SIGTERM/SIGINT flips it). |

Exceptions that cross module boundaries: `ccusage.CcusageUnavailable`,
`action.ActionError`, `update.UpdateError`, `tmux.TmuxError`, `lock.AlreadyRunning`.
All are caught where they'd otherwise crash the daemon.

## Testing model

Every external effect is injectable, so the suite is **offline and fast** (~300
tests, ~0.4s): `watch.run` takes `clock`/`sleep`/`get_block`/`perform`/`stop`;
ccusage is mocked via fixtures or the `CLAUDE_CONTINUE_CCUSAGE_CMD` hook;
`osenv.detect()` honors `CLAUDE_CONTINUE_PLATFORM`; subprocess calls (osascript,
tmux, ditto, Popen) are mocked. Pure decision functions (`schedule.*`,
`update_decision`, `watch_explanation`, `should_auto_recheck`, …) are tested
directly. See `CONTRIBUTING.md`.
