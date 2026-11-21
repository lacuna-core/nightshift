---
name: done
description: Ship the work in the working tree - create a branch, commit, push, open a pull request, wait for CI and squash-merge it into main. Run only when the user types /done.
disable-model-invocation: true
argument-hint: "[optional note for the commit or PR]"
---

# /done: ship the current work

Typing `/done` is the user's explicit request to branch, commit, push, open a pull request
and merge it once CI passes. Do all of it without asking for confirmation at each step.
That approval covers this one run only. Stop and report instead of improvising when a
step below says to stop.

`$ARGUMENTS`, if given, is a note from the user about the change: use it for the commit
and PR wording.

## 1. Look at what is there

Run `git status --short`, `git diff HEAD --stat` and `git branch --show-current`, and
read the diff if the changes were not made in this conversation.

- Nothing to commit and nothing unpushed: say so and stop.
- Leave out files that are not part of the work (stray untracked files, local config,
  anything that looks like a secret) and name them in the final report. If it is unclear
  whether a file belongs, ask once.

## 2. Branch

Follow "Branch naming" in `CLAUDE.md`: `<type>/<short-kebab-description>`.

- On `main`, or on an auto-generated branch such as `ccr-*`: create the named branch from
  the current state (`git switch -c ...`). Uncommitted changes come along.
- Already on a suitable branch whose pull request is not merged: stay on it.
- On a branch whose pull request is already merged: create a new branch from it.

## 3. Check, then commit

Run the checks for what changed before committing, and fix what they find:

- Python (`src/`, `tests/`): `uv run ruff check`, `uv run ruff format --check`,
  `uv run mypy`, `uv run pytest`.
- Anything else the project has added (a frontend, migrations): the checks listed for
  it in `CLAUDE.md`.

Skip a check that already passed in this conversation on the same files. If a check fails
and the fix is not obvious, stop and report.

Commit with Conventional Commits as in `CLAUDE.md`. Unrelated changes go into separate
commits. Stage files by name, not `git add -A`. If the pre-commit hooks change files,
stage them again and repeat the commit; never use `--no-verify`.

## 4. Push and open the pull request

```bash
git push -u origin HEAD
```

If the branch has no open pull request, create one against `main` with `gh pr create`.
The title is a Conventional Commits subject (it becomes the squash commit on `main`). The
body says what changed and why, how it was tested, and anything a release or deploy
needs (a migration, a new setting, a command to run). If one is already open, update its title and
body when the new commits change what it does.

## 5. Wait for CI

```bash
gh pr checks --watch --fail-fast
```

- A check fails: read the log (`gh run view --log-failed`), fix it, commit, push and
  watch again. After two failed rounds, or a failure that is not caused by this change,
  stop and report with the failing output.
- The branch conflicts with `main`: merge `origin/main` into it, resolve, push and watch
  again. Stop and ask if a conflict is not clearly resolvable.

## 6. Merge

Only when every check has passed:

```bash
gh pr merge --squash --delete-branch
```

Then bring the local repository up to date:

```bash
git switch main && git pull --ff-only
```

Delete the local branch if `gh` left it behind. Do not release or deploy: those
workflows are started by hand.

## 7. Report

One short message: the branch, the commits, the pull request link, the CI result and
whether it is merged. Mention files left out in step 1, and anything the next release
or deploy needs.
