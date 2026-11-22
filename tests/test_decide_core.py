from typing import Any

import pytest

from nightshift.decide import decide, unlisted
from nightshift.model import (
    Claim,
    Complete,
    Drop,
    Flag,
    Item,
    Pick,
    Presence,
    Provision,
    RunState,
    Unflag,
    Update,
)
from tests.build import (
    BOARD,
    OTHER_BOARD,
    SETTLED,
    agent,
    board,
    item,
    pr,
    run,
    snapshot,
    ticket,
    view,
)

BOTH = frozenset({BOARD.lower(), OTHER_BOARD.lower()})


def picked(*items: Item, **changes: Any) -> list[int]:
    actions = decide(snapshot(boards=(board(*items),), **changes))
    return [int(i.ticket_id) for a in actions if isinstance(a, Pick) for i in a.items]


# --- FR-1, FR-2, FR-4 -----------------------------------------------------------------------


def test_picks_in_rank_order() -> None:
    assert picked(item(3, rank=2), item(1, rank=0), item(2, rank=1)) == [1, 2, 3]


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "Backlog"},
        {"status": None},
        {"labels": frozenset()},
        {"labels": frozenset({"nightshift", "nightshift:needs-human"})},
        {"is_open": False},
        {"repo_ref": "R_unlisted"},
        {"boards": BOTH},
    ],
)
def test_not_eligible(changes: dict[str, Any]) -> None:
    assert picked(item(**{"status": "Todo", **changes})) == []


def test_other_labels_do_not_matter() -> None:
    assert picked(item(labels=frozenset({"nightshift", "bug"}))) == [42]


def test_on_an_unlisted_board_too() -> None:
    assert picked(item(boards=frozenset({BOARD.lower(), "https://other"}))) == [42]


@pytest.mark.parametrize("state", list(RunState))
def test_a_current_run_blocks_its_ticket(state: RunState) -> None:
    actions = decide(snapshot(view(run(state), ticket("Todo")), boards=(board(item(), item(43)),)))
    picks = [a for a in actions if isinstance(a, Pick)]
    free = [] if not picks else [i.ticket_id for i in picks[0].items]
    assert ("42" in free) == (state is RunState.DISCARDED)


@pytest.mark.parametrize("state", list(RunState))
def test_only_live_runs_use_a_slot(state: RunState) -> None:
    actions = decide(snapshot(view(run(state)), boards=(board(item(43)),)))
    picks = [a for a in actions if isinstance(a, Pick)]
    assert picks[0].free == (1 if run(state).live else 2)


def test_full_board_picks_nothing_and_the_other_goes_on() -> None:
    views = [view(run(id=f"r{n}", ticket_ref=f"I_{n}")) for n in (1, 2, 3)]
    boards = (board(item(43)), board(item(44, board_url=OTHER_BOARD), url=OTHER_BOARD))
    picks = [a for a in decide(snapshot(*views, boards=boards)) if isinstance(a, Pick)]
    assert [p.board_url for p in picks] == [OTHER_BOARD]


def test_nothing_is_picked_when_a_tool_is_missing() -> None:
    assert picked(item(), ready=False) == []


def test_nothing_is_picked_from_a_broken_board() -> None:
    actions = decide(snapshot(boards=(board(item(), problem="no status field"),)))
    assert not [a for a in actions if isinstance(a, Pick)]


def test_unlisted_repo_is_reported_only() -> None:
    it = item(repo_ref="R_x", repo="acme/docs")
    snap = snapshot(boards=(board(it, item(5, repo_ref="R_x", labels=frozenset())),))
    assert unlisted(snap) == [it]
    assert decide(snap) == []


# --- tickets without a run ------------------------------------------------------------------


def test_ticket_on_two_boards_is_flagged_once() -> None:
    boards = (
        board(item(boards=BOTH)),
        board(item(boards=BOTH, board_url=OTHER_BOARD), url=OTHER_BOARD),
    )
    actions = decide(snapshot(boards=boards))
    assert [type(a) for a in actions] == [Flag]
    key = actions[0].key  # type: ignore[union-attr]
    assert decide(snapshot(boards=boards, signalled=frozenset({key}))) == []


def test_in_progress_without_a_run_is_flagged_once() -> None:
    boards = (board(item(status="In Progress")),)
    assert [type(a) for a in decide(snapshot(boards=boards))] == [Flag]
    assert decide(snapshot(boards=boards, signalled=frozenset({"orphan:I_42"}))) == []
    flagged = item(status="In Progress", labels=frozenset({"nightshift", "nightshift:needs-human"}))
    assert decide(snapshot(boards=(board(flagged),))) == []


def test_in_progress_of_an_unlisted_repo_is_left_alone() -> None:
    assert decide(snapshot(boards=(board(item(status="In Progress", repo_ref="R_x")),))) == []


def test_flag_is_forgotten_when_its_reason_is_gone() -> None:
    signalled = frozenset({"orphan:I_42"})
    actions = decide(snapshot(boards=(board(),), signalled=signalled))
    assert actions == [Unflag("orphan:I_42")]
    assert decide(snapshot(boards=(board(problem="down"),), signalled=signalled)) == []


# --- the life of a run ----------------------------------------------------------------------


def acts(*args: Any, **kwargs: Any) -> list[type]:
    return [type(a) for a in decide(snapshot(view(*args, **kwargs))) if not isinstance(a, Update)]


def test_claim_is_repeated_while_eligible() -> None:
    assert acts(run("claimed"), ticket("Todo")) == [Claim]
    assert acts(run("claimed"), ticket("In Progress")) == [Claim]


@pytest.mark.parametrize(
    "facts",
    [
        ticket("Backlog"),
        ticket("Todo", labels=frozenset()),
        ticket("Todo", labels=frozenset({"nightshift", "nightshift:needs-human"})),
        ticket("Todo", is_open=False),
        ticket(None, presence=Presence.REMOVED),
    ],
)
def test_claim_is_dropped_when_not_eligible(facts: Any) -> None:
    assert acts(run("claimed"), facts) == [Drop]


def test_provision_is_repeated() -> None:
    assert acts(run("provisioning")) == [Provision]


def test_nothing_is_decided_when_github_cannot_say() -> None:
    from dataclasses import replace

    for state in RunState:
        assert decide(snapshot(replace(view(run(state)), ticket=None))) == []


@pytest.mark.parametrize("state", ["running", "waiting_for_human"])
def test_ready_pr_and_finished_agent_complete_the_run(state: str) -> None:
    assert acts(run(state), ticket("In Progress", pr()), agent("done"), SETTLED) == [Complete]
    assert acts(run(state), ticket("In Progress", pr()), agent("idle"), SETTLED) == [Complete]


@pytest.mark.parametrize(
    ("facts", "by", "streaks"),
    [
        (ticket("In Progress"), agent("done"), SETTLED),
        (ticket("In Progress", pr(draft=True)), agent("done"), SETTLED),
        (ticket("In Progress", pr("closed")), agent("done"), SETTLED),
        (ticket("In Progress", pr()), agent("working"), SETTLED),
        (ticket("In Progress", pr()), agent("blocked"), SETTLED),
        (ticket("In Progress", pr()), agent("none"), SETTLED),
        (ticket("In Progress", pr()), agent("none", "unknown"), SETTLED),
    ],
)
def test_not_complete(facts: Any, by: Any, streaks: Any) -> None:
    assert Complete not in acts(run(), facts, by, streaks)


@pytest.mark.parametrize("place", ["exited", "lost", "closed"])
def test_agent_that_is_gone_with_a_ready_pr_completes(place: str) -> None:
    facts = ticket("In Progress", pr())
    assert acts(run(), facts, agent("none", place), SETTLED) == [Complete]


def test_completing_is_repeated_whatever_the_agent_does() -> None:
    assert acts(run("completing"), ticket("In Progress", pr())) == [Complete]
    assert acts(run("completing"), ticket("In Review", pr())) == [Complete]


def test_the_pr_is_the_open_one_then_the_merged_one_then_the_newest() -> None:
    from datetime import timedelta

    from tests.build import NOW

    old = pr("closed", ref="old", created_at=NOW - timedelta(hours=2))
    merged = pr("merged", ref="merged", created_at=NOW - timedelta(hours=3))
    new = pr("closed", ref="new", created_at=NOW)
    opened = pr("open", ref="open", created_at=NOW - timedelta(hours=4))

    def the(*prs: Any) -> str | None:
        found = ticket("In Progress", *prs).pr
        return found.ref if found else None

    assert the(old, merged, new, opened) == "open"
    assert the(old, merged, new) == "merged"
    assert the(old, new) == "new"
    assert the() is None


def test_what_was_seen_is_recorded() -> None:
    fresh = run(seen_out_of_todo=False)
    actions = decide(snapshot(view(fresh, ticket("In Progress", pr()))))
    assert actions == [Update(fresh, {"seen_out_of_todo": True, "pr_ref": "PR_1"})]
    still = run("claimed", seen_out_of_todo=False)
    assert decide(snapshot(view(still, ticket("Todo")))) == [Claim(still)]
