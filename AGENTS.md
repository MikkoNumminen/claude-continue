# AGENTS.md

Guidance for AI coding agents (and humans) working in this repo. Read this first.

## What this project is

`claude-continue` keeps Claude Code's rolling **5-hour usage windows** running
back-to-back: the instant a window resets, it resumes your paused Claude
sessions (or just opens a fresh window) so quota is never left idle. It's a
**stdlib-only Python CLI** plus a tiny Tkinter GUI, packaged as a macOS `.app`
and a Windows `.exe`. See `README.md` for the product story and `ARCHITECTURE.md`
for the module map.

## Golden rules

- **Standard library only at runtime.** No third-party Python packages in
  `src/claude_continue/`. PyInstaller is a *build-time* dev tool only.
- **Target Python 3.9.** Use `from __future__ import annotations`; do **not** use
  3.10+ syntax at runtime (no `match`, no `X | Y` in non-annotation positions,
  no `tomllib`). Config is JSON, not TOML, for this reason.
- **Never crash the daemon.** The watch loop must degrade (log + poll/retry), not
  raise. External effects (clock, sleep, ccusage, the action) are injectable so
  the loop is unit-testable offline.
- **Cross-platform.** Code runs on macOS, Windows, WSL, and Linux. Gate
  OS-specific behavior through `osenv` (which honors `CLAUDE_CONTINUE_PLATFORM`
  for tests). Don't assume POSIX (`os.getuid`, `/bin/sh`, `signal.SIGTERM`-only).
- **Touch Tk only on the main thread.** GUI worker threads mutate plain dicts;
  all widget access happens in `root.after`-driven polls. Keep pure logic in
  module-level functions (e.g. `watch_explanation`, `update_decision`) so it's
  testable without a display.

## Setup, build, test, run

No install step is required — it's stdlib-only.

```bash
# Run from source (no install)
PYTHONPATH=src python3 -m claude_continue.cli --help
./bin/claude-continue status          # bare shim: sets PYTHONPATH for you
.\bin\claude-continue status          # Windows equivalent (bin\claude-continue.cmd)

# Run the full test suite (offline, fast — ~0.4s, ~300 tests)
python3 -m unittest discover -s tests -q

# Run one test module / filter to one case (use discover so tests/_support, which
# sets up sys.path, is importable — the dotted `unittest tests.x` form won't find it)
python3 -m unittest discover -s tests -p "test_watch.py"
python3 -m unittest discover -s tests -p "test_update.py" -k SslContext

# Feed canned ccusage JSON instead of calling the real tool (no Node needed)
CLAUDE_CONTINUE_CCUSAGE_CMD="cat tests/fixtures/active.json" ./bin/claude-continue status

# Force a platform in tests / manual runs
CLAUDE_CONTINUE_PLATFORM=windows ./bin/claude-continue doctor

# Lint + type-check (CI-enforced; tools are dev-only, never runtime deps)
python3 -m pip install ".[dev]"   # ruff + mypy, pinned
ruff check src tests
mypy src/claude_continue

# Build the standalone app (throwaway venv; doesn't touch system Python)
./packaging/build-macos.sh        # -> dist/claude-continue.app   (macOS)
.\packaging\build-windows.ps1     # -> dist/claude-continue.exe   (Windows)
```

**CI gates (all must pass):** tests on **Python 3.9 + 3.12 × {ubuntu, macOS,
windows}**, `ruff check`, `mypy`, and a **gitleaks secret scan** (full history;
`.gitleaks.toml` adds an Anthropic `sk-ant-` rule on top of the defaults — run it
locally with `gitleaks detect --source . --config .gitleaks.toml --redact`). The
3.9 job enforces the "runs on 3.9 / no 3.10+ syntax" rule. Style: 4-space indent, double quotes, `%`-style logging,
comments that explain *why* — `ruff` config (in `pyproject.toml`) deliberately
does **not** enable pyupgrade, so `%`-formatting stays.

## Project layout

```
src/claude_continue/      the package (stdlib only) — see ARCHITECTURE.md
  cli.py                  argparse entrypoint: status|doctor|watch|gui|once|fire|install|uninstall|update
  config.py               Config dataclass + precedence (flags > env > JSON file > defaults)
  watch.py                the self-rescheduling loop (the heart)
  ccusage.py / model.py   read + parse the active 5-hour block
  schedule.py             next-fire-time math
  action.py               what to do at a reset (dispatch)
  iterm.py / tmux.py / winterm.py   the per-platform "resume" mechanisms
  update.py               self-update from GitHub releases (TLS verify + checksum)
  selfremove.py           "remove completely" (agent + config + bundle)
  gui.py                  Tkinter window (lazy-imports tkinter inside run())
  osenv.py                platform detection + detached-process helpers
  launchd.py / tasksched.py / scheduler.py   the unattended agents
tests/                    offline unit tests + fixtures/ (mirror src module names)
packaging/                build-macos.sh, build-windows.ps1, claude_continue_app.py (frozen entrypoint)
templates/                launchd plist template
.github/workflows/        ci.yml (test matrix + builds), release.yml (tag-triggered)
bin/claude-continue(.cmd) no-pip shim (.cmd = the Windows wrapper)
```

## How to add a feature (the loop we follow)

1. Branch from `main` (`feat/…` or `fix/…`).
2. Implement in `src/`, keeping pure logic in testable module-level functions.
3. Add/extend tests in `tests/test_<module>.py`. Keep them **offline** — mock
   subprocesses (ccusage, osascript, tmux, ditto) and inject clock/sleep.
4. `python3 -m unittest discover -s tests -q` must pass.
5. Open a PR; CI (ubuntu/macOS/windows + the two builds) must be green.
6. Review (we run an adversarial review), fix findings, squash-merge.
7. Releases: bump `__version__` in `src/claude_continue/__init__.py` **and**
   `version` in `pyproject.toml`, merge, then `git tag vX.Y.Z && git push --tags`
   — `release.yml` builds and attaches the `.app` zip and `.exe`.

## Conventions

- **Commits:** imperative subject; body explains *why*. End commit messages with
  the `Co-Authored-By: Claude …` trailer; end PR bodies with the
  "Generated with Claude Code" line.
- **Config:** every `Config` field is settable via a `--flag`, the
  `CLAUDE_CONTINUE_<FIELD>` env var, or the JSON config file. Add bool fields to
  `_BOOL_FIELDS` (etc.) in `config.py` and a flag in `cli.add_action_args`; make
  it round-trip through `cli.overrides_to_argv` (so `install` can bake it into
  the agent).
- **Versioning:** keep `__init__.__version__` and `pyproject` version in lockstep;
  update `CHANGELOG.md`.

## Gotchas worth knowing

- ccusage `endTime` is an **estimate**, sometimes early; the watch loop *verifies*
  the window actually rolled after firing and retries (don't trust the estimate
  blindly). See `watch._verify_and_retry`.
- AppleScript reserved words bite inside `tell application "iTerm2"` blocks
  (`tab`, `rows` are iTerm terms; `st` is reserved) — see comments in `iterm.py`.
- The macОS native Tk button ignores `fg`/`bg`; convey color via a **glyph in the
  label** (see `gui.update_button_label`).
- A frozen PyInstaller app's bundled OpenSSL can load **zero CA certs**;
  `update._ssl_context()` falls back to the system CA bundle.
- `packaging/build-windows.ps1` is maintained by the Windows side — coordinate
  before editing it.
