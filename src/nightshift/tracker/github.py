"""GitHub: boards, issues, labels, comments and PRs.

Nothing outside this module knows that a ticket ID is an issue number.
"""

import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from nightshift.conventions import LABEL, LABELS
from nightshift.errors import (
    TrackerAuthError,
    TrackerError,
    TrackerNotFoundError,
    TrackerUnavailableError,
)
from nightshift.model import BoardOverview, IssueCount, RepoInfo

API = "https://api.github.com"
_BOARD = re.compile(r"https://github\.com/(users|orgs)/([^/]+)/projects/(\d+)")
_SLUG = re.compile(
    r"(?:https://(?:[^@/]+@)?github\.com/|ssh://git@(?:ssh\.)?github\.com(?::\d+)?/|git@github\.com:)"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?"
)


def slug_of_remote(url: str) -> str | None:
    """``<owner>/<repo>`` of a GitHub remote URL, or None for another host."""
    match = _SLUG.fullmatch(url.strip())
    return f"{match[1]}/{match[2]}" if match else None


class GitHub:
    def __init__(self, http: httpx.Client, token: Callable[[], str]) -> None:
        """``token`` is asked again when GitHub refuses the one it gave before."""
        self._http = http
        self._get_token = token
        self._token: str | None = None

    # --- plumbing ---------------------------------------------------------------------

    def _request(self, method: str, path: str, body: Any = None) -> httpx.Response:
        for attempt in (1, 2):
            if self._token is None:
                self._token = self._get_token()
            try:
                response = self._http.request(
                    method,
                    f"{API}{path}",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "nightshift",
                    },
                )
            except httpx.HTTPError as e:
                raise TrackerUnavailableError(f"GitHub cannot be reached: {e}") from e
            if response.status_code == 401 and attempt == 1:
                self._token = None
                continue
            break
        self._raise_for(response)
        return response

    def _raise_for(self, response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        message = f"GitHub answered {status}"
        if status >= 500 or status == 408:
            raise TrackerUnavailableError(message)
        limited = (
            status == 429
            or "retry-after" in response.headers
            or response.headers.get("x-ratelimit-remaining") == "0"
        )
        if status in (403, 429) and limited:
            raise TrackerUnavailableError("GitHub is rate limiting", _retry_at(response.headers))
        detail = _json(response).get("message", "")
        if status == 401:
            raise TrackerAuthError(f"GitHub refused the `gh` login: {detail}")
        if status == 404:
            raise TrackerNotFoundError(f"{message}: {detail}")
        raise TrackerError(f"{message}: {detail}")

    def graphql(
        self, query: str, variables: dict[str, Any] | None = None, *, partial: bool = False
    ) -> dict[str, Any]:
        """Run a query. With ``partial``, what GitHub could not find is null instead of an error."""
        response = self._request("POST", "/graphql", {"query": query, "variables": variables or {}})
        answer = _json(response)
        data = answer.get("data")
        errors = [e for e in answer.get("errors") or [] if isinstance(e, dict)]
        if partial and isinstance(data, dict) and all(e.get("type") == "NOT_FOUND" for e in errors):
            return data
        if errors:
            message = "; ".join(str(e.get("message", "unknown error")) for e in errors)
            kinds = {str(e.get("type", "")) for e in errors}
            if kinds & {"RATE_LIMITED", "RATE_LIMIT"}:
                raise TrackerUnavailableError(
                    "GitHub is rate limiting", _retry_at(response.headers)
                )
            if "INSUFFICIENT_SCOPES" in kinds:
                raise TrackerAuthError(message)
            if "NOT_FOUND" in kinds:
                raise TrackerNotFoundError(message)
            if not kinds - {""}:  # no type at all: GitHub's own timeouts and crashes
                raise TrackerUnavailableError(message)
            raise TrackerError(message)
        if not isinstance(data, dict):
            raise TrackerUnavailableError("GitHub sent an answer without data")
        return data

    # --- setup ------------------------------------------------------------------------

    def login(self) -> tuple[str, frozenset[str] | None]:
        """Who is logged in, and the token's scopes (None: the token has no scopes)."""
        response = self._request("GET", "/user")
        header = response.headers.get("x-oauth-scopes")
        scopes = (
            None if header is None else frozenset(s.strip() for s in header.split(",") if s.strip())
        )
        return str(_json(response).get("login", "")), scopes

    def list_boards(self) -> list[tuple[str, str]]:
        """``(url, title)`` of the open projects of the user and their organizations."""
        fragment = "projectsV2(first:50){nodes{url title closed}}"
        data = self.graphql(
            "query{viewer{%s organizations(first:50){nodes{%s}}}}" % (fragment, fragment),  # noqa: UP031
            partial=True,
        )
        owners = [data["viewer"], *(o for o in data["viewer"]["organizations"]["nodes"] if o)]
        boards = [p for o in owners for p in (o.get("projectsV2") or {}).get("nodes") or [] if p]
        return [(p["url"], p["title"]) for p in boards if not p["closed"]]

    def repo_info(self, slug: str) -> RepoInfo:
        owner, name = slug.split("/")
        aliases = " ".join(
            f'l{i}: label(name: "{label}") {{ name }}' for i, label in enumerate(LABELS)
        )
        data = self.graphql(
            "query($o:String!,$n:String!){repository(owner:$o,name:$n){id nameWithOwner "
            f"viewerPermission defaultBranchRef{{name}} {aliases}}}}}",
            {"o": owner, "n": name},
        )["repository"]
        if data is None:
            raise TrackerNotFoundError(f"{slug} was not found on GitHub")
        branch = data["defaultBranchRef"]
        return RepoInfo(
            slug=data["nameWithOwner"],
            ref=data["id"],
            default_branch=branch["name"] if branch else "",
            can_push=data["viewerPermission"] in ("WRITE", "MAINTAIN", "ADMIN"),
            labels=frozenset(
                label for i, label in enumerate(LABELS) if data.get(f"l{i}") is not None
            ),
        )

    def create_label(self, slug: str, label: str) -> None:
        color, description = LABELS[label]
        try:
            self._request(
                "POST",
                f"/repos/{slug}/labels",
                {"name": label, "color": color, "description": description},
            )
        except TrackerError as e:
            if "422" not in str(e):  # 422: it exists already
                raise

    # --- boards -----------------------------------------------------------------------

    def board_overview(self, url: str) -> BoardOverview:
        statuses: tuple[str, ...] = ()
        counts: dict[str, IssueCount] = {}
        for project in self._board_pages(
            url,
            'field(name:"Status"){... on ProjectV2SingleSelectField{options{name}}}',
            "content{__typename ... on Issue{state repository{nameWithOwner} "
            "labels(first:100){nodes{name}}}}",
        ):
            statuses = tuple(o["name"] for o in (project["field"] or {}).get("options", []))
            for item in project["items"]["nodes"]:
                issue = item["content"]
                if not issue or issue["__typename"] != "Issue" or issue["state"] != "OPEN":
                    continue
                slug = issue["repository"]["nameWithOwner"]
                count = counts.get(slug, IssueCount())
                labelled = LABEL in (n["name"] for n in issue["labels"]["nodes"])
                counts[slug] = IssueCount(count.open + 1, count.labelled + labelled)
        return BoardOverview(url, statuses, dict(sorted(counts.items())))

    def _board_pages(
        self, url: str, fields: str, item_fields: str, search: str | None = None
    ) -> Iterator[dict[str, Any]]:
        match = _BOARD.fullmatch(url)
        if match is None:
            raise TrackerError(f"{url} is not a GitHub Project URL")
        owner = "user" if match[1] == "users" else "organization"
        query = (
            "query($login:String!,$number:Int!,$after:String,$search:String){"
            f"owner:{owner}(login:$login){{projectV2(number:$number){{id {fields} "
            "items(first:100,after:$after,query:$search,"
            "orderBy:{field:POSITION,direction:ASC}){pageInfo{hasNextPage endCursor} "
            f"nodes{{id isArchived {item_fields}}}}}}}}}}}"
        )
        after = None
        while True:
            data = self.graphql(
                query,
                {"login": match[2], "number": int(match[3]), "after": after, "search": search},
            )
            project = (data["owner"] or {}).get("projectV2")
            if project is None:
                raise TrackerNotFoundError(f"board {url} was not found")
            yield project
            page = project["items"]["pageInfo"]
            if not page["hasNextPage"]:
                return
            after = page["endCursor"]


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as e:
        if response.status_code >= 400:
            return {}
        raise TrackerUnavailableError("GitHub sent an answer that is not JSON") from e
    return data if isinstance(data, dict) else {}


def _retry_at(headers: httpx.Headers) -> datetime | None:
    now = datetime.now(UTC)
    if (after := headers.get("retry-after", "")).isdigit():
        return now + timedelta(seconds=int(after))
    if (reset := headers.get("x-ratelimit-reset", "")).isdigit():
        return datetime.fromtimestamp(int(reset), UTC)
    return None
