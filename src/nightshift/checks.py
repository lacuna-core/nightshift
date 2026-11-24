"""The checks shared by ``init``, ``check`` and ``start`` (spec FR-14). They only read."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from nightshift.claude import Claude
from nightshift.conventions import HERDR_TESTED, LABELS, REQUIRED_STATUSES
from nightshift.errors import (
    NightshiftError,
    ToolError,
    TrackerAuthError,
    TrackerNotFoundError,
    TrackerUnavailableError,
)
from nightshift.git import Git
from nightshift.herdr import Herdr, is_supported
from nightshift.homes import Homes
from nightshift.model import BoardOverview, RepoInfo
from nightshift.project_config import ProjectConfig
from nightshift.tracker.github import slug_of_remote


class Level(StrEnum):
    OK = "ok"
    WARN = "warning"
    FAIL = "problem"


@dataclass(frozen=True)
class Finding:
    level: Level
    text: str
    fix: str = ""


@dataclass(frozen=True)
class RepoReport:
    path: Path
    findings: tuple[Finding, ...]
    info: RepoInfo | None = None

    @property
    def usable(self) -> bool:
        return self.info is not None and all(f.level is not Level.FAIL for f in self.findings)


@dataclass(frozen=True)
class BoardReport:
    url: str
    findings: tuple[Finding, ...]
    overview: BoardOverview | None = None

    @property
    def missing(self) -> tuple[str, ...]:
        have = self.overview.statuses if self.overview else ()
        return tuple(s for s in REQUIRED_STATUSES if s not in have)

    @property
    def usable(self) -> bool:
        return self.overview is not None and all(f.level is not Level.FAIL for f in self.findings)


@dataclass(frozen=True)
class Report:
    tools: tuple[Finding, ...] = ()
    repos: tuple[RepoReport, ...] = ()
    boards: tuple[BoardReport, ...] = ()
    unavailable: bool = False
    """GitHub could not be reached, so repos and boards could not be checked."""

    listed: frozenset[str] = field(default_factory=frozenset)
    user: str = ""

    @property
    def fatal(self) -> bool:
        """Something that concerns everything is missing: ``start`` refuses."""
        return any(f.level is Level.FAIL for f in self.tools)

    @property
    def failed(self) -> bool:
        return (
            self.fatal
            or self.unavailable
            or not all(r.usable for r in self.repos)
            or not all(b.usable for b in self.boards)
        )


class SetupTracker(Protocol):
    def login(self) -> tuple[str, frozenset[str] | None]: ...
    def user_exists(self, login: str) -> bool: ...
    def repo_info(self, slug: str) -> RepoInfo: ...
    def board_overview(self, url: str, user: str = "") -> BoardOverview: ...


class Checks:
    def __init__(
        self,
        tracker: SetupTracker,
        herdr: Herdr,
        claude: Claude,
        git: Git,
        homes: Homes | None = None,
    ) -> None:
        self._tracker = tracker
        self._herdr = herdr
        self._claude = claude
        self._git = git
        self._homes = homes

    def run(self, config: ProjectConfig) -> Report:
        tools = [self._herdr_check(), self._claude_check()]
        github, available, login = self._github_check()
        tools.append(github)
        if github.level is Level.FAIL or not available:
            return Report(tuple(tools), unavailable=not available)
        tools.append(self._user_check(config.user, login))
        repos = self._repos(config)
        elsewhere = self._homes.boards_elsewhere() if self._homes else {}
        return Report(
            tuple(tools),
            repos,
            tuple(self._shared(self._board(b.url, config.user), elsewhere) for b in config.boards),
            user=config.user,
            listed=frozenset(r.info.slug for r in repos if r.info),
        )

    # --- tools ------------------------------------------------------------------------

    def _herdr_check(self) -> Finding:
        try:
            version = self._herdr.version()
        except NightshiftError as e:
            return Finding(
                Level.FAIL, f"Herdr: {e}", "Install herdr-app and make sure `hc` is on PATH."
            )
        if not is_supported(version):
            return Finding(
                Level.FAIL,
                f"Herdr core {version} is not supported",
                f"Nightshift is tested with {HERDR_TESTED}; run `hc update`, "
                "or install a 0.1 version.",
            )
        return Finding(Level.OK, f"Herdr core {version}")

    def _claude_check(self) -> Finding:
        try:
            return Finding(Level.OK, f"Claude Code {self._claude.version()}")
        except NightshiftError as e:
            return Finding(
                Level.FAIL, f"Claude Code: {e}", "Install Claude Code and log in with `claude`."
            )

    def _github_check(self) -> tuple[Finding, bool, str]:
        try:
            login, scopes = self._tracker.login()
        except TrackerUnavailableError as e:
            return Finding(Level.WARN, f"GitHub: {e}", "Try again later."), False, ""
        except ToolError as e:
            return Finding(Level.FAIL, f"GitHub: {e}", "Install the GitHub CLI, `gh`."), True, ""
        except NightshiftError as e:
            return (
                Finding(
                    Level.FAIL, f"GitHub: {e}", "Run `gh auth login -h github.com -s project`."
                ),
                True,
                "",
            )
        if scopes is not None and not {"project", "repo"} <= scopes:
            missing = ", ".join(sorted({"project", "repo"} - scopes))
            return (
                Finding(
                    Level.FAIL,
                    f"GitHub: the login of {login} lacks the scope {missing}",
                    "Run `gh auth refresh -h github.com -s project,repo`.",
                ),
                True,
                login,
            )
        return Finding(Level.OK, f"GitHub as {login}"), True, login

    def _user_check(self, user: str, login: str) -> Finding:
        """Only tickets assigned to ``user`` are picked up; agents act as the ``gh`` login."""
        try:
            exists = self._tracker.user_exists(user)
        except NightshiftError as e:
            return Finding(Level.WARN, f"user {user}: {e}", "Try again later.")
        if not exists:
            return Finding(
                Level.FAIL,
                f"user {user} does not exist on GitHub",
                "Set `user` in nightshift.yaml to your GitHub login.",
            )
        if login and user.lower() != login.lower():
            return Finding(
                Level.WARN,
                f"picks up tickets assigned to {user}, but agents act as {login}",
                "Set `user` to your `gh` login, or log `gh` in as that user.",
            )
        return Finding(Level.OK, f"picks up tickets assigned to {user}")

    # --- repos ------------------------------------------------------------------------

    def _repos(self, config: ProjectConfig) -> tuple[RepoReport, ...]:
        reports: list[RepoReport] = []
        seen: dict[str, Path] = {}
        for repo in config.repos:
            report = self._guard_repo(repo.path)
            if report.info is not None:
                first = seen.setdefault(report.info.ref, repo.path)
                if first != repo.path:
                    double = Finding(
                        Level.FAIL,
                        f"{report.info.slug} is listed twice: also as {first}",
                        "Remove one of the two from nightshift.yaml.",
                    )
                    report = RepoReport(repo.path, (*report.findings, double), report.info)
            reports.append(report)
        return tuple(reports)

    def _guard_repo(self, path: Path) -> RepoReport:
        try:
            return self._repo(path)
        except NightshiftError as e:
            return RepoReport(path, (Finding(Level.FAIL, str(e), "See the message."),))

    def _repo(self, path: Path) -> RepoReport:
        def failed(text: str, fix: str) -> RepoReport:
            return RepoReport(path, (Finding(Level.FAIL, text, fix),))

        if not self._git.is_clone(path):
            return failed(
                "is not the folder of a git clone",
                "Clone the repo there, or correct the path in nightshift.yaml.",
            )
        origin = self._git.origin(path)
        slug = slug_of_remote(origin) if origin else None
        if slug is None:
            return failed(
                f"its `origin` is not a GitHub repo ({origin or 'there is none'})",
                "Set it with `git remote set-url origin ...`.",
            )
        try:
            info = self._tracker.repo_info(slug)
        except TrackerNotFoundError:
            return failed(
                f"{slug} does not exist on GitHub, or you cannot see it",
                "Check the `origin` remote and the `gh` login.",
            )
        findings: list[Finding] = []
        if not info.can_push:
            findings.append(
                Finding(Level.FAIL, f"you cannot push to {info.slug}", "Ask for write access.")
            )
        if not info.default_branch:
            findings.append(
                Finding(Level.FAIL, f"{info.slug} has no commits yet", "Push a first commit.")
            )
        for label in LABELS:
            if label not in info.labels:
                findings.append(
                    Finding(
                        Level.WARN,
                        f"label {label} is missing",
                        "`nightshift init` and `nightshift start` create it.",
                    )
                )
        if tools := self._claude.missing_tools(path):
            findings.append(
                Finding(
                    Level.WARN,
                    f".claude/settings.json does not allow {', '.join(tools)}: "
                    "every agent would stop and wait for you",
                    "Add them to permissions.allow, e.g. "
                    + ", ".join(f'"Bash({tool} *)"' for tool in tools)
                    + ".",
                )
            )
        if self._claude.trusts(path) is False:
            findings.append(
                Finding(
                    Level.WARN,
                    "Claude Code does not trust this folder yet: every agent would stop "
                    "and wait for you",
                    f"Run `claude` in {path} once and accept.",
                )
            )
        return RepoReport(path, tuple(findings), info)

    # --- boards -----------------------------------------------------------------------

    @staticmethod
    def _shared(report: BoardReport, elsewhere: dict[str, Path]) -> BoardReport:
        """Two instances on one board would take each other's tickets for ones without a run."""
        other = elsewhere.get(report.url)
        if other is None:
            return report
        finding = Finding(
            Level.FAIL,
            f"also listed by the Nightshift in {other}",
            "List each board in one home only (NIGHTSHIFT_HOME).",
        )
        return BoardReport(report.url, (*report.findings, finding), report.overview)

    def _board(self, url: str, user: str) -> BoardReport:
        try:
            overview = self._tracker.board_overview(url, user)
        except TrackerAuthError as e:
            return BoardReport(
                url,
                (Finding(Level.FAIL, str(e), "Run `gh auth refresh -h github.com -s project`."),),
            )
        except NightshiftError as e:
            return BoardReport(
                url, (Finding(Level.FAIL, str(e), "Check the URL and your access to the board."),)
            )
        report = BoardReport(url, (), overview)
        if not report.missing:
            return report
        return BoardReport(
            url,
            (
                Finding(
                    Level.FAIL,
                    f"missing: {', '.join(report.missing)}",
                    "Add it to the options of the board's Status field.",
                ),
            ),
            overview,
        )
