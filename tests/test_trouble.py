"""FR-10, FR-11, FR-18, FR-19, NFR-6: when things go wrong."""

from datetime import timedelta
from pathlib import Path

import pytest

from nightshift.conventions import NEEDS_HUMAN
from nightshift.errors import TrackerUnavailableError
from nightshift.model import RunState
from tests.world import BOARD, Issue, World


@pytest.fixture
def world(tmp_path: Path) -> World:
    world = World(tmp_path)
    world.issue()
    world.tick()
    world.agent("working")
    return world


@pytest.fixture
def issue(world: World) -> Issue:
    return world.tracker.issues["I_acme/web_42"]


def one_signal(world: World, issue: Issue, event: str) -> None:
    assert NEEDS_HUMAN in issue.labels
    assert len([e for e in world.events(issue) if e.startswith(event)]) == 1
    assert len(world.notifier.sent) == 1


@pytest.mark.parametrize(
    ("kind", "text"), [("permission", "permission"), ("question", "question"), (None, "blocked")]
)
def test_blocked_agent_needs_you(world: World, issue: Issue, kind: str | None, text: str) -> None:
    world.agent("blocked", kind=kind)
    world.tick(5)
    assert world.state() is RunState.WAITING
    one_signal(world, issue, "waiting")
    assert text in issue.comments[-1]
    assert issue.status[BOARD] == "In Progress"


def test_error_needs_you(world: World, issue: Issue) -> None:
    world.agent("error")
    world.tick(5)
    one_signal(world, issue, "waiting")


@pytest.mark.parametrize("state", ["idle", "done"])
def test_idle_without_a_pr_needs_you_after_five_minutes(
    world: World, issue: Issue, state: str
) -> None:
    world.agent(state)
    world.tick(5)
    assert world.state() is RunState.RUNNING
    world.tick(3)
    assert world.state() is RunState.WAITING
    one_signal(world, issue, "waiting")


def test_draft_pr_needs_you_too(world: World, issue: Issue) -> None:
    world.open_pr(issue, draft=True)
    world.agent("done")
    world.tick(8)
    assert world.state() is RunState.WAITING


def test_a_new_turn_starts_the_five_minutes_again(world: World) -> None:
    for _ in range(4):
        world.agent("working")
        world.agent("done")
        world.tick(4)
    assert world.state() is RunState.RUNNING


def test_trust_dialog_needs_you(world: World, issue: Issue) -> None:
    world.terminal()["status"] = None
    world.terminal()["reported"] = False
    world.tick(4)
    assert world.state() is RunState.WAITING
    one_signal(world, issue, "waiting")


def stuck_start(tmp_path: Path) -> tuple[World, Issue]:
    world = World(tmp_path)
    world.hc.shell_asks = True
    issue = world.issue()
    world.tick(5)
    return world, issue


def test_shell_that_asks_before_claude_starts_needs_you(tmp_path: Path) -> None:
    world, issue = stuck_start(tmp_path)
    assert world.state() is RunState.WAITING
    one_signal(world, issue, "waiting")
    world.hc.launch(world.terminal())
    world.tick()
    assert world.state() is RunState.RUNNING
    assert NEEDS_HUMAN not in issue.labels


def test_agent_that_starts_after_its_run_ended_is_stopped(tmp_path: Path) -> None:
    world, issue = stuck_start(tmp_path)
    world.unlabel(issue)
    world.tick(2)
    assert world.state() is RunState.CANCELLED
    world.hc.launch(world.terminal())
    world.tick()
    assert world.terminal()["status"]["state"] == "idle"


def test_start_over_waits_until_the_agent_can_be_ended(tmp_path: Path) -> None:
    world, issue = stuck_start(tmp_path)
    world.tick()
    world.move(issue, "Todo")
    world.tick(2)
    assert world.state() is RunState.RESETTING
    world.hc.launch(world.terminal())
    world.tick(2)
    assert [r.state for r in world.store.runs()] == [RunState.DISCARDED, RunState.RUNNING]


def test_answered_agent_goes_on(world: World, issue: Issue) -> None:
    world.agent("blocked", kind="question")
    world.tick()
    world.agent("working")
    world.tick()
    assert world.state() is RunState.RUNNING
    assert NEEDS_HUMAN not in issue.labels
    world.agent("blocked", kind="question")
    world.tick()
    assert NEEDS_HUMAN in issue.labels
    assert len([e for e in world.events(issue) if e.startswith("waiting")]) == 2


def test_answered_agent_that_finished_completes(world: World, issue: Issue) -> None:
    world.agent("blocked", kind="question")
    world.tick()
    world.open_pr(issue)
    world.agent("done")
    world.tick(2)
    assert world.state() is RunState.DONE
    assert NEEDS_HUMAN not in issue.labels
    assert issue.status[BOARD] == "In Review"


def test_label_you_add_or_remove_has_no_effect(world: World, issue: Issue) -> None:
    issue.labels.add(NEEDS_HUMAN)
    world.tick(3)
    assert world.state() is RunState.RUNNING
    world.agent("blocked")
    world.tick()
    issue.labels.discard(NEEDS_HUMAN)
    world.tick(3)
    assert NEEDS_HUMAN not in issue.labels


def test_waiting_holds_the_slot_and_has_no_timeouts(world: World) -> None:
    world.agent("blocked")
    world.tick(300)
    assert world.state() is RunState.WAITING
    world.issue(43, "Two")
    world.issue(44, "Three")
    world.tick()
    assert [r.ticket_id for r in world.store.runs()] == ["42", "43"]


def test_stall(world: World, issue: Issue) -> None:
    world.terminal()["screen"] = "✻ Cooking… (12s · esc to interrupt)\nrunning tests"
    world.tick(19)
    assert world.state() is RunState.RUNNING
    world.terminal()["screen"] = "✶ Cooking… (99s · esc to interrupt)\nrunning tests"
    world.tick(3)
    assert world.state() is RunState.FAILED
    one_signal(world, issue, "failed")
    assert world.hc.calls.count("interrupt") == 1
    assert world.terminal()["status"]["state"] == "idle"


def test_screen_changes_are_activity(world: World) -> None:
    for n in range(40):
        world.terminal()["screen"] = "line " + "x" * n
        world.tick()
    assert world.state() is RunState.RUNNING


def test_timeout(world: World, issue: Issue) -> None:
    for n in range(185):
        world.terminal()["screen"] = "x" * n
        world.tick()
    assert world.state() is RunState.FAILED
    one_signal(world, issue, "failed")


def test_timeouts_do_not_run_while_nightshift_is_away(world: World) -> None:
    world.tick()
    world.clock += timedelta(hours=9)  # the machine sleeps
    world.tick(2)
    world.clock += timedelta(hours=9)
    world.restart()
    world.tick(2)
    world.hc.down = True
    world.tick(60)
    world.hc.down = False
    world.tick(5)
    assert world.state() is RunState.RUNNING
    assert world.run().active_seconds < 1200


def test_timeouts_are_set_per_repo(tmp_path: Path) -> None:
    world = World(tmp_path)
    repo = world.config.repos[0].model_copy(update={"stall_timeout": timedelta(minutes=60)})
    world.config = world.config.model_copy(update={"repos": [repo]})
    world.restart()
    world.issue()
    world.tick()
    world.agent("working")
    world.tick(50)
    assert world.state() is RunState.RUNNING
    world.tick(20)
    assert world.state() is RunState.FAILED


def test_interrupt_is_repeated_but_never_after_a_new_turn(world: World, issue: Issue) -> None:
    world.hc.sticky_interrupt = True
    world.tick(25)
    assert world.state() is RunState.FAILED
    tries = world.hc.calls.count("interrupt")
    assert tries >= 2
    world.hc.sticky_interrupt = False
    world.agent("idle")  # it stopped, and you start a new turn
    world.agent("working")
    world.tick(5)
    assert world.hc.calls.count("interrupt") == tries
    assert world.terminal()["status"]["state"] == "working"
    assert "cannot-stop" not in world.events(issue)


def test_agent_that_cannot_be_stopped_is_reported_once(world: World, issue: Issue) -> None:
    world.hc.sticky_interrupt = True
    world.tick(60)
    assert world.events(issue).count("cannot-stop") == 1
    tries = world.hc.calls.count("interrupt")
    world.tick(10)
    assert world.hc.calls.count("interrupt") == tries


def failed(world: World) -> None:
    world.tick(25)
    assert world.state() is RunState.FAILED
    world.notifier.sent.clear()


def test_failed_run_you_helped_along_is_done(world: World, issue: Issue) -> None:
    failed(world)
    world.agent("working")
    world.tick(3)
    world.open_pr(issue)
    world.agent("done")
    world.tick(2)
    assert world.state() is RunState.DONE
    assert issue.status[BOARD] == "In Review"
    assert NEEDS_HUMAN not in issue.labels
    assert world.notifier.sent == ["In Review"]


def test_failed_run_with_an_old_pr_stays_failed(world: World, issue: Issue) -> None:
    world.open_pr(issue)
    failed(world)
    world.tick(10)
    assert world.state() is RunState.FAILED


def test_failed_run_with_a_new_pr_is_done(world: World, issue: Issue) -> None:
    failed(world)
    world.clock += timedelta(minutes=5)
    world.open_pr(issue)
    world.tick(2)
    assert world.state() is RunState.DONE


def test_failed_run_takes_no_slot(world: World) -> None:
    failed(world)
    world.issue(43, "Two")
    world.issue(44, "Three")
    world.tick()
    assert [r.ticket_id for r in world.store.runs()] == ["42", "43", "44"]


def test_helped_run_outside_in_progress_is_left_alone(world: World, issue: Issue) -> None:
    failed(world)
    world.move(issue, "Backlog")
    world.clock += timedelta(minutes=5)
    world.open_pr(issue)
    world.tick(5)
    assert world.state() is RunState.FAILED
    assert issue.status[BOARD] == "Backlog"


def test_outage_backs_off(world: World) -> None:
    world.tracker.down = True
    delays = [world.loop.tick() for _ in range(8)]
    assert delays[:4] == [60, 120, 240, 480]
    assert delays[-1] == 900
    world.tracker.down = False
    assert world.loop.tick() == 60


def test_rate_limit_waits_until_it_resets(world: World) -> None:
    def limited() -> None:
        raise TrackerUnavailableError("slow down", world.clock + timedelta(minutes=40))

    world.tracker._up = limited  # type: ignore[method-assign]
    assert world.loop.tick() == 40 * 60 + 1


def test_a_bug_in_one_poll_does_not_end_the_loop(world: World) -> None:
    real = world.observer.observe
    world.observer.observe = lambda: 1 / 0  # type: ignore[assignment, return-value]
    assert world.loop.tick() == 60
    world.observer.observe = real  # type: ignore[method-assign]
    world.tick()
    assert world.state() is RunState.RUNNING


def test_missing_clone_stops_only_its_repo(tmp_path: Path) -> None:
    world = World(tmp_path, repos=("web", "api"))
    world.issue(1, repo="acme/web")
    world.issue(2, repo="acme/api")
    (tmp_path / "web").rename(tmp_path / "moved")
    world.tick()
    assert [r.repo for r in world.store.runs()] == ["acme/api"]
    (tmp_path / "moved").rename(tmp_path / "web")
    world.tick()
    assert len(world.store.runs()) == 2
