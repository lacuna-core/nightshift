"""The pure function at the heart of Nightshift: what was read, to what to do.

Every rule about a ticket, its run and its agent is here or in the executor's re-check right
before it writes. Nothing here does I/O.
"""

from collections.abc import Iterator
from typing import Any

from nightshift.conventions import (
    FINISHED_POLLS,
    GONE_POLLS,
    IN_PROGRESS,
    LABEL,
    NEEDS_HUMAN,
    TODO,
)
from nightshift.model import (
    Action,
    AgentPlace,
    AgentState,
    Claim,
    Complete,
    Drop,
    Flag,
    Item,
    Pick,
    Presence,
    Provision,
    RunState,
    RunView,
    Snapshot,
    Unflag,
    Update,
)

GONE = frozenset({AgentPlace.EXITED, AgentPlace.LOST, AgentPlace.CLOSED})
STOPPED = frozenset({AgentState.IDLE, AgentState.DONE})


def decide(snapshot: Snapshot) -> list[Action]:
    actions: list[Action] = []
    for view in snapshot.runs:
        actions += _run(view, snapshot)
    actions += _flags(snapshot)
    actions += _picks(snapshot)
    return actions


# --- runs ---------------------------------------------------------------------------------


def _run(view: RunView, snapshot: Snapshot) -> Iterator[Action]:
    if view.ticket is None:
        return
    if changes := _seen(view):
        yield Update(view.run, changes)
    yield from _live(view, snapshot)


def _seen(view: RunView) -> dict[str, Any]:
    run, ticket = view.run, view.ticket
    assert ticket is not None
    changes: dict[str, Any] = {}
    if not run.seen_out_of_todo and ticket.presence is Presence.ON and ticket.status != TODO:
        changes["seen_out_of_todo"] = True
    if ticket.pr is not None and ticket.pr.ref != run.pr_ref:
        changes["pr_ref"] = ticket.pr.ref
    return changes


def _live(view: RunView, snapshot: Snapshot) -> Iterator[Action]:
    run, ticket = view.run, view.ticket
    assert ticket is not None
    match run.state:
        case RunState.CLAIMED:
            yield _claimed(view)
        case RunState.PROVISIONING:
            yield Provision(run)
        case RunState.RUNNING | RunState.WAITING | RunState.COMPLETING:
            complete = run.state is RunState.COMPLETING or finished(view)
            if ticket.pr is not None and ticket.pr.ready and complete:
                yield Complete(run, ticket.pr)


def _claimed(view: RunView) -> Action:
    run, ticket = view.run, view.ticket
    assert ticket is not None
    if ticket.status == IN_PROGRESS:
        return Claim(run)  # set by the claim, before a crash
    if reason := _not_eligible(ticket.status, ticket.labels, ticket.is_open):
        return Drop(run, reason)
    if ticket.presence is not Presence.ON:
        return Drop(run, "the ticket is not on the board")
    return Claim(run)


def finished(view: RunView) -> bool:
    """Has the agent finished its turn (FR-9)?"""
    agent = view.agent
    if agent.place in GONE:
        return view.streaks.gone_polls >= GONE_POLLS
    return (
        agent.place is AgentPlace.ALIVE
        and agent.state in STOPPED
        and agent.limit is None
        and view.streaks.finished_polls >= FINISHED_POLLS
    )


# --- tickets without a run ----------------------------------------------------------------


def _not_eligible(status: str | None, labels: frozenset[str], is_open: bool) -> str | None:
    if not is_open:
        return "the issue is closed"
    if status != TODO:
        return f"the ticket is not in {TODO}"
    if LABEL not in labels:
        return f"the ticket has no `{LABEL}` label"
    if NEEDS_HUMAN in labels:
        return f"the ticket has the `{NEEDS_HUMAN}` label"
    return None


def eligible(item: Item, snapshot: Snapshot) -> bool:
    return (
        _not_eligible(item.status, item.labels, item.is_open) is None
        and item.repo_ref in snapshot.listed
        and not _on_several(item, snapshot)
        and item.ticket_ref not in _current(snapshot)
    )


def unlisted(snapshot: Snapshot) -> list[Item]:
    """Ready tickets from repos that are not listed: shown by ``status`` only."""
    return [
        item
        for board in snapshot.boards
        for item in board.items
        if _not_eligible(item.status, item.labels, item.is_open) is None
        and item.repo_ref not in snapshot.listed
    ]


def _current(snapshot: Snapshot) -> set[str]:
    """Tickets that have a current run."""
    return {v.run.ticket_ref for v in snapshot.runs if v.run.state is not RunState.DISCARDED}


def _on_several(item: Item, snapshot: Snapshot) -> bool:
    return len(item.boards & {url.lower() for url in snapshot.limits}) > 1


def _flags(snapshot: Snapshot) -> Iterator[Action]:
    current = _current(snapshot)
    seen: set[str] = set()
    for board in snapshot.boards:
        for item in board.items:
            if LABEL not in item.labels or not item.is_open or item.ticket_ref in current:
                continue
            if item.status == TODO and _on_several(item, snapshot):
                key = f"boards:{item.ticket_ref}"
                text = (
                    "This ticket is on more than one board that Nightshift watches, so it has "
                    "no single status or rank. Nightshift will not pick it. Leave it on one "
                    f"board, then remove the `{NEEDS_HUMAN}` label."
                )
            elif item.status == IN_PROGRESS and item.repo_ref in snapshot.listed:
                key = f"orphan:{item.ticket_ref}"
                text = (
                    f"This ticket is in {IN_PROGRESS}, but Nightshift has no agent working on "
                    f"it. Move it to {TODO} to have it picked."
                )
            else:
                continue
            if key in seen:
                continue
            seen.add(key)
            if key not in snapshot.signalled and NEEDS_HUMAN not in item.labels:
                yield Flag(item, key, text)
    if all(board.problem is None for board in snapshot.boards):
        for key in sorted(snapshot.signalled - seen):
            yield Unflag(key)


def _picks(snapshot: Snapshot) -> Iterator[Action]:
    if not snapshot.ready:
        return
    for board in snapshot.boards:
        if board.problem is not None or board.url not in snapshot.limits:
            continue
        used = sum(v.run.live and v.run.board_url == board.url for v in snapshot.runs)
        free = snapshot.limits[board.url] - used
        items = sorted(
            (i for i in board.items if eligible(i, snapshot)), key=lambda item: item.rank
        )
        if free > 0 and items:
            yield Pick(board.url, tuple(items), free)
