"""Herdr, through its core's CLI ``hc``."""

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from nightshift.conventions import HERDR_MINOR, HERDR_TESTED
from nightshift.errors import HerdrError, ToolError
from nightshift.model import Agent, AgentPlace, AgentState
from nightshift.proc import Runner

SHELL = "/bin/zsh"
RUN_LABEL = "nightshift.run"
TICKET_LABEL = "nightshift.ticket"
SHELL_LABEL = "nightshift.shell"
SPACE_LABEL = "herdr.space"
TAB_LABEL = "herdr.tab"
# Oh My Zsh would ask whether to update, and the agent would never start.
_QUIET = ("DISABLE_AUTO_UPDATE=true",)
_AGENT_SCRIPT = f'claude "$@"; exec {SHELL} -l -i'


def is_claude(name: str, argv: Sequence[str]) -> bool:
    return name == "claude" or (bool(argv) and argv[0].rsplit("/", 1)[-1] == "claude")


@dataclass(frozen=True)
class Terminal:
    """One of Herdr's terminals."""

    address: str
    live: bool
    argv: tuple[str, ...]
    cwd: str
    labels: dict[str, str]
    pid: int | None
    turn: int
    reason: str
    reported: bool
    state: AgentState
    has_status: bool
    kind: str
    fg_name: str
    fg_argv: tuple[str, ...]
    fg_cwd: str
    fg_pid: int | None

    @property
    def has_claude(self) -> bool:
        return self.live and is_claude(self.fg_name, self.fg_argv)

    @property
    def is_bare_shell(self) -> bool:
        """An empty shell prompt: no program, and not a shell that runs a script."""
        if not self.live or not self.fg_argv:
            return False
        program = self.fg_argv[0].rsplit("/", 1)[-1].lstrip("-")
        options = self.fg_argv[1:]
        return program in _SHELLS and all(o in ("-l", "-i", "-li", "--login") for o in options)

    @property
    def replaced(self) -> bool:
        """Is this a fresh shell that Herdr put in place of the lost program?"""
        if known := self.labels.get(SHELL_LABEL):
            return str(self.pid) != known
        return _AGENT_SCRIPT not in self.argv

    @property
    def place(self) -> AgentPlace:
        if not self.live:
            return AgentPlace.CLOSED
        if self.has_claude:
            return AgentPlace.ALIVE
        if self.replaced:
            return AgentPlace.LOST
        if self.is_bare_shell or (self.reported and not self.has_status):
            return AgentPlace.EXITED
        return AgentPlace.STARTING

    def agent(self) -> Agent:
        place = self.place
        # A terminal can still show the last state after Claude Code ended.
        state = self.state if place is AgentPlace.ALIVE else AgentState.NONE
        return Agent(place, state, self.turn, self.kind)


_SHELLS = frozenset({"zsh", "bash", "sh", "fish", "nu"})


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

    # --- reading ----------------------------------------------------------------------

    def get(self, address: str) -> Terminal | None:
        try:
            return _terminal(self._hc("get", address, "--json"))
        except HerdrError as e:
            if e.kind == "not_found":
                return None
            raise

    def terminals(self, *labels: str, finished: bool = False) -> list[Terminal]:
        """The terminals that carry all these ``key=value`` labels, oldest first."""
        args = ["ls", "--json"]
        if finished:
            args.append("--all")
        for label in labels:
            args += ["--label", label]
        listed = self._hc(*args).get("terminals", [])
        found = [self.get(entry["address"]) for entry in listed]
        return [t for t in found if t is not None]

    def run_terminal(self, run_id: str) -> Terminal | None:
        """The terminal of a run. The label decides, not a stored address."""
        found = self.terminals(f"{RUN_LABEL}={run_id}", finished=True)
        return max(found, key=lambda t: (t.has_claude, t.live), default=None)

    def screen(self, address: str) -> str:
        return str(self._hc("read", address, "--json").get("text", ""))

    # --- starting ---------------------------------------------------------------------

    def start_agent(
        self,
        *,
        cwd: Path,
        space: str,
        tab: str,
        run_id: str,
        ticket: str,
        claude_args: Sequence[str],
    ) -> Terminal:
        """Start Claude Code inside a shell that stays open after it.

        Its arguments are passed as arguments, so no shell interprets them.
        """
        address = self._hc(
            "run", "--json", "--bg", "--keep", "--no-parent", "--cwd", str(cwd),
            "--label", f"{SPACE_LABEL}={space}", "--label", f"{TAB_LABEL}={tab}",
            "--label", f"{RUN_LABEL}={run_id}", "--label", f"{TICKET_LABEL}={ticket}",
            "--", "/usr/bin/env", *_QUIET, SHELL, "-l", "-i", "-c", _AGENT_SCRIPT, "nightshift", *claude_args,
        )["address"]  # fmt: skip
        terminal = self.get(address)
        if terminal is None:
            raise HerdrError("the terminal was gone right after it started", "spawn_failed")
        return terminal

    def mark_shell(self, terminal: Terminal) -> None:
        """Remember the shell, to tell it from one that replaces it after a crash."""
        if terminal.pid is not None:
            self._hc("label", terminal.address, f"{SHELL_LABEL}={terminal.pid}", "--json")

    def ensure_repo_space(self, clone: Path) -> str | None:
        """Make the space on a clone if there is none. Returns a new space's ID."""
        wanted = clone.resolve()
        for entry in self._hc("ls", "--json").get("terminals", []):
            if Path(entry.get("cwd", "/")).resolve() == wanted:
                terminal = self.get(entry["address"])
                if terminal is not None and terminal.live and SPACE_LABEL in terminal.labels:
                    return None
        space = new_id("sp")
        self._hc(
            "run", "--json", "--bg", "--keep", "--no-parent", "--cwd", str(clone),
            "--label", f"{SPACE_LABEL}={space}", "--label", f"{TAB_LABEL}={new_id('tb')}",
        )  # fmt: skip
        return space

    def wait_for_claude(
        self, address: str, sleep: Callable[[float], None], seconds: int = 30
    ) -> bool:
        for _ in range(seconds * 2):
            terminal = self.get(address)
            if terminal is None or not terminal.live:
                return False
            if terminal.has_claude:
                return True
            sleep(0.5)
        return False

    # --- acting -----------------------------------------------------------------------

    def send(self, address: str, text: str) -> None:
        self._hc("send", address, text, "--json")

    def kill(self, address: str) -> None:
        self._hc("kill", address, "--json", timeout=40)

    def exit_claude(self, address: str, sleep: Callable[[float], None], seconds: int = 10) -> bool:
        """Have Claude Code exit; its shell stays. Not with ``/exit``: text that is already in
        the input box would turn that into a message."""
        for _ in range(3):
            self._hc("keys", address, "ctrl+c", "ctrl+c", "--json")
            for _ in range(seconds):
                terminal = self.get(address)
                if terminal is None or not terminal.has_claude:
                    return True
                sleep(0.4)
        return False

    def interrupt(self, address: str) -> bool:
        """Stop the current turn, not the process. Did it work?"""
        answer = self._hc("interrupt", address, "--json", timeout=40)
        return answer.get("outcome") in ("stopped", "exited")

    def name_tab(self, tab: str, name: str) -> bool:
        """Name a tab that still has its default name. False: try again later."""
        try:
            listed = self._run(["herdr-app", "ls"], timeout=10)
            if listed.code != 0:
                return False
            tabs = [t for s in json.loads(listed.out)["spaces"] for t in s["tabs"]]
            found = next((t for t in tabs if t["id"] == tab), None)
            if found is None:
                return False
            if found["name"] != "1":
                return True
            renamed = self._run(
                ["herdr-app", "do", "tab.rename", "--id", tab, "--name", name], timeout=10
            )
            return renamed.code == 0
        except ToolError, ValueError, KeyError, TypeError:
            return False


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid4().hex}"


def _terminal(data: dict[str, Any]) -> Terminal:
    status = data.get("status") or {}
    foreground = data.get("foreground") or {}
    try:
        state = AgentState(status.get("state", "none"))
    except ValueError:
        state = AgentState.NONE
    return Terminal(
        address=data["address"],
        live=data.get("state") == "live",
        argv=tuple(data.get("argv") or ()),
        cwd=data.get("cwd") or "",
        labels=dict(data.get("labels") or {}),
        pid=data.get("pid"),
        turn=data.get("turn") or 0,
        reason=data.get("reason") or "",
        reported=bool(data.get("reported")),
        state=state,
        has_status=bool(status),
        kind=status.get("kind") or "",
        fg_name=foreground.get("name") or "",
        fg_argv=tuple(foreground.get("argv") or ()),
        fg_cwd=foreground.get("cwd") or "",
        fg_pid=foreground.get("pid"),
    )


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
