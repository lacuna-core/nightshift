"""``~/.nightshift``: its paths, and the config file ``nightshift.yaml``."""

import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    ValidationError,
    field_validator,
    model_validator,
)

from nightshift.errors import ConfigError

DEFAULT_MAX_PARALLEL_AGENTS = 5
DEFAULT_STALL_TIMEOUT = timedelta(minutes=20)
DEFAULT_MAX_RUN_TIME = timedelta(hours=3)

_BOARD_URL = re.compile(
    r"https://github\.com/(?P<kind>users|orgs)/(?P<owner>[A-Za-z0-9-]+)"
    r"/projects/(?P<number>[1-9][0-9]*)(?:/.*)?"
)
_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


@dataclass(frozen=True)
class Paths:
    """Where Nightshift keeps its files (spec 8.2)."""

    root: Path

    @classmethod
    def default(cls) -> Paths:
        return cls(Path.home() / ".nightshift")

    @property
    def config(self) -> Path:
        return self.root / "nightshift.yaml"

    @property
    def database(self) -> Path:
        return self.root / "nightshift.db"

    @property
    def lock(self) -> Path:
        return self.root / "nightshift.lock"

    @property
    def log(self) -> Path:
        return self.root / "logs" / "nightshift.log"

    @property
    def worktrees(self) -> Path:
        return self.root / "worktrees"


def parse_duration(value: Any) -> timedelta:
    """Read ``40m``, ``5h`` or ``1h30m``."""
    if isinstance(value, timedelta):
        return value
    match = _DURATION.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None or not any(match.groups()):
        raise ValueError("must be a duration such as 40m, 5h or 1h30m")
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    duration = timedelta(hours=hours, minutes=minutes, seconds=seconds)
    if not duration:
        raise ValueError("must be longer than zero")
    return duration


def format_duration(value: timedelta) -> str:
    total = int(value.total_seconds())
    parts = ((total // 3600, "h"), (total % 3600 // 60, "m"), (total % 60, "s"))
    return "".join(f"{n}{unit}" for n, unit in parts if n) or "0s"


Duration = Annotated[
    timedelta, BeforeValidator(parse_duration), PlainSerializer(format_duration, return_type=str)
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BoardConfig(_Model):
    url: str
    """The board, as ``https://github.com/users/<owner>/projects/<n>`` (or ``orgs``)."""

    max_parallel_agents: int = Field(default=DEFAULT_MAX_PARALLEL_AGENTS, ge=1, strict=True)
    """How many live runs the board may have. Unset: 5."""

    @field_validator("url")
    @classmethod
    def _canonical(cls, value: str) -> str:
        match = _BOARD_URL.fullmatch(value.strip())
        if match is None:
            raise ValueError(
                "must look like https://github.com/users/<you>/projects/<n> "
                "or https://github.com/orgs/<org>/projects/<n>"
            )
        return f"https://github.com/{match['kind']}/{match['owner']}/projects/{match['number']}"


class RepoConfig(_Model):
    path: Path
    """The local clone. ``~`` is allowed."""

    stall_timeout: Duration = DEFAULT_STALL_TIMEOUT
    """How long a working agent may show no activity. Unset: 20m."""

    max_run_time: Duration = DEFAULT_MAX_RUN_TIME
    """How long a run may be running. Unset: 3h."""

    @field_validator("path")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        expanded = value.expanduser()
        if not expanded.is_absolute():
            raise ValueError("must be an absolute path, or start with ~")
        return expanded

    @property
    def resolved(self) -> Path:
        return self.path.resolve()


class ProjectConfig(_Model):
    boards: list[BoardConfig] = Field(min_length=1)
    repos: list[RepoConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_duplicates(self) -> ProjectConfig:
        urls = [board.url.lower() for board in self.boards]
        for i, url in enumerate(urls):
            if url in urls[:i]:
                raise ValueError(f"boards[{i}].url: {self.boards[i].url} is listed twice")
        paths = [repo.resolved for repo in self.repos]
        for i, path in enumerate(paths):
            if path in paths[:i]:
                raise ValueError(f"repos[{i}].path: {self.repos[i].path} is listed twice")
        return self

    def board(self, url: str) -> BoardConfig | None:
        return next((b for b in self.boards if b.url.lower() == url.lower()), None)

    def repo(self, path: Path) -> RepoConfig | None:
        return next((r for r in self.repos if r.resolved == path.resolve()), None)


def _where(loc: tuple[int | str, ...]) -> str:
    text = ""
    for part in loc:
        text += f"[{part}]" if isinstance(part, int) else f".{part}" if text else str(part)
    return text


def parse_config(data: Any) -> ProjectConfig:
    """Validate, with an error that names every bad key."""
    if not isinstance(data, dict):
        raise ConfigError("the file must have the keys `boards` and `repos`")
    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as e:
        lines = []
        for error in e.errors():
            where = _where(error["loc"])
            message = error["msg"].removeprefix("Value error, ")
            if error["type"] == "extra_forbidden":
                message = "unknown key"
            elif error["type"] == "missing":
                message = "is missing"
            lines.append(f"{where}: {message}" if where else message)
        raise ConfigError("\n".join(lines)) from e


def load_config(path: Path) -> ProjectConfig:
    try:
        text = path.read_text()
    except FileNotFoundError as e:
        raise ConfigError(f"{path} does not exist. Run `nightshift init`.") from e
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path} cannot be read: {e}") from e
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e
    return parse_config(data)


def _tilde(path: Path) -> str:
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def save_config(config: ProjectConfig, path: Path) -> None:
    """Write the file, without the settings that have their default."""
    data = config.model_dump(mode="json", exclude_defaults=True)
    for dumped, repo in zip(data["repos"], config.repos, strict=True):
        dumped["path"] = _tilde(repo.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    temporary.replace(path)
