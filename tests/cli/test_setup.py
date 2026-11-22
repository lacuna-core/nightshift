import json
from pathlib import Path

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
    if "viewer{" in query:
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


@pytest.fixture(autouse=True)
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path
    created.clear()
    monkeypatch.setenv("HOME", str(home))
    tools = FakeRunner(
        {
            "hc server status": ok('{"version": "0.1.18"}'),
            "claude --version": ok("2"),
            "gh auth token": ok("t"),
            "git": lambda args: proc.run(args, cwd=home / "web"),
        }
    )

    original = wiring.build

    def build(settings: Settings) -> wiring.Boundaries:
        return original(settings, run=tools, transport=httpx.MockTransport(handler), home=home)

    monkeypatch.setattr(wiring, "build", build)
    return home


def test_init_then_check(world: Path, clone: Path) -> None:  # noqa: F811
    result = runner.invoke(app, ["init"], input=f"1\n2\n{clone}\n")
    assert result.exit_code == 0, result.output
    config = load_config(world / ".nightshift" / "nightshift.yaml")
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
        f"boards:\n- url: {BOARD}\n  max_parallel_agents: 3\n"
        f"repos:\n- path: {clone}\n  stall_timeout: 40m\n"
    )
    before = load_config(path)
    result = runner.invoke(app, ["init"], input="\n\n\nn\n")
    assert result.exit_code == 1
    result = runner.invoke(app, ["init", "--yes"], input="\n\n\n")
    assert result.exit_code == 0, result.output
    assert load_config(path) == before


def test_init_writes_also_if_a_check_fails(world: Path) -> None:
    (world / "web").mkdir()
    result = runner.invoke(app, ["init"], input=f"{BOARD}\n\n{world / 'web'}\n")
    assert result.exit_code == 1
    assert (world / ".nightshift" / "nightshift.yaml").exists()


def test_check_without_config() -> None:
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "nightshift init" in result.output


def test_fake_runner_is_strict() -> None:
    assert FakeRunner({})(["x"]) == Result(127, "", "unexpected: x")
