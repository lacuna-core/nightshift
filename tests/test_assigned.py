"""Only tickets assigned to the configured user are picked up (issue #13)."""

from typing import Any

from nightshift.decide import claimed, decide, not_assigned
from nightshift.model import Claim, Drop, End, Flag, Pick, RunState, TakeOver
from tests.build import SETTLED, board, item, run, snapshot, ticket, view


def picked(*items: Any) -> list[str]:
    actions = decide(snapshot(boards=(board(*items),)))
    return [i.ticket_id for a in actions if isinstance(a, Pick) for i in a.items]


def test_only_tickets_assigned_to_the_user_are_picked() -> None:
    mine = item(1)
    shared = item(2, assignees=frozenset({"me", "someone"}))
    theirs = item(3, assignees=frozenset({"someone"}))
    nobodys = item(4, assignees=frozenset())
    assert picked(mine, shared, theirs, nobodys) == ["1", "2"]


def test_skipped_tickets_are_listed_for_status() -> None:
    theirs = item(3, assignees=frozenset({"someone"}))
    unready = item(5, assignees=frozenset(), status="Backlog")
    other_repo = item(6, assignees=frozenset(), repo_ref="R_unlisted")
    shown = not_assigned(snapshot(boards=(board(item(1), theirs, unready, other_repo),)))
    assert [i.ticket_id for i in shown] == ["3"]


def test_someone_elses_ticket_is_never_flagged() -> None:
    orphan = item(7, status="In Progress", assignees=frozenset({"someone"}))
    assert not [a for a in decide(snapshot(boards=(board(orphan),))) if isinstance(a, Flag)]
    mine = item(8, status="In Progress")
    assert [a for a in decide(snapshot(boards=(board(mine),))) if isinstance(a, Flag)]


def test_claim_is_dropped_when_unassigned_before_it() -> None:
    claim = run(RunState.CLAIMED, seen_out_of_todo=False)
    assert isinstance(claimed(view(claim, ticket("Todo")), "me"), Claim)
    dropped = claimed(view(claim, ticket("Todo", assignees=frozenset())), "me")
    assert isinstance(dropped, Drop)
    assert "not assigned to me" in dropped.reason


def ends(facts: Any, state: RunState = RunState.RUNNING) -> list[Any]:
    return decide(snapshot(view(run(state), facts, streaks=SETTLED)))


def test_reassigned_live_run_is_taken_over() -> None:
    [end] = [a for a in ends(ticket(assignees=frozenset({"someone"}))) if isinstance(a, End)]
    assert (end.state, end.event, end.interrupt) == (RunState.CANCELLED, "taken-over", True)
    assert "me is no longer assigned" in end.reason


def test_unassigned_and_back_is_still_taken_over() -> None:
    facts = ticket(unassigned=frozenset({"me"}))
    assert [a for a in ends(facts) if isinstance(a, End) and a.event == "taken-over"]


def test_other_assignees_coming_and_going_change_nothing() -> None:
    facts = ticket(assignees=frozenset({"me", "new"}), unassigned=frozenset({"someone"}))
    assert not [a for a in ends(facts) if isinstance(a, End)]


def test_reassigned_ended_run_is_taken_over_with_the_reason() -> None:
    [take] = [
        a for a in ends(ticket(assignees=frozenset()), RunState.DONE) if isinstance(a, TakeOver)
    ]
    assert "no longer assigned" in take.reason
