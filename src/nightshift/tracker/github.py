"""GitHub: boards, issues, labels, comments and PRs.

Nothing outside this module knows that a ticket ID is an issue number.
"""

import contextlib
import re
from collections.abc import Callable, Collection, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from nightshift.conventions import IN_PROGRESS, LABEL, LABELS, REQUIRED_STATUSES, TODO
from nightshift.errors import (
    TrackerAuthError,
    TrackerError,
    TrackerNotFoundError,
    TrackerUnavailableError,
)
from nightshift.model import (
    Board,
    BoardOverview,
    Brief,
    IssueCount,
    Item,
    Pr,
    Presence,
    PrState,
    RepoInfo,
    Run,
    TicketFacts,
)

API = "https://api.github.com"
_BOARD = re.compile(r"https://github\.com/(users|orgs)/([^/]+)/projects/(\d+)")
_SLUG = re.compile(
    r"(?:https://(?:[^@/]+@)?github\.com/|ssh://git@(?:ssh\.)?github\.com(?::\d+)?/|git@github\.com:)"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?"
)


_BATCH = 10
_MARKER = "nightshift"
_STATUS = 'fieldValueByName(name:"Status"){... on ProjectV2ItemFieldSingleSelectValue{name}}'
_ISSUE_FRAGMENT = (
    " fragment issue on Issue{id state labels(first:100){nodes{name}} "
    "repository{id nameWithOwner} "
    "projectItems(first:50,includeArchived:true){pageInfo{hasNextPage} "
    f"nodes{{id isArchived project{{url}} {_STATUS}}}}} "
    "timelineItems(last:100,itemTypes:[UNLABELED_EVENT])"
    "{nodes{... on UnlabeledEvent{createdAt label{name}}}}}"
)
_PR_FRAGMENT = (
    " fragment pr on PullRequest{id url state isDraft isCrossRepository baseRefName "
    "headRefOid createdAt timelineItems(last:1,itemTypes:[REOPENED_EVENT])"
    "{nodes{... on ReopenedEvent{createdAt}}}}"
)


@dataclass(frozen=True)
class _BoardIds:
    project: str
    field: str
    options: dict[str, str]


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
        self._boards: dict[str, _BoardIds] = {}

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
                    follow_redirects=True,  # a renamed repo
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

    def read_board(self, url: str) -> Board:
        """The candidate tickets in rank order. The search only saves points: it is checked."""
        items: list[Item] = []
        for project in self._board_pages(
            url,
            'field(name:"Status"){... on ProjectV2SingleSelectField{id options{id name}}}',
            _STATUS + " content{__typename ... on Issue{id number title state "
            "repository{id nameWithOwner} labels(first:100){nodes{name}} "
            "projectItems(first:20){nodes{isArchived project{url}}}}}",
            f'is:issue is:open label:{LABEL} status:"{TODO}","{IN_PROGRESS}"',
        ):
            status_field = project["field"] or {}
            options = {o["name"]: o["id"] for o in status_field.get("options", [])}
            if missing := [s for s in REQUIRED_STATUSES if s not in options]:
                return Board(url, problem=f"the status {', '.join(missing)} is missing")
            self._boards[url] = _BoardIds(project["id"], status_field["id"], options)
            for node in project["items"]["nodes"]:
                if item := _item(node, url, len(items)):
                    items.append(item)
        return Board(url, tuple(items))

    def set_status(self, board_url: str, item_id: str, status: str) -> None:
        ids = self._boards.get(board_url)
        if ids is None:
            self.read_board(board_url)
            ids = self._boards.get(board_url)
        if ids is None:
            raise TrackerError(f"board {board_url} has no usable Status field")
        self.graphql(
            "mutation($input:UpdateProjectV2ItemFieldValueInput!)"
            "{updateProjectV2ItemFieldValue(input:$input){clientMutationId}}",
            {
                "input": {
                    "projectId": ids.project,
                    "itemId": item_id,
                    "fieldId": ids.field,
                    "value": {"singleSelectOptionId": ids.options[status]},
                }
            },
        )

    # --- tickets ----------------------------------------------------------------------

    def read_tickets(
        self, runs: Sequence[Run], readable: Collection[str]
    ) -> dict[str, TicketFacts | None]:
        """The tickets of runs, by run ID. None where GitHub does not say.

        ``readable`` are the boards that could be read just now: only on those can a missing
        item mean that it was removed.
        """
        facts: dict[str, TicketFacts | None] = {}
        for start in range(0, len(runs), _BATCH):
            batch = runs[start : start + _BATCH]
            names = [n for i in range(len(batch)) for n in (f"$i{i}:ID!", f"$r{i}:ID!")]
            parts = []
            variables: dict[str, Any] = {}
            for i, run in enumerate(batch):
                variables |= {f"i{i}": run.ticket_ref, f"r{i}": run.repo_ref}
                parts.append(f"i{i}:node(id:$i{i}){{...issue}}")
                if run.branch is None:
                    parts.append(f"r{i}:node(id:$r{i}){{id}}")
                else:
                    names.append(f"$b{i}:String!")
                    variables[f"b{i}"] = run.branch
                    parts.append(
                        f"r{i}:node(id:$r{i}){{... on Repository{{id defaultBranchRef{{name}} "
                        f"pullRequests(headRefName:$b{i},first:20,"
                        "orderBy:{field:CREATED_AT,direction:DESC}){nodes{...pr}}}}"
                    )
            data = self.graphql(
                f"query({','.join(names)}){{{' '.join(parts)}}}{_ISSUE_FRAGMENT}"
                + (_PR_FRAGMENT if any(r.branch for r in batch) else ""),
                variables,
                partial=True,
            )
            for i, run in enumerate(batch):
                facts[run.id] = _facts(run, data.get(f"i{i}"), data.get(f"r{i}"), readable)
        return facts

    def brief(self, run: Run) -> Brief:
        """What the agent is told about its ticket, without comments of outsiders."""
        node = self.graphql(
            "query($id:ID!){node(id:$id){... on Issue{number title body url "
            "labels(first:100){nodes{name}} repository{nameWithOwner} "
            "comments(last:100){nodes{body viewerDidAuthor author{__typename login}}}}}}",
            {"id": run.ticket_ref},
        )["node"]
        if not node:
            raise TrackerNotFoundError(f"issue {run.repo}#{run.ticket_id} was not found")
        slug = node["repository"]["nameWithOwner"]
        trusted: dict[str, bool] = {}
        comments = []
        for comment in node["comments"]["nodes"]:
            login = (comment["author"] or {}).get("login")
            if login is None or (comment["viewerDidAuthor"] and _MARKER in comment["body"]):
                continue
            if comment["author"]["__typename"] != "User":
                continue
            if login not in trusted:
                trusted[login] = comment["viewerDidAuthor"] or self._can_write(slug, login)
            if trusted[login]:
                comments.append((login, comment["body"]))
        return Brief(
            repo=slug,
            ticket_id=run.ticket_id,
            title=node["title"],
            body=node["body"] or "",
            url=node["url"],
            labels=tuple(sorted(n["name"] for n in node["labels"]["nodes"])),
            comments=tuple(comments),
            closing_line=f"Closes #{node['number']}",
        )

    def _can_write(self, slug: str, login: str) -> bool:
        try:
            response = self._request("GET", f"/repos/{slug}/collaborators/{login}/permission")
        except TrackerUnavailableError:
            raise
        except TrackerError:
            return False
        return _json(response).get("permission") in ("admin", "write")

    def add_label(self, run_or_item: Run | Item, label: str) -> None:
        self._request(
            "POST",
            f"/repos/{run_or_item.repo}/issues/{run_or_item.ticket_id}/labels",
            {"labels": [label]},
        )

    def remove_label(self, run_or_item: Run | Item, label: str) -> None:
        with contextlib.suppress(TrackerNotFoundError):
            self._request(
                "DELETE",
                f"/repos/{run_or_item.repo}/issues/{run_or_item.ticket_id}/labels/"
                f"{quote(label, safe='')}",
            )

    def comment(self, ticket_ref: str, mark: str, text: str) -> None:
        """Comment once: a comment with the same mark is not posted again."""
        marker = f"<!-- {_MARKER} {mark} -->"
        after = None
        while True:
            node = self.graphql(
                "query($id:ID!,$after:String){node(id:$id){... on Issue{"
                "comments(last:100,before:$after){pageInfo{hasPreviousPage startCursor} "
                "nodes{body viewerDidAuthor}}}}}",
                {"id": ticket_ref, "after": after},
            )["node"]
            if not node:
                raise TrackerNotFoundError("the issue was not found")
            comments = node["comments"]
            if any(c["viewerDidAuthor"] and marker in c["body"] for c in comments["nodes"]):
                return
            if not comments["pageInfo"]["hasPreviousPage"]:
                break
            after = comments["pageInfo"]["startCursor"]
        self.graphql(
            "mutation($input:AddCommentInput!){addComment(input:$input){clientMutationId}}",
            {"input": {"subjectId": ticket_ref, "body": f"{text}\n\n{marker}"}},
        )

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


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _status(node: dict[str, Any]) -> str | None:
    return (node.get("fieldValueByName") or {}).get("name")


def _item(node: dict[str, Any], url: str, rank: int) -> Item | None:
    issue = node["content"]
    if node["isArchived"] or not issue or issue["__typename"] != "Issue":
        return None
    item = Item(
        item_id=node["id"],
        board_url=url,
        rank=rank,
        status=_status(node),
        repo=issue["repository"]["nameWithOwner"],
        repo_ref=issue["repository"]["id"],
        ticket_id=str(issue["number"]),
        ticket_ref=issue["id"],
        title=issue["title"],
        labels=frozenset(n["name"] for n in issue["labels"]["nodes"]),
        is_open=issue["state"] == "OPEN",
        boards=frozenset(
            p["project"]["url"].lower()
            for p in issue["projectItems"]["nodes"]
            if p and not p["isArchived"]
        ),
    )
    wanted = item.is_open and LABEL in item.labels and item.status in (TODO, IN_PROGRESS)
    return item if wanted else None


def _facts(
    run: Run, issue: dict[str, Any] | None, repo: dict[str, Any] | None, readable: Collection[str]
) -> TicketFacts | None:
    if not issue or not repo:
        return None
    items = issue["projectItems"]
    item = next(
        (
            n
            for n in items["nodes"]
            if n and not n["isArchived"] and n["project"]["url"].lower() == run.board_url.lower()
        ),
        None,
    )
    if item is not None:
        presence = Presence.ON
    elif run.board_url in readable and not items["pageInfo"]["hasNextPage"]:
        presence = Presence.REMOVED
    else:
        presence = Presence.UNKNOWN
    default = (repo.get("defaultBranchRef") or {}).get("name")
    return TicketFacts(
        labels=frozenset(n["name"] for n in issue["labels"]["nodes"]),
        is_open=issue["state"] == "OPEN",
        presence=presence,
        status=_status(item) if item else None,
        item_id=item["id"] if item else None,
        label_removed=any(
            event["label"]["name"] == LABEL and _time(event["createdAt"]) >= run.created_at
            for event in issue["timelineItems"]["nodes"]
            if event and event.get("label")
        ),
        prs=tuple(
            _pr(node)
            for node in (repo.get("pullRequests") or {}).get("nodes", [])
            if not node["isCrossRepository"] and node["baseRefName"] == default
        ),
        transferred=issue["repository"]["id"] != run.repo_ref,
        repo=issue["repository"]["nameWithOwner"],
    )


def _pr(node: dict[str, Any]) -> Pr:
    reopened = [e for e in node["timelineItems"]["nodes"] if e]
    return Pr(
        ref=node["id"],
        url=node["url"],
        state=PrState(node["state"].lower()),
        is_draft=node["isDraft"],
        head=node["headRefOid"],
        created_at=_time(node["createdAt"]),
        reopened_at=_time(reopened[-1]["createdAt"]) if reopened else None,
    )
