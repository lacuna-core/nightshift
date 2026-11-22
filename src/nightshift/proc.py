"""Running command-line tools."""

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from nightshift.errors import ToolError


@dataclass(frozen=True)
class Result:
    code: int
    out: str
    err: str


class Runner(Protocol):
    def __call__(
        self, args: Sequence[str], *, cwd: Path | None = None, timeout: float = 60
    ) -> Result: ...


def run(args: Sequence[str], *, cwd: Path | None = None, timeout: float = 60) -> Result:
    """Run a tool to its end and return what it wrote, whatever its exit code.

    It gets its own session, so that Ctrl-C in the loop's terminal reaches only Nightshift
    and never stops a tool halfway.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    try:
        done = subprocess.run(
            list(args),
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        if cwd is not None and not cwd.is_dir():
            raise ToolError(f"folder {cwd} does not exist") from e
        raise ToolError(f"`{args[0]}` was not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise ToolError(f"`{' '.join(args[:2])}` took longer than {timeout:.0f} s") from e
    except OSError as e:
        raise ToolError(f"`{args[0]}` could not be run: {e}") from e
    return Result(done.returncode, done.stdout, done.stderr)
