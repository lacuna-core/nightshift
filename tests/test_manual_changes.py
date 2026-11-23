"""FR-12: every row of the table, except moving back to Todo and cleanup."""

from collections.abc import Callable
from pathlib import Path

import pytest

from nightshift import wiring
from nightshift.checks import Report
from nightshift.conventions import NEEDS_HUMAN
from nightshift.model import RunState
from nightshift.project_config import parse_config
from tests.world import BOARD, BOARD_2, Issue, World


@pytest.fixture
def world(tmp_path: Path) -> World:
    world = World(tmp_path)
    world.issue()
    world.tick()
    world.agent("working")
    world.tick()
    return world


@pytest.fixture
def issue(world: World) -> Issue:
    return world.tracker.issues["I_acme/web_42"]


def done(world: World, issue: Issue) -> None:
    world.open_pr(issue)
    world.agent("done")
    world.tick(2)
    assert world.state() is RunState.DONE


def kept(world: World) -> bool:
    return Path(world.run().worktree_path or "").is_dir() and world.terminal()["state"] == "live"


def stopped(world: World) -> bool:
    return bool(world.terminal()["status"]["state"] == "idle")


Change = Callable[[World, Issue], None]
CANCELS: dict[str, tuple[Change, bool]] = {
    "label removed": (lambda w, i: w.unlabel(i), False),
    "issue closed": (lambda w, i: setattr(i, "is_open", False), False),
    "moved to Done": (lambda w, i: w.move(i, "Done"), False),
    "moved to Backlog": (lambda w, i: w.move(i, "Backlog"), False),
    "status cleared": (lambda w, i: w.move(i, None), False),
    "In Review without a ready PR": (lambda w, i: w.move(i, "In Review"), False),
    "removed from the board": (lambda w, i: i.status.clear(), False),
}


@pytest.mark.parametrize("name", CANCELS)
def test_cancels_a_live_run(world: World, issue: Issue, name: str) -> None:
    change, needs_human = CANCELS[name]
    change(world, issue)
    before = (dict(issue.status), issue.is_open)
    writes = len(world.tracker.log)
    world.tick(5)
    assert world.state() is RunState.CANCELLED
    assert stopped(world)
    assert world.hc.calls.count("interrupt") == 1
    assert kept(world)
    assert (issue.status, issue.is_open) == before
    assert (NEEDS_HUMAN in issue.labels) is needs_human
    assert len(world.tracker.log) == writes + 1  # one comment, nothing else


@pytest.mark.parametrize("name", CANCELS)
def test_keeps_the_label_as_it_is(world: World, issue: Issue, name: str) -> None:
    world.agent("blocked")
    world.tick()
    assert NEEDS_HUMAN in issue.labels
    CANCELS[name][0](world, issue)
    world.tick(3)
    assert world.state() is RunState.CANCELLED
    assert NEEDS_HUMAN in issue.labels
    assert stopped(world)


def test_label_removed_and_put_back_between_polls(world: World, issue: Issue) -> None:
    world.unlabel(issue)
    issue.labels.add("nightshift")
    world.tick()
    assert world.state() is RunState.CANCELLED
    assert world.run().taken_over


def test_taken_over_after_the_end_is_said_once(world: World, issue: Issue) -> None:
    done(world, issue)
    world.unlabel(issue)
    world.tick(3)
    assert world.state() is RunState.DONE
    assert world.run().taken_over
    assert world.events(issue).count("taken-over") == 1


def test_taken_over_run_is_not_helped_along(world: World, issue: Issue) -> None:
    world.unlabel(issue)
    world.tick(2)
    issue.labels.add("nightshift")
    world.open_pr(issue)
    world.agent("done")
    world.tick(5)
    assert world.state() is RunState.CANCELLED
    assert issue.status[BOARD] == "In Progress"


def test_taken_over_agent_is_interrupted_until_it_stops(world: World, issue: Issue) -> None:
    world.hc.sticky_interrupt = True
    world.unlabel(issue)
    world.tick(3)
    assert not stopped(world)
    world.hc.sticky_interrupt = False
    world.tick()
    assert stopped(world)


def test_merged_lets_the_agent_finish_its_turn(world: World, issue: Issue) -> None:
    world.open_pr(issue)
    world.set_pr(issue, "merged")
    issue.is_open = False
    world.move(issue, "Done")
    world.tick(5)
    assert world.state() is RunState.RUNNING
    world.agent("done")
    world.tick(2)
    assert world.state() is RunState.DONE
    assert issue.status[BOARD] == "Done"
    assert "interrupt" not in world.hc.calls


def test_merged_run_that_stalls_is_done_all_the_same(world: World, issue: Issue) -> None:
    world.open_pr(issue)
    world.set_pr(issue, "merged")
    world.tick(25)
    assert world.state() is RunState.DONE
    assert NEEDS_HUMAN not in issue.labels


def test_pr_closed_counts_only_after_the_turn(world: World, issue: Issue) -> None:
    world.open_pr(issue)
    world.set_pr(issue, "closed")
    world.tick(5)
    assert world.state() is RunState.RUNNING
    world.agent("done")
    world.tick(2)
    assert world.state() is RunState.CANCELLED
    assert NEEDS_HUMAN in issue.labels
    assert kept(world)


def test_agent_replaces_its_own_pr(world: World, issue: Issue) -> None:
    world.open_pr(issue)
    world.set_pr(issue, "closed")
    world.tick(2)
    world.open_pr(issue, ref="PR_2")
    world.agent("done")
    world.tick(2)
    assert world.state() is RunState.DONE
    assert world.run().pr_ref == "PR_2"


def test_pr_closed_after_the_end_changes_nothing(world: World, issue: Issue) -> None:
    done(world, issue)
    writes = len(world.tracker.log)
    world.set_pr(issue, "closed")
    world.tick(3)
    assert world.state() is RunState.DONE
    assert len(world.tracker.log) == writes


def test_moved_to_in_review_with_a_ready_pr(world: World, issue: Issue) -> None:
    world.open_pr(issue)
    world.move(issue, "In Review")
    writes = len(world.tracker.log)
    world.tick(5)
    assert world.state() is RunState.RUNNING
    world.agent("done")
    world.tick(2)
    assert world.state() is RunState.DONE
    assert len(world.tracker.log) == writes


@pytest.mark.parametrize("ready", [True, False])
def test_tab_closed(world: World, issue: Issue, ready: bool) -> None:
    if ready:
        world.open_pr(issue)
    world.hc._kill([world.terminal()["address"]])
    world.tick(2)
    assert world.state() is (RunState.DONE if ready else RunState.CANCELLED)
    assert (NEEDS_HUMAN in issue.labels) is not ready
    assert issue.status[BOARD] == ("In Review" if ready else "In Progress")
    assert Path(world.run().worktree_path or "").is_dir()


def test_tab_closed_after_the_end(world: World, issue: Issue) -> None:
    done(world, issue)
    writes = len(world.tracker.log)
    world.hc._kill([world.terminal()["address"]])
    world.tick(3)
    assert world.state() is RunState.DONE
    assert len(world.tracker.log) == writes


def test_ended_run_is_not_interrupted(world: World, issue: Issue) -> None:
    done(world, issue)
    world.agent("working")
    issue.is_open = False
    world.move(issue, "Backlog")
    world.tick(3)
    assert "interrupt" not in world.hc.calls
    assert world.state() is RunState.DONE


@pytest.mark.parametrize(
    ("first", "second", "event"),
    [
        ("label removed", "issue closed", "taken-over"),
        ("issue closed", "moved to Done", "closed"),
    ],
)
def test_order(world: World, issue: Issue, first: str, second: str, event: str) -> None:
    CANCELS[second][0](world, issue)
    CANCELS[first][0](world, issue)
    world.tick(2)
    assert world.events(issue) == ["picked", event]


def test_unreadable_board_is_not_removed(world: World, issue: Issue) -> None:
    del issue.status[BOARD]
    world.tracker.broken_boards = {BOARD}
    world.tick(5)
    assert world.state() is RunState.RUNNING


def test_unreadable_ticket_changes_nothing(world: World, issue: Issue) -> None:
    del world.tracker.issues[issue.ref]
    world.tick(5)
    assert world.state() is RunState.RUNNING


def test_own_status_change_is_not_taken_as_yours(world: World, issue: Issue) -> None:
    world.open_pr(issue)
    world.agent("done")
    world.tick()

    def crash(what: str) -> None:
        if what == "comment in-review":
            raise KeyboardInterrupt

    world.tracker.before_write = crash
    with pytest.raises(KeyboardInterrupt):
        world.tick()
    assert issue.status[BOARD] == "In Review"
    world.tracker.before_write = lambda what: None
    world.restart()
    world.tick(2)
    assert world.state() is RunState.DONE
    assert world.events(issue) == ["picked", "in-review"]


def test_two_boards_and_two_repos(tmp_path: Path) -> None:
    world = World(tmp_path, repos=("web", "api"), limit=2)
    for n in range(1, 5):
        world.issue(n, f"Web {n}", "acme/web", BOARD)
        world.issue(n + 10, f"Api {n}", "acme/api", BOARD_2)
    world.issue(5, "Crossing", "acme/api", BOARD)
    world.tick()
    assert sorted(r.ticket_id for r in world.store.runs()) == ["1", "11", "12", "2"]
    assert {
        t["cwd"] for t in world.hc.terminals.values() if "nightshift.run" not in t["labels"]
    } == {
        str(tmp_path / "web"),
        str(tmp_path / "api"),
    }


def test_lower_limit_stops_picking_only(world: World) -> None:
    world.issue(43, "Two")
    world.tick()
    boards = [b.model_copy(update={"max_parallel_agents": 1}) for b in world.config.boards]
    world.config = world.config.model_copy(update={"boards": boards})
    world.restart()
    world.issue(44, "Three")
    world.tick(3)
    assert [r.state for r in world.store.runs()] == [RunState.RUNNING, RunState.RUNNING]


def test_unlisted_runs(world: World, issue: Issue) -> None:
    config = parse_config(
        {
            "boards": [{"url": BOARD_2}],
            "repos": [{"path": str(r.path)} for r in world.config.repos],
        }
    )
    report = Report(unavailable=True)
    assert wiring.unlisted_runs(world.config, report, world.store) == []
    [run] = wiring.unlisted_runs(config, report, world.store)
    world.executor.perform(wiring.cancel_unlisted(run))
    assert world.state() is RunState.CANCELLED
    assert stopped(world)
    assert kept(world)


def test_moved_clone_is_followed(world: World, tmp_path: Path) -> None:
    (tmp_path / "web").rename(tmp_path / "new-web")
    wiring.follow_clone(world.git, world.store, "R_acme/web", tmp_path / "new-web")
    assert world.run().clone_path == str(tmp_path / "new-web")
    worktree = Path(world.run().worktree_path or "")
    assert world.git.worktree_branch(worktree) == "42/export-csv"
