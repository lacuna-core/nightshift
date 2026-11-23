"""Herdr's answers as recorded from core 0.1.18."""

import json
from typing import Any

import pytest

from nightshift.herdr import _AGENT_SCRIPT, Herdr, Terminal
from nightshift.model import AgentPlace, AgentState
from nightshift.proc import Result
from tests.fakes import FakeRunner, ok

OURS = ["/bin/zsh", "-l", "-i", "-c", _AGENT_SCRIPT, "nightshift", "--session-id", "x", "prompt"]
CLAUDE = {"argv": ["claude"], "cwd": "/wt", "name": "claude", "pid": 46474}
SHELL_AFTER = {"argv": ["/bin/zsh", "-l", "-i"], "cwd": "/wt", "name": "zsh", "pid": 60831}
SHELL_FRESH = {"argv": ["/bin/zsh", "-l"], "cwd": "/wt", "name": "zsh", "pid": 63039}


def recorded(**changes: Any) -> Terminal:
    data = {
        "address": "t3", "argv": OURS, "cwd": "/wt", "exit_code": None, "foreground": CLAUDE,
        "keep": True, "labels": {"nightshift.run": "r1"}, "pid": 60831, "reason": None,
        "reported": True, "state": "live", "status": {"app": "claude-code", "state": "working"},
        "turn": 5,
    }  # fmt: skip
    found = Herdr(FakeRunner({"hc get": ok(json.dumps(data | changes))})).get("t3")
    assert found is not None
    return found


def test_working() -> None:
    agent = recorded().agent()
    assert (agent.place, agent.state, agent.turn) == (AgentPlace.ALIVE, AgentState.WORKING, 5)


def test_blocked_says_what_on() -> None:
    status = {"app": "claude-code", "kind": "permission", "msg": "approve", "state": "blocked"}
    agent = recorded(status=status).agent()
    assert (agent.state, agent.kind) == (AgentState.BLOCKED, "permission")


def test_trust_dialog_reports_nothing() -> None:
    agent = recorded(status=None, reported=False, turn=0).agent()
    assert (agent.place, agent.state) == (AgentPlace.ALIVE, AgentState.NONE)


@pytest.mark.parametrize("status", [None, {"app": "claude-code", "state": "done"}])
def test_exited_also_when_the_last_state_still_shows(status: Any) -> None:
    agent = recorded(foreground=SHELL_AFTER, status=status).agent()
    assert (agent.place, agent.state) == (AgentPlace.EXITED, AgentState.NONE)


def test_exited_while_you_use_its_shell() -> None:
    vim = {"argv": ["vim"], "cwd": "/wt", "name": "vim", "pid": 7}
    assert recorded(foreground=vim, status=None).place is AgentPlace.EXITED


@pytest.mark.parametrize("labels", [{}, {"nightshift.shell": "60831"}])
def test_lost_after_a_reboot(labels: dict[str, str]) -> None:
    lost = recorded(
        argv=["/bin/zsh", "-l"], pid=63039, foreground=SHELL_FRESH, status=None, reported=False,
        labels=labels,
    )  # fmt: skip
    assert lost.place is AgentPlace.LOST


def test_resumed_agent_that_exited_is_not_lost_again() -> None:
    exited = recorded(
        argv=["/bin/zsh", "-l"], pid=63039, foreground=SHELL_FRESH, status=None,
        labels={"nightshift.shell": "63039"},
    )  # fmt: skip
    assert exited.place is AgentPlace.EXITED


def test_still_starting() -> None:
    starting = {"argv": OURS[:5], "cwd": "/wt", "name": "zsh", "pid": 60831}
    terminal = recorded(foreground=starting, status=None, reported=False)
    assert terminal.place is AgentPlace.STARTING
    assert recorded(foreground=None, status=None, reported=False).place is AgentPlace.STARTING


def test_closed() -> None:
    closed = recorded(state="finished", reason="killed", foreground=None, status=None)
    assert closed.place is AgentPlace.CLOSED


def test_unknown_state_is_none() -> None:
    assert recorded(status={"state": "dreaming"}).state is AgentState.NONE


def test_not_found() -> None:
    error = '{"error":{"kind":"not_found","message":"no terminal \\"t999\\""}}'
    assert Herdr(FakeRunner({"hc get": Result(3, "", error)})).get("t999") is None


def test_the_label_decides_and_the_live_terminal_wins() -> None:
    terminals = {
        "t1": {"address": "t1", "state": "finished"},
        "t2": {"address": "t2", "state": "live", "argv": OURS, "foreground": CLAUDE},
    }
    runner = FakeRunner(
        {
            "hc ls": ok(json.dumps({"terminals": list(terminals.values())})),
            "hc get": lambda args: ok(json.dumps(terminals[args[2]])),
        }
    )
    found = Herdr(runner).run_terminal("r1")
    assert found is not None
    assert found.address == "t2"
    assert runner.calls[0] == ["hc", "ls", "--json", "--all", "--label", "nightshift.run=r1"]
