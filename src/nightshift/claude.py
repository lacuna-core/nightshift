"""Claude Code: the ``claude`` CLI, its settings and its transcripts."""

import json
from pathlib import Path
from typing import Any

from nightshift.errors import ToolError
from nightshift.proc import Runner

NEEDED_TOOLS = ("git commit", "git push", "gh pr create")


class Claude:
    def __init__(self, run: Runner, home: Path) -> None:
        """``home`` is the user's home folder, where Claude Code keeps its own files."""
        self._run = run
        self._home = home

    def version(self) -> str:
        result = self._run(["claude", "--version"])
        if result.code != 0:
            raise ToolError(f"`claude --version` failed: {result.err.strip()}")
        return result.out.strip()

    def trusts(self, clone: Path) -> bool | None:
        """Was the trust dialog accepted for this folder? None if that cannot be told.

        The file is Claude Code's own and not documented, so this is only for a warning.
        """
        data = _read_json(self._home / ".claude.json")
        projects = data.get("projects") if data else None
        if not isinstance(projects, dict):
            return None
        folder = clone.resolve()
        return any(
            isinstance(entry := projects.get(str(path)), dict)
            and entry.get("hasTrustDialogAccepted") is True
            for path in (folder, *folder.parents)
        )

    def missing_tools(self, clone: Path) -> list[str]:
        """The commands every agent needs that the repo's allowlist does not allow."""
        allowed: list[str] = []
        for path in (
            self._home / ".claude" / "settings.json",
            clone / ".claude" / "settings.json",
        ):
            permissions = (_read_json(path) or {}).get("permissions")
            rules = permissions.get("allow") if isinstance(permissions, dict) else None
            allowed += [r for r in rules if isinstance(r, str)] if isinstance(rules, list) else []
        return [tool for tool in NEEDED_TOOLS if not any(_allows(r, tool) for r in allowed)]


def _allows(rule: str, command: str) -> bool:
    if rule in ("Bash", "Bash(*)", "Bash(:*)"):
        return True
    if not (rule.startswith("Bash(") and rule.endswith(")")):
        return False
    pattern = rule[5:-1]
    for suffix in (":*", " *", "*"):
        if pattern.endswith(suffix):
            prefix = pattern.removesuffix(suffix)
            return command == prefix or command.startswith(prefix.rstrip() + " ")
    return False


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except OSError, ValueError:
        return None
    return data if isinstance(data, dict) else None
