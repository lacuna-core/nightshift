from pathlib import Path

import pytest

from nightshift.conventions import NEEDS_HUMAN
from nightshift.model import RunState
from nightshift.proc import Result
from tests.world import BOARD, World, git


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_one_ticket_from_todo_to_in_review(world: World) -> None:
    issue = world.issue()
    world.tick()
    run = world.run()
    assert run.state is RunState.RUNNING
    assert run.branch == "42/export-csv"
    assert issue.status[BOARD] == "In Progress"
    worktree = world.paths.worktrees / "acme-web" / "42-export-csv"
    assert run.worktree_path == str(worktree)
    assert git(worktree, "symbolic-ref", "--short", "HEAD") == "42/export-csv"
    assert world.git._git(worktree, "rev-parse", "--abbrev-ref", "@{u}").code != 0
    assert git(world.tmp / "web", "symbolic-ref", "--short", "HEAD") == "main"

    world.agent("working")
    world.tick(3)
    assert world.state() is RunState.RUNNING
    pr = world.open_pr(issue)
    world.tick()
    assert world.state() is RunState.RUNNING
    world.agent("done")
    world.tick()
    assert world.state() is RunState.RUNNING  # only one poll
    world.tick()
    assert world.state() is RunState.DONE
    assert issue.status[BOARD] == "In Review"
    assert world.events(issue) == ["picked", "in-review"]
    assert pr.url in issue.comments[-1]
    assert world.notifier.sent == ["In Review"]
    assert worktree.is_dir()
    assert issue.is_open


def test_how_the_agent_is_started(world: World) -> None:
    world.issue(title="Danger $(rm -rf /) `x` \"y\" 'z'")
    world.tick()
    argv = world.hc.starts[0]
    assert argv[:4] == ["/bin/zsh", "-l", "-i", "-c"]
    assert argv[4] == 'claude "$@"; exec /bin/zsh -l -i'
    assert argv[6:8] == ["--permission-mode", "acceptEdits"]
    assert argv[8:10] == ["--session-id", world.run().claude_session_id]
    prompt = argv[10]
    assert "$(rm -rf /)" in prompt
    assert "Closes #1" in prompt
    assert "42/danger-rm-rf-x-y-z" in prompt
    assert not any("dangerously" in a for a in argv)
    labels = world.terminal()["labels"]
    assert labels["herdr.space"] == world.run().herdr_space_id
    assert labels["nightshift.ticket"] == "acme/web#42"


def test_slots_and_rank(world: World) -> None:
    for number in (1, 2, 3):
        world.issue(number, title=f"Ticket {number}")
    world.tick()
    assert [r.ticket_id for r in world.store.runs()] == ["1", "2"]
    world.open_pr(world.tracker.issues["I_acme/web_1"])
    world.agent("done", 1)
    world.tick(2)
    assert world.state(1) is RunState.DONE
    world.tick()
    assert world.state(3) is RunState.RUNNING


def test_branch_name_taken(world: World) -> None:
    git(world.tmp / "web", "branch", "42/export-csv")
    git(world.tmp / "web", "push", "-q", "origin", "HEAD:refs/heads/42/export-csv-2")
    (world.paths.worktrees / "acme-web" / "42-export-csv-3").mkdir(parents=True)
    world.issue()
    world.tick()
    assert world.run().branch == "42/export-csv-4"


def test_repo_space_is_made_once(world: World) -> None:
    world.issue(1, title="One")
    world.issue(2, title="Two")
    world.tick()
    clone = str(world.tmp / "web")
    assert sum(t["cwd"] == clone for t in world.hc.terminals.values()) == 1


def test_fetch_failure_leaves_the_ticket_in_todo(world: World) -> None:
    issue = world.issue()
    (world.tmp / "web.git").rename(world.tmp / "away.git")
    world.tick(3)
    assert world.store.runs() == []
    assert issue.status[BOARD] == "Todo"
    assert "acme/web" in world.store.get("report")["problems"]
    (world.tmp / "away.git").rename(world.tmp / "web.git")
    world.tick()
    assert world.state() is RunState.RUNNING


def test_herdr_down_picks_nothing(world: World) -> None:
    issue = world.issue()
    world.hc.down = True
    world.tick(2)
    assert world.store.runs() == []
    assert "Herdr" in world.store.get("report")["problems"]
    world.hc.down = False
    world.tick()
    assert issue.status[BOARD] == "In Progress"


def test_ticket_changed_between_the_poll_and_the_claim(world: World) -> None:
    issue = world.issue()
    real = world.tracker.read_tickets

    def read(runs, readable):  # type: ignore[no-untyped-def]
        world.unlabel(issue)
        return real(runs, readable)

    world.tracker.read_tickets = read  # type: ignore[method-assign]
    world.tick()
    assert world.state() is RunState.DISCARDED
    assert issue.status[BOARD] == "Todo"
    assert issue.comments == []


def test_worktree_that_cannot_be_made_fails_the_run(world: World) -> None:
    issue = world.issue()
    blocker = world.paths.worktrees
    blocker.parent.mkdir(parents=True, exist_ok=True)
    git(world.tmp / "web", "config", "core.hooksPath", str(world.tmp / "hooks"))
    hook = world.tmp / "hooks" / "post-checkout"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    world.tick()
    assert world.state() is RunState.FAILED
    assert NEEDS_HUMAN in issue.labels
    assert world.events(issue) == ["picked", "failed"]
    assert len(world.notifier.sent) == 1


def test_herdr_that_refuses_a_terminal_fails_the_run(world: World) -> None:
    issue = world.issue()
    refuse = Result(1, "", '{"error": {"kind": "spawn_failed", "message": "no pty"}}')
    world.hc._run = lambda args: refuse  # type: ignore[method-assign]
    world.tick()
    assert world.state() is RunState.FAILED
    assert "no pty" in issue.comments[-1]


def test_label_and_comment_survive_github_going_down(world: World) -> None:
    issue = world.issue()
    world.tick()
    world.open_pr(issue)
    world.agent("done")
    world.tick()
    down_at = len(world.tracker.log) + 1

    def outage(what: str) -> None:
        if len(world.tracker.log) >= down_at:
            world.tracker.down = True
            world.tracker._up()

    world.tracker.before_write = outage
    world.tick()
    assert world.state() is RunState.DONE
    assert world.events(issue) == ["picked"]
    world.tracker.before_write = lambda what: None
    world.tracker.down = False
    world.tick()
    assert world.events(issue) == ["picked", "in-review"]


def test_tab_is_named_once_the_window_is_open(world: World) -> None:
    world.issue()
    world.tick(2)
    assert not world.run().tab_named
    tab = world.run().herdr_tab_id
    assert tab
    world.hc.window = {tab: "1"}
    world.tick()
    assert world.hc.window[tab] == "42-export-csv"
    world.hc.window[tab] = "mine"
    world.tick(2)
    assert world.hc.window[tab] == "mine"


def test_stop_finishes_the_step_it_is_in(world: World) -> None:
    for number in (1, 2):
        world.issue(number, title=f"Ticket {number}")

    def stop(what: str) -> None:
        world.stop.value = True

    world.tracker.before_write = stop
    world.tick()
    assert world.state(1) is RunState.RUNNING
    world.loop.run()  # returns at once


def test_broken_board_does_not_stop_the_other(world: World) -> None:
    from tests.world import BOARD_2

    world.issue(1)
    world.issue(2, board=BOARD_2)
    world.tracker.broken_boards = {BOARD}
    world.tick()
    assert [r.ticket_id for r in world.store.runs()] == ["2"]
    assert BOARD in world.store.get("report")["problems"]


def test_flags_are_posted_once(world: World) -> None:
    issue = world.issue(status="In Progress")
    world.tick(3)
    assert NEEDS_HUMAN in issue.labels
    assert len(issue.comments) == 1
    issue.labels.discard(NEEDS_HUMAN)
    world.tick(3)
    assert NEEDS_HUMAN not in issue.labels
    assert len(issue.comments) == 1
