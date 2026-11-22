"""Plain data for tests of the decide function."""

from datetime import UTC, datetime, timedelta
from typing import Any

from nightshift.model import (
    Agent,
    AgentPlace,
    AgentState,
    Board,
    Item,
    Pr,
    Presence,
    PrState,
    Run,
    RunState,
    RunView,
    Snapshot,
    Streaks,
    TicketFacts,
)

NOW = datetime(2026, 11, 22, 3, 0, tzinfo=UTC)
BOARD = "https://github.com/users/me/projects/1"
OTHER_BOARD = "https://github.com/users/me/projects/2"
SETTLED = Streaks(finished_polls=2, gone_polls=2, todo_polls=3)


def run(state: RunState | str = RunState.RUNNING, **changes: Any) -> Run:
    values: dict[str, Any] = {
        "id": "r1",
        "repo": "acme/web",
        "ticket_id": "42",
        "repo_ref": "R_web",
        "ticket_ref": "I_42",
        "clone_path": "/dev/web",
        "board_url": BOARD,
        "board_item_id": "PVTI_42",
        "created_at": NOW - timedelta(hours=1),
        "branch": "42/export-csv",
        "worktree_path": "/wt/acme-web/42-export-csv",
        "seen_out_of_todo": True,
    }
    return Run(**(values | {"state": RunState(state)} | changes))


def pr(state: str = "open", *, draft: bool = False, **changes: Any) -> Pr:
    values: dict[str, Any] = {
        "ref": "PR_1",
        "url": "https://github.com/acme/web/pull/1",
        "state": PrState(state),
        "is_draft": draft,
        "head": "abc",
        "created_at": NOW - timedelta(minutes=30),
    }
    return Pr(**(values | changes))


def ticket(status: str | None = "In Progress", *prs: Pr, **changes: Any) -> TicketFacts:
    values: dict[str, Any] = {
        "labels": frozenset({"nightshift"}),
        "is_open": True,
        "presence": Presence.ON,
        "status": status,
        "item_id": "PVTI_42",
        "label_removed": False,
        "prs": prs,
        "repo": "acme/web",
    }
    return TicketFacts(**(values | changes))


def agent(state: str = "working", place: str = "alive", **changes: Any) -> Agent:
    return Agent(AgentPlace(place), AgentState(state), **changes)


def view(
    of: Run | None = None,
    facts: TicketFacts | None = None,
    by: Agent | None = None,
    streaks: Streaks = Streaks(),  # noqa: B008
    **changes: Any,
) -> RunView:
    return RunView(of or run(), facts or ticket(), by or agent(), streaks, **changes)


def item(number: int = 42, status: str | None = "Todo", **changes: Any) -> Item:
    values: dict[str, Any] = {
        "item_id": f"PVTI_{number}",
        "board_url": BOARD,
        "rank": number,
        "status": status,
        "repo": "acme/web",
        "repo_ref": "R_web",
        "ticket_id": str(number),
        "ticket_ref": f"I_{number}",
        "title": f"Ticket {number}",
        "labels": frozenset({"nightshift"}),
        "boards": frozenset({BOARD.lower()}),
    }
    return Item(**(values | changes))


def board(*items: Item, url: str = BOARD, problem: str | None = None) -> Board:
    return Board(url, items, problem)


def snapshot(*views: RunView, boards: tuple[Board, ...] = (), **changes: Any) -> Snapshot:
    values: dict[str, Any] = {
        "now": NOW,
        "boards": boards,
        "runs": views,
        "limits": {BOARD: 2, OTHER_BOARD: 2},
        "listed": {"R_web": "/dev/web", "R_api": "/dev/api"},
    }
    return Snapshot(**(values | changes))
