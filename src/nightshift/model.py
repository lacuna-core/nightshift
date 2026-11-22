"""What Nightshift knows and does, as plain data. No I/O."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RepoInfo:
    """A repo as the tracker sees it."""

    slug: str
    ref: str
    default_branch: str
    can_push: bool
    labels: frozenset[str]


@dataclass(frozen=True)
class IssueCount:
    open: int = 0
    labelled: int = 0


@dataclass(frozen=True)
class BoardOverview:
    """What ``check`` shows about a board."""

    url: str
    statuses: tuple[str, ...]
    issues: dict[str, IssueCount] = field(default_factory=dict)
