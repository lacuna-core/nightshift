"""Puts the boundaries together for the CLI."""

from dataclasses import dataclass
from pathlib import Path

import httpx

from nightshift import proc
from nightshift.checks import Checks
from nightshift.claude import Claude
from nightshift.config import Settings
from nightshift.conventions import LABELS
from nightshift.errors import ToolError, TrackerAuthError
from nightshift.git import Git
from nightshift.herdr import Herdr
from nightshift.model import RepoInfo
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
    )
