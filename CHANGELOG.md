# Changelog

All notable changes to `claude-continue`. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

Fixes from the 2026-06-22 robustness audit (each finding adversarially verified).

### Fixed
- **Windows scheduled-task wrapper is now cmd-injection / `%`-safe.** The
  `claude-continue watch` wrapper `.cmd` was built with `list2cmdline`, which quotes
  for the exe's argv parser but NOT for cmd.exe's batch layer — a config value with a
  `%` (variable expansion, even inside quotes) or an unquoted `& | < >` (operator)
  could corrupt or inject into the scheduled command. Tokens are now quoted for the
  batch context and every `%` doubled. The `schtasks /tr` action path is also quoted,
  so installing under a spaced path (e.g. `C:\Users\First Last\…`) registers correctly.

## [0.10.0] — 2026-06-22

### Added
- **Windows: the app registers itself in the Start Menu so the search bar finds it.**
  On launch the frozen Windows build creates (and keeps current) a Start Menu shortcut
  plus an `App Paths` entry for `claude-continue.exe`, so typing "claude-continue" in
  the search bar — or Win+R — opens it. Because the one-dir self-update swaps the
  install folder's contents in place (the exe path is stable), the shortcut always
  points at the latest build with no per-update upkeep. It self-heals if the install
  moves, and `uninstall --app` removes the shortcut + key. Best-effort and Windows-only
  (no-op from source / other OSes); a failure never blocks the GUI.

## [0.9.0] — 2026-06-22

Make the Windows build survive antivirus. The one-file `.exe` re-unpacked
`python311.dll` into `%TEMP%` on every launch, which heuristic scanners (confirmed
with **IPVanish Threat Protection**) blocked — the app died with "Failed to load
Python DLL `python311.dll`. LoadLibrary: the specified module could not be found",
including right after a self-update.

### Changed
- **Windows ships a one-dir build, zipped, instead of a single `.exe`.** The Python
  runtime now lives on disk in `_internal\` and is scanned once, rather than being
  re-extracted to `%TEMP%` each launch for the scanner to flag. The release asset is
  `claude-continue-windows-x64.zip` (a top-level `claude-continue\` folder), matching
  the macOS `.zip`.
- **The Windows self-update now swaps the whole install directory**, the analogue of
  the macOS bundle swap: it extracts the zip in-process (stdlib `zipfile`, with a
  zip-slip guard), then a detached console-less helper waits for this process to exit
  (the v0.8.1 PID-poll), moves the install folder aside, `robocopy`s the new tree in,
  and **rolls back** to the old folder if the copy fails or the new exe is missing —
  un-updated beats bricked. `uninstall --app` likewise removes the whole folder.

### Added
- **Windows exes embed a version resource** (CompanyName/ProductName/version), and the
  **macOS `Info.plist` is stamped with the version**. Legitimate metadata measurably
  lowers heuristic false positives on unsigned binaries (it is not a substitute for
  signing). UPX is now explicitly disabled on both platforms (it inflates AV false
  positives on Windows and corrupts macOS binaries).

### Upgrade (Windows, from any earlier build)
- One manual hop: the old `.exe`'s updater looks for a `.exe` asset that no longer
  exists. Download `claude-continue-windows-x64.zip` from the latest release, unzip
  it, and replace your install folder. The Update button works normally from 0.9.0 on.

### Notes
- The builds remain **unsigned**: this change removes the specific behavior AV flagged
  and makes the binaries look far more legitimate, but only Authenticode signing
  (Windows) + Developer-ID notarization (macOS) can *guarantee* a heuristic scanner
  never blocks them. A strict AV may still need an allow-list entry.
- As with the v0.8.1 swap helper, the new Windows directory-swap `.cmd` is unit-tested
  for its emitted text and validated on CI, but an actual Update/Remove against a live
  install has not yet been smoke-tested on real hardware. The move-aside + abort +
  rollback keep the worst case bounded and never-bricked.

## [0.8.1] — 2026-06-20

Follow-up hardening for the v0.7.0–v0.8.0 Windows work, from an adversarial audit
and code review (each finding independently verified before fixing).

### Fixed
- **A silently-failed Windows self-update is now actually reported.** When a prior
  update didn't land, the next launch warns "the last update to X didn't complete"
  — but that warning could only arise on the frozen *windowed* `.exe` (no console),
  where it was printed to a `None` stdout and vanished. It's now shown in the GUI
  via a dialog, the one place the user will see it. The launcher also reaps stale
  `cc-update-*` temp dirs left by an interrupted update.
- **Windows update/uninstall helpers wait for the app to actually exit before
  acting.** The detached `.cmd` now polls for this process's PID to disappear
  (capped so it can never hang) before swapping/deleting the binary, instead of a
  blind fixed sleep — closing the exit race that could leave a duplicate instance.
  A failed `tasklist` keeps waiting rather than acting prematurely; `move`-aside +
  rollback keep the binary path never-empty (un-updated beats bricked).
- **The Windows self-delete (`uninstall --app`) now refuses an unsafe install
  path** (e.g. one containing `%`) instead of emitting a corrupt `del` script —
  mirroring the guard the self-update already had.
- **Claude-process detection on Windows is scoped to the real install path**
  (`@anthropic-ai/claude-code`) rather than a bare `claude-code` substring, so an
  unrelated `node` process or an `npm install` line can no longer be mistaken for a
  Claude session.
- **`doctor`'s "continue all" action label and checks are correctly scoped to
  Windows** (they could otherwise surface on macOS/Linux where the path never
  fires), and the "nothing running" message no longer cites the irrelevant
  `filter`/`skip_busy` settings on that path.
- `status` no longer risks a `cp1252` crash when printing a non-ASCII `--text`.

### Docs
- README scopes `--keystroke-all` to **native Windows** (it can't enumerate WSL's
  Linux Claude processes; WSL uses the single-window `--keystroke` path), documents
  that it has no skip-busy filter and re-injects on retry, and adds `keystroke_all`
  to the configuration key reference.

### Notes
- The detached Windows `.cmd` swap/self-delete helpers are unit-tested for their
  emitted text and validated on Windows CI, but an actual Update/Remove against a
  live running `.exe` has not yet been smoke-tested on real hardware.

## [0.8.0] — 2026-06-16

### Added
- **Windows: "Continue terminals" now resumes EVERY running Claude session, not
  just one window.** The macOS app broadcasts `continue` to all iTerm2 sessions;
  Windows could only type into a single titled window, so multiple sessions — e.g.
  several tabs *or split panes* in one Windows Terminal window — were left paused
  (the panel listed N instances but only one, at most, got continued). The watcher
  now writes `continue` straight into each Claude process's **console input**
  (`AttachConsole` + `WriteConsoleInput`), targeting every session **by PID**. This
  bypasses the window entirely, so it works no matter how sessions are arranged —
  separate windows, tabs, or split panes — **without stealing focus** and even
  when the terminal is in the background. It's the default for the GUI on native
  Windows; the instances panel annotates each row "-> will continue" while
  watching. Exposed on the CLI as `--keystroke-all`. (Verified against Windows
  Terminal's ConPTY in both cooked and raw input modes. WSL keeps the single
  titled-window path — its Claude is a Linux process Windows can't enumerate.)

### Fixed
- **`doctor` no longer crashes on Windows.** Running `doctor` (including the
  frozen `.exe`, which is built `console=False`) tracebacked with
  `UnicodeEncodeError: '✓'` — it printed the `✓`/`✗` status glyphs (and the
  `✳` in the default filter) to a legacy cp1252 console. The in-place UTF-8
  reconfigure couldn't touch the frozen windowed stream, so output is now layered:
  reconfigure → rebuild a UTF-8 wrapper over the raw buffer → ASCII-fallback
  symbols (`[ok]`/`[!]`/`[X]`) with `errors="replace"` as a last resort. A doctor
  that prints `?` beats a doctor that crashes.
- **The GUI no longer fails silently when a fire doesn't land.** A failed fire
  (e.g. `--keystroke` can't find its target window) logs at WARNING, but the GUI's
  watch logger dropped every non-`fired ->` line — so the window kept showing
  "WATCHING" all night while nothing actually resumed. The controller now captures
  the latest warning and the UI shows it (`⚠ …`), cleared by the next real fire.
  The GUI watch also writes to a rotating log file (`gui.log`, beside the config)
  so an overnight run leaves a trail.

### Changed
- **`doctor`'s `--keystroke` check now verifies the target window exists.** It
  enumerates open window titles and FAILs (listing what's open) when
  `--window-title` matches nothing, instead of a misleading dry-run "OK". This
  catches the most common keystroke failure: Windows Terminal's window title is
  the active *tab's* name, not the literal "Windows Terminal", so the default
  target finds nothing to type into.

## [0.7.2] — 2026-06-16

### Fixed
- **Windows self-update now actually updates and restarts.** Clicking **Update**
  (or **Remove app…**) on the Windows `.exe` closed the app but left it un-updated
  and never relaunched. The helper `.cmd` ran in a console-less window where its
  `tasklist | find` wait-pipe and `ping` delay both silently fail, so it hung
  before ever swapping the exe — and even if it hadn't, a running `.exe` can't be
  `copy`-overwritten. The helper now waits with `waitfor /t` (works without a
  console), **moves** the old exe aside and copies the new one into the freed path
  (you can rename a running exe even though you can't overwrite it), **rolls back**
  if the copy fails so the install is never left empty, relaunches, and clears the
  leftover `.old` on next launch. `Remove app…` got the same `waitfor` fix.

  ⚠️ **Upgrading from 0.7.1 or earlier on Windows needs a one-time manual
  download** — the *old, broken* updater is what runs for that final hop, so the
  in-app button can't perform it. Grab the `.exe` from the
  [latest release](https://github.com/MikkoNumminen/claude-continue/releases/latest)
  once; the in-app Update button works normally from this version on.

## [0.7.1] — 2026-06-16

### Fixed
- **Windows GUI no longer flashes console windows.** The GUI polls ccusage and
  the process list on a timer; spawned from the windowed `.exe` (which has no
  console), each `subprocess` briefly popped up — and stole focus from — a console
  window, swallowing the user's keystrokes mid-type. Every GUI subprocess now
  spawns with `CREATE_NO_WINDOW`. (Regression introduced in 0.7.0.)

### Changed
- **Windows "Claude instances" panel now lists running Claude Code processes**
  (`claude.exe`, or the npm `node` CLI) — the real analogue of the macOS session
  list — instead of guessing at terminal windows by title. Working/idle isn't
  shown: Windows has no iTerm2-style "is processing" signal.

## [0.7.0] — 2026-06-16

### Added
- **Windows/WSL GUI parity.** The GUI is now zero-config on Windows the same way
  it is on macOS: **Continue terminals** defaults to keystroke mode (types
  `continue` into your terminal window) instead of erroring with "no resume
  action", and the pre-watch explanation describes that instead of iTerm2. The
  **Claude instances** panel, previously "macOS/iTerm2 only", now lists your
  visible terminal windows and marks the one a keystroke will land in. The CLI
  keeps keystroke opt-in (a focus-stealing SendKeys shouldn't be a silent default
  for an unattended `watch`/`fire`).

## [0.6.1] — 2026-06-16

### Added
- CI **secret scan** (gitleaks, full git history) with a `.gitleaks.toml` that
  extends the default rules with an Anthropic `sk-ant-` key rule — verified to
  catch a planted key (the defaults alone did not). No runtime/behavior change.

## [0.6.0] — 2026-06-16

### Added
- AI-first / contributor docs: `AGENTS.md`, `ARCHITECTURE.md`, `CONTRIBUTING.md`,
  `CHANGELOG.md`, `SECURITY.md`, `llms.txt`, `.editorconfig`.
- Enforced quality gates in CI: tests on **Python 3.9 + 3.12 × {ubuntu, macOS,
  windows}** (the 3.9 job guards the "no 3.10+ syntax" runtime invariant),
  `ruff` lint, and `mypy` type-check. Added `py.typed` and typed the watch-loop
  ports.

(No runtime/behavior change vs 0.5.5 — this release ships the documentation and
quality-gate layer and stamps the version.)

## [0.5.5] — 2026-06-15

### Added
- **Quota mode** — a **Start quota** button (and `--start-window` / `--window-cmd`)
  that opens a fresh 5-hour window headlessly via a tiny `claude -p`, touching no
  terminal: immediately when you have none active and again at each reset, keeping
  windows back-to-back without resuming work. The main button is renamed
  **Continue terminals**.

### Fixed
- Quota idle-open is poll-paced, not a tight re-open loop, when an opened window
  never registers. `status`, `doctor`, and the GUI all describe quota mode
  consistently; the **Start quota** button opens a window even if `exec_cmd` is set.

## [0.5.4] — 2026-06-15

### Fixed
- The Update button's green/gray state now renders on macOS (native Tk ignores
  button `fg`/`bg`) via a color **glyph** in the label (🟢 / ✓ / ⟳).
- Update checks **retry transient failures** (GitHub 502/503/504, timeouts,
  connection resets) instead of surfacing a one-off blip as "update check failed".

## [0.5.3] — 2026-06-15

### Added
- The Update button **auto-refreshes** — re-checks every 6h and on window focus
  (debounced) — so it turns green on its own when a release appears.

## [0.5.2] — 2026-06-15

### Added
- **Remove app…** — complete self-removal (GUI button + `uninstall --app`):
  stop watching, remove the unattended agent, delete config + logs, and
  self-delete the `.app`/`.exe` via a detached helper. A failed self-delete is
  surfaced, not silently swallowed.

### Fixed
- The Update button no longer paints an ugly gray box on macOS (tint the text /
  status line instead of the native button background).

## [0.5.1] — 2026-06-15

### Fixed
- The frozen `.app` Update button failed with `ssl: certificate verify failed`;
  it now falls back to the OS system CA bundle when the bundled OpenSSL ships no
  certs.

### Added
- The Update button is color-coded (green = available, gray = up to date) via a
  background check on launch. New `claude-continue update [--apply]` CLI.

## [0.5.0] — 2026-06-15

### Added
- **tmux mode** (`--tmux`) — resume Claude running in any terminal (Terminal.app,
  Ghostty, Warp, kitty, …) on macOS or Linux via `tmux send-keys`.

### Fixed
- After firing before a reset, the loop no longer gives up when ccusage shows no
  active window; it keeps retrying until the window truly rolls (retry coverage
  widened to ~1h).

## [0.4.0] — 2026-06-15

### Added
- **Self-update**: a ⟳ Update button that downloads and replaces the app in place
  (SHA-256 verified, GitHub host-allowlisted, race-free relaunch).
- A pre-watch explanation of what "Start watching" will do, given your config.

## [0.3.0] — 2026-06-15

### Added
- A live **Claude instances** panel in the GUI (each session's working/idle status
  and, while watching, whether it'll be resumed or skipped).

## [0.2.0] — 2026-06-14

### Added
- CI macOS build + tag-triggered release workflow (publishes the `.app` zip).

## [0.1.0] — 2026-06-14

### Added
- Initial release: the `claude-continue` CLI (`status`/`watch`/`once`/`fire`/
  `doctor`/`gui`/`install`/`uninstall`), the self-rescheduling watch loop with
  ccusage auto-detection and a fixed-schedule fallback, iTerm2 broadcast with
  skip-busy, launchd / Windows Task Scheduler agents, and the standalone macOS
  `.app` / Windows `.exe` builds.

[0.8.1]: https://github.com/MikkoNumminen/claude-continue/releases/tag/v0.8.1
[0.6.1]: https://github.com/MikkoNumminen/claude-continue/releases/tag/v0.6.1
[0.6.0]: https://github.com/MikkoNumminen/claude-continue/releases/tag/v0.6.0
[0.5.5]: https://github.com/MikkoNumminen/claude-continue/releases/tag/v0.5.5
[0.5.4]: https://github.com/MikkoNumminen/claude-continue/releases/tag/v0.5.4
[0.5.3]: https://github.com/MikkoNumminen/claude-continue/releases/tag/v0.5.3
[0.5.2]: https://github.com/MikkoNumminen/claude-continue/releases/tag/v0.5.2
[0.5.1]: https://github.com/MikkoNumminen/claude-continue/releases/tag/v0.5.1
[0.5.0]: https://github.com/MikkoNumminen/claude-continue/releases/tag/v0.5.0
[0.4.0]: https://github.com/MikkoNumminen/claude-continue/releases/tag/v0.4.0
[0.3.0]: https://github.com/MikkoNumminen/claude-continue/releases/tag/v0.3.0
[0.2.0]: https://github.com/MikkoNumminen/claude-continue/releases/tag/v0.2.0
[0.1.0]: https://github.com/MikkoNumminen/claude-continue/releases/tag/v0.1.0
