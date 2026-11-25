import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from nightshift import proc, wiring
from nightshift.cli.app import app
from nightshift.config import Settings
from nightshift.proc import Result
from nightshift.project_config import load_config
from tests.fakes import FakeRunner, ok
from tests.test_checks import clone  # noqa: F401  (a fixture)

BOARD = "https://github.com/users/me/projects/1"
runner = CliRunner()


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/user":
        return httpx.Response(
            200, json={"login": "me"}, headers={"x-oauth-scopes": "repo, project"}
        )
    if path.endswith("/labels"):
        created.append(json.loads(request.content)["name"])
        return httpx.Response(201, json={})
    query = json.loads(request.content)["query"]
    data: dict[str, Any]
    if "user(login:$l)" in query:
        login = json.loads(request.content)["variables"]["l"]
        data = {"user": None if login in missing_users else {"login": login}}
    elif "viewer{" in query:
        node = {"url": BOARD, "title": "Factory", "closed": False}
        data = {"viewer": {"projectsV2": {"nodes": [node]}, "organizations": {"nodes": []}}}
    elif "repository(owner" in query:
        data = {
            "repository": {
                "id": "R",
                "nameWithOwner": "acme/web",
                "viewerPermission": "ADMIN",
                "defaultBranchRef": {"name": "main"},
            }
        }
    else:
        options = [{"name": n} for n in ("Todo", "In Progress", "In Review")]
        issue = {
            "__typename": "Issue",
            "state": "OPEN",
            "repository": {"nameWithOwner": "acme/docs"},
            "labels": {"nodes": []},
        }
        items = {"pageInfo": {"hasNextPage": False}, "nodes": [{"id": "x", "content": issue}]}
        data = {"owner": {"projectV2": {"id": "P", "field": {"options": options}, "items": items}}}
    return httpx.Response(200, json={"data": data})


created: list[str] = []
missing_users = {"Lacuna"}
git_config: dict[str, str] = {}


def git_setting(args: list[str]) -> Result:
    value = git_config.get(args[-1])
    return Result(0, value + "\n", "") if value else Result(1, "", "")


@pytest.fixture(autouse=True)
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path
    created.clear()
    git_config.clear()
    monkeypatch.setenv("HOME", str(home))
    tools = FakeRunner(
        {
            "hc server status": ok('{"version": "0.1.18"}'),
            "claude --version": ok("2"),
            "gh auth token": ok("t"),
            "git": lambda args: proc.run(args, cwd=home / "web"),
            "git config --get": git_setting,
        }
    )

    original = wiring.build

    def build(settings: Settings) -> wiring.Boundaries:
        return original(settings, run=tools, transport=httpx.MockTransport(handler), home=home)

    monkeypatch.setattr(wiring, "build", build)
    return home


def test_init_then_check(world: Path, clone: Path) -> None:  # noqa: F811
    result = runner.invoke(app, ["init"], input=f"\n1\n2\n{clone}\n")
    assert result.exit_code == 0, result.output
    config = load_config(world / ".nightshift" / "nightshift.yaml")
    assert config.user == "me"  # the gh login, since git has no usable user
    assert config.boards[0].url == BOARD
    assert config.boards[0].max_parallel_agents == 2
    assert created == ["nightshift", "nightshift:needs-human"]

    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    assert "acme/docs" in result.output
    assert "not listed" in result.output


def test_init_again_keeps_optional_settings(world: Path, clone: Path) -> None:  # noqa: F811
    path = world / ".nightshift" / "nightshift.yaml"
    path.parent.mkdir()
    path.write_text(
        f"user: me\nboards:\n- url: {BOARD}\n  max_parallel_agents: 3\n"
        f"repos:\n- path: {clone}\n  stall_timeout: 40m\n  permission_mode: acceptEdits\n"
    )
    before = load_config(path)
    result = runner.invoke(app, ["init"], input="\n\n\n\nn\n")
    assert result.exit_code == 1
    result = runner.invoke(app, ["init", "--yes"], input="\n\n\n\n")
    assert result.exit_code == 0, result.output
    assert load_config(path) == before


def test_init_suggests_the_clone_it_is_run_in(world: Path, clone: Path) -> None:  # noqa: F811
    # the fake git runs in the clone, as if `init` were run there
    result = runner.invoke(app, ["init"], input="\n1\n2\n\n")
    assert result.exit_code == 0, result.output
    config = load_config(world / ".nightshift" / "nightshift.yaml")
    assert [r.path for r in config.repos] == [clone]


def test_init_suggests_no_repo_outside_git(world: Path) -> None:
    (world / "web").mkdir()
    result = runner.invoke(app, ["init"], input=f"\n{BOARD}\n\n{world / 'web'}\n")
    assert "Repos (paths of local clones, separated by commas):" in result.output


def test_init_writes_also_if_a_check_fails(world: Path) -> None:
    (world / "web").mkdir()
    result = runner.invoke(app, ["init"], input=f"\n{BOARD}\n\n{world / 'web'}\n")
    assert result.exit_code == 1
    assert (world / ".nightshift" / "nightshift.yaml").exists()


def test_check_without_config() -> None:
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "nightshift init" in result.output


def test_fake_runner_is_strict() -> None:
    assert FakeRunner({})(["x"]) == Result(127, "", "unexpected: x")


@pytest.mark.parametrize(
    ("settings", "suggested"),
    [
        ({"user.name": "octocat"}, "me"),  # a display name, even if it looks like a login
        ({"github.user": "hubber", "user.name": "octocat"}, "hubber"),
        ({"user.name": "Octo Cat"}, "me"),
        ({"github.user": "Octo Cat"}, "me"),  # not a login at all
        ({}, "me"),
    ],
)
def test_init_suggests_the_git_user(
    world: Path,
    clone: Path,  # noqa: F811
    settings: dict[str, str],
    suggested: str,
) -> None:
    git_config.update(settings)
    result = runner.invoke(app, ["init"], input=f"\n1\n2\n{clone}\n")
    assert f"picked up [{suggested}]" in result.output
    assert load_config(world / ".nightshift" / "nightshift.yaml").user == suggested
    assert ("logged in as me" in result.output) == (suggested != "me")


def test_init_again_suggests_the_current_user(world: Path, clone: Path) -> None:  # noqa: F811
    git_config["user.name"] = "octocat"
    path = world / ".nightshift" / "nightshift.yaml"
    path.parent.mkdir()
    path.write_text(f"user: me\nboards:\n- url: {BOARD}\nrepos:\n- path: {clone}\n")
    result = runner.invoke(app, ["init", "--yes"], input="\n\n\n\n")
    assert "picked up [me]" in result.output


def test_init_asks_again_for_a_login_that_does_not_exist(
    world: Path,
    clone: Path,  # noqa: F811
) -> None:
    git_config["github.user"] = "Lacuna"
    result = runner.invoke(app, ["init"], input=f"\n\n1\n2\n{clone}\n")
    assert "There is no GitHub user Lacuna" in result.output
    assert "picked up [me]" in result.output
    assert load_config(world / ".nightshift" / "nightshift.yaml").user == "me"
