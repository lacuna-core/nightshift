"""``nightshift start``, ``stop`` and ``status``."""

import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.markup import escape

if TYPE_CHECKING:
    from nightshift.checks import Report
    from nightshift.model import Run
    from nightshift.project_config import Paths, ProjectConfig
    from nightshift.store import Store
    from nightshift.wiring import Boundaries

console = Console()


def _config(paths: Paths) -> ProjectConfig:
    from nightshift.errors import ConfigError
    from nightshift.project_config import load_config

    try:
        return load_config(paths.config)
    except ConfigError as e:
        console.print(f"[red]problem[/] {escape(str(paths.config))}\n{escape(str(e))}")
        raise typer.Exit(1) from None


def start(
    yes: bool = typer.Option(
        False, "--yes", help="Cancel runs of boards and repos that are no longer listed."
    ),
) -> None:
    """Start the loop, in the foreground."""
    import signal

    from nightshift import wiring
    from nightshift.cli import render
    from nightshift.config import SETTINGS
    from nightshift.errors import NightshiftError
    from nightshift.lock import Lock
    from nightshift.logs import configure
    from nightshift.project_config import Paths
    from nightshift.store import Store

    paths = Paths.default()
    config = _config(paths)
    lock = Lock(paths.lock)
    try:
        lock.acquire()
    except NightshiftError as e:
        console.print(escape(str(e)))
        raise typer.Exit(1) from None
    try:
        configure(paths.log, SETTINGS.log_level, stdout=sys.stdout)
        boundaries = wiring.build(SETTINGS)
        report = boundaries.checks.run(config)
        render.report(console, report)
        if report.fatal:
            console.print("Nightshift does not start until the problems above are fixed.")
            raise typer.Exit(1)
        try:
            store = Store(paths.database)
        except NightshiftError as e:
            console.print(escape(str(e)))
            raise typer.Exit(1) from None

        unlisted = [r for r in wiring.unlisted_runs(config, report, store) if r.live]
        if unlisted:
            console.print("The board or repo of these live runs is no longer listed:")
            for run in unlisted:
                console.print(f"  {escape(run.repo)} #{run.ticket_id}  {run.board_url}")
            if not yes and not typer.confirm(
                "Cancel them? Their tabs, worktrees and branches are kept.", default=False
            ):
                console.print("Not started. List them again, or answer yes.")
                raise typer.Exit(1)

        stop = threading.Event()
        for number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(number, lambda *_: stop.set())

        loop = wiring.build_loop(boundaries, config, store, paths, stop)
        try:
            for run in unlisted:
                loop.executor.perform(wiring.cancel_unlisted(run))
        except NightshiftError as e:
            console.print(f"The runs cannot be cancelled now: {escape(str(e))}")
            raise typer.Exit(1) from None
        _prepare(boundaries, report, store)
        loop.run()
        store.close()
    finally:
        lock.release()


def _prepare(boundaries: Boundaries, report: Report, store: Store) -> None:
    """Missing labels and repo spaces, and links to the worktrees of moved clones."""
    from loguru import logger

    from nightshift import wiring
    from nightshift.errors import NightshiftError

    for repo in report.repos:
        if repo.info is None or not repo.usable:
            continue
        try:
            for label in wiring.missing_labels(repo.info):
                boundaries.tracker.create_label(repo.info.slug, label)
            wiring.follow_clone(boundaries.git, store, repo.info.ref, repo.path)
            boundaries.herdr.ensure_repo_space(repo.path)
        except NightshiftError as e:
            logger.error("{}: {}", repo.info.slug, e)


def stop() -> None:
    """Stop the loop. Agents keep running in Herdr."""
    import os
    import signal
    import time

    from nightshift.lock import holder
    from nightshift.project_config import Paths

    lock = Paths.default().lock
    pid = holder(lock)
    if pid is None:
        console.print("Nightshift is not running.")
        return
    if not pid:
        console.print("Nightshift is just starting. Try again.")
        raise typer.Exit(1)
    os.kill(pid, signal.SIGTERM)
    console.print("Stopping: the loop finishes the step it is in.")
    while holder(lock) == pid:
        time.sleep(0.2)
    console.print("Stopped. Agents keep running in Herdr.")


def status() -> None:
    """Show what Nightshift is doing and what waits for you."""
    from nightshift.lock import holder
    from nightshift.project_config import Paths
    from nightshift.store import Store

    paths = Paths.default()
    config = _config(paths)
    pid = holder(paths.lock)
    console.print("Loop       " + ("not running" if pid is None else f"running (process {pid})"))
    if not paths.database.exists():
        return
    store = Store(paths.database, readonly=True)
    try:
        _status(store, config)
    finally:
        store.close()


def _status(store: Store, config: ProjectConfig) -> None:
    from nightshift.model import RunState

    runs = store.runs()
    report: dict[str, Any] = store.get("report", {})
    if report:
        console.print(f"Last poll  {report['at'][:19].replace('T', ' ')} UTC")
    for board in config.boards:
        used = sum(r.live and r.board_url == board.url for r in runs)
        free = max(board.max_parallel_agents - used, 0)
        console.print(f"Board      {board.url}: {free} of {board.max_parallel_agents} slots free")
    for name, text in report.get("problems", {}).items():
        console.print(f"[red]Problem[/]    {escape(name)}: {escape(text)}")
    for ticket in report.get("unlisted", []):
        console.print(
            f"[yellow]Not listed[/] {escape(ticket)} is ready, but its repo is not listed"
        )

    shown = [r for r in runs if r.state is not RunState.DISCARDED and not r.cleaned_up_at]
    for repo in sorted({r.repo for r in shown}):
        console.print(f"\n[bold]{escape(repo)}[/]")
        for run in (r for r in shown if r.repo == repo):
            _status_run(run, store)
    paths = {r.resolved for r in config.repos}
    unwatched = [
        r
        for r in shown
        if not r.live
        and (config.board(r.board_url) is None or Path(r.clone_path).resolve() not in paths)
    ]
    if unwatched:
        console.print("\n[bold]Not watched: their board or repo is no longer listed[/]")
        for run in unwatched:
            console.print(f"  {escape(run.repo)} #{run.ticket_id} {escape(run.branch or '')}")
    waiting = [r for r in shown if r.state is RunState.WAITING]
    if waiting:
        console.print("\n[bold]Waiting for you[/]")
        for run in waiting:
            console.print(
                f"  {escape(run.repo)} #{run.ticket_id}: {escape(run.state_reason or '')}"
            )
    kept = [r for r in runs if r.kept_reason and not r.cleaned_up_at]
    if kept:
        console.print("\n[bold]Kept worktrees[/]")
        for run in kept:
            console.print(f"  {escape(run.worktree_path or '')}: {escape(run.kept_reason or '')}")


def _status_run(run: Run, store: Store) -> None:
    from nightshift.model import RunState

    colour = {RunState.WAITING: "yellow", RunState.FAILED: "red"}.get(run.state, "default")
    text = f"  #{run.ticket_id:<6} [{colour}]{run.state.value:<17}[/] {escape(run.branch or '')}"
    if run.state_reason:
        text += f"  [dim]{escape(run.state_reason)}[/]"
    console.print(text)
    if failed := store.get(f"failed:{run.id}"):
        console.print(f"          [red]repeating[/] {escape(failed)}")
