"""How reports look in the terminal."""

from typing import TYPE_CHECKING

from rich.console import Console
from rich.markup import escape

if TYPE_CHECKING:
    from nightshift.checks import Finding, Report

_MARKS = {"ok": "[green]ok[/]     ", "warning": "[yellow]warning[/]", "problem": "[red]problem[/]"}


def _finding(console: Console, finding: Finding, indent: str) -> None:
    console.print(f"{indent}{_MARKS[finding.level.value]} {escape(finding.text)}")
    if finding.fix:
        console.print(f"{indent}        [dim]{escape(finding.fix)}[/]")


def report(console: Console, result: Report) -> None:
    console.print("[bold]Tools[/]")
    for finding in result.tools:
        _finding(console, finding, "  ")
    for repo in result.repos:
        name = f" ({repo.info.slug})" if repo.info else ""
        console.print(f"[bold]Repo[/] {escape(str(repo.path))}{escape(name)}")
        for finding in repo.findings:
            _finding(console, finding, "  ")
        if not repo.findings:
            console.print("  [green]ok[/]")
    for board in result.boards:
        console.print(f"[bold]Board[/] {board.url}")
        if board.overview is not None:
            console.print(f"  Statuses   {escape(', '.join(board.overview.statuses) or 'none')}")
        for finding in board.findings:
            _finding(console, finding, "             ")
        rows = board.overview.issues.items() if board.overview else []
        width = max((len(slug) for slug, _ in rows), default=0)
        digits = max((len(str(c.open)) for _, c in rows), default=1)
        for i, (slug, count) in enumerate(rows):
            listed = slug in result.listed
            colour, word = ("green", "listed") if listed else ("red", "not listed")
            head = "Issues" if i == 0 else ""
            console.print(
                f"  {head:<10} [{colour}]{slug:<{width}}  {count.open:>{digits}} open, "
                f"{count.labelled} nightshift, {count.assigned} assigned to {result.user}"
                f"   {word}[/]"
            )
    if result.unavailable:
        console.print("GitHub cannot be reached, so boards and repos were not checked.")
