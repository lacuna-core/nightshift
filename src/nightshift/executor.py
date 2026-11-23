"""Carries out actions through the boundaries. Every step is safe to repeat.

What GitHub must be told (comments, labels) and notifications go through an outbox that is
written in the same transaction as the state change, so a crash loses none and, because each
comment carries a mark, posts none twice.
"""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from loguru import logger

from nightshift import claude as claude_code
from nightshift import decide, naming
from nightshift.cleanup import Cleanup, Reason
from nightshift.conventions import (
    IN_PROGRESS,
    IN_REVIEW,
    LABEL,
    NEEDS_HUMAN,
    REPORT_ONLY,
    TODO,
)
from nightshift.errors import (
    GitError,
    HerdrError,
    NightshiftError,
    TrackerAuthError,
    TrackerNotFoundError,
    TrackerUnavailableError,
)
from nightshift.git import Git
from nightshift.herdr import Herdr, new_id
from nightshift.model import (
    UNKNOWN_AGENT,
    Action,
    Agent,
    AgentPlace,
    AgentState,
    Brief,
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
    Limit,
    NameTab,
    Pick,
    Pr,
    Presence,
    Provision,
    PrState,
    Reset,
    Resume,
    Run,
    RunState,
    RunView,
    SetLimit,
    TakeOver,
    TicketFacts,
    Unflag,
    Update,
    UsageLimit,
    Wait,
)
from nightshift.observe import RepoState
from nightshift.project_config import Paths
from nightshift.store import DuplicateRunError, Store

Effect = dict[str, str]
REPORT_ONLY_KEY = "report-only-until"
LIMIT_KEY = "usage-limit"
REMOVING = "it is being removed"


class WriteTracker(Protocol):
    def read_tickets(
        self, runs: list[Run], readable: set[str]
    ) -> dict[str, TicketFacts | None]: ...
    def set_status(self, board_url: str, item_id: str, status: str) -> None: ...
    def add_label(self, run_or_item: Run | Item, label: str) -> None: ...
    def remove_label(self, run_or_item: Run | Item, label: str) -> None: ...
    def comment(self, ticket_ref: str, mark: str, text: str) -> None: ...
    def brief(self, run: Run) -> Brief: ...
    def close_pr(self, pr_ref: str) -> None: ...
    def pr_commits(self, pr_ref: str) -> frozenset[str]: ...


class ReadLimits(Protocol):
    def limit(self, session_id: str) -> Limit | None: ...


class Notifier(Protocol):
    def send(self, title: str, text: str) -> None: ...


def comment(event: str, text: str) -> Effect:
    return {"do": "comment", "event": event, "text": text}


def label(name: str) -> Effect:
    return {"do": "label", "label": name}


def unlabel(name: str) -> Effect:
    return {"do": "unlabel", "label": name}


def notification(text: str) -> Effect:
    return {"do": "notify", "text": text}


class Executor:
    def __init__(
        self,
        *,
        store: Store,
        tracker: WriteTracker,
        herdr: Herdr,
        git: Git,
        claude: ReadLimits,
        cleanup: Cleanup,
        notifier: Notifier,
        paths: Paths,
        repos: Callable[[str], RepoState | None],
        now: Callable[[], datetime],
        sleep: Callable[[float], None],
    ) -> None:
        self._store = store
        self._tracker = tracker
        self._herdr = herdr
        self._git = git
        self._claude = claude
        self._cleanup = cleanup
        self._notifier = notifier
        self._paths = paths
        self._repos = repos
        self._now = now
        self._sleep = sleep
        self.repo_problems: dict[str, str] = {}

    # --- carrying out -----------------------------------------------------------------

    def perform(self, action: Action) -> None:
        """Carry out one action. What fails is recorded and decided again on the next poll."""
        run = getattr(action, "run", None)
        key = run.id if isinstance(run, Run) else ""
        with _context(run):
            try:
                self._dispatch(action)
            except TrackerUnavailableError, TrackerAuthError:
                raise
            except NightshiftError as e:
                logger.error("{} failed, will be repeated: {}", type(action).__name__, e)
                if key:
                    self._store.put(f"failed:{key}", f"{type(action).__name__}: {e}")
                return
            if key and not isinstance(action, Update | NameTab):
                self._store.remove(f"failed:{key}")

    def _dispatch(self, action: Action) -> None:
        match action:
            case Pick():
                self._pick(action)
            case Claim(run):
                self._claim(run)
            case Drop(run, reason):
                logger.info("claim dropped: {}", reason)
                self._to(run, RunState.DISCARDED, state_reason=reason, ended_at=self._now())
            case Provision(run):
                self._provision(run)
            case Complete(run, pr):
                self._complete(run, pr.ref)
            case End():
                self._end(action)
            case Wait(run, event, reason):
                self._wait(self._fresh(run), event, reason)
            case Finish(run, reason):
                self._to(run, RunState.DONE, state_reason=reason, ended_at=self._now())
            case TakeOver(run):
                self._take_over(run)
            case Reset(run):
                self._reset(run)
            case CleanUp(run, merged):
                self._clean_up(run, merged)
            case SetLimit(limit, new):
                self._set_limit(limit, new)
            case ClearLimit():
                logger.info("the usage limit is over")
                self._store.remove(LIMIT_KEY)
            case Continue(run):
                self._continue(run)
            case GoOn(run):
                self._go_on(run)
            case Interrupt(run):
                self._interrupt(run)
            case GiveUpStopping(run):
                self._give_up_stopping(run)
            case Resume(run, message):
                self._resume(run, message)
            case Update(run, changes):
                self._store.update_run(run, **changes)
            case NameTab(run):
                name = naming.folder_name(run.branch or "")
                if self._herdr.name_tab(run.herdr_tab_id or "", name):
                    self._store.update_run(run, tab_named=True)
            case Flag(item, key, text):
                self._flag(item, key, text)
            case Unflag(key):
                self._store.remove(f"flag:{key}")

    # --- state and outbox -------------------------------------------------------------

    def _to(self, run: Run, state: RunState, *effects: Effect, **changes: Any) -> Run:
        """Change the state, and tell GitHub and you about it."""
        current = self._fresh(run)
        with self._store.transaction():
            updated = self._store.update_run(current, state=state, **changes)
            if effects:
                key = f"outbox:{run.id}"
                self._store.put(key, [*self._store.get(key, []), *effects])
        logger.info("{} -> {} {}", current.state, state, changes.get("state_reason") or "")
        self.flush(updated)
        return updated

    def _fresh(self, run: Run) -> Run:
        found = self._store.get_run(run.id)
        if found is None:
            raise NightshiftError(f"run {run.id} is not in the database")
        return found

    def flush_all(self) -> None:
        for key in self._store.items("outbox:"):
            run = self._store.get_run(key.removeprefix("outbox:"))
            if run is None:
                self._store.remove(key)
                continue
            with _context(run):
                try:
                    self.flush(run)
                except TrackerUnavailableError, TrackerAuthError:
                    raise
                except NightshiftError as e:
                    logger.error("comment or label failed, will be repeated: {}", e)
                    self._store.put(f"failed:{run.id}", f"comment or label: {e}")

    def flush(self, run: Run) -> None:
        key = f"outbox:{run.id}"
        while effects := self._store.get(key, []):
            try:
                self._effect(run, effects[0])
            except TrackerNotFoundError as e:
                logger.warning("dropped, the ticket is gone: {}", e)
            if effects[1:]:
                self._store.put(key, effects[1:])
            else:
                self._store.remove(key)

    def _effect(self, run: Run, effect: Effect) -> None:
        match effect["do"]:
            case "comment":
                mark = f"run={run.id} event={effect['event']}"
                self._tracker.comment(run.ticket_ref, mark, effect["text"])
            case "label":
                self._tracker.add_label(run, effect["label"])
            case "unlabel":
                self._tracker.remove_label(run, effect["label"])
            case "notify":
                self._notifier.send(f"{run.repo} #{run.ticket_id}", effect["text"])

    def _reread(self, run: Run) -> TicketFacts:
        facts = self._tracker.read_tickets([run], {run.board_url}).get(run.id)
        if facts is None:
            raise NightshiftError("the ticket cannot be read")
        return facts

    def _flag(self, item: Item, key: str, text: str) -> None:
        # The flag is stored last: the label and the comment are safe to repeat.
        self._tracker.add_label(item, NEEDS_HUMAN)
        self._tracker.comment(item.ticket_ref, f"event={key}", text)
        self._store.put(f"flag:{key}", item.ticket_ref)

    # --- picking ----------------------------------------------------------------------

    def _pick(self, pick: Pick) -> None:
        free = pick.free
        for item in pick.items:
            if free <= 0:
                return
            repo = self._repos(item.repo_ref)
            if repo is None or item.repo_ref in self.repo_problems:
                continue
            run = self._new_run(item, repo.config.path)
            try:
                self._git.fetch(repo.config.path)
            except NightshiftError as e:
                logger.error("{}: {}", item.repo, e)
                self.repo_problems[item.repo_ref] = f"{item.repo}: {e}"
                continue
            try:
                self._store.add_run(run)
            except DuplicateRunError:
                continue
            with _context(run):
                logger.info("claimed: {}", item.title)
            self.perform(Claim(run))
            free -= 1

    def _new_run(self, item: Item, clone: Path) -> Run:
        return Run(
            id=uuid4().hex[:12],
            repo=item.repo,
            ticket_id=item.ticket_id,
            repo_ref=item.repo_ref,
            ticket_ref=item.ticket_ref,
            clone_path=str(clone),
            board_url=item.board_url,
            board_item_id=item.item_id,
            state=RunState.CLAIMED,
            created_at=self._now(),
        )

    def _claim(self, run: Run) -> None:
        run = self._fresh(run)
        if run.state is not RunState.CLAIMED:
            return
        facts = self._reread(run)
        decision = decide.claimed(RunView(run, facts))
        if isinstance(decision, Drop):
            self._dispatch(decision)
            return
        if facts.status != IN_PROGRESS:
            self._tracker.set_status(run.board_url, facts.item_id or "", IN_PROGRESS)
        run = self._to(
            run,
            RunState.PROVISIONING,
            comment("picked", "Nightshift picked this ticket. An agent is starting on it."),
            board_item_id=facts.item_id,
            repo=facts.repo or run.repo,
        )
        self._provision(run)

    # --- starting the agent -----------------------------------------------------------

    def _provision(self, run: Run) -> None:
        run = self._fresh(run)
        if run.state is not RunState.PROVISIONING:
            return
        repo = self._repos(run.repo_ref)
        if repo is None or repo.info is None:
            raise NightshiftError("the repo cannot be reached")
        clone = Path(run.clone_path)
        self._git.fetch(clone)
        brief = self._tracker.brief(run)
        try:
            run = self._name(run, clone, brief)
            assert run.branch is not None
            worktree = Path(run.worktree_path or "")
            self._git.add_worktree(clone, worktree, run.branch, repo.info.default_branch)
            self._herdr.ensure_repo_space(clone)
            terminal = self._herdr.run_terminal(run.id)
            if terminal is None:
                terminal = self._herdr.start_agent(
                    cwd=worktree,
                    space=run.herdr_space_id or "",
                    tab=run.herdr_tab_id or "",
                    run_id=run.id,
                    ticket=f"{run.repo}#{run.ticket_id}",
                    claude_args=claude_code.start_args(
                        run.claude_session_id or "", claude_code.prompt(brief, run.branch)
                    ),
                )
                self._herdr.mark_shell(terminal)
                self._herdr.wait_for_claude(terminal.address, self._sleep)
        except GitError as e:
            self._cannot_start(run, str(e))
            return
        except HerdrError as e:
            if not e.kind:  # not refused but unreachable: that passes
                raise
            self._cannot_start(run, str(e))
            return
        self._to(
            run,
            RunState.RUNNING,
            herdr_terminal=terminal.address,
            started_at=self._now(),
            last_activity_at=self._now(),
        )

    def _name(self, run: Run, clone: Path, brief: Brief) -> Run:
        """Choose and record every name before anything is created."""
        if run.branch is not None:
            return run
        folder = self._paths.worktrees / naming.repo_folder(brief.repo)
        used = self._store.branches(run.repo_ref)

        def taken(branch: str) -> bool:
            return (
                branch in used
                or (folder / naming.folder_name(branch)).exists()
                or self._git.branch_exists(clone, branch)
            )

        branch = naming.free_branch(run.ticket_id, brief.title, taken)
        return self._store.update_run(
            run,
            branch=branch,
            worktree_path=str(folder / naming.folder_name(branch)),
            herdr_space_id=new_id("sp"),
            herdr_tab_id=new_id("tb"),
            claude_session_id=str(uuid4()),
        )

    def _resume(self, run: Run, message: str | None) -> None:
        run = self._fresh(run)
        terminal = self._herdr.run_terminal(run.id)
        if terminal is None or terminal.place is not AgentPlace.LOST:
            return  # the next poll sees what changed
        if not Path(run.worktree_path or "").is_dir():
            self._fail(run, "The agent was lost in a crash or restart, and its worktree is gone.")
            return
        if not terminal.is_bare_shell:
            self._wait(
                run,
                f"waiting-resumed-{run.resume_count}",
                "The agent was lost in a crash or restart, and its terminal is in use, so "
                "Nightshift does not resume it.",
            )
            return
        run = self._store.update_run(run, resume_count=run.resume_count + 1)
        logger.info("resuming the lost agent")
        line = claude_code.resume_line(run.claude_session_id or "", message)
        self._herdr.send(terminal.address, line)
        if not self._herdr.wait_for_claude(terminal.address, self._sleep):
            # The shell may have asked something first, which took the line as its answer.
            again = self._herdr.get(terminal.address)
            if again is not None and again.is_bare_shell:
                self._herdr.send(terminal.address, line)
                self._herdr.wait_for_claude(terminal.address, self._sleep)
        self._herdr.mark_shell(terminal)

    def _wait(self, run: Run, event: str, reason: str) -> None:
        if run.state is RunState.WAITING:
            return
        self._to(
            run,
            RunState.WAITING,
            label(NEEDS_HUMAN),
            comment(event, f"The agent needs you. {reason}"),
            notification(f"Needs you: {reason}"),
            state_reason=reason,
        )

    def _end(self, end: End) -> None:
        run = self._fresh(end.run)
        if not run.live:
            return
        agent = self._agent(run)
        busy = agent.place is AgentPlace.STARTING or (
            agent.place is AgentPlace.ALIVE
            and agent.state in (AgentState.WORKING, AgentState.BLOCKED)
        )
        # The turn is recorded first: only that one may be interrupted, not a later one.
        run = self._store.update_run(
            run,
            turn_at_end=agent.turn,
            stop_requested_at=self._now() if end.interrupt and busy else None,
        )
        if end.state is RunState.FAILED:
            run = self._fail(run, end.reason)
        else:
            run = self._cancel(run, end)
        if run.stop_requested_at is not None:
            self._interrupt(run)

    def _cancel(self, run: Run, end: End) -> Run:
        effects = [label(NEEDS_HUMAN)] if end.needs_human else []
        effects.append(comment(end.event, f"Nightshift stopped working on this. {end.reason}"))
        return self._to(
            run,
            end.state,
            *effects,
            state_reason=end.reason,
            ended_at=self._now(),
            taken_over=run.taken_over or end.event == "taken-over",
        )

    def _agent(self, run: Run) -> Agent:
        if run.claude_session_id is None:
            return UNKNOWN_AGENT
        terminal = self._herdr.run_terminal(run.id)
        return Agent(AgentPlace.CLOSED) if terminal is None else terminal.agent()

    def _interrupt(self, run: Run) -> None:
        """Stop the turn the run ended in, and see whether it worked."""
        run = self._fresh(run)
        terminal = self._herdr.run_terminal(run.id)
        if run.stop_requested_at is None or terminal is None or not terminal.has_claude:
            return
        if terminal.turn != run.turn_at_end:
            return  # a new turn, which is yours
        logger.info("interrupting the agent")
        if self._herdr.interrupt(terminal.address):
            turn = self._agent(run).turn
            self._store.update_run(run, stop_requested_at=None, turn_at_end=turn)

    def _take_over(self, run: Run) -> None:
        text = (
            f"The `{LABEL}` label was removed, so this is yours now. Nightshift leaves the "
            "agent's tab, the worktree, the branch and the pull request alone."
        )
        self._to(run, run.state, comment("taken-over", text), taken_over=True)

    def _give_up_stopping(self, run: Run) -> None:
        self._to(
            run,
            run.state,
            label(NEEDS_HUMAN),
            comment("cannot-stop", "The agent cannot be stopped. Please stop it in its tab."),
            notification("The agent cannot be stopped"),
            stop_requested_at=None,
        )

    def _go_on(self, run: Run) -> None:
        self._to(run, RunState.RUNNING, unlabel(NEEDS_HUMAN), state_reason=None)

    def _cannot_start(self, run: Run, reason: str) -> None:
        self._fail(run, f"The agent could not be started: {reason}")

    def _fail(self, run: Run, reason: str) -> Run:
        return self._to(
            run,
            RunState.FAILED,
            label(NEEDS_HUMAN),
            comment(
                "failed",
                f"The run failed. {reason}\n\nThe branch and the worktree are kept. "
                "Move the ticket to `Todo` to start over.",
            ),
            notification(f"Failed: {reason}"),
            state_reason=reason,
            ended_at=self._now(),
        )

    # --- starting over ----------------------------------------------------------------

    def _reset(self, run: Run) -> None:
        run = self._fresh(run)
        if run.state is not RunState.RESETTING:
            facts = self._reread(run)
            if not (
                facts.presence is Presence.ON
                and facts.status == TODO
                and facts.is_open
                and LABEL in facts.labels
            ):
                return  # the next poll sees what changed
            run = self._to(run, RunState.RESETTING)
        closed: list[str] = self._store.get(f"reset:{run.id}", [])
        if not run.taken_over:
            if run.ended_at is None:
                self._end_agent(run)
            closed = self._close_prs(run, closed)
        self._tracker.remove_label(run, NEEDS_HUMAN)

        worktree = Path(run.worktree_path) if run.worktree_path else None
        gone = worktree is None or not worktree.exists()
        kept = None if gone or run.taken_over else self._why_kept(run, Reason.DISCARDED)
        run = self._to(
            run,
            RunState.DISCARDED,
            comment("start-over", _start_over_text(run, closed, kept)),
            ended_at=run.ended_at or self._now(),
            stop_requested_at=None,
            kept_reason=kept,
            cleaned_up_at=self._now() if gone else run.cleaned_up_at,
        )
        self._store.remove(f"reset:{run.id}")
        if not gone and not run.taken_over and kept is None:
            self._clean_up(run, None)

    def _end_agent(self, run: Run) -> None:
        """Interrupt the agent and have Claude Code exit. Its shell stays."""
        if run.claude_session_id is None:
            return
        terminal = self._herdr.run_terminal(run.id)
        if terminal is None or not terminal.live:
            return
        if terminal.place is AgentPlace.STARTING:
            raise NightshiftError("the agent is still starting, so it cannot be ended yet")
        if not terminal.has_claude:
            return
        if terminal.state in (AgentState.WORKING, AgentState.BLOCKED):
            self._herdr.interrupt(terminal.address)
        if not self._herdr.exit_claude(terminal.address, self._sleep):
            raise NightshiftError("the agent could not be ended")

    def _close_prs(self, run: Run, closed: list[str]) -> list[str]:
        """Close the open PRs of the ticket's runs, except of those you took over."""
        runs = [
            r
            for r in self._store.ticket_runs(run.ticket_ref)
            if r.branch and not r.taken_over and r.repo_ref == run.repo_ref
        ]
        for facts in self._tracker.read_tickets(runs, set()).values():
            if facts is None:
                raise NightshiftError("the ticket's pull requests cannot be read")
            for pr in facts.prs:
                if pr.state is PrState.OPEN:
                    closed = [*closed, pr.url]
                    self._store.put(f"reset:{run.id}", closed)
                    self._tracker.close_pr(pr.ref)
        return list(dict.fromkeys(closed))

    # --- cleaning up ------------------------------------------------------------------

    def _report_only(self) -> str | None:
        """In its first week Nightshift only says what it would remove."""
        until = self._store.get(REPORT_ONLY_KEY)
        if until is None:
            until = (self._now() + REPORT_ONLY).isoformat()
            self._store.put(REPORT_ONLY_KEY, until)
        if self._now() >= datetime.fromisoformat(until):
            return None
        return f"Nightshift removes nothing before {until[:10]}, its first week"

    def _why_kept(self, run: Run, reason: Reason) -> str | None:
        verdict = self._cleanup.check(run, reason, frozenset(), "")
        return verdict.unsafe or self._report_only()

    def _clean_up(self, run: Run, pr: Pr | None) -> None:
        run = self._fresh(run)
        worktree, clone = Path(run.worktree_path or ""), Path(run.clone_path)
        if run.state is RunState.DISCARDED:
            reason = Reason.DISCARDED
        else:
            reason = Reason.MERGED if pr else Reason.CLOSED
        if not clone.is_dir():
            raise NightshiftError(f"the clone {clone} is missing")
        commits = self._tracker.pr_commits(pr.ref) if pr else frozenset()
        head = pr.head if pr else ""
        if not worktree.exists():
            # Removed by you, or by Nightshift right before a crash.
            if run.kept_reason == REMOVING and pr and run.branch:
                tip = self._git.tip(clone, run.branch)
                if tip and self._cleanup.is_merged(clone, tip, commits, head):
                    self._git.delete_branch(clone, run.branch, tip)
            self._store.update_run(run, cleaned_up_at=self._now(), kept_reason=None)
            return
        verdict = self._cleanup.check(run, reason, commits, head)
        if verdict.unsafe is not None:
            self._keep(run, reason, verdict.unsafe)
            return
        if wait := self._report_only():
            if run.kept_reason != wait:
                logger.info("would remove {}: {}", worktree, reason)
                self._store.update_run(run, kept_reason=wait)
            return

        logger.info("removing {}: {}", worktree, reason)
        run = self._store.update_run(run, kept_reason=REMOVING)
        self._cleanup.close_space(run)
        verdict = self._cleanup.check(run, reason, commits, head)
        if verdict.unsafe is not None:
            self._keep(run, reason, verdict.unsafe)
            return
        self._git.remove_worktree(clone, worktree)
        if verdict.branch_tip and run.branch:
            self._git.delete_branch(clone, run.branch, verdict.branch_tip)
        self._store.update_run(run, cleaned_up_at=self._now(), kept_reason=None)

    def _keep(self, run: Run, reason: Reason, why: str) -> None:
        if run.kept_reason == why:
            return
        text = (
            f"The worktree `{run.worktree_path}` could be removed, because {reason}, but "
            f"Nightshift keeps it: {why}. It looks again on every poll."
        )
        # A discarded run that kept its worktree said so when it started over.
        told = reason is Reason.DISCARDED and run.kept_reason not in (None, REMOVING)
        effects = [] if told else [comment("kept", text)]
        self._to(run, run.state, *effects, kept_reason=why)

    # --- usage limits -----------------------------------------------------------------

    def _set_limit(self, limit: UsageLimit, new: bool) -> None:
        self._store.put(
            LIMIT_KEY,
            {
                "since": limit.since.isoformat(),
                "resets_at": limit.resets_at.isoformat() if limit.resets_at else None,
                "probed_at": limit.probed_at.isoformat() if limit.probed_at else None,
            },
        )
        if new:
            at = limit.resets_at.astimezone().strftime("%H:%M") if limit.resets_at else None
            text = f"It resets at {at}." if at else "It is not known when it resets."
            logger.info("usage limit: no new tickets are picked. {}", text)
            self._notifier.send("Nightshift: usage limit", text)

    def _continue(self, run: Run) -> None:
        run = self._fresh(run)
        terminal = self._herdr.run_terminal(run.id)
        if not run.live or terminal is None or not terminal.has_claude:
            return
        if self._limit(run) is None or terminal.state is AgentState.WORKING:
            return  # you continued it yourself
        logger.info("the usage limit is over for this agent: continuing")
        if terminal.state is AgentState.BLOCKED:
            self._herdr.interrupt(terminal.address)  # close the limit's own menu
        self._herdr.send(terminal.address, "continue")

    def _limit(self, run: Run) -> Limit | None:
        return self._claude.limit(run.claude_session_id or "")

    # --- handing over -----------------------------------------------------------------

    def _complete(self, run: Run, pr_ref: str) -> None:
        run = self._fresh(run)
        facts = self._reread(run)
        pr = facts.pr
        still = (
            pr is not None
            and pr.ready
            and facts.is_open
            and not facts.label_removed
            and LABEL in facts.labels
            and facts.presence is Presence.ON
            and facts.status in (IN_PROGRESS, IN_REVIEW)
        )
        if not still or pr is None:
            return  # the next poll sees what changed
        run = self._to(run, RunState.COMPLETING, pr_ref=pr.ref)
        if facts.status == IN_PROGRESS:
            self._tracker.set_status(run.board_url, facts.item_id or "", IN_REVIEW)
        effects = [comment("in-review", f"The pull request is ready for review: {pr.url}")]
        if NEEDS_HUMAN in facts.labels:
            effects.append(unlabel(NEEDS_HUMAN))
        effects.append(notification("In Review"))
        self._to(run, RunState.DONE, *effects, ended_at=self._now())


def _start_over_text(run: Run, closed: list[str], kept: str | None) -> str:
    if run.taken_over:
        return (
            "The ticket starts over. You took its earlier run over, so Nightshift leaves that "
            f"run's branch `{run.branch}`, its pull request, its worktree and its agent alone."
        )
    lines = ["The ticket starts over.", ""]
    lines += [f"- Closed: {url}" for url in closed]
    if run.branch:
        lines.append(f"- Kept: the branch `{run.branch}`, with every commit on it.")
    if kept:
        lines.append(f"- Kept for now: the worktree `{run.worktree_path}`, because {kept}.")
    return "\n".join(lines)


def _context(run: object) -> Any:
    if not isinstance(run, Run):
        return logger.contextualize()
    return logger.contextualize(
        repo=run.repo, ticket_id=run.ticket_id, run_id=run.id, branch=run.branch or ""
    )
