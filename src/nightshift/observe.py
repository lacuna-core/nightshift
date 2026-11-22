"""Reads GitHub, git, Herdr and the database into one snapshot per poll."""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from loguru import logger

from nightshift.conventions import POLL_INTERVAL, TODO
from nightshift.errors import (
    HerdrError,
    NightshiftError,
    TrackerAuthError,
    TrackerUnavailableError,
)
from nightshift.git import Git
from nightshift.herdr import Herdr, is_supported
from nightshift.model import (
    LIVE,
    UNKNOWN_AGENT,
    Agent,
    AgentPlace,
    AgentState,
    Board,
    Presence,
    RepoInfo,
    Run,
    RunState,
    RunView,
    Snapshot,
    Streaks,
    TicketFacts,
)
from nightshift.project_config import ProjectConfig, RepoConfig
from nightshift.store import Store
from nightshift.tracker.github import slug_of_remote

_REPO_INFO_AGE = timedelta(minutes=10)
_STOPPED = (AgentState.IDLE, AgentState.DONE)
_GONE = (AgentPlace.EXITED, AgentPlace.LOST, AgentPlace.CLOSED)


class ReadTracker(Protocol):
    def read_board(self, url: str) -> Board: ...
    def read_tickets(
        self, runs: list[Run], readable: set[str]
    ) -> dict[str, TicketFacts | None]: ...
    def repo_info(self, slug: str) -> RepoInfo: ...


class ToolVersion(Protocol):
    def version(self) -> str: ...


@dataclass
class _Memory:
    streaks: Streaks
    signature: str
    at: float


@dataclass(frozen=True)
class RepoState:
    config: RepoConfig
    info: RepoInfo | None
    problem: str | None


def screen_signature(text: str) -> str:
    """A screen without what changes by itself while nothing happens: spinner and counters."""
    lines = [
        re.sub(r"[\d.,]+|[^\x20-\x7e]", "", line)
        for line in text.splitlines()
        if "to interrupt" not in line and not re.search(r"\(\s*(\d+[hms]\s*)+(·|\))", line)
    ]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def advance(
    before: Streaks,
    run: Run,
    ticket: TicketFacts | None,
    agent: Agent,
    elapsed: float,
    active: bool,
) -> Streaks:
    """How long things have been so, one poll later."""
    alive = agent.place is AgentPlace.ALIVE
    stopped = alive and agent.state in _STOPPED
    in_todo = ticket is not None and ticket.presence is Presence.ON and ticket.status == TODO
    working = alive and agent.state is AgentState.WORKING
    return Streaks(
        finished_polls=before.finished_polls + 1 if stopped and agent.limit is None else 0,
        gone_polls=before.gone_polls + 1 if agent.place in _GONE else 0,
        todo_polls=before.todo_polls + 1 if in_todo else 0,
        idle_seconds=before.idle_seconds + elapsed if stopped and not active else 0,
        quiet_seconds=before.quiet_seconds + elapsed
        if working and not active and agent.limit is None
        else 0,
        silent_seconds=before.silent_seconds + elapsed
        if alive and agent.state is AgentState.NONE
        else 0,
    )


class Observer:
    def __init__(
        self,
        *,
        config: ProjectConfig,
        store: Store,
        tracker: ReadTracker,
        herdr: Herdr,
        claude: ToolVersion,
        git: Git,
        now: Callable[[], datetime],
        monotonic: Callable[[], float],
    ) -> None:
        self._config = config
        self._store = store
        self._tracker = tracker
        self._herdr = herdr
        self._claude = claude
        self._git = git
        self._now = now
        self._monotonic = monotonic
        self._memory: dict[str, _Memory] = {}
        self._infos: dict[str, tuple[datetime, RepoInfo]] = {}
        self.repos: list[RepoState] = []
        self.problems: dict[str, str] = {}
        """What cannot be reached now, for ``status``."""

    def observe(self) -> Snapshot:
        self.problems = {}
        herdr_ok = self._tool("Herdr", self._herdr_problem)
        claude_ok = self._tool("Claude Code", self._claude_problem)
        boards = tuple(self._board(b.url) for b in self._config.boards)
        self.repos = [self._repo(repo) for repo in self._config.repos]
        listed = {r.info.ref: str(r.config.path) for r in self.repos if r.info and not r.problem}
        runs = self._watched(boards)
        readable = {b.url for b in boards if b.problem is None}
        facts = self._tickets(runs, readable)
        views = tuple(self._view(run, facts.get(run.id), herdr_ok) for run in runs)
        self._memory = {k: m for k, m in self._memory.items() if k in {r.id for r in runs}}
        return Snapshot(
            now=self._now(),
            boards=boards,
            runs=views,
            limits={b.url: b.max_parallel_agents for b in self._config.boards},
            listed=listed,
            ready=herdr_ok and claude_ok,
            signalled=frozenset(key.removeprefix("flag:") for key in self._store.items("flag:")),
        )

    # --- what concerns everything -----------------------------------------------------

    def _tool(self, name: str, problem: Callable[[], str | None]) -> bool:
        try:
            text = problem()
        except NightshiftError as e:
            text = str(e)
        if text:
            self.problems[name] = text
        return text is None

    def _herdr_problem(self) -> str | None:
        version = self._herdr.version()
        return None if is_supported(version) else f"core {version} is not supported"

    def _claude_problem(self) -> str | None:
        self._claude.version()
        return None

    def _board(self, url: str) -> Board:
        try:
            board = self._tracker.read_board(url)
        except TrackerUnavailableError, TrackerAuthError:
            raise  # it concerns everything: this poll is over
        except NightshiftError as e:
            board = Board(url, problem=str(e))
        if board.problem:
            self.problems[url] = board.problem
        return board

    def _repo(self, repo: RepoConfig) -> RepoState:
        try:
            info = self._repo_info(repo)
            return RepoState(repo, info, None)
        except TrackerUnavailableError, TrackerAuthError:
            raise
        except NightshiftError as e:
            self.problems[str(repo.path)] = str(e)
            return RepoState(repo, None, str(e))

    def _repo_info(self, repo: RepoConfig) -> RepoInfo:
        if not self._git.is_clone(repo.path):
            raise NightshiftError("it is not the folder of a git clone")
        origin = self._git.origin(repo.path) or ""
        slug = slug_of_remote(origin)
        if slug is None:
            raise NightshiftError("its `origin` is not a GitHub repo")
        known = self._infos.get(origin)
        if known is None or self._now() - known[0] > _REPO_INFO_AGE:
            known = (self._now(), self._tracker.repo_info(slug))
            self._infos[origin] = known
        if not known[1].can_push:
            raise NightshiftError(f"you cannot push to {slug}")
        return known[1]

    # --- runs -------------------------------------------------------------------------

    def is_listed(self, run: Run) -> bool:
        """Are the run's board and repo still in the config? A broken one counts as listed."""
        if self._config.board(run.board_url) is None:
            return False
        refs = {r.info.ref for r in self.repos if r.info}
        paths = {r.config.resolved for r in self.repos if r.info is None}
        return run.repo_ref in refs or Path(run.clone_path).resolve() in paths

    def repo(self, repo_ref: str) -> RepoState | None:
        """A listed repo that works, by the tracker's ID."""
        return next(
            (r for r in self.repos if r.info and not r.problem and r.info.ref == repo_ref), None
        )

    def repo_config(self, run: Run) -> RepoConfig | None:
        for repo in self.repos:
            if repo.info and repo.info.ref == run.repo_ref:
                return repo.config
        return self._config.repo(Path(run.clone_path))

    def _watched(self, boards: tuple[Board, ...]) -> list[Run]:
        """Runs to look at. One that is cleaned up or taken over only if its ticket shows up."""
        shown = {item.ticket_ref for board in boards for item in board.items}
        return [
            run
            for run in self._store.watched_runs()
            if self.is_listed(run)
            and (
                run.state in LIVE
                or run.state is RunState.RESETTING
                or run.ticket_ref in shown
                or (run.cleaned_up_at is None and not run.taken_over)
            )
        ]

    def _tickets(self, runs: list[Run], readable: set[str]) -> dict[str, TicketFacts | None]:
        if not runs:
            return {}
        try:
            return self._tracker.read_tickets(runs, readable)
        except TrackerUnavailableError, TrackerAuthError:
            raise
        except NightshiftError as e:
            self.problems["tickets"] = str(e)
            return {}

    def _view(self, run: Run, ticket: TicketFacts | None, herdr_ok: bool) -> RunView:
        agent, signature = self._agent(run) if herdr_ok else (UNKNOWN_AGENT, "")
        before = self._memory.get(run.id)
        at = self._monotonic()
        elapsed = 0.0
        if before is not None:
            gap = at - before.at
            elapsed = gap if gap <= 2 * POLL_INTERVAL.total_seconds() else 0.0
        active = before is None or before.signature != signature
        streaks = advance(
            before.streaks if before else Streaks(), run, ticket, agent, elapsed, active
        )
        self._memory[run.id] = _Memory(streaks, signature, at)
        config = self.repo_config(run)
        view = RunView(run, ticket, agent, streaks, elapsed, active)
        if config is None:
            return view
        return replace(view, stall_timeout=config.stall_timeout, max_run_time=config.max_run_time)

    def _agent(self, run: Run) -> tuple[Agent, str]:
        if run.claude_session_id is None or run.state is RunState.PROVISIONING:
            return UNKNOWN_AGENT, ""
        try:
            terminal = self._herdr.run_terminal(run.id)
            if terminal is None:
                return Agent(AgentPlace.CLOSED), ""
            agent = terminal.agent()
            screen = ""
            if agent.place is AgentPlace.ALIVE and agent.state is AgentState.WORKING:
                screen = screen_signature(self._herdr.screen(terminal.address))
            return agent, f"{agent.place} {agent.state} {agent.turn} {screen}"
        except HerdrError as e:
            logger.warning("Herdr: {}", e)
            self.problems["Herdr"] = str(e)
            return UNKNOWN_AGENT, ""
