"""FR-16: moving a ticket back to Todo starts from scratch and loses nothing."""

from pathlib import Path

import pytest

from nightshift.conventions import NEEDS_HUMAN
from nightshift.model import PrState, RunState
from tests.test_cleanup import UNSAFE
from tests.world import BOARD, Crash, Issue, World, git


@pytest.fixture
def world(tmp_path: Path) -> World:
    world = World(tmp_path)
    world.arm()
    world.issue()
    world.tick()
    world.agent("working")
    world.tick()
    return world


@pytest.fixture
def issue(world: World) -> Issue:
    return world.tracker.issues["I_acme/web_42"]


def first(world: World) -> Path:
    return Path(world.store.runs()[0].worktree_path or "")


def states(world: World) -> list[RunState]:
    return [r.state for r in world.store.runs()]


def test_live_run_starts_over(world: World, issue: Issue) -> None:
    world.open_pr(issue)
    world.agent("blocked")
    world.tick()
    assert NEEDS_HUMAN in issue.labels
    path = first(world)
    world.move(issue, "Todo")
    world.tick()
    assert states(world) == [RunState.DISCARDED]
    assert NEEDS_HUMAN not in issue.labels
    assert issue.status[BOARD] == "Todo"
    assert world.tracker.closed_prs == ["PR_1"]
    assert not path.exists()
    world.tick()
    assert states(world) == [RunState.DISCARDED, RunState.RUNNING]
    assert world.run().branch == "42/export-csv-2"
    clone = world.tmp / "web"
    assert "42/export-csv" in git(clone, "branch", "--format=%(refname:short)").splitlines()
    assert "42/export-csv" in git(world.tmp / "web.git", "branch")
    said = next(c for c in issue.comments if "event=start-over" in c)
    assert "https://pr/PR_1" in said
    assert "`42/export-csv`" in said


@pytest.mark.parametrize("state", ["done", "failed", "cancelled"])
def test_ended_run_starts_over(world: World, issue: Issue, state: str) -> None:
    if state == "done":
        world.open_pr(issue)
        world.agent("done")
        world.tick(2)
    elif state == "failed":
        world.hc.exit_claude(world.terminal())
        world.tick(2)
    else:
        world.move(issue, "Backlog")
        world.tick(2)
    assert states(world) == [RunState(state)]
    world.move(issue, "Todo")
    world.tick(2)
    assert states(world) == [RunState.DISCARDED, RunState.RUNNING]
    assert not first(world).exists()


def test_after_the_merge_and_cleanup(world: World, issue: Issue) -> None:
    world.open_pr(issue)
    world.agent("done")
    world.tick(2)
    world.set_pr(issue, "merged")
    issue.is_open = False
    world.move(issue, "Done")
    world.tick()
    assert world.run().cleaned_up_at
    world.tick(5)
    issue.is_open = True
    world.move(issue, "Todo")
    world.tick(2)
    assert states(world) == [RunState.DISCARDED, RunState.RUNNING]
    assert world.tracker.closed_prs == []
    assert issue.prs["42/export-csv"][0].state is PrState.MERGED


@pytest.mark.parametrize("how", ["closed", "no label"])
def test_not_without_the_label_or_on_a_closed_issue(world: World, issue: Issue, how: str) -> None:
    world.open_pr(issue)
    world.agent("done")
    world.tick(2)
    if how == "closed":
        issue.is_open = False
    else:
        issue.labels.discard("nightshift")
    world.move(issue, "Todo")
    world.tick(5)
    assert states(world) == [RunState.DONE]
    assert world.tracker.closed_prs == []


# --- the old worktree -----------------------------------------------------------------------


def write_ignored(world: World, path: Path) -> None:
    (world.tmp / "web" / ".git" / "info" / "exclude").write_text(".venv\n")
    (path / ".venv").mkdir()
    (path / ".venv" / "x").write_text("x")


def your_tab(world: World, path: Path) -> None:
    world.hc._run(["--cwd", str(path)])


def program_in_tab(world: World, path: Path) -> None:
    terminal = world.terminal()
    world.hc.exit_claude(terminal)
    terminal["foreground"] = terminal["foreground"] | {"name": "vim", "argv": ["vim"]}


KEEPS = {
    "ignored files": write_ignored,
    "your tab": your_tab,
    "program in the tab": program_in_tab,
} | {k: v for k, v in UNSAFE.items() if k not in ("extra commit", "rebase in progress")}


@pytest.mark.parametrize("name", KEEPS)
def test_unsafe_worktree_stays_and_the_new_run_starts(
    world: World, issue: Issue, name: str
) -> None:
    path = first(world)
    (path / "feature.py").write_text("x = 1\n")
    git(path, "add", "feature.py")
    world.open_pr(issue)
    world.agent("done")
    world.tick(2)
    KEEPS[name](world, path)
    old = world.terminal()
    world.move(issue, "Todo")
    world.tick(4)
    assert states(world) == [RunState.DISCARDED, RunState.RUNNING]
    assert path.is_dir()
    assert old["state"] == "live"
    assert world.store.runs()[0].kept_reason
    said = [c for c in issue.comments if str(path) in c]
    assert len(said) == 1
    assert "start-over" in said[0]


def test_kept_worktree_is_removed_once_safe(world: World, issue: Issue) -> None:
    path = first(world)
    (path / "notes.txt").write_text("mine")
    world.move(issue, "Todo")
    world.tick(3)
    assert path.is_dir()
    (path / "notes.txt").unlink()
    world.tick()
    assert not path.exists()
    assert world.state() is RunState.RUNNING
    assert "42/export-csv" in git(world.tmp / "web", "branch")


def test_your_working_agent_of_an_ended_run_is_not_ended(world: World, issue: Issue) -> None:
    world.open_pr(issue)
    world.agent("done")
    world.tick(2)
    world.agent("working")
    old = world.terminal()
    world.move(issue, "Todo")
    world.tick(3)
    assert states(world) == [RunState.DISCARDED, RunState.RUNNING]
    assert old["foreground"]["name"] == "claude"
    assert old["status"]["state"] == "working"


def test_taken_over_run_is_left_alone_entirely(world: World, issue: Issue) -> None:
    world.open_pr(issue)
    path = first(world)
    old = world.terminal()
    world.unlabel(issue)
    world.tick(2)
    issue.labels |= {"nightshift", NEEDS_HUMAN}
    world.move(issue, "Todo")
    world.tick(3)
    assert states(world) == [RunState.DISCARDED, RunState.RUNNING]
    assert world.tracker.closed_prs == []
    assert path.is_dir()
    assert old["state"] == "live"
    assert old["foreground"]["name"] == "claude"
    assert any("took its earlier run over" in c for c in issue.comments)
    # A later start over leaves it alone too.
    world.agent("working")
    world.tick()
    world.move(issue, "Todo")
    world.tick(3)
    assert world.tracker.closed_prs == []
    assert path.is_dir()


# --- when it counts -------------------------------------------------------------------------


def test_stale_todo_right_after_the_claim(tmp_path: Path) -> None:
    world = World(tmp_path)
    issue = world.issue()
    world.tick()
    world.agent("working")
    world.move(issue, "Todo")  # GitHub has not caught up
    world.tick(2)
    assert states(world) == [RunState.RUNNING]
    world.move(issue, "In Progress")
    world.tick(3)
    assert states(world) == [RunState.RUNNING]


def test_todo_on_three_polls_in_a_row(tmp_path: Path) -> None:
    world = World(tmp_path)
    issue = world.issue()
    world.tick()
    world.agent("working")
    world.move(issue, "Todo")
    world.tick(3)
    assert states(world)[0] is RunState.DISCARDED


def test_moved_again_during_the_reset(world: World, issue: Issue) -> None:
    world.open_pr(issue)
    world.move(issue, "Todo")
    real = world.hc._keys
    world.hc._keys = lambda args: {}  # type: ignore[method-assign]
    world.tick()
    assert states(world) == [RunState.RESETTING]
    world.move(issue, "Backlog")
    world.tick()
    assert states(world) == [RunState.RESETTING]
    assert "could not be ended" in world.store.get(f"failed:{world.run().id}")
    world.hc._keys = real  # type: ignore[method-assign]
    world.tick()
    assert states(world) == [RunState.DISCARDED]
    assert issue.status[BOARD] == "Backlog"
    assert world.store.get(f"failed:{world.run().id}") is None


def test_killed_at_every_side_effect(tmp_path: Path) -> None:
    def play(folder: Path, crash_at: int | None) -> tuple[int, object]:
        folder.mkdir()
        world = World(folder)
        world.arm()
        issue = world.issue()
        world.tick()
        world.open_pr(issue)
        world.agent("working")
        world.tick()
        count = 0

        def effect(what: str) -> None:
            nonlocal count
            if what not in ("ls", "get", "read", "server"):
                count += 1
                if count == crash_at:
                    raise Crash

        world.tracker.before_write = effect
        world.hc.before = effect
        world.move(issue, "Todo")
        for _ in range(6):
            try:
                world.tick()
            except Crash:
                world.restart()
        agents = [t["state"] for t in world.hc.terminals.values() if t["argv"][-1:] != ["-l"]]
        return count, (
            [(r.state, r.branch) for r in world.store.runs()],
            world.tracker.closed_prs,
            sorted(world.events(issue)),
            agents,
            first(world).exists(),
        )

    effects, whole = play(tmp_path / "whole", None)
    assert effects > 6
    for n in range(1, effects + 1):
        assert play(tmp_path / f"crash{n}", n)[1] == whole, f"crash at side effect {n}"
