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
small buffer, fires `continue` into your paused iTerm2 sessions, then re-arms for
the next window.

## Requirements

- **macOS** (uses `launchd` and AppleScript / iTerm2).
- **iTerm2** for the default action (broadcasting `continue` to live sessions).
  Not needed if you use `--exec` headless mode.
- **Node + `ccusage`** for auto-detecting the reset time. `npx ccusage` works
  with no global install. Not needed if you only use the fixed-schedule mode
  (`--at` / `--every`).
- **Python ≥ 3.9** (stdlib only — no third-party Python packages).

## Install

```bash
# Option A: pip (gives you the `claude-continue` command on PATH)
pip install -e .

# Option B: no install — run straight from the checkout
./bin/claude-continue status
```

## Usage

```bash
# What's the current window, and what would fire?
claude-continue status

# Run the loop in the foreground (Ctrl-C to stop)
claude-continue watch

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

## Choosing what fires

By default it broadcasts `continue` to iTerm2 sessions whose name contains
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

# headless instead of iTerm2: open a fresh window with a real task, no terminal
claude-continue watch --exec "claude -p 'resume the migration' --permission-mode bypassPermissions"
```

## Triggering

- **Auto (default):** reset time comes from `ccusage`. The loop adapts as windows
  drift forward each cycle.
- **Fixed schedule:** if you pass `--at HH:MM` or `--every H [--anchor HH:MM]`,
  that schedule is used instead of ccusage — useful for anchoring windows to your
  working hours, or when Node/ccusage isn't available.

## Configuration

Precedence: **CLI flags > env vars > config file > defaults.**

- Config file: `~/.config/claude-continue/config.json` (JSON, e.g.
  `{"buffer": 120, "filter": ["claude"], "exec_cmd": "claude -p ... "}`).
- Env vars: `CLAUDE_CONTINUE_<FIELD>` (e.g. `CLAUDE_CONTINUE_BUFFER=120`).

Key settings: `buffer` (90s after reset before firing), `verify_delay` (90s),
`poll_interval` (600s while idle), `retry_interval` (300s) / `retry_cap` (6),
`skip_busy` (true), `filter`, `text`, `exec_cmd`, `session`, `timeout` (30s).

Flags passed to `install` are baked into the launchd plist, so the unattended
agent runs with the same options you tested with `watch`.

## Two honest caveats

1. **The reset time is a best estimate.** `ccusage` reconstructs the window from
   your local transcripts; on idle paths it can be up to the ~1-hour rounding
   granularity early (it's spot-on when you run continuously into the limit,
   which is the case this tool targets). So after firing, `claude-continue`
   **re-reads `ccusage` to confirm the window actually rolled**, and retries a
   bounded number of times if it didn't. That verification is the correctness
   mechanism — not the raw estimate.

2. **A sleeping Mac runs no timers.** If the Mac is asleep when a window resets,
   nothing fires until it wakes (the loop then fires immediately for the *current*
   window — it doesn't try to replay windows that already passed). For long
   unattended runs, keep the Mac awake with `caffeinate`.

## How it stays unattended

`install` writes a launchd LaunchAgent
(`~/Library/LaunchAgents/com.mikko.claude-continue.plist`) that runs `watch` with
`RunAtLoad` + `KeepAlive` (restarts on crash, stays down after a clean
uninstall). It injects node's bin directory into the agent's `PATH` (nvm's node
isn't on launchd's default PATH, so `npx ccusage` would otherwise fail silently).
Logs go to `~/Library/Logs/claude-continue.log`. A pidfile prevents a manual
`watch` and the agent from both firing.

```bash
launchctl print gui/$(id -u)/com.mikko.claude-continue   # inspect the agent
tail -f ~/Library/Logs/claude-continue.log               # watch it work
```

## Migrating from `claude-continue.sh`

The original one-shot shell script (sleep until `HH:MM`, then AppleScript-broadcast
`continue`) is replaced by `claude-continue once --at HH:MM`, which does the same
thing and adds the ccusage-aware auto mode, skip-busy, and the launchd agent.

## License

[MIT](LICENSE).
