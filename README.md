# Nightshift

A personal software factory. Nightshift watches your GitHub Project boards, takes the
highest-ranked ready ticket, and gives it to a Claude Code agent in its own git worktree, which
opens a pull request. You watch and step in through [Herdr](https://github.com/herdrdev/herdr-app).

Label tickets `nightshift`, rank them in the Todo column, start Nightshift and walk away. When you
come back, each ticket is In Review with a pull request, or flagged `nightshift:needs-human` with a
comment saying why.

## Requirements

- macOS, with [uv](https://docs.astral.sh/uv/), git and [gh](https://cli.github.com/)
- `gh` logged in with the `project` scope: `gh auth refresh -h github.com -s project`
- [herdr-app](https://github.com/herdrdev/herdr-app) with herdr-core 0.1.18 or a later 0.1 (`hc`
  on `PATH`)
- [Claude Code](https://claude.com/claude-code), logged in
- A GitHub Project whose Status field has `Todo`, `In Progress` and `In Review`
- Local clones of the repos. In each:
  - run `claude` once and accept the trust dialog;
  - allow at least `git commit`, `git push` and `gh pr create` in `.claude/settings.json`,
    committed to the repo. Agents run in `acceptEdits` mode; a permission prompt nobody
    answers makes the ticket wait for you.

## Install and set up

```bash
uv tool install git+https://github.com/lacuna-core/nightshift
nightshift init     # choose boards and repos; writes ~/.nightshift/nightshift.yaml
nightshift check    # what is missing, and how to fix it
nightshift start    # runs in the foreground
```

```yaml
# ~/.nightshift/nightshift.yaml
boards:
  - url: https://github.com/users/<you>/projects/<n>
    max_parallel_agents: 2    # optional, default 5
repos:
  - path: ~/dev/webapp
    stall_timeout: 40m        # optional, default 20m
    max_run_time: 5h          # optional, default 3h
```

Changes take effect when Nightshift restarts. `NIGHTSHIFT_LOG_LEVEL` (default `INFO`) sets the
log level.

## Use

| You | Nightshift |
|-----|------------|
| Add the `nightshift` label to a Todo ticket | Picks it when its rank comes up and a slot is free |
| Answer or approve in the agent's tab | Goes on, and removes `nightshift:needs-human` |
| Move a ticket back to Todo | Starts over: closes the old PR, keeps its branch, makes a new one |
| Remove the `nightshift` label | Stops the agent and leaves everything to you |
| Merge the PR | Removes the worktree and the local branch, when nothing can be lost |
| Move the ticket elsewhere, or close the issue or the PR | Stops the agent and keeps everything |

- `nightshift status` shows runs, free slots, problems, kept worktrees and what waits for you.
- `nightshift stop` (or Ctrl-C) stops the loop. Agents keep running, and the next `start` picks
  them up again.
- Nightshift and its agents never merge and never close issues.
- **In its first week Nightshift removes nothing.** It logs what it would remove, and `status`
  shows it. The week starts with the first worktree it could remove.

State is in `~/.nightshift/nightshift.db`, logs are in `~/.nightshift/logs/`, worktrees in
`~/.nightshift/worktrees/`. The full behaviour is in the
[product spec](docs/specs/2026-11-21-initial.md).

## Development

[direnv](https://direnv.net/) is optional: it loads `.env` and the virtualenv's tools.

```bash
uv sync --dev
uv run pre-commit install   # ruff + mypy on every commit, and a commit message check
uv run nightshift --help
```

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

`src/nightshift/` observes (`observe.py`), decides in one pure function (`decide.py`) and acts
(`executor.py`). Each outside tool has one module: `tracker/github.py`, `herdr.py`, `git.py`,
`claude.py`, `notify.py`. The rest of the map is in `CLAUDE.md`.

Docs:

- **`docs/design.md`** — how Nightshift is built, with diagrams; read this first
- **`docs/decisions.md`** — append-only log of significant decisions and why they were made

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
