"""Herdr, through its core's CLI ``hc``."""

import json
import re
from typing import Any

from nightshift.conventions import HERDR_MINOR, HERDR_TESTED
from nightshift.errors import HerdrError, ToolError
from nightshift.proc import Runner


class Herdr:
    def __init__(self, run: Runner) -> None:
        self._run = run

    def _hc(self, *args: str, timeout: float = 30) -> Any:
        try:
            result = self._run(["hc", *args], timeout=timeout)
        except HerdrError:
            raise
        except ToolError as e:
            raise HerdrError(str(e)) from e
        if result.code != 0:
            error = _error(result.err) or _error(result.out)
            message = error.get("message") or result.err.strip() or f"exit code {result.code}"
            raise HerdrError(f"`hc {args[0]}`: {message}", error.get("kind", ""))
        try:
            return json.loads(result.out) if result.out.strip() else {}
        except ValueError as e:
            raise HerdrError(f"`hc {args[0]}` did not answer with JSON") from e

    def version(self) -> str:
        """The running core's version. ``hc`` starts the core if it is not running."""
        return str(self._hc("server", "status", "--json").get("version", ""))


def is_supported(version: str) -> bool:
    """Same 0.x minor as the tested version, and not older than it."""
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        return False
    parts = tuple(int(p) for p in match.groups())
    tested = tuple(int(p) for p in HERDR_TESTED.split("."))
    return parts[:2] == HERDR_MINOR and parts >= tested


def _error(text: str) -> dict[str, str]:
    for line in reversed(text.strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            return {k: str(v) for k, v in data["error"].items()}
    return {}
