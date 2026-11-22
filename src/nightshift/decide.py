"""The pure function at the heart of Nightshift: what was read, to what to do.

Every rule about a ticket, its run and its agent is here or in the executor's re-check right
before it writes. Nothing here does I/O.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from nightshift.conventions import (
    FINISHED_POLLS,
    GIVE_UP_STOPPING,
    GONE_POLLS,
    IDLE_NEEDS_YOU,
    IN_PROGRESS,
    IN_REVIEW,
    LABEL,
    LIMIT_GRACE,
    LIMIT_RETRY,
    NEEDS_HUMAN,
    NO_STATE_NEEDS_YOU,
    POLL_INTERVAL,
    TODO,
    TODO_POLLS,
)
from nightshift.model import (
    Action,
    AgentPlace,
    AgentState,
    Claim,
    CleanUp,
    ClearLimit,
    Complete,
    Continue,
    Drop,
    End,
    Finish,
    Flag,
    GiveUpStopping,
    GoOn,
    Interrupt,
    Item,
    NameTab,
    Pick,
    Presence,
    Provision,
    PrState,
    Reset,
    Resume,
    RunState,
    RunView,
    SetLimit,
    Snapshot,
    TakeOver,
    Unflag,
    Update,
    UsageLimit,
    Wait,
)

GONE = frozenset({AgentPlace.EXITED, AgentPlace.LOST, AgentPlace.CLOSED})
STOPPED = frozenset({AgentState.IDLE, AgentState.DONE})


def decide(snapshot: Snapshot) -> list[Action]:
    actions: list[Action] = []
    for view in snapshot.runs:
        actions += _run(view, snapshot)
    actions += _flags(snapshot)
    actions += _limits(snapshot)
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
    if view.run.live:
        changed = list(_changed_by_you(view))
        yield from changed or _live(view)
    elif view.run.state is RunState.RESETTING:
        yield Reset(view.run)  # one that has started always finishes
    elif view.run.state is RunState.DISCARDED:
        if view.run.cleaned_up_at is None and not view.run.taken_over:
            yield CleanUp(view.run, None)
    else:
        yield from _stopping(view, snapshot)
        yield from _ended(view)


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
        if view.agent.limit is not None and run.limited_since is None:
            changes["limited_since"] = view.agent.limit.at
        elif view.agent.limit is None and run.limited_since is not None and _going(view):
            changes["limited_since"] = None
        if view.active:
            changes["last_activity_at"] = now
    return changes


def _taken_over(view: RunView) -> bool:
    ticket = view.ticket
    assert ticket is not None
    return view.run.taken_over or ticket.label_removed or LABEL not in ticket.labels


def moved_to_todo(view: RunView) -> bool:
    """Did you move the ticket back, or is this a stale answer (FR-16)?"""
    ticket = view.ticket
    assert ticket is not None
    if ticket.presence is not Presence.ON or ticket.status != TODO:
        return False
    return view.run.seen_out_of_todo or view.streaks.todo_polls >= TODO_POLLS


def _changed_by_you(view: RunView) -> Iterator[Action]:
    """What you did to the ticket of a live run (FR-12). The first that applies wins."""
    run, ticket = view.run, view.ticket
    assert ticket is not None
    if run.state is RunState.CLAIMED:
        return  # the claim itself checks whether the ticket is still eligible
    pr = ticket.pr
    merged = pr is not None and pr.state is PrState.MERGED
    gone = view.agent.place in GONE and view.streaks.gone_polls >= GONE_POLLS

    def cancel(event: str, reason: str, *, needs_human: bool = False) -> End:
        return End(run, RunState.CANCELLED, event, reason, needs_human=needs_human, interrupt=True)

    if _taken_over(view):
        yield cancel("taken-over", _TAKEN_OVER)
    elif ticket.transferred:
        yield cancel("transferred", "The issue was transferred to another repository.")
    elif merged:
        if finished(view) or run.state is RunState.COMPLETING:
            yield Finish(run, "the pull request was merged")
    elif not ticket.is_open:
        yield cancel("closed", "The issue was closed.")
    elif moved_to_todo(view):
        yield Reset(run)
    elif pr is not None and pr.state is PrState.CLOSED and (finished(view) or gone):
        yield cancel("pr-closed", "The pull request was closed without merging.", needs_human=True)
    elif ticket.presence is Presence.REMOVED:
        yield cancel("removed", "The ticket was removed from the board.")
    elif ticket.presence is Presence.UNKNOWN or ticket.status in (TODO, IN_PROGRESS):
        return
    elif ticket.status == IN_REVIEW and pr is not None and pr.ready:
        if run.state is not RunState.COMPLETING and finished(view):
            yield Finish(run, f"you moved the ticket to {IN_REVIEW}")
    else:
        yield cancel(
            f"moved-{_word(ticket.status)}",
            f"The ticket was moved to {ticket.status or 'no status'}.",
        )


def _word(status: str | None) -> str:
    return "".join(c if c.isalnum() else "-" for c in (status or "none").lower())


_TAKEN_OVER = (
    f"The `{LABEL}` label was removed, so this is yours now. Nightshift leaves the agent's tab, "
    "the worktree, the branch and the pull request alone."
)


def _ended(view: RunView) -> Iterator[Action]:
    run, ticket = view.run, view.ticket
    assert ticket is not None
    if _taken_over(view) and not run.taken_over:
        yield TakeOver(run)
    elif moved_to_todo(view) and ticket.is_open and LABEL in ticket.labels:
        yield Reset(run)
    elif not run.taken_over:
        yield from list(_helped(view)) or _clean_up(view)


def _clean_up(view: RunView) -> Iterator[Action]:
    """Is there a reason to clean up after a run that has ended (FR-13)?"""
    run, ticket = view.run, view.ticket
    assert ticket is not None
    if run.cleaned_up_at is not None or run.worktree_path is None:
        return
    pr = ticket.pr
    if pr is not None and pr.state is PrState.MERGED:
        yield CleanUp(run, pr)
    elif (
        not ticket.is_open
        and view.agent.place is AgentPlace.CLOSED
        and view.streaks.gone_polls >= GONE_POLLS
    ):
        yield CleanUp(run, None)


def _live(view: RunView) -> Iterator[Action]:
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
            elif view.agent.place is AgentPlace.ALIVE:
                yield from _alive(view)


def claimed(view: RunView) -> Action:
    run, ticket = view.run, view.ticket
    assert ticket is not None
    # In Progress is where the claim itself put it, before a crash.
    claimed_before = ticket.status == IN_PROGRESS
    labels = ticket.labels - {NEEDS_HUMAN} if claimed_before else ticket.labels
    status = TODO if claimed_before else ticket.status
    if reason := _not_eligible(status, labels, ticket.is_open):
        return Drop(run, reason)
    if ticket.presence is not Presence.ON or ticket.transferred:
        return Drop(run, "the ticket is not on the board")
    return Claim(run)


def _alive(view: RunView) -> Iterator[Action]:
    """Does the agent need you (FR-10), or has it failed (FR-11)?"""
    run, agent, streaks = view.run, view.agent, view.streaks
    if agent.limit is not None:
        if run.state is RunState.WAITING:
            yield GoOn(run)  # it waits for the limit, not for you
        return
    if run.state is RunState.WAITING:
        if agent.state is AgentState.WORKING:
            yield GoOn(run)
        return
    if reason := _needs_you(view):
        yield Wait(run, f"waiting-{agent.turn}-{agent.state}", reason)
    elif agent.state is AgentState.WORKING and streaks.quiet_seconds > _seconds(view.stall_timeout):
        minutes = int(_seconds(view.stall_timeout) // 60)
        yield _failed(view, f"The agent showed no activity for more than {minutes} minutes.")
    elif run.active_seconds > _seconds(view.max_run_time):
        yield _failed(view, f"The run took longer than {view.max_run_time}.")


def _seconds(duration: timedelta) -> float:
    return duration.total_seconds()


def _failed(view: RunView, reason: str) -> End:
    run, pr = view.run, view.ticket.pr if view.ticket else None
    if pr is not None and pr.state is PrState.MERGED:
        return End(run, RunState.DONE, "merged", f"{reason} Its PR is merged.", interrupt=True)
    return End(run, RunState.FAILED, "failed", reason, needs_human=True, interrupt=True)


def _needs_you(view: RunView) -> str | None:
    agent, streaks = view.agent, view.streaks
    match agent.state:
        case AgentState.BLOCKED:
            what = {
                "permission": "It waits for a permission.",
                "question": "It asks a question.",
                "auth": "It needs you to log in.",
            }
            return what.get(agent.kind, "It is blocked on a question or a permission.")
        case AgentState.ERROR:
            return "It stopped with an error."
        case AgentState.NONE if streaks.silent_seconds >= _seconds(NO_STATE_NEEDS_YOU):
            return "Claude Code shows a dialog from before its session, such as the trust dialog."
        case AgentState.IDLE | AgentState.DONE if streaks.idle_seconds >= _seconds(IDLE_NEEDS_YOU):
            pr = view.ticket.pr if view.ticket else None
            if pr is not None and pr.state is PrState.MERGED:
                return None
            if pr is not None and pr.ready:
                return None
            return "It stopped without a pull request that is ready for review."
    return None


def _stopping(view: RunView, snapshot: Snapshot) -> Iterator[Action]:
    """Repeat an interrupt until the agent stopped, but never after a new turn (FR-12)."""
    run, agent = view.run, view.agent
    if run.stop_requested_at is None or agent.place is AgentPlace.UNKNOWN:
        return
    busy = agent.place is AgentPlace.ALIVE and agent.state in (
        AgentState.WORKING,
        AgentState.BLOCKED,
    )
    if not busy or agent.turn != run.turn_at_end:
        yield Update(run, {"stop_requested_at": None, "turn_at_end": agent.turn})
    elif snapshot.now - run.stop_requested_at > GIVE_UP_STOPPING:
        yield GiveUpStopping(run)
    else:
        yield Interrupt(run)


def _helped(view: RunView) -> Iterator[Action]:
    """A failed or cancelled run that you helped along (FR-18)."""
    run, ticket = view.run, view.ticket
    assert ticket is not None
    pr = ticket.pr
    if run.state not in (RunState.FAILED, RunState.CANCELLED):
        return
    if ticket.presence is not Presence.ON or ticket.status != IN_PROGRESS or not ticket.is_open:
        return
    if pr is None or not pr.ready or not finished(view):
        return
    ended = run.ended_at or run.created_at
    new_turn = run.turn_at_end is not None and view.agent.turn > run.turn_at_end
    new_pr = pr.created_at > ended or (pr.reopened_at is not None and pr.reopened_at > ended)
    if new_turn or new_pr:
        yield Complete(run, pr)


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


# --- usage limits (FR-17) -----------------------------------------------------------------


def _going(view: RunView) -> bool:
    """Does the agent get answers again?"""
    states = (AgentState.WORKING, AgentState.IDLE, AgentState.DONE, AgentState.BLOCKED)
    return view.agent.place is AgentPlace.ALIVE and view.agent.state in states


def _limited(snapshot: Snapshot) -> list[RunView]:
    return [
        v
        for v in snapshot.runs
        if v.run.state in (RunState.RUNNING, RunState.WAITING)
        and v.agent.place is AgentPlace.ALIVE
        and v.agent.limit is not None
        and v.ticket is not None
    ]


def _limits(snapshot: Snapshot) -> Iterator[Action]:
    now, limit, limited = snapshot.now, snapshot.usage_limit, _limited(snapshot)
    resets = [v.agent.limit.resets_at for v in limited if v.agent.limit]
    coming = max((r for r in resets if r is not None and _ahead(r, now)), default=None)
    if limit is None:
        if coming is not None or None in resets:
            yield SetLimit(UsageLimit(now, coming), new=True)
        else:
            yield from (Continue(v.run) for v in limited)
    elif coming is not None and (limit.resets_at is None or coming > limit.resets_at):
        yield SetLimit(replace(limit, resets_at=coming), new=False)
    elif limit.resets_at is not None:
        if not _ahead(limit.resets_at, now):
            yield ClearLimit()
            yield from (Continue(v.run) for v in limited if v.agent.limit)
    elif _recovered(limit, snapshot):
        yield ClearLimit()
        yield from (Continue(v.run) for v in limited)
    elif _probe_due(limit, now) and limited:
        yield SetLimit(replace(limit, probed_at=now), new=False)
        yield Continue(limited[0].run)


def _ahead(resets_at: datetime, now: datetime) -> bool:
    return resets_at + LIMIT_GRACE > now


def _probe_due(limit: UsageLimit, now: datetime) -> bool:
    return now - (limit.probed_at or limit.since) >= LIMIT_RETRY


def _recovered(limit: UsageLimit, snapshot: Snapshot) -> bool:
    """Does an agent work that was stopped before, or that was started as a probe?"""
    for view in snapshot.runs:
        if not view.run.live or view.agent.limit is not None or not _going(view):
            continue
        if view.run.limited_since is not None:
            return True
        started = view.run.started_at
        if (
            limit.probed_at is not None
            and started is not None
            and started >= limit.probed_at
            and snapshot.now - started >= POLL_INTERVAL
        ):
            return True
    return False


def _picks(snapshot: Snapshot) -> Iterator[Action]:
    if not snapshot.ready:
        return
    limit = snapshot.usage_limit
    limited = _limited(snapshot)
    if limit is not None or any(_still_stopped(v, snapshot.now) for v in limited):
        probing = (
            limit is not None
            and limit.resets_at is None
            and not limited
            and _probe_due(limit, snapshot.now)
        )
        if probing and limit is not None:
            for pick in _free_picks(snapshot):
                yield SetLimit(replace(limit, probed_at=snapshot.now), new=False)
                yield replace(pick, free=1)
                return
        return
    yield from _free_picks(snapshot)


def _still_stopped(view: RunView, now: datetime) -> bool:
    limit = view.agent.limit
    return limit is not None and (limit.resets_at is None or _ahead(limit.resets_at, now))


def _free_picks(snapshot: Snapshot) -> Iterator[Pick]:
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
