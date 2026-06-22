# claude-continue

Keep Claude Code's 5-hour usage windows running **back-to-back**. The instant a
window resets, `claude-continue` resumes your paused Claude sessions — so quota
is never left idle in the gap between when a window resets and when you next
happen to type into it.

Built for long autonomous runs on a Max plan: start a job, let it hit the limit,
walk away. When the window rolls over, the paused sessions get a `continue` and
keep going.

**Docs:** [AGENTS.md](AGENTS.md) (agent/dev guide) ·
[ARCHITECTURE.md](ARCHITECTURE.md) (module map) ·
[CONTRIBUTING.md](CONTRIBUTING.md) · [CHANGELOG.md](CHANGELOG.md) ·
[SECURITY.md](SECURITY.md)

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
  - Any terminal (macOS/Linux) — **`--tmux`** if you run Claude inside tmux
    (terminal-agnostic; needs `tmux` on PATH).
  - Windows / WSL — **headless `--exec`** (recommended), or **`--keystroke`**
    (opt-in; needs PowerShell, which ships with Windows).
  - Any platform — **headless `--exec`** (no terminal needed at all).
- **Node + `ccusage`** for auto-detecting the reset time. `npx ccusage` works
  with no global install. Not needed if you only use the fixed-schedule mode
  (`--at` / `--every`).
- **Python ≥ 3.9** (stdlib only — no third-party Python packages).

## Install

```bash
# Option A (recommended, all platforms): pip — gives you `claude-continue` on PATH
pip install -e .

# Option B: no install — run straight from the checkout (same `claude-continue`
# command name on every platform; add `bin/` to PATH to drop the leading path)
./bin/claude-continue status          # macOS / Linux / WSL
.\bin\claude-continue status          # Windows (cmd or PowerShell)
python -m claude_continue.cli status  # any platform (fallback, from the repo root)
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
claude-continue uninstall          # --purge also deletes the plist; --app removes EVERYTHING (agent, settings, logs, the app itself)

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
✓ platform  macos
✓ ccusage   active window resets 2026-06-14T09:00:00+03:00 (in 3h 02m)
✓ node      /Users/you/.nvm/.../bin/node (a stable node dir will be on the launchd PATH)
! agent     not installed (run `claude-continue install` to run unattended)
✓ config    action=filter ['claude', '✳'], trigger=ccusage auto, buffer=90s
✓ action    1 target(s): ✳ Claude Code (claude)

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
│      [  ⏹  Stop  ]                   │
│      [  ＋ Start quota  ]            │
│   [ 🟢 Update ]                      │
│        Remove app…                   │
└──────────────────────────────────────┘
```

Two action buttons:
- **▶ Continue terminals** — start watching; at each reset, resume your paused
  Claude sessions (the default). Click again to stop. On macOS this broadcasts
  `continue` to iTerm2; on **Windows/WSL** the GUI works zero-config too — it
  defaults to keystroke mode and types `continue` into your terminal window
  (`--window-title`, default "Windows Terminal"). The CLI keeps keystroke opt-in.
- **＋ Start quota** — start watching in *quota mode*: open a fresh 5-hour window
  **headlessly, without touching any terminal** (a tiny `claude -p`), right away
  if you have none and again at each reset — so windows stay back-to-back even
  when you're not resuming work. The automated version of "type something to
  start the window". (CLI: `--start-window`, with `--window-cmd` to customise.)

Before you start, the window spells out **what watching will do** given your
config (e.g. "sends 'continue' to idle Claude sessions in iTerm2 … Busy
sessions are skipped") so there are no surprises. That line is shown in the
idle state and hidden once watching.

It also shows a live **Claude instances** panel. On macOS it reads iTerm2 (each
session's status — working/idle — and, while watching, whether it'll be resumed
or skipped), or tmux panes in `--tmux` mode (any platform). On **Windows** it
lists the running Claude Code processes (`claude.exe`, or the npm `node` CLI) —
the closest equivalent to the macOS session list; Windows has no per-session
"is processing" signal, so there's no working/idle marker. (On WSL, where Claude
runs as a Linux process the Windows process query can't see, the panel notes it
has no live view.)

The **⟳ Update** button checks the latest GitHub release and, if a newer one
exists, downloads it and restarts the app in place (the standalone macOS `.app`
bundle / Windows install folder). It's checked once on launch and tinted green
when an update is waiting, gray when you're up to date. Run from source instead?
It tells you to `git pull`.

> **Windows, upgrading to 0.9.0 from any earlier build:** 0.9.0 switches the
> Windows release from a single `.exe` to a **one-dir folder shipped as a `.zip`**.
> (The old one-file exe re-unpacked its Python runtime into `%TEMP%` on every
> launch, which antivirus — e.g. IPVanish Threat Protection — kept blocking with
> "Failed to load Python DLL `python311.dll`". The one-dir build keeps the DLL on
> disk, so it's scanned once instead of fighting the scanner each launch.) The old
> `.exe`'s updater looks for a `.exe` asset that no longer exists, so make this one
> hop **by hand**: download `claude-continue-windows-x64.zip` from the
> [latest release](https://github.com/MikkoNumminen/claude-continue/releases/latest),
> unzip it, and replace your install folder. The Update button works normally from
> 0.9.0 on — it now swaps the whole folder for you.

The **Remove app…** button removes claude-continue **completely** — after a
confirmation it stops watching, removes the background agent, deletes your
settings + logs, and deletes the app bundle itself (a detached helper removes
the bundle once the app quits). The CLI equivalent is `claude-continue uninstall
--app`.

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

### Standalone Windows build (no Python required)

Build it **on a Windows machine** (PyInstaller can't cross-compile from macOS),
in PowerShell from the repo root. Building needs Python ≥ 3.9 on PATH; the
resulting build needs no Python to run:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1            # one .exe
powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1 -OneDir    # a folder
.\dist\claude-continue.exe                  # run the one-file build
.\dist\claude-continue\claude-continue.exe  # run the one-dir build
```

It builds in a throwaway venv (never touching your system Python). The default is
a single `dist\claude-continue.exe` — handy to hand to someone. **Releases use
`-OneDir`** (a `dist\claude-continue\` folder, shipped zipped): a one-file exe
re-unpacks `python311.dll` into `%TEMP%` on every launch, which antivirus
heuristics (e.g. IPVanish Threat Protection) flag and block — the one-dir build
keeps the DLL on disk so it's scanned once. Both embed a version resource and skip
UPX, which also lowers false positives.

Either way the exe is the full `claude-continue` (double-click opens the GUI;
`claude-continue.exe doctor` / `status` / `watch` work too), built `--windowed`
so no console flashes up — which also means it doesn't attach to a console, so
for CLI text output prefer the `pip install`. Same runtime deps as the CLI: Node
(`npx ccusage`) for reset detection, and PowerShell for the optional `--keystroke`
action. The build is **unsigned**, so Windows SmartScreen may still warn the first
time ("More info → Run anyway"); a strict third-party antivirus may need an
allow-list entry for the install folder. Code signing is the only thing that fully
removes those prompts.

On first launch the Windows build **registers itself in the Start Menu** (and adds an
`App Paths` entry), so typing "claude-continue" in the search bar — or Win+R — opens
it. Self-updates keep the install path stable, so the shortcut always points at the
latest build; `uninstall --app` removes it. Run it from one canonical folder (don't
keep multiple copies) so the shortcut and search resolve to the build you actually use.

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

**Not on iTerm2? Use tmux (`--tmux`).** The iTerm2 broadcast is macOS+iTerm2 only.
If you run Claude inside **tmux** — in *any* terminal (Terminal.app, Ghostty, Warp,
kitty, GNOME Terminal, Konsole, …), on **macOS or Linux** — `--tmux` types
`continue` into matching panes via `tmux send-keys`. It targets panes precisely (no
focus-stealing, no Accessibility permission) and honors the same `--session` /
`--filter` / `--all` / `--force` gating:

```bash
# resume Claude panes running in tmux (works in any terminal, macOS + Linux)
claude-continue watch --tmux
```

Because tmux has no `is processing` flag, "busy" is detected by reading the pane's
visible content for the marker Claude shows while working (default `esc to
interrupt`); skip-busy then leaves mid-turn panes alone. Tune it with
`--tmux-busy-pattern "<text>"` if your Claude build shows something different.

On **Windows / WSL** there is no per-session "type into it" API, so the
**headless `--exec`** path above is the reliable default. If you want to mimic
the macOS behavior of resuming live terminals, there are two opt-in modes:

```powershell
# NATIVE WINDOWS ONLY: continue ALL running Claude sessions (the GUI's default on
# Windows): writes `continue`+Enter into each Claude console's input — any
# window/tab/pane, no focus stealing
claude-continue watch --keystroke-all

# Windows AND WSL: target ONE window by title via SendKeys `continue`+Enter
# (focus-stealing)
claude-continue watch --keystroke --window-title "Windows Terminal"
```

> **WSL note:** `--keystroke-all` works on **native Windows only** — it enumerates
> Windows processes (`claude.exe` / `node.exe`), which can't see Claude running as a
> Linux process inside WSL. On WSL, use the single-window `--keystroke
> --window-title` path (or `--exec`). The GUI applies this automatically:
> continue-all on native Windows, single-window keystroke on WSL.

Caveats for the Windows resume modes:

- **`--keystroke-all` types into _every_ detected Claude console**, found by
  enumerating `claude.exe` / `node.exe @anthropic-ai/claude-code` processes — not a
  single chosen window. There is **no skip-busy filter** on this path (unlike the
  iTerm2/tmux broadcast), so a session that happens to be mid-turn at reset will
  also receive `continue`. In practice the paused-at-limit sessions are the idle
  ones, but if you run sessions you don't want nudged, prefer `--exec` or
  `--keystroke` with a specific `--window-title`.
- It re-injects on every fire (and on each bounded retry), so the same console can
  be sent `continue` more than once across a single reset if the first attempt
  didn't visibly take.
- Both keystroke paths are **best-effort and unverified on a live Windows box in
  this build** — run `claude-continue doctor` first; it lists exactly which
  sessions/windows would be targeted before you arm the watch.

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
`poll_interval` (600s while idle), `retry_interval` (120s) / `retry_cap` (30,
so retries span ~1h — enough to cover a worst-case-early estimate),
`skip_busy` (true), `filter`, `text`, `exec_cmd`, `session`, `timeout` (30s),
`tmux` (false) / `tmux_busy_pattern` ("esc to interrupt"), (Windows/WSL)
`keystroke` (false) / `window_title` ("Windows Terminal"), and (native Windows)
`keystroke_all` (false; the GUI's default on Windows — continue every session).

## Honest caveats

1. **The reset time is a best estimate.** `ccusage` reconstructs the window from
   your local transcripts; on idle paths it can be up to the ~1-hour rounding
   granularity early (it's spot-on when you run continuously into the limit,
   which is the case this tool targets). So after firing, `claude-continue`
   **re-reads `ccusage` to confirm the window actually rolled** — a real resume
   produces a *new* window with a later reset. If it hasn't rolled (same window,
   or no active window yet because we fired before the true reset), it keeps
   re-sending `continue` every `retry_interval` until the window rolls, up to
   `retry_cap` (~1h of coverage). That verification-and-retry is the correctness
   mechanism — not the raw estimate.

2. **A sleeping machine runs no timers.** If the machine is asleep when a window
   resets, nothing fires until it wakes (the loop then fires immediately for the
   *current* window — it doesn't replay windows that already passed). For long
   unattended runs, keep it awake (`caffeinate` on macOS; a "do not sleep" power
   plan / `presentationsettings` on Windows).

3. **"Use it or lose it" means fire-and-forget — mind the review debt.** This
   fills the gaps between consumption-triggered windows; it isn't bending any
   limits (on a Max plan you're paying for the capacity either way). But the whole
   pattern is *start it, walk away, let it run* — so Claude produces work nobody is
   watching in real time. On long autonomous runs the marginal value can fall off
   fast: you get a lot of output, but **review debt piles up**, and an hour spent
   going the wrong direction is worse than not running at all. The tool doesn't
   cause that, but it makes the pattern temptingly easy — so point it at
   well-scoped work and actually read what comes back.

## Platform support

| | macOS | Windows | WSL | Linux |
| --- | --- | --- | --- | --- |
| Reset detection (`ccusage`) | ✅ | ✅ | ✅ | ✅ |
| Default action | iTerm2 broadcast | `--exec` headless | `--exec` headless | `--exec` headless |
| Resume-a-live-session | iTerm2, or `--tmux` | `--keystroke` (opt-in) | `--keystroke` (opt-in) | `--tmux` |
| Any-terminal resume (`--tmux`) | ✅ (in tmux) | — | — | ✅ (in tmux) |
| Unattended agent | launchd | Task Scheduler | Task Scheduler → `wsl.exe` | — |

`claude-continue doctor` reports the detected platform and checks the right
pieces for it. (Linux has no bundled unattended agent yet — run `watch` under your
own service manager, e.g. a systemd user unit.)

**Scope, honestly.** The zero-config experience is macOS-first — iTerm2 and
launchd are what tie it there. The cross-platform routes (`--exec` headless,
`--tmux`) already work; fuller Linux/tmux support is the main thing that would
widen the audience beyond a personal tool.

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
