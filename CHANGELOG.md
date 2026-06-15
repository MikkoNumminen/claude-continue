# Changelog

All notable changes to `claude-continue`. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
