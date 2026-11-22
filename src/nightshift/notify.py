"""macOS notifications."""

from loguru import logger

from nightshift.errors import ToolError
from nightshift.proc import Runner

_SCRIPT = (
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "end run",
)


class Notifier:
    def __init__(self, run: Runner) -> None:
        self._run = run

    def send(self, title: str, text: str) -> None:
        """Show a notification. One that cannot be shown never holds anything up."""
        script = [part for line in _SCRIPT for part in ("-e", line)]
        try:
            result = self._run(["osascript", *script, title, text], timeout=10)
        except ToolError as e:
            logger.warning("notification not shown: {}", e)
            return
        if result.code != 0:
            logger.warning("notification not shown: {}", result.err.strip())
