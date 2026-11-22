import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from nightshift.errors import (
    TrackerAuthError,
    TrackerError,
    TrackerNotFoundError,
    TrackerUnavailableError,
)
from nightshift.model import IssueCount
from nightshift.tracker.github import GitHub, slug_of_remote

BOARD = "https://github.com/users/me/projects/1"


def github(handler: Callable[[httpx.Request], httpx.Response]) -> GitHub:
    return GitHub(httpx.Client(transport=httpx.MockTransport(handler)), lambda: "t0ken")


def answers(*bodies: Any) -> GitHub:
    queue = list(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        body = queue.pop(0)
        return body if isinstance(body, httpx.Response) else httpx.Response(200, json=body)

    return github(handler)


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:acme/web.app.git",
        "https://github.com/acme/web.app",
        "https://user:pw@github.com/acme/web.app.git",
        "ssh://git@github.com/acme/web.app.git",
        "ssh://git@ssh.github.com:443/acme/web.app.git",
    ],
)
def test_slug_of_remote(url: str) -> None:
    assert slug_of_remote(url) == "acme/web.app"


@pytest.mark.parametrize("url", ["git@gitlab.com:a/b.git", "/tmp/repo", "https://github.com/a"])
def test_no_slug(url: str) -> None:
    assert slug_of_remote(url) is None


def test_expired_token_is_asked_for_again() -> None:
    tokens = iter(["old", "new"])
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(401 if len(seen) == 1 else 200, json={"login": "me"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert GitHub(client, lambda: next(tokens)).login() == ("me", None)
    assert seen == ["Bearer old", "Bearer new"]


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(502), TrackerUnavailableError),
        (httpx.Response(200, text="<html>"), TrackerUnavailableError),
        (httpx.Response(429), TrackerUnavailableError),
        (
            httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "99"}),
            TrackerUnavailableError,
        ),
        (httpx.Response(403, json={"message": "no"}), TrackerError),
        (httpx.Response(200, json={"errors": [{"message": "timeout"}]}), TrackerUnavailableError),
        (
            httpx.Response(200, json={"errors": [{"type": "RATE_LIMITED", "message": "x"}]}),
            TrackerUnavailableError,
        ),
        (
            httpx.Response(200, json={"errors": [{"type": "NOT_FOUND", "message": "x"}]}),
            TrackerNotFoundError,
        ),
        (
            httpx.Response(200, json={"errors": [{"type": "INSUFFICIENT_SCOPES", "message": "x"}]}),
            TrackerAuthError,
        ),
    ],
)
def test_errors(response: httpx.Response, error: type[Exception]) -> None:
    with pytest.raises(error) as caught:
        answers(response).graphql("query{x}")
    assert type(caught.value) is error


def test_rate_limit_says_when() -> None:
    response = httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9"})
    with pytest.raises(TrackerUnavailableError) as caught:
        answers(response).graphql("query{x}")
    assert caught.value.retry_at is not None
    assert caught.value.retry_at.timestamp() == 9


def test_connection_error_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(TrackerUnavailableError):
        github(handler).login()


def test_scopes() -> None:
    tracker = answers(httpx.Response(200, json={"login": "me"}, headers={"x-oauth-scopes": "a, b"}))
    assert tracker.login() == ("me", frozenset({"a", "b"}))


def test_repo_info() -> None:
    info = answers(
        {
            "data": {
                "repository": {
                    "id": "R_1",
                    "nameWithOwner": "Acme/Web",
                    "viewerPermission": "WRITE",
                    "defaultBranchRef": {"name": "trunk"},
                    "l0": {"name": "nightshift"},
                    "l1": None,
                }
            }
        }
    ).repo_info("acme/web")
    assert info.slug == "Acme/Web"
    assert info.default_branch == "trunk"
    assert info.can_push
    assert info.labels == {"nightshift"}


def issue(repo: str, *labels: str, state: str = "OPEN") -> dict[str, Any]:
    return {
        "id": "I",
        "isArchived": False,
        "content": {
            "__typename": "Issue",
            "state": state,
            "repository": {"nameWithOwner": repo},
            "labels": {"nodes": [{"name": label} for label in labels]},
        },
    }


def page(*items: Any, cursor: str | None = None) -> dict[str, Any]:
    return {
        "data": {
            "owner": {
                "projectV2": {
                    "id": "P",
                    "field": {"options": [{"name": "Todo"}, {"name": "Done"}]},
                    "items": {
                        "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                        "nodes": list(items),
                    },
                }
            }
        }
    }


def test_board_overview_counts_over_pages() -> None:
    overview = answers(
        page(
            issue("acme/web", "nightshift"),
            issue("acme/web", state="CLOSED"),
            {"id": "D", "isArchived": False, "content": {"__typename": "DraftIssue"}},
            {"id": "X", "isArchived": False, "content": None},
            cursor="c",
        ),
        page(issue("acme/web"), issue("acme/api")),
    ).board_overview(BOARD)
    assert overview.statuses == ("Todo", "Done")
    assert overview.issues == {"acme/api": IssueCount(1, 0), "acme/web": IssueCount(2, 1)}


def test_board_not_found() -> None:
    with pytest.raises(TrackerNotFoundError):
        answers({"data": {"owner": {"projectV2": None}}}).board_overview(BOARD)


def test_board_without_status_field() -> None:
    body = page()
    body["data"]["owner"]["projectV2"]["field"] = None
    assert answers(body).board_overview(BOARD).statuses == ()


def test_create_label_that_exists() -> None:
    answers(httpx.Response(422, json={"message": "already_exists"})).create_label(
        "a/b", "nightshift"
    )
    with pytest.raises(TrackerError):
        answers(httpx.Response(403, json={"message": "no"})).create_label("a/b", "nightshift")


def test_list_boards() -> None:
    def projects(*titles: str) -> dict[str, Any]:
        return {
            "projectsV2": {
                "nodes": [{"url": f"u/{t}", "title": t, "closed": t == "old"} for t in titles]
            }
        }

    body = {"viewer": {**projects("mine", "old"), "organizations": {"nodes": [projects("org")]}}}
    assert answers({"data": body}).list_boards() == [("u/mine", "mine"), ("u/org", "org")]


def test_request_shape() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=page())

    github(handler).board_overview("https://github.com/orgs/acme/projects/7")
    assert "organization(login:$login)" in seen[0]["query"]
    assert seen[0]["variables"]["number"] == 7
