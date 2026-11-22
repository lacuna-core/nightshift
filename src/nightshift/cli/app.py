import typer
from rich.console import Console

from nightshift.cli import operate, setup

app = typer.Typer(
    help="Nightshift: hands ready tickets to Claude Code agents.", no_args_is_help=True
)
console = Console()

app.command()(setup.init)
app.command()(setup.check)
app.command()(operate.start)
app.command()(operate.stop)
app.command()(operate.status)


@app.command()
def version() -> None:
    """Print the current version."""
    from nightshift import __version__

    console.print(__version__)
