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
