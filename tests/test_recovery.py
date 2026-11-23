"""NFR-4: after any crash, no ticket is lost and none has two agents."""

from pathlib import Path

import pytest

from nightshift.conventions import NEEDS_HUMAN
from nightshift.model import RunState
from tests.world import BOARD, Crash, World


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def started(world: World) -> None:
    world.issue()
    world.tick()
    world.agent("working")


def test_agent_survives_a_restart_of_nightshift(world: World) -> None:
    started(world)
    world.restart()
    world.tick(3)
    assert world.state() is RunState.RUNNING
    assert len(world.hc.starts) == 1


def test_finished_while_nightshift_was_stopped(world: World) -> None:
    started(world)
    issue = world.tracker.issues["I_acme/web_42"]
    world.open_pr(issue)
    world.agent("done")
    world.restart()
    world.tick(2)
    assert world.state() is RunState.DONE


def test_lost_agent_is_resumed_once(world: World) -> None:
    started(world)
    session = world.run().claude_session_id
    world.hc.reboot()
    world.tick()
    assert "sent" not in world.terminal()
    world.tick()
    assert world.terminal()["sent"] == [
        f"claude --permission-mode acceptEdits --resume {session} continue"
    ]
    assert world.state() is RunState.RUNNING
    world.tick(3)
    assert len(world.terminal()["sent"]) == 1
    world.hc.reboot()
    world.tick(2)
    assert world.state() is RunState.FAILED
    assert "second time" in (world.run().state_reason or "")


def test_resume_is_typed_again_if_the_shell_took_it_as_an_answer(world: World) -> None:
    started(world)
    world.hc.reboot()
    world.hc.shell_asks = True
    world.tick(2)
    assert len(world.terminal()["sent"]) == 2
    world.tick(3)
    assert world.state() is RunState.RUNNING


def test_resumed_agent_that_exits_fails(world: World) -> None:
    started(world)
    world.hc.reboot()
    world.tick(2)
    world.hc.exit_claude(world.terminal())
    world.tick(2)
    assert world.state() is RunState.FAILED
    assert "exited" in (world.run().state_reason or "")


def test_lost_agent_with_a_ready_pr_is_complete(world: World) -> None:
    started(world)
    world.open_pr(world.tracker.issues["I_acme/web_42"])
    world.hc.reboot()
    world.tick(2)
    assert world.state() is RunState.DONE
    assert "sent" not in world.terminal()


def test_not_resumed_while_claude_runs_in_the_worktree(world: World) -> None:
    started(world)
    world.hc.reboot()
    world.hc.elsewhere = [str(Path(world.run().worktree_path or "").resolve() / "src")]
    world.tick(3)
    assert "sent" not in world.terminal()
    assert world.state() is RunState.WAITING


def test_not_resumed_while_its_terminal_is_in_use(world: World) -> None:
    started(world)
    world.hc.reboot()
    terminal = world.terminal()
    terminal["foreground"] = terminal["foreground"] | {"name": "vim", "argv": ["vim"]}
    world.tick(3)
    assert "sent" not in terminal
    assert world.state() is RunState.WAITING


def test_closed_terminal_cancels(world: World) -> None:
    started(world)
    issue = world.tracker.issues["I_acme/web_42"]
    world.hc._kill([world.terminal()["address"]])
    world.restart()
    world.tick(2)
    assert world.state() is RunState.CANCELLED
    assert NEEDS_HUMAN in issue.labels
    assert issue.status[BOARD] == "In Progress"


def test_exited_claude_fails(world: World) -> None:
    started(world)
    issue = world.tracker.issues["I_acme/web_42"]
    world.hc.exit_claude(world.terminal())
    world.tick()
    assert world.state() is RunState.RUNNING
    world.tick()
    assert world.state() is RunState.FAILED
    assert world.events(issue) == ["picked", "failed"]


def test_herdr_down_changes_nothing(world: World) -> None:
    started(world)
    world.hc.down = True
    world.tick(10)
    assert world.state() is RunState.RUNNING
    world.hc.down = False
    world.tick(2)
    assert world.state() is RunState.RUNNING


def test_github_down_changes_nothing(world: World) -> None:
    started(world)
    world.hc.exit_claude(world.terminal())
    world.tracker.down = True
    world.tick(10)
    assert world.state() is RunState.RUNNING


def play(world: World, crash_at: int | None) -> int:
    """One ticket from Todo to In Review, killed at one of its side effects."""
    count = 0

    def effect(what: str) -> None:
        nonlocal count
        if what in ("ls", "get", "read", "server"):
            return
        count += 1
        if count == crash_at:
            raise Crash

    world.tracker.before_write = effect
    world.hc.before = effect
    issue = world.issue()

    def tick(times: int) -> None:
        for _ in range(times):
            try:
                world.tick()
            except Crash:
                world.restart()

    tick(4)
    world.agent("working")
    world.open_pr(issue)
    world.agent("done")
    tick(5)
    return count


def outcome(world: World) -> object:
    issue = world.tracker.issues["I_acme/web_42"]
    agents = [t for t in world.hc.terminals.values() if "nightshift.run" in t["labels"]]
    return (
        [(r.state, r.branch) for r in world.store.runs()],
        world.events(issue),
        issue.status,
        issue.labels,
        len(agents),
    )


def test_killed_at_every_side_effect(tmp_path: Path) -> None:
    (tmp_path / "whole").mkdir()
    whole = World(tmp_path / "whole")
    effects = play(whole, None)
    assert effects > 5
    for n in range(1, effects + 1):
        folder = tmp_path / f"crash{n}"
        folder.mkdir()
        crashed = World(folder)
        play(crashed, n)
        assert outcome(crashed) == outcome(whole), f"crash at side effect {n}"


def test_killed_after_every_database_write(tmp_path: Path) -> None:
    (tmp_path / "whole").mkdir()
    whole = World(tmp_path / "whole")
    play(whole, None)
    n = 0
    while True:
        n += 1
        folder = tmp_path / f"crash{n}"
        folder.mkdir()
        world = World(folder)
        left = n

        def crashing(world: World = world) -> None:
            real = world.store.update_run

            def update(run, **changes):  # type: ignore[no-untyped-def]
                nonlocal left
                done = real(run, **changes)
                left -= 1
                if left == 0 and not world.store._db.in_transaction:
                    raise Crash
                return done

            world.store.update_run = update  # type: ignore[method-assign]

        crashing()
        play(world, None)
        assert outcome(world) == outcome(whole), f"crash after write {n}"
        if left > 0:
            break
