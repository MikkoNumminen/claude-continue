# Architecture

How `claude-continue` is put together. Pairs with `AGENTS.md` (conventions) and
the per-module docstrings (the authoritative detail).

## The idea in one paragraph

Claude Code enforces a rolling 5-hour usage window. When the quota is exhausted
mid-job the session pauses until someone nudges it. `claude-continue` runs a
long-lived **watch loop**: read the active window's reset time, sleep until
`reset + reset_offset + buffer` (the optional `reset_offset` corrects a
systematically-early ccusage estimate), perform an **action** (resume paused sessions, or open a fresh
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
   │  schedule.next_target(block, buffer, reset_offset) ─ fixed_target(at/every)│
   │      │ sleep in ≤60s slices (survives Mac sleep)              │
   │      ▼                                                        │
   │  action.perform(cfg) ──┬─ exec_cmd?  → headless `claude -p`   │  the action
   │      │                 ├─ start_window? → headless window cmd │  (action.py)
   │      │  limit gate ────┤  limits.state_for_cwd(session cwd):  │
   │      │  (require_limit)│  parked on a SPENT limit? else hold  │  (limits.py)
   │      │                 ├─ tmux?       → tmux.broadcast        │
   │      │                 ├─ macOS       → iterm.broadcast       │
   │      │                 └─ Win/WSL     → winterm.send_keystroke│
   │      ▼                                                        │
   │  _verify_and_retry ── transcripts first (limits), ccusage 2nd │
   └──────────────────────────────────────────────────────────────┘

   limits.py     per-session rate-limit state read from Claude Code's own transcripts
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
- **`endTime` is an estimate, not gospel.** It can be early or late. The
  correctness mechanism is `watch._verify_and_retry`, which prefers the sessions'
  own transcripts (`limits.py`) and falls back to ccusage:
  1. **Transcript (preferred).** Claude Code writes a `rate_limit` entry into a
     session's JSONL when it cuts it off, and stops once the session resumes, so
     "did the resume take?" is answerable at once. Nothing limited → done. Still
     limited with a future reset → re-arm on *that* time, don't retry. Still
     limited with the reset passed → the keystroke missed; retry.
  2. **ccusage (fallback, and the only signal in quota mode).** A real resume
     produces a *new* window with a later reset.

  Why the order matters: ccusage floors a block's start to the hour and calls the
  end five hours later, so a resume landing inside that bucket is attributed to
  the SAME block. Trusting it alone made the check read "never rolled" forever
  while the sessions worked, and the retry loop typed `continue` into them every
  two minutes. Quota mode opening from idle still succeeds when *any* active
  window appears.
- **Nothing is typed into a session that isn't parked on a spent limit**
  (`limits.py` + `action.gate_instances`, `require_limit`, default on). "We could
  not read a transcript" (`known=False`) is deliberately NOT the same as "not
  limited": an unreadable session is held back rather than guessed at, and the
  `doctor` limits check is what makes that quiet failure visible.
- **A window is tracked by which fire *times* were tried, not by a handled flag**
  (`watch.run`'s `fired_targets`). A `reset_offset` that fires early can miss;
  the uncorrected reset must still get its attempt, or the window expires
  unattended.
- **The action is pluggable** (`action.perform`): `exec_cmd` > `start_window`
  (quota) > `tmux` > macOS iTerm2 > Windows/WSL `--keystroke`. Each per-platform
  module raises a tame error wrapped as `ActionError`.
- **Skip-busy safety:** iTerm2 uses its `is processing` flag; tmux reads the pane
  for a "working" marker. We never type into a mid-turn session unless `--force`.
  Windows has no such flag, which is what the limit gate covers there: a session
  mid-turn is not parked on a spent limit, so it isn't a target.
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
| `perform` | `(cfg, dry_run=False) -> list[str]` | do the action; returns labels acted on; raises `action.ActionError` on failure (the loop logs + degrades) or `action.NothingToResume` when the limit gate declines (not a failure — the loop re-arms). |
| `snapshot` | `() -> action.Snapshot` | how many sessions are ready / waiting / capped / idle per their transcripts; `known=False` means no signal, and verification falls back to ccusage. `capped` is a limit no `continue` clears (a model cap), counted apart so it never keeps `blocked` from reaching zero and never drives the re-arm. |
| `stop` | `() -> bool` | True when the loop should exit (SIGTERM/SIGINT flips it). |

`perform` and `snapshot` are one port in practice: `snapshot` reads the world
`perform` acts on, so a caller that injects a performer and inherits the real
snapshot would have a unit test scanning the developer's actual `~/.claude`.
`watch.run` therefore only builds the default `snapshot` when `perform` is also
its own default.

Exceptions that cross module boundaries: `ccusage.CcusageUnavailable`,
`action.ActionError`, `update.UpdateError`, `tmux.TmuxError`, `lock.AlreadyRunning`.
All are caught where they'd otherwise crash the daemon.

## Testing model

Every external effect is injectable, so the suite is **offline and fast** (~650
tests, a couple of seconds): `watch.run` takes
`clock`/`sleep`/`get_block`/`perform`/`snapshot`/`stop`; transcript discovery
takes a `root` and reads from a temp directory;
ccusage is mocked via fixtures or the `CLAUDE_CONTINUE_CCUSAGE_CMD` hook;
`osenv.detect()` honors `CLAUDE_CONTINUE_PLATFORM`; subprocess calls (osascript,
tmux, ditto, Popen) are mocked. Pure decision functions (`schedule.*`,
`update_decision`, `watch_explanation`, `should_auto_recheck`, …) are tested
directly. See `CONTRIBUTING.md`.
