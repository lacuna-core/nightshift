from datetime import timedelta
from pathlib import Path

import pytest

from nightshift.errors import ConfigError
from nightshift.project_config import (
    format_duration,
    load_config,
    parse_config,
    parse_duration,
    save_config,
)

BOARD = "https://github.com/users/me/projects/1"


def write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_example_from_spec(tmp_path: Path) -> None:
    config = load_config(
        write(
            tmp_path / "nightshift.yaml",
            """
user: you
boards:
  - url: https://github.com/users/you/projects/1
  - url: https://github.com/orgs/acme/projects/2/views/3
    max_parallel_agents: 2
repos:
  - path: ~/dev/webapp
  - path: ~/dev/api
    stall_timeout: 40m
    max_run_time: 5h
""",
        )
    )
    assert [b.max_parallel_agents for b in config.boards] == [5, 2]
    assert config.boards[1].url == "https://github.com/orgs/acme/projects/2"
    assert config.repos[0].path == Path.home() / "dev/webapp"
    assert config.repos[0].stall_timeout == timedelta(minutes=20)
    assert config.repos[1].max_run_time == timedelta(hours=5)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (None, "boards"),
        ({}, "boards: is missing"),
        ({"boards": [{"url": BOARD}], "repos": [{"path": "/a"}]}, "user: is missing"),
        ({"user": "no spaces", "boards": [{"url": BOARD}], "repos": [{"path": "/a"}]}, "user: "),
        ({"user": "me", "boards": [], "repos": [{"path": "/a"}]}, "boards: "),
        (
            {"user": "me", "boards": [{"url": BOARD}], "repos": [{"path": "/a"}], "x": 1},
            "x: unknown key",
        ),
        (
            {"user": "me", "boards": [{"url": BOARD, "agents": 1}], "repos": [{"path": "/a"}]},
            "boards[0].agents: unknown key",
        ),
        (
            {
                "user": "me",
                "boards": [{"url": BOARD, "max_parallel_agents": 0}],
                "repos": [{"path": "/a"}],
            },
            "boards[0].max_parallel_agents: ",
        ),
        (
            {
                "user": "me",
                "boards": [{"url": BOARD, "max_parallel_agents": True}],
                "repos": [{"path": "/a"}],
            },
            "boards[0].max_parallel_agents: ",
        ),
        (
            {"user": "me", "boards": [{"url": "https://x.org"}], "repos": [{"path": "/a"}]},
            "boards[0].url: ",
        ),
        ({"user": "me", "boards": [{"url": BOARD}], "repos": [{"path": "a"}]}, "repos[0].path: "),
        (
            {
                "user": "me",
                "boards": [{"url": BOARD}],
                "repos": [{"path": "/a", "stall_timeout": 40}],
            },
            "repos[0].stall_timeout: ",
        ),
        (
            {
                "user": "me",
                "boards": [{"url": BOARD}, {"url": BOARD + "/"}],
                "repos": [{"path": "/a"}],
            },
            "boards[1].url: ",
        ),
        (
            {
                "user": "me",
                "boards": [{"url": BOARD}],
                "repos": [{"path": "/a"}, {"path": "/b/../a"}],
            },
            "repos[1].path: ",
        ),
    ],
)
def test_errors_name_the_key(data: object, message: str) -> None:
    with pytest.raises(ConfigError) as error:
        parse_config(data)
    assert message in str(error.value)


def test_missing_and_broken_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="nightshift init"):
        load_config(tmp_path / "none.yaml")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(write(tmp_path / "a.yaml", "a: [1"))


@pytest.mark.parametrize("text", ["40m", "5h", "1h30m", "45s"])
def test_durations_round_trip(text: str) -> None:
    assert format_duration(parse_duration(text)) == text


@pytest.mark.parametrize("value", ["", "0m", "5", "5 minutes", 5, None])
def test_bad_durations(value: object) -> None:
    with pytest.raises(ValueError):
        parse_duration(value)


def test_save_leaves_out_defaults_and_keeps_the_rest(tmp_path: Path) -> None:
    config = parse_config(
        {
            "user": "me",
            "boards": [{"url": BOARD, "max_parallel_agents": 2}],
            "repos": [
                {"path": "~/dev/a", "stall_timeout": "40m"},
                {"path": "/srv/b", "max_run_time": "3h"},
            ],
        }
    )
    path = tmp_path / "new" / "nightshift.yaml"
    save_config(config, path)
    assert path.read_text() == (
        "user: me\n"
        f"boards:\n- url: {BOARD}\n  max_parallel_agents: 2\n"
        "repos:\n- path: ~/dev/a\n  stall_timeout: 40m\n- path: /srv/b\n"
    )
    assert load_config(path) == config
