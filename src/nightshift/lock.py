"""The single-instance lock (spec NFR-5)."""

import fcntl
import os
from pathlib import Path
from typing import IO

from nightshift.errors import NightshiftError


class Lock:
    """Held by the running loop. The system releases it when the process ends, however it ends."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: IO[str] | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        file = self._path.open("a+")
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            file.close()
            pid = holder(self._path)
            raise NightshiftError(
                f"Nightshift is already running (process {pid or 'unknown'}). "
                "Stop it with `nightshift stop`."
            ) from None
        file.truncate(0)
        file.write(str(os.getpid()))
        file.flush()
        self._file = file

    def release(self) -> None:
        if self._file is not None:
            self._file.truncate(0)
            self._file.close()
            self._file = None


def holder(path: Path) -> int | None:
    """The process of the running loop, or None if no loop runs."""
    try:
        with path.open("r") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                text = file.read().strip()
                return int(text) if text.isdigit() else 0
            return None
    except FileNotFoundError:
        return None
