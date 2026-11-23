"""Several instances on one machine (spec NFR-5): the homes, so that no two serve one board."""

import json
import os
from pathlib import Path

from nightshift.errors import ConfigError
from nightshift.project_config import Paths, load_config


def default_register() -> Path:
    return Path.home() / ".config" / "nightshift" / "homes.json"


class Homes:
    """The homes (``NIGHTSHIFT_HOME``) Nightshift was set up or started in on this machine.

    The register is one file for the whole machine, outside every home.
    """

    def __init__(self, register: Path, home: Path) -> None:
        self._register = register
        self._home = home.resolve()

    def record(self) -> None:
        """Add this home, and forget homes whose config is gone."""
        homes = [h for h in self._read() if h != self._home and Paths(h).config.exists()]
        self._register.parent.mkdir(parents=True, exist_ok=True)
        new = self._register.with_name(f"{self._register.name}.{os.getpid()}")
        new.write_text(json.dumps([str(h) for h in [*homes, self._home]], indent=2) + "\n")
        new.replace(self._register)

    def boards_elsewhere(self) -> dict[str, Path]:
        """Each board another home lists, with that home."""
        found: dict[str, Path] = {}
        for home in self._read():
            path = Paths(home).config
            if home == self._home or not path.exists():
                continue
            try:
                config = load_config(path)
            except ConfigError:
                continue
            for board in config.boards:
                found.setdefault(board.url, home)
        return found

    def _read(self) -> list[Path]:
        try:
            data = json.loads(self._register.read_text())
        except FileNotFoundError, json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        return [Path(h).resolve() for h in data if isinstance(h, str)]
