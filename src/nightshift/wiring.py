"""Puts the boundaries together for the CLI."""

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from nightshift import proc
from nightshift.checks import Checks, Report
from nightshift.claude import Claude
from nightshift.cleanup import Cleanup
from nightshift.config import Settings
from nightshift.conventions import LABELS
from nightshift.errors import ToolError, TrackerAuthError
from nightshift.executor import Executor
from nightshift.git import Git
from nightshift.herdr import Herdr
from nightshift.loop import Loop, Stop
from nightshift.model import End, RepoInfo, Run, RunState
from nightshift.notify import Notifier
from nightshift.observe import Observer
from nightshift.project_config import Paths, ProjectConfig
from nightshift.store import Store
from nightshift.tracker.github import GitHub


def gh_token(run: proc.Runner) -> str:
    try:
        result = run(["gh", "auth", "token", "--hostname", "github.com"])
    except ToolError as e:
        raise TrackerAuthError(str(e)) from e
    if result.code != 0 or not result.out.strip():
        raise TrackerAuthError("`gh` is not logged in")
    return result.out.strip()


def missing_labels(info: RepoInfo) -> list[str]:
    return [label for label in LABELS if label not in info.labels]


@dataclass(frozen=True)
class Boundaries:
    tracker: GitHub
    herdr: Herdr
    claude: Claude
    git: Git
    notifier: Notifier

    @property
    def checks(self) -> Checks:
        return Checks(self.tracker, self.herdr, self.claude, self.git)


def build(
    settings: Settings,
    *,
    run: proc.Runner = proc.run,
    transport: httpx.BaseTransport | None = None,
    home: Path | None = None,
) -> Boundaries:
    http = httpx.Client(timeout=settings.request_timeout, transport=transport)
    return Boundaries(
        tracker=GitHub(http, lambda: gh_token(run)),
        herdr=Herdr(run),
        claude=Claude(run, home or Path.home()),
        git=Git(run),
        notifier=Notifier(run),
    )


def build_loop(
    boundaries: Boundaries, config: ProjectConfig, store: Store, paths: Paths, stop: Stop
) -> Loop:
    def now() -> datetime:
        return datetime.now(UTC)

    observer = Observer(
        config=config,
        store=store,
        tracker=boundaries.tracker,
        herdr=boundaries.herdr,
        claude=boundaries.claude,
        git=boundaries.git,
        now=now,
        monotonic=time.monotonic,
    )
    executor = Executor(
        store=store,
        tracker=boundaries.tracker,
        herdr=boundaries.herdr,
        git=boundaries.git,
        claude=boundaries.claude,
        cleanup=Cleanup(boundaries.herdr, boundaries.claude, boundaries.git),
        notifier=boundaries.notifier,
        paths=paths,
        repos=observer.repo,
        now=now,
        sleep=time.sleep,
    )
    return Loop(store=store, observer=observer, executor=executor, stop=stop, now=now)


def follow_clone(git: Git, store: Store, repo_ref: str, path: Path) -> None:
    """If the clone was moved, point its runs and their worktrees at the new place."""
    moved = [
        run
        for run in store.runs()
        if run.repo_ref == repo_ref and Path(run.clone_path) != path and not run.cleaned_up_at
    ]
    if not moved:
        return
    git.repair(path, [Path(run.worktree_path) for run in moved if run.worktree_path])
    for run in moved:
        store.update_run(run, clone_path=str(path))


def unlisted_runs(config: ProjectConfig, report: Report, store: Store) -> list[Run]:
    """Runs whose board or repo is no longer in the config. A broken one counts as listed."""
    refs = {r.info.ref for r in report.repos if r.info}
    unknown = {r.path.resolve() for r in report.repos if r.info is None}
    if report.unavailable:
        unknown = {r.resolved for r in config.repos}
    return [
        run
        for run in store.runs()
        if not run.cleaned_up_at
        and run.state is not RunState.DISCARDED
        and (
            config.board(run.board_url) is None
            or (run.repo_ref not in refs and Path(run.clone_path).resolve() not in unknown)
        )
    ]


def cancel_unlisted(run: Run) -> End:
    return End(
        run,
        RunState.CANCELLED,
        "unlisted",
        "Its board or repository is no longer listed in Nightshift's config.",
        interrupt=True,
    )
