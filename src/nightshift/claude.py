"""Claude Code: the ``claude`` CLI, its settings and its transcripts."""

import json
import os
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nightshift.errors import ToolError
from nightshift.model import Brief, Limit
from nightshift.proc import Runner

_TAIL = 512 * 1024
_MAX_TEXT = 20_000
_MAX_COMMENTS = 20
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

    def processes_in(self, worktree: Path) -> list[int]:
        """The Claude Code processes that run in a folder, inside Herdr or not."""
        result = self._run(["lsof", "-a", "-c", "claude", "-d", "cwd", "-F", "pn"], timeout=30)
        wanted = worktree.resolve()
        found, pid = [], 0
        for line in result.out.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                pid = int(line[1:])
            elif line.startswith("n"):
                folder = Path(line[1:])
                if folder == wanted or wanted in folder.parents:
                    found.append(pid)
        return found

    def runs_in(self, worktree: Path) -> bool:
        return bool(self.processes_in(worktree))

    def limit(self, session_id: str) -> Limit | None:
        """The usage limit that stopped a session, if it did and nothing came after.

        The transcript is Claude Code's own and not documented, so anything unexpected counts
        as no limit.
        """
        for path in (self._home / ".claude" / "projects").glob(f"*/{session_id}.jsonl"):
            try:
                with path.open("rb") as file:
                    file.seek(0, os.SEEK_END)
                    file.seek(max(file.tell() - _TAIL, 0))
                    lines = file.read().decode(errors="replace").splitlines()
            except OSError:
                continue
            return _last_limit(lines)
        return None

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


def _last_limit(lines: list[str]) -> Limit | None:
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") not in ("user", "assistant"):
            continue
        if entry.get("isSidechain") or entry.get("isMeta"):
            continue
        if not entry.get("isApiErrorMessage") or entry.get("apiError") != "usage_limit_reached":
            return None
        return _limit(entry)
    return None


def _limit(entry: dict[str, Any]) -> Limit:
    try:
        at = datetime.fromisoformat(entry["timestamp"])
    except KeyError, TypeError, ValueError:
        at = datetime.fromtimestamp(0, UTC)
    quota = entry.get("quotaLimits")
    seconds = quota.get("resetsAt") if isinstance(quota, dict) else None
    try:
        resets_at = None if seconds is None else datetime.fromtimestamp(float(seconds), UTC)
    except TypeError, ValueError, OverflowError, OSError:
        resets_at = None
    if resets_at is not None and resets_at <= at:
        resets_at = None  # it said so after the reset, so it is not the real one
    return Limit(resets_at, at)


def start_args(session_id: str, prompt: str, mode: str) -> list[str]:
    return ["--permission-mode", mode, "--session-id", session_id, prompt]


def resume_line(session_id: str, message: str | None, mode: str) -> str:
    """The command that resumes a session, as typed into a shell. Holds no outside text."""
    parts = ["claude", "--permission-mode", mode, "--resume", session_id]
    return shlex.join([*parts, message] if message else parts)


def prompt(brief: Brief, branch: str) -> str:
    """The fixed prompt (spec FR-7)."""
    comments = "\n\n".join(
        f"{author} wrote:\n{_cut(text)}" for author, text in brief.comments[-_MAX_COMMENTS:]
    )
    return f"""\
You are working unattended on one ticket. Do the work and open a pull request for it.

Repository: {brief.repo}
Ticket: {brief.ticket_id} ({brief.url})
Labels: {", ".join(brief.labels) or "none"}
Branch: {branch}

# How to work

- Implement the ticket following this repository's CLAUDE.md.
- You are in a git worktree that is already on the branch `{branch}`. Work on it. Never create
  or rename a branch, even if the repository's branch-naming rule says otherwise.
- Commit with Conventional Commits.
- Push the branch (`git push -u origin {branch}`) and open a pull request against the default
  branch. Not a draft. Its body says `{brief.closing_line}`.
- **Stop at the pull request.** Never merge it, never close the issue, and never move the
  ticket on its board: that is done for you. This overrides any skill or rule in the
  repository that ships or merges.
- If you need a decision you cannot make, ask. A person reads it here.

# {brief.title}

{_cut(brief.body) or "(The ticket has no description.)"}
{f"# Comments{chr(10)}{chr(10)}{comments}" if comments else ""}"""


def _cut(text: str) -> str:
    text = text.strip().replace("\0", "")
    return text if len(text) <= _MAX_TEXT else f"{text[:_MAX_TEXT]}\n(cut: it is longer)"


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
