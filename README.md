# nightshift

Personal dark factory

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — Python package manager
- [direnv](https://direnv.net/) — loads `.env` and the virtualenv's tools when
  you `cd` into the repo (`direnv allow` once)

## Install

```bash
git clone <repo-url> your_project_name
cd your_project_name
uv sync --dev
uv run pre-commit install   # ruff + mypy on every commit, and a commit message check
```

## Configure

Copy `.env.example` to `.env` and set values:

```bash
cp .env.example .env
```

| Variable | Default | Description |
|----------|---------|-------------|
| `NIGHTSHIFT_LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## Usage

```bash
uv run nightshift --help
```

## Development

### Running tests

```bash
uv run pytest                        # all tests with coverage
uv run pytest tests/cli/             # single directory
uv run pytest -k test_version        # single test
```

### Lint and type-check

```bash
uv run ruff check --fix   # lint + auto-fix
uv run ruff format        # format
uv run mypy               # strict type-check
```

### Project structure

`src/nightshift/` is layered:

- **`config.py`** — pydantic-settings with `NIGHTSHIFT_` env prefix
- **`cli/app.py`** — Typer CLI; add sub-commands as `cli/<name>.py` and register via `app.add_typer(...)`

Docs:

- **`docs/decisions.md`** — append-only log of significant decisions and why they were made
- **`docs/changes/`** — dated historical records (build prompts, plans)

See `CLAUDE.md` for coding conventions. `AGENTS.md` points other coding agents to it.

### Commit conventions

This project uses [Conventional Commits](https://www.conventionalcommits.org):

```
<type>[optional scope]: <description>
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `devops`, `chore`

Subject line ≤ 72 chars, lowercase, imperative mood, no trailing period. Pull requests
are squash-merged, so a PR's title becomes its commit on `main` and its line in the
release notes.

### Claude Code

- `/done` ships the working tree: branch, commit, push, pull request, wait for CI,
  squash-merge (`.claude/skills/done/SKILL.md`).
- `.claude/statusline.sh` shows the model, context window use and session cost.

## Releases

A release is published by the **Release** workflow, started by hand from the Actions tab
on `main`. It creates a [GitHub release](../../releases) with the changes since the
previous one. There is nothing to bump, tag or write by hand.

- **Version**: the last `v*` tag, raised by [git-cliff](https://git-cliff.org/) from the
  commits since then: a `feat` raises the minor number (`v0.2.0` → `v0.3.0`), anything
  else the patch number (`v0.2.0` → `v0.2.1`). Below 1.0 a breaking change (`feat!`)
  raises the minor number too. The first release is `v0.1.0`.
- **Notes**: one line per commit on `main`, grouped into Features, Bug Fixes,
  Performance, Other and Maintenance (`cliff.toml`). A title that is not a
  Conventional Commit lands under Other.
- **Tag**: created by the release, on the released commit.
- Running it again without new commits publishes nothing.

To preview the next version and its notes (needs the tags: `git fetch --tags`):

```bash
uvx git-cliff --bumped-version
uvx git-cliff --unreleased --strip header
```

To go to 1.0, or to pick any version by hand, tag the commit yourself
(`git tag v1.0.0 <commit> && git push origin v1.0.0`); later versions count from that
tag. The `version` in `pyproject.toml` and `nightshift version` are placeholders and stay as
they are.
