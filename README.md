# claude-continue

Keep Claude Code's 5-hour usage windows running **back-to-back**. The instant a
window resets, `claude-continue` resumes your paused Claude sessions — so quota
is never left idle in the gap between when a window resets and when you next
happen to type into it.

Built for long autonomous runs on a Max plan: start a job, let it hit the limit,
walk away. When the window rolls over, the paused sessions get a `continue` and
keep going.

## How the limit actually works

Claude Code's usage window is **consumption-triggered**, not a wall clock. It
starts on your first message and resets ~5 hours later (rounded to the hour).
When the window's quota is exhausted mid-job, the session pauses with a
"limit reached, resets at HH:MM" message and waits. "Every time it resets"
therefore means: *the moment a window ends, send something to open the next one.*
If you stay idle, no new window opens on its own.

`claude-continue` reads the active window's reset time from
[`ccusage`](https://github.com/ryoppippi/ccusage) (which reconstructs usage
blocks from your local Claude Code transcripts — the only local source for this;
`~/.claude.json` and the `claude` CLI expose nothing), waits until reset + a
small buffer, fires the configured action, then re-arms for the next window.

Runs on **macOS, Windows, and WSL** — the reset detection and scheduling are
portable; only the "fire" action and the unattended agent differ per platform
(see [Platform support](#platform-support)).

## Requirements

- **macOS, Windows, or WSL.** The unattended agent uses `launchd` (macOS) or
  Windows Task Scheduler (Windows/WSL).
- **A way to act at reset**, by platform:
  - macOS — **iTerm2** (default: broadcast `continue` to live sessions).
  - Windows / WSL — **headless `--exec`** (recommended), or **`--keystroke`**
    (opt-in; needs PowerShell, which ships with Windows).
- **Node + `ccusage`** for auto-detecting the reset time. `npx ccusage` works
  with no global install. Not needed if you only use the fixed-schedule mode
  (`--at` / `--every`).
- **Python ≥ 3.9** (stdlib only — no third-party Python packages).

## Install

```bash
# Option A (recommended, all platforms): pip — gives you `claude-continue` on PATH
pip install -e .

# Option B: no install — run straight from the checkout
./bin/claude-continue status          # macOS / Linux / WSL
python -m claude_continue.cli status  # any platform (run from the repo root)
```

On native Windows, prefer Option A — `install` (Task Scheduler) needs a runnable
program path, which the console script provides.

## Usage

```bash
# Will it actually work here? Check ccusage, node, iTerm2, the agent, config.
claude-continue doctor

# What's the current window, and what would fire?
claude-continue status

# Run the loop in the foreground (Ctrl-C to stop)
claude-continue watch

# Or a one-button window: press to start watching, press again to stop
claude-continue gui

# Install as a launchd agent — runs unattended, survives reboots
claude-continue install
claude-continue uninstall          # add --purge to also delete the plist

# Fire right now (testing); --dry-run shows targets without sending
claude-continue fire --dry-run
claude-continue fire

# One-shot: wait for the next reset, fire once, exit
claude-continue once
claude-continue once --at 14:00    # or a fixed clock time (replaces the old script)
```

### `status` example

```
Active window:
  started: 2026-06-14T04:00:00+03:00
  resets:  2026-06-14T09:00:00+03:00  (in 3h 44m)
  fire at: 2026-06-14T09:01:30+03:00  (reset + 90s buffer)
Action: send 'continue' to 1 session(s):
  - ✳ Claude Code (claude)
```

### `doctor` example

```
✓ python    Python 3.9.6
✓ ccusage   active window resets 2026-06-14T09:00:00+03:00 (in 3h 02m)
✓ node      /Users/you/.nvm/.../bin/node (launchd PATH will include …)
✓ iterm2    /Applications/iTerm.app present
! agent     not installed (run `claude-continue install` to run unattended)
✓ config    action=filter ['claude', '✳'], trigger=ccusage auto, buffer=90s
✓ targets   1 session(s) currently match: ✳ Claude Code (claude)

Ready, with warnings.
```

`doctor` exits non-zero if any check fails, so it doubles as a CI/health probe.

## GUI

`claude-continue gui` opens a tiny Tkinter window: one button toggles watching
on/off, with a live "next reset · in 2h13m" countdown and a last-fired indicator.
It watches **only while the window is open** — closing it stops the watch (no
agent is installed). Tkinter ships with Python, so there are no extra
dependencies; for an unattended, survives-reboot setup use `install` instead.

```
┌──────── claude-continue ────────────┐
│            ●  WATCHING               │
│   next reset 19:00 · in 2h13m        │
│   Claude instances (2):              │
│     ● working  -- skipped (busy)  …  │
│     ○ idle     -> will resume     …  │
│        [  ⏹  Stop watching  ]        │
│   [ ⟳ Update ]                       │
└──────────────────────────────────────┘
```

It also shows a live **Claude instances** panel (each iTerm2 session's
status — working/idle — and, while watching, whether it'll be resumed or
skipped). *macOS only*; on Windows the panel notes it isn't available.

The **⟳ Update** button checks the latest GitHub release and, if a newer one
exists, downloads it and restarts the app in place (the standalone `.app`/`.exe`
builds). Run from source instead? It tells you to `git pull`.

### Standalone macOS app (no Python required)

Build a double-clickable `claude-continue.app` with PyInstaller (in a throwaway
venv — it doesn't touch your system Python):

```bash
./packaging/build-macos.sh
open dist/claude-continue.app                  # run it
cp -R dist/claude-continue.app /Applications/  # or install it
```

Then it launches like any Mac app (double-click / Spotlight) — no terminal, no
`pip`. It still uses `npx ccusage` for reset detection and iTerm2 for the action,
so Node and iTerm2 remain runtime requirements (same as the CLI). The build is
arm64/x86 matching the machine you build on; PyInstaller can't cross-compile, so
build on the architecture you'll run.

The app is only **ad-hoc signed** (not Developer-ID signed or notarized). A copy
you build and run on the same machine just works, but a copy you *download or
copy to another Mac* gets quarantined and Gatekeeper will block first launch
("cannot be opened because Apple cannot check it for malware"). To clear it:
right-click → Open the first time, or `xattr -dr com.apple.quarantine
claude-continue.app`.

### Standalone Windows .exe (no Python required)

Build it **on a Windows machine** (PyInstaller can't cross-compile from macOS),
in PowerShell from the repo root. Building needs Python ≥ 3.9 on PATH; the
resulting exe needs no Python to run:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1
.\dist\claude-continue.exe          # run it (or double-click in Explorer)
```

It builds in a throwaway venv (never touching your system Python) and produces a
single `dist\claude-continue.exe` that opens the GUI, built `--windowed` so no
console window flashes up behind it. The exe is actually the full
`claude-continue` (double-click opens the GUI; `claude-continue.exe doctor` /
`status` / `watch` work too), but because it's `--windowed` it doesn't attach to
a console, so for CLI text output prefer the `pip install`. Same runtime deps as
the CLI: Node (`npx ccusage`) for reset detection, and PowerShell for the
optional `--keystroke` action. Windows SmartScreen may warn on an unsigned exe
the first time — choose "More info → Run anyway".

## Choosing what fires

On **macOS** it broadcasts `continue` to iTerm2 sessions whose name contains
`claude` or `✳`, **skipping any session that's mid-turn** (so it never injects
into a job that's actively working — see the caveat below).

```bash
# target one session by name substring (safest for a single job)
claude-continue watch --session "my long build"

# match every session (drops the name filter; still skips busy ones)
claude-continue watch --all
claude-continue watch --all --force      # also disable skip-busy

# custom name filter / custom text
claude-continue watch --filter claude,agent --text "continue"

# headless: open a fresh run with a real task, no terminal needed (all platforms)
claude-continue watch --exec "claude -p 'resume the migration' --permission-mode bypassPermissions"
```

On **Windows / WSL** there is no per-session "type into it" API, so the
**headless `--exec`** path above is the reliable default. If you want to mimic
the macOS behavior of typing into a live terminal, opt into keystroke mode:

```powershell
# best-effort: SendKeys `continue`+Enter into a terminal window (focus-stealing)
claude-continue watch --keystroke --window-title "Windows Terminal"
```

## Triggering

- **Auto (default):** reset time comes from `ccusage`. The loop adapts as windows
  drift forward each cycle.
- **Fixed schedule:** if you pass `--at HH:MM` or `--every H [--anchor HH:MM]`,
  that schedule is used instead of ccusage — useful for anchoring windows to your
  working hours, or when Node/ccusage isn't available.
  - `--every H` fires on a single continuous H-hour grid (constant gaps, no
    day-boundary glitch). When `H` divides 24 (1, 2, 3, 4, 6, 8, 12) the
    `--anchor HH:MM` time is hit every day; otherwise the cadence stays regular
    but the wall-clock times shift across days.
  - Fixed times are wall-clock: across a daylight-saving transition a fire may
    land up to an hour off for that one day (the cadence itself stays regular).

## Configuration

Precedence: **CLI flags > env vars > config file > defaults.**

- Config file: `~/.config/claude-continue/config.json` (JSON, e.g.
  `{"buffer": 120, "filter": ["claude"], "exec_cmd": "claude -p ... "}`).
- Env vars: `CLAUDE_CONTINUE_<FIELD>` (e.g. `CLAUDE_CONTINUE_BUFFER=120`).

Key settings: `buffer` (90s after reset before firing), `verify_delay` (90s),
`poll_interval` (600s while idle), `retry_interval` (300s) / `retry_cap` (6),
`skip_busy` (true), `filter`, `text`, `exec_cmd`, `session`, `timeout` (30s),
and (Windows/WSL) `keystroke` (false) / `window_title` ("Windows Terminal").

## Two honest caveats

1. **The reset time is a best estimate.** `ccusage` reconstructs the window from
   your local transcripts; on idle paths it can be up to the ~1-hour rounding
   granularity early (it's spot-on when you run continuously into the limit,
   which is the case this tool targets). So after firing, `claude-continue`
   **re-reads `ccusage` to confirm the window actually rolled**, and retries a
   bounded number of times if it didn't. That verification is the correctness
   mechanism — not the raw estimate.

2. **A sleeping machine runs no timers.** If the machine is asleep when a window
   resets, nothing fires until it wakes (the loop then fires immediately for the
   *current* window — it doesn't replay windows that already passed). For long
   unattended runs, keep it awake (`caffeinate` on macOS; a "do not sleep" power
   plan / `presentationsettings` on Windows).

## Platform support

| | macOS | Windows | WSL |
| --- | --- | --- | --- |
| Reset detection (`ccusage`) | ✅ | ✅ | ✅ |
| Default action | iTerm2 broadcast | `--exec` headless | `--exec` headless |
| Resume-a-live-session | iTerm2 (built in) | `--keystroke` (opt-in) | `--keystroke` (opt-in) |
| Unattended agent | launchd | Task Scheduler | Task Scheduler → `wsl.exe` |

`claude-continue doctor` reports the detected platform and checks the right
pieces for it.

## How it stays unattended

`install` registers `watch` to run at logon and restart on crash:

- **macOS** — a launchd LaunchAgent
  (`~/Library/LaunchAgents/com.mikko.claude-continue.plist`) with `RunAtLoad` +
  `KeepAlive`. It injects node's bin directory into the agent's `PATH` (nvm's
  node isn't on launchd's default PATH, so `npx ccusage` would otherwise fail
  silently). Logs: `~/Library/Logs/claude-continue.log`.

  ```bash
  launchctl print gui/$(id -u)/com.mikko.claude-continue
  tail -f ~/Library/Logs/claude-continue.log
  ```

- **Windows / WSL** — a Task Scheduler task (`claude-continue`, `/sc onlogon
  /rl highest`). Under WSL the task lives on the Windows side and runs
  `wsl.exe -d <distro> -e claude-continue watch` back into your distro.

  ```powershell
  schtasks /query /tn claude-continue
  ```

Flags you pass to `install` are baked into the registered command, so the
unattended agent runs with the same options you tested with `watch`. A pidfile
prevents a manual `watch` and the agent from both firing.

## Migrating from `claude-continue.sh`

The original one-shot shell script (sleep until `HH:MM`, then AppleScript-broadcast
`continue`) is replaced by `claude-continue once --at HH:MM`, which does the same
thing and adds the ccusage-aware auto mode, skip-busy, and the launchd agent.

## License

[MIT](LICENSE).
