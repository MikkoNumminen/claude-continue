# Contributing

Thanks for hacking on `claude-continue`. It's intentionally small and
dependency-free; these notes keep it that way. See `AGENTS.md` for the rules of
the road and `ARCHITECTURE.md` for the module map.

## Dev setup

There's nothing to install — the package is **standard-library only** and
targets **Python 3.9+**.

```bash
git clone https://github.com/MikkoNumminen/claude-continue
cd claude-continue
PYTHONPATH=src python3 -m claude_continue.cli --help   # or: ./bin/claude-continue --help
# Windows: .\bin\claude-continue --help                # same command, cmd or PowerShell
```

## Running tests

```bash
python3 -m unittest discover -s tests -q                       # all (~300, offline, ~0.4s)
python3 -m unittest discover -s tests -p "test_watch.py"       # one module
python3 -m unittest discover -s tests -p "test_update.py" -k SslContext   # filter to one case
```

Tests must stay **offline and fast**. Don't call real `ccusage`, `osascript`,
`tmux`, `ditto`, or the network:

- Feed canned ccusage JSON with `CLAUDE_CONTINUE_CCUSAGE_CMD="cat tests/fixtures/active.json"`.
- Inject `clock`/`sleep`/`get_block`/`perform`/`stop` into `watch.run`.
- Force the OS with `CLAUDE_CONTINUE_PLATFORM=windows|macos|wsl|linux`.
- Mock `subprocess.run`/`Popen` for anything that shells out.

## Lint & type-check

CI runs (and you should too, before pushing):

```bash
python3 -m pip install ".[dev]"   # ruff + mypy (pinned; dev-only, not runtime deps)
ruff check src tests
mypy src/claude_continue
```

## Style

- 4-space indent, double quotes, `%`-style logging args — enforced by `ruff`
  (config in `pyproject.toml`). pyupgrade (`UP`) is intentionally **off** so the
  `%`-formatting idiom stays; the "no 3.10+ syntax" rule is enforced by the
  Python 3.9 CI job, not by a linter.
- `from __future__ import annotations` at the top of every module; **no 3.10+
  runtime syntax** (it must import on Python 3.9).
- Comments and docstrings explain **why**, not what. Keep pure logic in
  module-level functions so it can be unit-tested without a display or a subprocess.

## Adding a config option

1. Add the field to the `Config` dataclass in `config.py` (and to `_BOOL_FIELDS`
   / `_INT_FIELDS` / … if it needs coercion from env/JSON).
2. Add a `--flag` in `cli.add_action_args`.
3. Make it round-trip in `cli.overrides_to_argv` (so `install` bakes it into the
   unattended agent's command).
4. Surface it where relevant: `doctor._check_config`, `gui.watch_explanation`, README.
5. Add tests covering the flag round-trip and the behavior.

## Pull requests

- Branch from `main` (`feat/…`, `fix/…`, `docs/…`).
- Keep the suite green and CI green (ubuntu/macOS/windows tests + the `.app`/`.exe`
  builds).
- Commit subject in the imperative; body explains the why. End commit messages
  with `Co-Authored-By: …` and PR bodies with the Claude Code line.
- Squash-merge.

## Cutting a release

1. Bump the version in **both** `src/claude_continue/__init__.py` (`__version__`)
   and `pyproject.toml` (`version`).
2. Add a `CHANGELOG.md` entry.
3. Merge to `main`, then:
   ```bash
   git tag -a vX.Y.Z -m "claude-continue vX.Y.Z" && git push origin vX.Y.Z
   ```
   `release.yml` builds the macOS `.app` zip and the Windows one-dir zip and
   attaches them to the GitHub release. The in-app **Update** button picks it up
   from there.
