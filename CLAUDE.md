# CLAUDE.md

Nightshift is a personal software factory for one macOS machine. It polls GitHub Project boards,
takes the highest-ranked `Todo` ticket that has the `nightshift` label, and gives it to a Claude
Code agent in its own git worktree, shown in Herdr. When the agent has opened a pull request,
Nightshift moves the ticket to In Review; when it cannot go on, it flags the ticket as needing a
person. The product spec is `docs/specs/2026-11-21-initial.md`; FR and NFR numbers in the code
refer to it.

## Commands

```bash
uv sync --dev                        # install all dependencies
uv run pre-commit install            # wire up git hooks, incl. commit-msg (once per clone)

uv run pytest                        # run all tests with coverage
uv run pytest tests/cli/             # run a single test directory
uv run pytest -k test_version        # run a single test by name

uv run ruff check                    # lint (src/ and tests/)
uv run ruff check --fix              # lint with auto-fix
uv run ruff format                   # format
uv run mypy                          # type-check (strict, src/ and tests/)

uv run nightshift --help                 # run the CLI

uvx git-cliff --bumped-version       # preview the next release version (needs `git fetch --tags`)
uvx git-cliff --unreleased --strip header   # preview its release notes
```

`.envrc` (direnv, optional) loads `.env` and puts `.venv/bin` on `PATH`, so the commands
above also work without `uv run`. Write commands in docs with `uv run`, which works in
every shell, including CI and agent shells.

## Project structure

`src/nightshift/` is one loop around one pure function: **observe, decide, act**.

- **`cli/`** — the commands, thin: `setup.py` (`init`, `check`), `operate.py` (`start`, `stop`, `status`), `render.py`. Registered in `app.py`.
- **`loop.py`** — polling, backing off, stopping; `lock.py` is the single-instance lock, `logs.py` the log file.
- **`observe.py`** — reads GitHub, git, Herdr and the database into one `Snapshot`.
- **`decide.py`** — the pure function `Snapshot` → actions. Every rule about tickets, runs and agents belongs here. No I/O.
- **`executor.py`** — carries out actions. Every step is safe to repeat after a crash. It re-reads a ticket right before changing it.
- **`cleanup.py`** — whether a worktree is safe to remove (FR-13).
- **`model.py`** — runs, tickets, snapshots and actions as plain data; `conventions.py` the fixed names and times (NFR-2); `naming.py` branch names.
- **`store.py`** — SQLite: runs, and `loop_state` for what `status` shows and the outbox.
- **Boundaries**, one module each: `tracker/github.py`, `herdr.py` (`hc`), `git.py`, `claude.py`, `notify.py`; `proc.py` runs the tools. `wiring.py` puts them together.
- **`checks.py`** — the checks of `init`, `check` and `start`; `project_config.py` is `nightshift.yaml` and the paths under `~/.nightshift`.
- **`config.py`** — frozen `Settings` (pydantic-settings, `NIGHTSHIFT_` env prefix). Calls `load_dotenv()` at import time.

Rules that keep it safe:

- Nothing is concluded from what could not be read: an unreadable ticket, board or terminal changes nothing.
- A state change and what it must tell GitHub (labels, comments) or you are written in one transaction (`Executor._to`); the outbox delivers them afterwards, each comment once.
- Names and IDs are recorded before the thing is created, so a repeated step finds it.
- Anything that closes or removes something goes through `Cleanup.check`; git is never forced.
- Nothing outside `tracker/` may assume that a ticket ID is a number.

Tests: `tests/build.py` builds plain data for `decide`; `tests/world.py` is a whole world (GitHub and Herdr faked, git real, a clock that tests move) that also kills Nightshift at chosen points. Git is never mocked.

Logging is first configured in `__init__.py`: loguru handler removed and re-added with the level from `SETTINGS.log_level`. `start` replaces it with stdout plus `~/.nightshift/logs/nightshift.log`. Set `NIGHTSHIFT_LOG_LEVEL=DEBUG` to see debug output.

`docs/decisions.md` is the decision log (see below).

## Commit Message Convention

This project follows **Conventional Commits** (https://www.conventionalcommits.org).
All commits generated or suggested by Claude must conform to this spec.

### Format
<type>[optional scope]: <description>
[optional body]
[optional footer(s)]

### Allowed types

- feat, fix, docs, style, refactor, perf, test, devops, chore

This list is the source of truth. The same list is repeated in `.pre-commit-config.yaml`
(the commit-msg hook), `cliff.toml` (release-note groups), `.github/copilot-instructions.md`
and `README.md`; change them together.

### Scopes for this project (pick the closest match)

- cli, loop, decide, executor, cleanup, store, tracker, herdr, git, claude, config

### Rules

1. Subject line ≤ 72 characters, lowercase, no trailing period.
2. Use imperative mood: "add feature" not "added feature".
3. Breaking changes: append `!` to the type OR add a `BREAKING CHANGE:` footer.
4. Never squash unrelated changes into one commit — split them.

Pull requests are squash-merged, so a PR's title becomes its commit on `main` and its line
in the release notes: make it a Conventional Commit that reads well to a user.

## Branch naming

Before the first commit of a task, create a branch named `<issue-num>/<short-kebab-description>`, where `<issue-num>` is the number of the GitHub issue the work is for, without `#` (e.g. `42/export-csv`, `57/retry-timeout`). The description comes from the issue title: lowercase, `[a-z0-9-]` only, whole branch under ~40 characters. This is the same naming Nightshift uses for its agents (`docs/specs/2026-11-21-initial.md`). Never commit directly to `main`.

Every branch belongs to an issue. If the task has no issue, find one with `gh issue list` or ask the user for the number; if there is none, create it with `gh issue create` (title and a short body describing the task) once the user agrees, and use its number. The pull request then says `Closes #<issue-num>` in its body.

A branch Nightshift created is already named: work on it, never create or rename one.

Minor fixes and improvements to the agent rules (`CLAUDE.md`, `.claude/`) made while working on a task can go into that task's branch and pull request, in a commit of their own (`docs: ...`), instead of a separate branch. A larger change to how the agent works gets its own branch.

Claude Code cloud sessions start on an auto-generated `ccr-*` branch, and the session's own instructions say to develop and push there. In this repository that branch is always replaced by a named one: before the first commit, create the `<issue-num>/<short-kebab-description>` branch from it, and when the user asks to push, push that branch, never the `ccr-*` one. The user's request to push is permission to push the named branch in its place. If the work was already committed on the `ccr-*` branch, create the named branch at that commit before pushing (the commits come with it).

Never push to the remote (`git push`, or anything that pushes for you, such as creating a pull request from local commits) unless the user asks for it in this session or a skill they ran says to, as `/done` does. A request to push covers that one push, not later commits: commit locally and ask before pushing again. "Merge main into this branch", "commit" or "fix the conflicts" are not requests to push.

`/done` (`.claude/skills/done/SKILL.md`) ships the work in the working tree in one go: branch, commit, push, pull request, wait for CI, squash-merge. It runs only when the user types it, and typing it is the approval to create the branch and merge that pull request.

## Session naming

In a Claude Code cloud session started from an issue or a link, rename the session to a short readable description of the work (e.g. "Add CSV export"), not the issue number or the URL. Do it once the task is understood.

## Key conventions and guidelines

- Dependency injection: pass config explicitly to any client/service — never read the global `SETTINGS` inside library code. Only the CLI commands (and, in an app, the module-level instance built by its factory) read `SETTINGS`; tests build their own `Settings`.
- Settings: give every field a docstring saying what it is and what leaving it unset does; type secrets as `SecretStr` so they never show up in logs or reprs.
- External services: every client of an outside API takes its `httpx` client or transport as a parameter. Tests never call the real service: they pass an `httpx.MockTransport`.
- mypy is strict. All functions including tests need return type annotations.
- Git hooks (pre-commit) run ruff (fix + format) and mypy on every commit, and check the commit message.
- CLI lazy imports: in `cli/app.py` and the group modules (`cli/<group>.py`), `nightshift.*` imports must be inside the command function body, not at module level (the group modules themselves, `nightshift.cli.*`, are the exception). `typer`, `rich`, and stdlib belong at the top of the file. This keeps `--help` fast and isolates heavy deps per command.
- CLI commands that delete or overwrite data ask for confirmation first, with a `--yes` option to skip it.
- Releases are made by the `release.yml` workflow (manual): git-cliff works out the next version from the commits since the last `v*` tag (`feat` raises the minor number, anything else the patch number) and publishes a GitHub release with notes made from those commits, which creates the tag. Never bump a version, push a `v*` tag or write release notes by hand as part of a change. The `version` in `pyproject.toml` and `__version__` are placeholders.
- `docs/decisions.md` is an append-only log of significant decisions, oldest first. Each entry is `## YYYY-MM-DD — <decision as a short statement>` followed by **Context**, **Decision** and **Consequences** paragraphs. Keep an entry compact: about 120 words, one to three sentences per paragraph, covering what was chosen, why, and the trade-offs someone would not guess. Leave out how it is implemented (file names, endpoints, flags, steps); that belongs in the code and the other docs. When you make a significant decision, add an entry in the same change: a new dependency, a change to a data format, API or stored data, a deviation from these conventions, or any hard-to-reverse choice the next person would wonder about. Minor choices belong in the PR description. Never edit past entries; to change a decision, add a new entry that names the one it replaces. This file (`CLAUDE.md`) is the source of truth for current rules; the log explains why.
- Scratch files (drafts, generated options) go in `/temp/`, which is gitignored.
- **After every significant change** — new CLI command, new data pipeline, new dependency, changed directory layout — update `CLAUDE.md` (architecture / conventions) and `README.md` (user-facing instructions, dev setup, release process). Keep them in sync with the actual codebase.
