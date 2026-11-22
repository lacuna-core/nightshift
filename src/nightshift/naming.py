"""Names of branches and folders (spec FR-5)."""

import re
import unicodedata
from collections.abc import Callable

from nightshift.conventions import BRANCH_MAX


def slug(title: str) -> str:
    """A title as lowercase words joined by dashes, ``ticket`` if nothing is left."""
    plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", plain.replace("'", "")).strip("-") or "ticket"


def branch_name(ticket_id: str, title: str, index: int) -> str:
    """``<ticket-id>/<description>[-<index>]``, at most 40 characters."""
    suffix = "" if index == 1 else f"-{index}"
    room = BRANCH_MAX - len(ticket_id) - 1 - len(suffix)
    words = slug(title)
    if len(words) > room:
        cut = words[: room + 1]
        words = (cut[: cut.rfind("-")] if "-" in cut else words[:room]).strip("-")
    return f"{ticket_id}/{words}{suffix}"


def free_branch(ticket_id: str, title: str, taken: Callable[[str], bool]) -> str:
    index = 1
    while taken(name := branch_name(ticket_id, title, index)):
        index += 1
    return name


def folder_name(branch: str) -> str:
    return branch.replace("/", "-")


def repo_folder(slug_: str) -> str:
    return slug_.replace("/", "-")
