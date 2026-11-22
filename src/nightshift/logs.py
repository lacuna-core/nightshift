"""Logging of the loop (spec NFR-7)."""

from pathlib import Path
from typing import Any, TextIO

from loguru import logger

_FIELDS = ("repo", "ticket_id", "run_id", "branch")


def _format(record: Any) -> str:
    fields = " ".join(f"{k}={{extra[{k}]}}" for k in _FIELDS if record["extra"].get(k))
    return (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> <level>{level: <7}</level> {message}"
        + (f" <dim>{fields}</dim>" if fields else "")
        + "\n{exception}"
    )


def configure(path: Path, level: str, *, stdout: TextIO) -> None:
    """Log to stdout, and as JSON lines to one rotated file."""
    logger.remove()
    logger.add(stdout, level=level.upper(), format=_format)
    logger.add(
        path, level=level.upper(), serialize=True, rotation="10 MB", retention="14 days",
        encoding="utf-8", diagnose=False,
    )  # fmt: skip
