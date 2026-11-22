"""``nightshift init`` and ``nightshift check``."""

from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.markup import escape

if TYPE_CHECKING:
    from nightshift.project_config import ProjectConfig
    from nightshift.wiring import Boundaries

console = Console()


def check() -> None:
    """Validate the setup and report each problem with how to fix it."""
    from nightshift import wiring
    from nightshift.cli import render
    from nightshift.config import SETTINGS
    from nightshift.errors import ConfigError
    from nightshift.project_config import Paths, load_config

    path = Paths.default().config
    try:
        config = load_config(path)
    except ConfigError as e:
        console.print(f"[red]problem[/] {escape(str(path))}")
        console.print(escape(str(e)))
        raise typer.Exit(1) from None
    result = wiring.build(SETTINGS).checks.run(config)
    render.report(console, result)
    raise typer.Exit(1 if result.failed else 0)


def init(
    yes: bool = typer.Option(False, "--yes", help="Rewrite an existing file without asking."),
) -> None:
    """Set up Nightshift on this machine: choose boards and repos, and write the config."""
    from nightshift import wiring
    from nightshift.cli import render
    from nightshift.config import SETTINGS
    from nightshift.errors import ConfigError, NightshiftError
    from nightshift.project_config import Paths, load_config, save_config

    path = Paths.default().config
    current: ProjectConfig | None = None
    if path.exists():
        try:
            current = load_config(path)
        except ConfigError as e:
            console.print(f"The current file is not valid and gives no defaults:\n{escape(str(e))}")

    boundaries = wiring.build(SETTINGS)
    config = _ask(boundaries, current)
    if path.exists() and not yes:
        typer.confirm(f"Rewrite {path}?", abort=True)
    save_config(config, path)
    console.print(f"Wrote {escape(str(path))}")

    result = boundaries.checks.run(config)
    for repo in result.repos:
        if repo.info is None or not repo.info.can_push:
            continue
        for label in wiring.missing_labels(repo.info):
            try:
                boundaries.tracker.create_label(repo.info.slug, label)
                console.print(f"Created label {label} in {repo.info.slug}")
            except NightshiftError as e:
                console.print(f"[red]problem[/] label {label}: {escape(str(e))}")
    result = boundaries.checks.run(config)
    render.report(console, result)
    if result.failed:
        console.print("Fix the problems above, then run `nightshift check`.")
        raise typer.Exit(1)
    console.print("Ready: run `nightshift start`.")


def _ask(boundaries: Boundaries, current: ProjectConfig | None) -> ProjectConfig:
    from nightshift.errors import ConfigError

    while True:
        boards = _ask_boards(boundaries, current)
        repos = _ask_repos(current)
        try:
            return _combine(boards, repos, current)
        except ConfigError as e:
            console.print(f"[red]problem[/] {escape(str(e))}\nTry again.")


def _combine(
    boards: list[tuple[str, int]], repos: list[str], current: ProjectConfig | None
) -> ProjectConfig:
    """The answers as a config, with the optional settings the current file has."""
    from nightshift.project_config import format_duration, parse_config

    entries = []
    for text in repos:
        entry = {"path": text}
        old = current.repo(Path(text).expanduser()) if current else None
        if old is not None:
            entry["stall_timeout"] = format_duration(old.stall_timeout)
            entry["max_run_time"] = format_duration(old.max_run_time)
        entries.append(entry)
    return parse_config(
        {
            "boards": [{"url": url, "max_parallel_agents": limit} for url, limit in boards],
            "repos": entries,
        }
    )


def _ask_boards(boundaries: Boundaries, current: ProjectConfig | None) -> list[tuple[str, int]]:
    from nightshift.errors import NightshiftError
    from nightshift.project_config import DEFAULT_MAX_PARALLEL_AGENTS

    known: list[tuple[str, str]] = []
    try:
        known = boundaries.tracker.list_boards()
    except NightshiftError as e:
        console.print(f"Your GitHub Projects cannot be listed: {escape(str(e))}")
    for i, (url, title) in enumerate(known, 1):
        console.print(f"  {i}. {escape(title)}  [dim]{url}[/]")
    default = ", ".join(b.url for b in current.boards) if current else None
    answer = typer.prompt("Boards (numbers or URLs, separated by commas)", default=default)
    urls = []
    for part in _split(answer):
        number = int(part) if part.isdigit() else 0
        urls.append(known[number - 1][0] if 1 <= number <= len(known) else part)
    boards = []
    for url in urls:
        old = current.board(url) if current else None
        limit = typer.prompt(
            f"Most agents at once for {url}",
            default=old.max_parallel_agents if old else DEFAULT_MAX_PARALLEL_AGENTS,
            type=int,
        )
        boards.append((url, int(limit)))
    return boards


def _ask_repos(current: ProjectConfig | None) -> list[str]:
    default = ", ".join(str(r.path) for r in current.repos) if current else None
    answer = typer.prompt("Repos (paths of local clones, separated by commas)", default=default)
    return _split(answer)


def _split(answer: str) -> list[str]:
    return [part.strip() for part in str(answer).split(",") if part.strip()]
