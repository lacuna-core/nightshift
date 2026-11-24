"""Git, run in the user's clones and Nightshift's worktrees."""

from pathlib import Path

from nightshift.errors import GitError
from nightshift.proc import Result, Runner


class Git:
    def __init__(self, run: Runner) -> None:
        self._run = run

    def _git(self, cwd: Path, *args: str, timeout: float = 60) -> Result:
        return self._run(["git", *args], cwd=cwd, timeout=timeout)

    def _ok(self, cwd: Path, *args: str, timeout: float = 60) -> str:
        result = self._git(cwd, *args, timeout=timeout)
        if result.code != 0:
            detail = result.err.strip() or result.out.strip()
            raise GitError(f"`git {' '.join(args[:2])}` failed in {cwd}: {detail}")
        return result.out

    def is_clone(self, path: Path) -> bool:
        """Is this the top folder of a main working tree (not a worktree, not bare)?"""
        if not path.is_dir():
            return False
        result = self._git(
            path, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-dir",
            "--git-common-dir",
        )  # fmt: skip
        lines = result.out.splitlines()
        return (
            result.code == 0
            and len(lines) == 3
            and Path(lines[0]).resolve() == path.resolve()
            and lines[1] == lines[2]
        )

    def config(self, key: str, cwd: Path) -> str | None:
        """A git setting as git sees it in ``cwd``, or None if it is not set."""
        result = self._git(cwd, "config", "--get", key)
        return result.out.strip() or None if result.code == 0 else None

    def origin(self, path: Path) -> str | None:
        result = self._git(path, "remote", "get-url", "origin")
        return result.out.strip() or None if result.code == 0 else None

    # --- creating ---------------------------------------------------------------------

    def fetch(self, clone: Path) -> None:
        self._ok(clone, "fetch", "--quiet", "origin", timeout=300)

    def branch_exists(self, clone: Path, branch: str) -> bool:
        """Locally, or on GitHub as of the last fetch or right now."""
        if self._tip(clone, f"refs/heads/{branch}") or self._tip(
            clone, f"refs/remotes/origin/{branch}"
        ):
            return True
        return bool(self._ok(clone, "ls-remote", "--heads", "origin", f"refs/heads/{branch}"))

    def add_worktree(self, clone: Path, path: Path, branch: str, default_branch: str) -> None:
        """Make the worktree on its new branch. Does nothing if it is there already."""
        if (path / ".git").exists() and self.worktree_branch(path) == branch:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git(clone, "worktree", "prune")
        if self._tip(clone, f"refs/heads/{branch}"):  # made before a crash
            self._ok(clone, "worktree", "add", "--quiet", str(path), branch, timeout=600)
        else:
            self._ok(
                clone, "worktree", "add", "--quiet", "--no-track", "-b", branch, str(path),
                f"refs/remotes/origin/{default_branch}", timeout=600,
            )  # fmt: skip

    def worktree_branch(self, worktree: Path) -> str | None:
        """The checked-out branch, or None on a detached HEAD."""
        result = self._git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD")
        return result.out.strip() if result.code == 0 else None

    def repair(self, clone: Path, worktrees: list[Path]) -> None:
        """Make worktrees point at a clone that was moved."""
        if existing := [str(w) for w in worktrees if w.is_dir()]:
            self._git(clone, "worktree", "repair", *existing)

    def _tip(self, cwd: Path, ref: str) -> str | None:
        result = self._git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        return result.out.strip() if result.code == 0 else None

    # --- removing ---------------------------------------------------------------------

    def tip(self, clone: Path, branch: str) -> str | None:
        return self._tip(clone, f"refs/heads/{branch}")

    def is_ancestor(self, clone: Path, commit: str, of: str) -> bool:
        """Is the commit part of the other's history? False if either is not known here."""
        return self._git(clone, "merge-base", "--is-ancestor", commit, of).code == 0

    def unsafe(self, clone: Path, worktree: Path, branch: str, *, ignored: bool) -> str | None:
        """Why removing this worktree could lose something, or would be refused by git.

        Anything that cannot be told counts as unsafe. ``ignored``: files that git ignores
        count as work too.
        """
        try:
            return self._unsafe(clone, worktree, branch, ignored=ignored)
        except GitError as e:
            return str(e)

    def _unsafe(self, clone: Path, worktree: Path, branch: str, *, ignored: bool) -> str | None:
        ours = self._ok(clone, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        theirs = self._ok(
            worktree, "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel"
        ).splitlines()
        if theirs[0] != ours or Path(theirs[1]).resolve() != worktree.resolve():
            return "it is not a worktree of the clone"
        if self.worktree_branch(worktree) != branch:
            return f"it is not on the branch `{branch}`"
        git_dir = Path(
            self._ok(worktree, "rev-parse", "--path-format=absolute", "--git-dir").strip()
        )
        for name, what in _IN_PROGRESS.items():
            if (git_dir / name).exists():
                return f"a {what} is in progress"
        if (git_dir / "locked").exists():
            return "it is locked"
        if (git_dir / "modules").exists() or any(
            line.startswith("160000 ")
            for line in self._ok(worktree, "ls-files", "--stage").splitlines()
        ):
            return "it has submodules, which git refuses to remove"
        if any(
            line[:1].islower() or line[:1] == "S"
            for line in self._ok(worktree, "ls-files", "-v").splitlines()
        ):
            return "it has files whose changes git was told to overlook"
        changes = self._ok(
            worktree, "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
            "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none",
            *(["--ignored=traditional"] if ignored else []), timeout=300,
        ).splitlines()  # fmt: skip
        if any(not line.startswith(("??", "!!")) for line in changes):
            return "it has uncommitted changes"
        if any(line.startswith("??") for line in changes):
            return "it has untracked files"
        if changes:
            return "it has files that git ignores"
        return None

    def remove_worktree(self, clone: Path, worktree: Path) -> None:
        """Never with ``--force``: git itself refuses to remove changes."""
        self._ok(clone, "worktree", "remove", str(worktree), timeout=300)

    def delete_branch(self, clone: Path, branch: str, expected: str) -> None:
        """Delete a local branch, only while it is at the expected commit."""
        listed = self._ok(clone, "worktree", "list", "--porcelain").splitlines()
        if f"branch refs/heads/{branch}" in listed:
            raise GitError(f"branch {branch} is checked out in a worktree")
        self._ok(clone, "update-ref", "-d", f"refs/heads/{branch}", expected)
        self._git(clone, "config", "--remove-section", f"branch.{branch}")


_IN_PROGRESS = {
    "rebase-merge": "rebase",
    "rebase-apply": "rebase",
    "MERGE_HEAD": "merge",
    "CHERRY_PICK_HEAD": "cherry-pick",
    "REVERT_HEAD": "revert",
    "BISECT_LOG": "bisect",
    "sequencer": "cherry-pick or revert",
}
