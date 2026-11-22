"""The pure function at the heart of Nightshift: what was read, to what to do.

Every rule about a ticket, its run and its agent is here or in the executor's re-check right
before it writes. Nothing here does I/O.
"""

from collections.abc import Iterator
from datetime import datetime
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
    End,
    Flag,
    Item,
    NameTab,
    Pick,
    Presence,
    Provision,
    Resume,
    RunState,
    RunView,
    Snapshot,
    Unflag,
    Update,
    Wait,
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
    if _has_tab(view):
        yield NameTab(view.run)
    if changes := _seen(view, snapshot.now):
        yield Update(view.run, changes)
    yield from _live(view, snapshot)


def _has_tab(view: RunView) -> bool:
    run = view.run
    placed = run.herdr_tab_id is not None and run.state not in (
        RunState.CLAIMED,
        RunState.PROVISIONING,
        RunState.DISCARDED,
    )
    return placed and not run.tab_named and view.agent.place is AgentPlace.ALIVE


def _seen(view: RunView, now: datetime) -> dict[str, Any]:
    run, ticket = view.run, view.ticket
    assert ticket is not None
    changes: dict[str, Any] = {}
    if not run.seen_out_of_todo and ticket.presence is Presence.ON and ticket.status != TODO:
        changes["seen_out_of_todo"] = True
    if ticket.pr is not None and ticket.pr.ref != run.pr_ref:
        changes["pr_ref"] = ticket.pr.ref
    if run.state is RunState.RUNNING and view.agent.place is AgentPlace.ALIVE:
        if view.agent.limit is None and int(view.elapsed):
            changes["active_seconds"] = run.active_seconds + int(view.elapsed)
        if view.active:
            changes["last_activity_at"] = now
    return changes


def _live(view: RunView, snapshot: Snapshot) -> Iterator[Action]:
    run, ticket = view.run, view.ticket
    assert ticket is not None
    match run.state:
        case RunState.CLAIMED:
            yield claimed(view)
        case RunState.PROVISIONING:
            yield Provision(run)
        case RunState.RUNNING | RunState.WAITING | RunState.COMPLETING:
            complete = run.state is RunState.COMPLETING or finished(view)
            if ticket.pr is not None and ticket.pr.ready and complete:
                yield Complete(run, ticket.pr)
            elif run.state is RunState.COMPLETING:
                yield Update(run, {"state": RunState.RUNNING})  # the PR is not ready after all
            elif view.agent.place in GONE:
                yield from _gone(view)


def claimed(view: RunView) -> Action:
    run, ticket = view.run, view.ticket
    assert ticket is not None
    if ticket.status == IN_PROGRESS:
        return Claim(run)  # set by the claim, before a crash
    if reason := _not_eligible(ticket.status, ticket.labels, ticket.is_open):
        return Drop(run, reason)
    if ticket.presence is not Presence.ON:
        return Drop(run, "the ticket is not on the board")
    return Claim(run)


def _gone(view: RunView) -> Iterator[Action]:
    """Why an agent is gone decides what happens (NFR-4)."""
    run = view.run
    if view.streaks.gone_polls < GONE_POLLS:
        return
    match view.agent.place:
        case AgentPlace.CLOSED:
            yield End(
                run,
                RunState.CANCELLED,
                "tab-closed",
                "The agent's tab was closed before there was a pull request.",
                needs_human=True,
            )
        case AgentPlace.EXITED:
            yield End(
                run,
                RunState.FAILED,
                "failed",
                "Claude Code exited without a pull request that is ready for review.",
                needs_human=True,
            )
        case AgentPlace.LOST if view.agent.other_claude:
            if run.state is not RunState.WAITING:
                yield Wait(
                    run,
                    f"waiting-resumed-{run.resume_count}",
                    "The agent was lost in a crash or restart, and some Claude Code already "
                    "runs in its worktree, so Nightshift does not resume it.",
                )
        case AgentPlace.LOST if run.resume_count:
            yield End(
                run,
                RunState.FAILED,
                "failed",
                "The agent was lost in a crash or restart for the second time.",
                needs_human=True,
            )
        case AgentPlace.LOST:
            go_on = run.state is RunState.RUNNING and run.limited_since is None
            yield Resume(run, "continue" if go_on else None)


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
