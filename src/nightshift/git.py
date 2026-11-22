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
