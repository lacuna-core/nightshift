"""FR-13 and NFR-8, against real git: nothing that could be lost is removed."""

from collections.abc import Callable
from pathlib import Path

import pytest

from nightshift.model import RunState
from tests.world import Issue, World, git

Change = Callable[[World, Path], object]


@pytest.fixture
def world(tmp_path: Path) -> World:
    world = World(tmp_path)
    world.arm()
    world.issue()
    world.tick()
    world.agent("working")
    return world


@pytest.fixture
def issue(world: World) -> Issue:
    return world.tracker.issues["I_acme/web_42"]


def worktree(world: World) -> Path:
    return Path(world.run().worktree_path or "")


def reviewed(world: World, issue: Issue) -> Path:
    path = worktree(world)
    (path / "feature.py").write_text("x = 1\n")
    git(path, "add", "feature.py")
    world.open_pr(issue)
    world.agent("done")
    world.tick(2)
    assert world.state() is RunState.DONE
    return path


def merge(world: World, issue: Issue) -> None:
    world.set_pr(issue, "merged")
    issue.is_open = False
    issue.status.clear()


def branches(world: World) -> str:
    return git(world.tmp / "web", "branch", "--list", "--format=%(refname:short)")


def removed(world: World, path: Path) -> bool:
    return not path.exists() and world.run().cleaned_up_at is not None


def test_merged_pr_is_cleaned_up(world: World, issue: Issue) -> None:
    path = reviewed(world, issue)
    merge(world, issue)
    world.tick()
    assert removed(world, path)
    assert world.terminal()["state"] == "finished"
    assert branches(world) == "main"
    assert "42/export-csv" in git(world.tmp / "web.git", "branch")  # kept on GitHub
    assert world.state() is RunState.DONE
    assert "kept" not in world.events(issue)


def test_your_tabs_in_the_space_close_with_it(world: World, issue: Issue) -> None:
    path = reviewed(world, issue)
    world.hc._run(["--cwd", str(path), "--label", f"herdr.space={world.run().herdr_space_id}"])
    merge(world, issue)
    world.tick()
    assert removed(world, path)
    inside = [t for t in world.hc.terminals.values() if t["cwd"] == str(path)]
    assert [t["state"] for t in inside] == ["finished", "finished"]
    repo_space = [t for t in world.hc.terminals.values() if t["cwd"] == str(world.tmp / "web")]
    assert [t["state"] for t in repo_space] == ["live"]


def begin_rebase(world: World, path: Path) -> None:
    (path / "feature.py").write_text("x = 2\n")
    git(path, "commit", "-qam", "more")
    head = git(path, "rev-parse", "HEAD")
    world.pr_commits["PR_1"].add(head)
    git(path, "-c", "sequence.editor=sed -i.bak s/pick/edit/", "rebase", "-qi", "HEAD~1")
    git(path, "symbolic-ref", "HEAD", "refs/heads/42/export-csv")


def begin_merge(world: World, path: Path) -> None:
    git(path, "branch", "side", "main")
    git(path, "switch", "-q", "side")
    git(path, "commit", "-q", "--allow-empty", "-m", "side")
    git(path, "switch", "-q", "42/export-csv")
    git(path, "merge", "-q", "--no-ff", "--no-commit", "side")


def extra_commit(world: World, path: Path) -> None:
    git(path, "commit", "-q", "--allow-empty", "-m", "not in the PR")


def submodule(world: World, path: Path) -> None:
    git(path, "submodule", "--quiet", "add", str(world.tmp / "web.git"), "sub")
    git(path, "commit", "-qam", "add sub")
    world.pr_commits["PR_1"].add(git(path, "rev-parse", "HEAD"))


def detach(world: World, path: Path) -> None:
    git(path, "switch", "-q", "--detach")


def detach_with_commit(world: World, path: Path) -> None:
    detach(world, path)
    extra_commit(world, path)


def nested_repo(world: World, path: Path) -> None:
    (path / "nested").mkdir()
    git(path / "nested", "init", "-q")
    (path / "nested" / "f").write_text("x")


def hide_change(world: World, path: Path) -> None:
    git(path, "update-index", "--skip-worktree", "feature.py")
    (path / "feature.py").write_text("hidden\n")


def assume_unchanged(world: World, path: Path) -> None:
    git(path, "update-index", "--assume-unchanged", "feature.py")
    (path / "feature.py").write_text("hidden\n")


def busy_agent_of_yours(world: World, path: Path) -> None:
    address = world.hc._run(["--cwd", str(path)])["address"]
    world.hc.launch(world.hc.terminals[address])


def claude_outside_herdr(world: World, path: Path) -> None:
    world.hc.elsewhere = [str(path.resolve())]


UNSAFE: dict[str, Change] = {
    "uncommitted change": lambda w, p: (p / "feature.py").write_text("changed\n"),
    "staged change": lambda w, p: ((p / "feature.py").write_text("s\n"), git(p, "add", ".")),
    "deleted file": lambda w, p: (p / "feature.py").unlink(),
    "untracked file": lambda w, p: (p / "notes.txt").write_text("mine"),
    "untracked file that the config hides": lambda w, p: (
        git(p, "config", "status.showUntrackedFiles", "no"),
        (p / "notes.txt").write_text("mine"),
    ),
    "untracked repo": nested_repo,
    "change hidden by skip-worktree": hide_change,
    "change hidden by assume-unchanged": assume_unchanged,
    "detached HEAD": detach,
    "detached HEAD with a commit": detach_with_commit,
    "other branch": lambda w, p: git(p, "switch", "-qc", "mine"),
    "rebase in progress": begin_rebase,
    "merge in progress": begin_merge,
    "bisect in progress": lambda w, p: git(p, "bisect", "start"),
    "submodule": submodule,
    "extra commit": extra_commit,
    "locked": lambda w, p: git(p, "worktree", "lock", str(p)),
    "agent working": lambda w, p: w.agent("working"),
    "agent blocked": lambda w, p: w.agent("blocked"),
    "your agent working": busy_agent_of_yours,
    "Claude Code outside Herdr": claude_outside_herdr,
}


@pytest.mark.parametrize("name", UNSAFE)
def test_unsafe_after_a_merge_keeps_everything(world: World, issue: Issue, name: str) -> None:
    path = reviewed(world, issue)
    UNSAFE[name](world, path)
    merge(world, issue)
    before = len(world.hc.terminals)
    world.tick(3)
    assert path.is_dir()
    assert world.run().cleaned_up_at is None
    assert world.run().kept_reason
    assert "42/export-csv" in branches(world)
    assert all(t["state"] == "live" for t in world.hc.terminals.values())
    assert len(world.hc.terminals) == before
    assert world.events(issue).count("kept") == 1


def test_removed_once_it_is_safe(world: World, issue: Issue) -> None:
    path = reviewed(world, issue)
    (path / "notes.txt").write_text("mine")
    merge(world, issue)
    world.tick(2)
    assert path.is_dir()
    (path / "notes.txt").unlink()
    world.tick()
    assert removed(world, path)


def test_ignored_files_do_not_keep_a_merged_worktree(world: World, issue: Issue) -> None:
    path = reviewed(world, issue)
    (world.tmp / "web" / ".git" / "info" / "exclude").write_text(".venv\n")
    (path / ".venv").mkdir()
    (path / ".venv" / "x").write_text("x")
    merge(world, issue)
    world.tick()
    assert removed(world, path)


def test_squash_merge(world: World, issue: Issue) -> None:
    path = reviewed(world, issue)
    clone = world.tmp / "web"
    git(clone, "merge", "--squash", "-q", "42/export-csv")
    git(clone, "commit", "-qm", "squashed (#1)")
    git(clone, "push", "-q", "origin", "main")
    merge(world, issue)
    world.tick()
    assert removed(world, path)
    assert branches(world) == "main"


def test_branch_that_is_behind_the_merged_pr(world: World, issue: Issue) -> None:
    path = reviewed(world, issue)
    git(path, "commit", "-q", "--allow-empty", "-m", "pushed later")
    git(path, "push", "-q", "origin", "HEAD")  # the PR has it, the PR's first commit stays local
    head = git(path, "rev-parse", "HEAD")
    git(path, "reset", "-q", "--hard", "HEAD~1")
    world.set_pr(issue, "merged", head=head)
    world.pr_commits["PR_1"] = {head}
    issue.is_open = False
    world.tick()
    assert removed(world, path)


def test_change_made_while_the_space_closes_is_kept(world: World, issue: Issue) -> None:
    path = reviewed(world, issue)
    merge(world, issue)

    def write(what: str) -> None:
        if what == "kill":
            (path / "saved-on-exit.txt").write_text("work")

    world.hc.before = write
    world.tick(2)
    assert (path / "saved-on-exit.txt").exists()
    assert world.run().cleaned_up_at is None


def test_crash_while_removing_is_finished_later(world: World, issue: Issue) -> None:
    path = reviewed(world, issue)
    merge(world, issue)
    real = world.git.delete_branch

    def crash(*args: object) -> None:
        raise KeyboardInterrupt

    world.git.delete_branch = crash  # type: ignore[method-assign, assignment]
    with pytest.raises(KeyboardInterrupt):
        world.tick()
    world.git.delete_branch = real  # type: ignore[method-assign]
    assert not path.exists()
    assert "42/export-csv" in branches(world)
    world.restart()
    world.tick()
    assert removed(world, path)
    assert branches(world) == "main"


def test_worktree_you_removed_counts_as_cleaned_up(world: World, issue: Issue) -> None:
    path = reviewed(world, issue)
    git(world.tmp / "web", "worktree", "remove", str(path))
    merge(world, issue)
    world.tick()
    assert removed(world, path)
    assert "42/export-csv" in branches(world)


def test_missed_merge_is_caught_up_after_a_restart(world: World, issue: Issue) -> None:
    path = reviewed(world, issue)
    merge(world, issue)
    world.restart()
    world.tick()
    assert removed(world, path)


def test_closed_issue_needs_a_closed_tab(world: World, issue: Issue) -> None:
    path = reviewed(world, issue)
    issue.is_open = False
    world.tick(3)
    assert path.is_dir()
    world.hc._kill([world.terminal()["address"]])
    world.tick(2)
    assert removed(world, path)
    assert "42/export-csv" in branches(world)


def test_closed_tab_alone_is_no_reason(world: World, issue: Issue) -> None:
    path = reviewed(world, issue)
    world.hc._kill([world.terminal()["address"]])
    world.tick(5)
    assert path.is_dir()


def test_taken_over_is_never_cleaned_up(world: World, issue: Issue) -> None:
    path = reviewed(world, issue)
    world.unlabel(issue)
    world.tick()
    merge(world, issue)
    world.tick(3)
    assert path.is_dir()
    assert world.terminal()["state"] == "live"


def test_failed_run_with_a_merged_pr_is_cleaned_up(world: World, issue: Issue) -> None:
    world.open_pr(issue)
    world.tick(25)
    assert world.state() is RunState.FAILED
    path = worktree(world)
    merge(world, issue)
    world.tick()
    assert removed(world, path)


def test_herdr_down_keeps_everything(world: World, issue: Issue) -> None:
    path = reviewed(world, issue)
    merge(world, issue)
    world.hc.down = True
    world.tick(2)
    assert path.is_dir()
    world.hc.down = False
    world.tick()
    assert removed(world, path)


def test_first_week_only_reports(tmp_path: Path) -> None:
    world = World(tmp_path)
    issue = world.issue()
    world.tick()
    path = reviewed(world, issue)
    merge(world, issue)
    world.tick(3)
    assert path.is_dir()
    assert world.terminal()["state"] == "live"
    assert "first week" in (world.run().kept_reason or "")
    assert "kept" not in world.events(issue)
    world.clock = world.clock.replace(month=12)
    world.tick()
    assert removed(world, path)
