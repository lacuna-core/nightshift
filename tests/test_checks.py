import json
import subprocess
from pathlib import Path

import pytest

from nightshift import proc
from nightshift.checks import Checks, Level, Report
from nightshift.claude import Claude
from nightshift.errors import NightshiftError, TrackerNotFoundError, TrackerUnavailableError
from nightshift.git import Git
from nightshift.herdr import Herdr, is_supported
from nightshift.model import BoardOverview, IssueCount, RepoInfo
from nightshift.project_config import ProjectConfig, parse_config
from tests.fakes import FakeRunner, ok

BOARD = "https://github.com/users/me/projects/1"
ALL = frozenset({"nightshift", "nightshift:needs-human"})


class FakeTracker:
    def __init__(self) -> None:
        self.scopes: frozenset[str] | None = frozenset({"repo", "project"})
        self.login_error: NightshiftError | None = None
        self.info = RepoInfo("acme/web", "R_1", "main", True, ALL)
        self.overview: BoardOverview | NightshiftError = BoardOverview(
            BOARD, ("Todo", "In Progress", "In Review"), {"acme/web": IssueCount(2, 1)}
        )

    def login(self) -> tuple[str, frozenset[str] | None]:
        if self.login_error:
            raise self.login_error
        return "me", self.scopes

    def repo_info(self, slug: str) -> RepoInfo:
        if slug != self.info.slug:
            raise TrackerNotFoundError("no")
        return self.info

    def board_overview(self, url: str) -> BoardOverview:
        if isinstance(self.overview, NightshiftError):
            raise self.overview
        return self.overview


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    path = tmp_path / "web"
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", "git@github.com:acme/web.git"],
        check=True,
    )
    settings = path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {
                "permissions": {
                    "allow": ["Bash(git commit:*)", "Bash(git push *)", "Bash(gh pr:*)", "Edit"]
                }
            }
        )
    )
    (tmp_path / ".claude.json").write_text(
        json.dumps({"projects": {str(path.resolve()): {"hasTrustDialogAccepted": True}}})
    )
    return path


def run(
    tracker: FakeTracker, *clones: Path, version: str = "0.1.18", home: Path | None = None
) -> Report:
    tools = FakeRunner(
        {
            "hc server status": ok(json.dumps({"version": version})),
            "claude --version": ok("2.1.323 (Claude Code)\n"),
        }
    )
    config: ProjectConfig = parse_config(
        {"boards": [{"url": BOARD}], "repos": [{"path": str(c)} for c in clones]}
    )
    checks = Checks(tracker, Herdr(tools), Claude(tools, home or clones[0].parent), Git(proc.run))
    return checks.run(config)


def texts(report: Report, level: Level) -> list[str]:
    found = [
        *report.tools,
        *(f for repo in report.repos for f in repo.findings),
        *(f for board in report.boards for f in board.findings),
    ]
    for finding in found:
        assert finding.level is Level.OK or finding.fix
    return [f.text for f in found if f.level is level]


def test_all_good(clone: Path) -> None:
    report = run(FakeTracker(), clone)
    assert not report.failed
    assert texts(report, Level.WARN) == []
    assert report.listed == {"acme/web"}


def test_missing_status(clone: Path) -> None:
    tracker = FakeTracker()
    tracker.overview = BoardOverview(BOARD, ("Todo", "In Progress", "Done"))
    report = run(tracker, clone)
    assert report.failed
    assert not report.fatal
    assert texts(report, Level.FAIL) == ["missing: In Review"]


def test_board_not_found(clone: Path) -> None:
    tracker = FakeTracker()
    tracker.overview = TrackerNotFoundError("gone")
    report = run(tracker, clone)
    assert report.failed
    assert not report.boards[0].usable
    assert report.repos[0].usable


def test_old_herdr_is_fatal(clone: Path) -> None:
    assert run(FakeTracker(), clone, version="0.1.2").fatal
    assert run(FakeTracker(), clone, version="0.2.0").fatal
    assert not is_supported("nonsense")


def test_scope_missing_is_fatal(clone: Path) -> None:
    tracker = FakeTracker()
    tracker.scopes = frozenset({"repo"})
    report = run(tracker, clone)
    assert report.fatal
    assert "project" in texts(report, Level.FAIL)[0]


def test_github_down_is_not_fatal(clone: Path) -> None:
    tracker = FakeTracker()
    tracker.login_error = TrackerUnavailableError("down")
    report = run(tracker, clone)
    assert report.failed
    assert report.unavailable
    assert not report.fatal


def test_cannot_push(clone: Path) -> None:
    tracker = FakeTracker()
    tracker.info = RepoInfo("acme/web", "R_1", "main", False, ALL)
    report = run(tracker, clone)
    assert not report.repos[0].usable


def test_missing_label_is_a_warning(clone: Path) -> None:
    tracker = FakeTracker()
    tracker.info = RepoInfo("acme/web", "R_1", "main", True, frozenset({"nightshift"}))
    report = run(tracker, clone)
    assert not report.failed
    assert texts(report, Level.WARN) == ["label nightshift:needs-human is missing"]


def test_not_a_clone(clone: Path, tmp_path: Path) -> None:
    (clone / "sub").mkdir()
    report = run(FakeTracker(), clone / "sub", tmp_path / "missing", home=tmp_path)
    assert [r.usable for r in report.repos] == [False, False]


def test_origin_elsewhere(clone: Path) -> None:
    subprocess.run(
        ["git", "-C", str(clone), "remote", "set-url", "origin", "git@gitlab.com:a/b"], check=True
    )
    assert not run(FakeTracker(), clone).repos[0].usable


def test_same_repo_twice(clone: Path, tmp_path: Path) -> None:
    second = tmp_path / "again"
    subprocess.run(["cp", "-R", str(clone), str(second)], check=True)
    report = run(FakeTracker(), clone, second)
    assert [r.usable for r in report.repos] == [True, False]


def test_allowlist_and_trust_warnings(clone: Path) -> None:
    (clone / ".claude" / "settings.json").write_text('{"permissions": {"allow": ["Bash(git:*)"]}}')
    (clone.parent / ".claude.json").write_text('{"projects": {}}')
    warnings = texts(run(FakeTracker(), clone), Level.WARN)
    assert warnings[0].startswith(".claude/settings.json does not allow gh pr create:")
    assert "does not trust" in warnings[1]


def test_worktree_is_not_a_clone(clone: Path, tmp_path: Path) -> None:
    git = ["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    subprocess.run([*git, "worktree", "add", "-q", str(tmp_path / "wt")], check=True)
    assert Git(proc.run).is_clone(clone)
    assert not Git(proc.run).is_clone(tmp_path / "wt")
