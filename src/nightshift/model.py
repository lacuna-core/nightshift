"""What Nightshift knows and does, as plain data. No I/O."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any


@dataclass(frozen=True)
class RepoInfo:
    """A repo as the tracker sees it."""

    slug: str
    ref: str
    default_branch: str
    can_push: bool
    labels: frozenset[str]


@dataclass(frozen=True)
class IssueCount:
    open: int = 0
    labelled: int = 0


@dataclass(frozen=True)
class BoardOverview:
    """What ``check`` shows about a board."""

    url: str
    statuses: tuple[str, ...]
    issues: dict[str, IssueCount] = field(default_factory=dict)


class RunState(StrEnum):
    CLAIMED = "claimed"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    WAITING = "waiting_for_human"
    COMPLETING = "completing"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RESETTING = "resetting"
    DISCARDED = "discarded"


LIVE = frozenset(
    {
        RunState.CLAIMED,
        RunState.PROVISIONING,
        RunState.RUNNING,
        RunState.WAITING,
        RunState.COMPLETING,
    }
)
ENDED = frozenset({RunState.DONE, RunState.FAILED, RunState.CANCELLED})


@dataclass(frozen=True)
class Run:
    """One attempt by one agent at one ticket: a row of the database (spec 8.1)."""

    id: str
    repo: str
    ticket_id: str
    repo_ref: str
    ticket_ref: str
    clone_path: str
    board_url: str
    state: RunState
    created_at: datetime
    board_item_id: str | None = None
    state_reason: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    herdr_space_id: str | None = None
    herdr_tab_id: str | None = None
    herdr_terminal: str | None = None
    claude_session_id: str | None = None
    seen_out_of_todo: bool = False
    taken_over: bool = False
    resume_count: int = 0
    limited_since: datetime | None = None
    active_seconds: int = 0
    turn_at_end: int | None = None
    stop_requested_at: datetime | None = None
    pr_ref: str | None = None
    tab_named: bool = False
    kept_reason: str | None = None
    started_at: datetime | None = None
    last_activity_at: datetime | None = None
    ended_at: datetime | None = None
    cleaned_up_at: datetime | None = None

    @property
    def live(self) -> bool:
        return self.state in LIVE

    @property
    def ended(self) -> bool:
        return self.state in ENDED


# --- what is read from GitHub -------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """A ticket on a board."""

    item_id: str
    board_url: str
    rank: int
    status: str | None
    repo: str
    repo_ref: str
    ticket_id: str
    ticket_ref: str
    title: str
    labels: frozenset[str]
    is_open: bool = True
    boards: frozenset[str] = frozenset()
    """The URLs of all boards the ticket is on, in lower case."""


@dataclass(frozen=True)
class Board:
    """The candidate items of a board, in rank order, or why it cannot be read."""

    url: str
    items: tuple[Item, ...] = ()
    problem: str | None = None


class PrState(StrEnum):
    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


@dataclass(frozen=True)
class Pr:
    ref: str
    url: str
    state: PrState
    is_draft: bool
    head: str
    created_at: datetime
    reopened_at: datetime | None = None

    @property
    def ready(self) -> bool:
        return self.state is PrState.OPEN and not self.is_draft


class Presence(StrEnum):
    """Is the ticket on the board its run was picked from?"""

    ON = "on"
    REMOVED = "removed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TicketFacts:
    """The ticket of a run, as it is on GitHub now."""

    labels: frozenset[str]
    is_open: bool
    presence: Presence
    status: str | None
    item_id: str | None
    label_removed: bool
    """The ``nightshift`` label was removed since the run started."""

    prs: tuple[Pr, ...] = ()
    transferred: bool = False
    repo: str = ""

    @property
    def pr(self) -> Pr | None:
        """The PR (glossary): the open one, else the merged one, else the newest."""
        for state in (PrState.OPEN, PrState.MERGED, PrState.CLOSED):
            if found := [pr for pr in self.prs if pr.state is state]:
                return max(found, key=lambda pr: pr.created_at)
        return None


@dataclass(frozen=True)
class Brief:
    """What the agent is told about its ticket."""

    repo: str
    ticket_id: str
    title: str
    body: str
    url: str
    labels: tuple[str, ...]
    comments: tuple[tuple[str, str], ...]
    """Author and text, only of people with write access."""

    closing_line: str
    """The line in the PR that links it to the ticket."""


# --- what is read from Herdr and Claude Code ----------------------------------------------


class AgentPlace(StrEnum):
    ALIVE = "alive"
    """Claude Code is the foreground program of the run's terminal."""

    EXITED = "exited"
    """The terminal is there, and Claude Code ended by itself."""

    LOST = "lost"
    """The terminal is there, with a fresh shell after a reboot or Herdr crash."""

    CLOSED = "closed"
    """The terminal is gone."""

    UNKNOWN = "unknown"
    """Herdr could not be asked, or the agent is still starting."""


class AgentState(StrEnum):
    NONE = "none"
    IDLE = "idle"
    WORKING = "working"
    DONE = "done"
    BLOCKED = "blocked"
    ERROR = "error"


@dataclass(frozen=True)
class Limit:
    """A usage limit that stopped an agent."""

    resets_at: datetime | None


@dataclass(frozen=True)
class Agent:
    place: AgentPlace
    state: AgentState = AgentState.NONE
    turn: int = 0
    kind: str = ""
    limit: Limit | None = None
    other_claude: bool = False
    """Some other Claude Code runs in the run's worktree."""


UNKNOWN_AGENT = Agent(AgentPlace.UNKNOWN)


# --- one poll -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Streaks:
    """How long some things have been so. Kept in memory only: a restart starts over."""

    finished_polls: int = 0
    gone_polls: int = 0
    todo_polls: int = 0
    idle_seconds: float = 0
    quiet_seconds: float = 0
    silent_seconds: float = 0


@dataclass(frozen=True)
class RunView:
    """All that is known about a run on one poll."""

    run: Run
    ticket: TicketFacts | None
    """None: GitHub could not say, so nothing may be concluded."""

    agent: Agent = UNKNOWN_AGENT
    streaks: Streaks = Streaks()
    elapsed: float = 0
    """Seconds since the last poll, 0 if it was too long ago to count."""

    active: bool = False
    """Its state, turn or screen changed since the last poll."""

    cleanup: Cleanup | None = None
    stall_timeout: timedelta = timedelta(minutes=20)
    max_run_time: timedelta = timedelta(hours=3)


@dataclass(frozen=True)
class Cleanup:
    """What is left of a run locally, and why it may not be removed (spec FR-13)."""

    worktree_exists: bool
    unsafe: str | None


@dataclass(frozen=True)
class UsageLimit:
    since: datetime
    resets_at: datetime | None
    probed_at: datetime | None = None


@dataclass(frozen=True)
class Snapshot:
    """Everything one poll read."""

    now: datetime
    boards: tuple[Board, ...]
    runs: tuple[RunView, ...]
    limits: dict[str, int]
    """``max_parallel_agents`` per board."""

    listed: dict[str, str]
    """Usable repos: the tracker's ref to the clone's path."""

    usage_limit: UsageLimit | None = None
    ready: bool = True
    """Herdr, Claude Code and the login work, so tickets may be picked."""

    signalled: frozenset[str] = frozenset()
    """Tickets without a run that were flagged already (FR-1)."""


# --- what to do ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pick:
    """Claim the first of these tickets that can be claimed, up to the free slots."""

    board_url: str
    items: tuple[Item, ...]
    free: int


@dataclass(frozen=True)
class Claim:
    """Set the ticket to In Progress, after checking that it is still eligible."""

    run: Run


@dataclass(frozen=True)
class Drop:
    """Discard a claim before anything started."""

    run: Run
    reason: str


@dataclass(frozen=True)
class Provision:
    """Create the branch, worktree and space, and start the agent."""

    run: Run


@dataclass(frozen=True)
class Complete:
    """Move the ticket to In Review, with its PR."""

    run: Run
    pr: Pr


@dataclass(frozen=True)
class End:
    """End a live run without handing it over."""

    run: Run
    state: RunState
    event: str
    reason: str
    needs_human: bool = False
    interrupt: bool = False


@dataclass(frozen=True)
class Wait:
    """The run needs you, and keeps its slot."""

    run: Run
    event: str
    reason: str


@dataclass(frozen=True)
class Finish:
    """The run is done, and you moved its ticket yourself."""

    run: Run
    reason: str


@dataclass(frozen=True)
class TakeOver:
    """You removed the label from the ticket of a run that has ended."""

    run: Run


@dataclass(frozen=True)
class GoOn:
    """The agent that needed you works again."""

    run: Run


@dataclass(frozen=True)
class Interrupt:
    """Stop the agent's current turn, as asked when the run ended."""

    run: Run


@dataclass(frozen=True)
class GiveUpStopping:
    """The agent of an ended run cannot be stopped: tell you."""

    run: Run


@dataclass(frozen=True)
class Resume:
    """Continue a lost agent in its own terminal, with the same session."""

    run: Run
    message: str | None


@dataclass(frozen=True)
class Update:
    """Record what was seen. Changes nothing outside the database."""

    run: Run
    changes: dict[str, Any]


@dataclass(frozen=True)
class NameTab:
    """Name the agent's tab, if the Herdr window is open."""

    run: Run


@dataclass(frozen=True)
class Flag:
    """Label a ticket without a run as needing you, and say why."""

    item: Item
    key: str
    text: str


@dataclass(frozen=True)
class Unflag:
    """Forget that a ticket was flagged: the reason is gone."""

    key: str


Action = (
    End
    | Wait
    | Finish
    | TakeOver
    | GoOn
    | Interrupt
    | GiveUpStopping
    | Resume
    | Pick
    | Claim
    | Drop
    | Provision
    | Complete
    | Update
    | NameTab
    | Flag
    | Unflag
)
