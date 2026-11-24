from pathlib import Path

import pytest

from nightshift.config import Settings
from nightshift.homes import Homes
from nightshift.project_config import Paths, parse_config, save_config

BOARD = "https://github.com/users/me/projects/1"
OTHER = "https://github.com/users/me/projects/2"


def set_up(home: Path, *boards: str) -> Path:
    config = parse_config(
        {"user": "me", "boards": [{"url": b} for b in boards], "repos": [{"path": "~/web"}]}
    )
    save_config(config, Paths(home).config)
    return home


def test_boards_of_other_homes(tmp_path: Path) -> None:
    register = tmp_path / "homes.json"
    real = set_up(tmp_path / "real", BOARD)
    dev = set_up(tmp_path / "dev", OTHER)
    Homes(register, real).record()
    Homes(register, dev).record()
    assert Homes(register, dev).boards_elsewhere() == {BOARD: real.resolve()}
    assert Homes(register, real).boards_elsewhere() == {OTHER: dev.resolve()}


def test_recording_twice_keeps_one_entry_and_forgets_removed_homes(tmp_path: Path) -> None:
    register = tmp_path / "homes.json"
    gone = set_up(tmp_path / "gone", OTHER)
    home = set_up(tmp_path / "home", BOARD)
    Homes(register, gone).record()
    Paths(gone).config.unlink()
    Homes(register, home).record()
    Homes(register, home).record()
    assert register.read_text().count(str(home.resolve())) == 1
    assert str(gone.resolve()) not in register.read_text()


@pytest.mark.parametrize("content", [None, "not json", '{"a": 1}', '[1, "/x/../nowhere"]'])
def test_missing_or_broken_register_is_empty(tmp_path: Path, content: str | None) -> None:
    register = tmp_path / "homes.json"
    if content is not None:
        register.write_text(content)
    assert Homes(register, tmp_path).boards_elsewhere() == {}


def test_broken_config_of_another_home_is_skipped(tmp_path: Path) -> None:
    register = tmp_path / "homes.json"
    broken = tmp_path / "broken"
    Paths(broken).config.parent.mkdir(parents=True)
    Paths(broken).config.write_text("boards: [")
    register.write_text(f'["{broken}"]')
    assert Homes(register, tmp_path / "home").boards_elsewhere() == {}


def test_home_setting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NIGHTSHIFT_HOME", raising=False)
    assert Paths.of(Settings().home).root == (tmp_path / ".nightshift").resolve()
    monkeypatch.setenv("NIGHTSHIFT_HOME", "~/dev-home")
    assert Paths.of(Settings().home).config == (tmp_path / "dev-home" / "nightshift.yaml").resolve()
