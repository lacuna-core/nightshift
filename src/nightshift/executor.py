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
from nightshift.conventions import IN_PROGRESS, IN_REVIEW, LABEL, NEEDS_HUMAN
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
    Action,
    Brief,
    Claim,
    Complete,
    Drop,
    Flag,
    Item,
    NameTab,
    Pick,
    Presence,
    Provision,
    Run,
    RunState,
    RunView,
    TicketFacts,
    Unflag,
    Update,
)
from nightshift.observe import RepoState
from nightshift.project_config import Paths
from nightshift.store import DuplicateRunError, Store

Effect = dict[str, str]


class WriteTracker(Protocol):
    def read_tickets(
        self, runs: list[Run], readable: set[str]
    ) -> dict[str, TicketFacts | None]: ...
    def set_status(self, board_url: str, item_id: str, status: str) -> None: ...
    def add_label(self, run_or_item: Run | Item, label: str) -> None: ...
    def remove_label(self, run_or_item: Run | Item, label: str) -> None: ...
    def comment(self, ticket_ref: str, mark: str, text: str) -> None: ...
    def brief(self, run: Run) -> Brief: ...


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


def _context(run: object) -> Any:
    if not isinstance(run, Run):
        return logger.contextualize()
    return logger.contextualize(
        repo=run.repo, ticket_id=run.ticket_id, run_id=run.id, branch=run.branch or ""
    )
