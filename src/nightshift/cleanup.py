"""Is it safe to close a run's space and remove its worktree (spec FR-13)?"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from nightshift.claude import Claude
from nightshift.errors import NightshiftError
from nightshift.git import Git
from nightshift.herdr import RUN_LABEL, SPACE_LABEL, Herdr, Terminal
from nightshift.model import AgentState, Run

_BUSY = (AgentState.WORKING, AgentState.BLOCKED)


class Reason(StrEnum):
    MERGED = "the pull request is merged"
    CLOSED = "the issue is closed and you closed the agent's tab"
    DISCARDED = "the ticket started over"


def inside(folder: str, worktree: Path) -> bool:
    if not folder:
        return False
    path, root = Path(folder).resolve(), worktree.resolve()
    return path == root or root in path.parents


def in_worktree(terminal: Terminal, worktree: Path) -> bool:
    return inside(terminal.cwd, worktree) or inside(terminal.fg_cwd, worktree)


def in_space(terminal: Terminal, run: Run) -> bool:
    if terminal.labels.get(RUN_LABEL) == run.id:
        return True
    return bool(run.herdr_space_id) and terminal.labels.get(SPACE_LABEL) == run.herdr_space_id


def in_use(
    run: Run, reason: Reason, terminals: Sequence[Terminal], outside: Sequence[int]
) -> str | None:
    """Who uses the worktree. ``outside``: Claude Code processes in it, inside Herdr or not."""
    worktree = Path(run.worktree_path or "")
    near = [t for t in terminals if t.live and (in_worktree(t, worktree) or in_space(t, run))]
    if any(t.state in _BUSY and t.has_status for t in near):
        return "an agent is working in it"
    known = {t.fg_pid for t in near}
    if any(pid not in known for pid in outside):
        return "Claude Code runs in it outside its tab"
    if reason is not Reason.DISCARDED:
        return None
    for terminal in near:
        if terminal.labels.get(RUN_LABEL) != run.id:
            return "another terminal is open in it"
        if not terminal.has_claude and not terminal.is_bare_shell:
            return "a program runs in the agent's tab"
    return None


@dataclass(frozen=True)
class Verdict:
    unsafe: str | None
    branch_tip: str | None = None
    """The local branch, if it may be deleted: at this commit only."""


class Cleanup:
    def __init__(self, herdr: Herdr, claude: Claude, git: Git) -> None:
        self._herdr = herdr
        self._claude = claude
        self._git = git

    def check(self, run: Run, reason: Reason, merged: frozenset[str], head: str) -> Verdict:
        """Everything FR-13 asks for, at once. ``merged`` and ``head``: the merged PR's commits."""
        clone, worktree = Path(run.clone_path), Path(run.worktree_path or "")
        branch = run.branch or ""
        try:
            unsafe = in_use(
                run, reason, self._herdr.terminals(), self._claude.processes_in(worktree)
            ) or self._git.unsafe(clone, worktree, branch, ignored=reason is Reason.DISCARDED)
            if unsafe is not None or reason is not Reason.MERGED:
                return Verdict(unsafe)
            tip = self._git.tip(clone, branch)
        except NightshiftError as e:
            return Verdict(str(e))
        if tip is None or not self.is_merged(clone, tip, merged, head):
            return Verdict("its branch has commits that are not in the merged pull request")
        return Verdict(None, tip)

    def is_merged(self, clone: Path, tip: str, merged: frozenset[str], head: str) -> bool:
        return tip in merged or self._git.is_ancestor(clone, tip, head)

    def close_space(self, run: Run) -> None:
        """Close the worktree space, with every tab in it."""
        for terminal in self._herdr.terminals():
            if terminal.live and in_space(terminal, run):
                self._herdr.kill(terminal.address)
