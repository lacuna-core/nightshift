"""FR-17."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nightshift.claude import Claude
from nightshift.conventions import NEEDS_HUMAN
from nightshift.model import RunState
from tests.fakes import FakeRunner
from tests.world import Issue, World


@pytest.fixture
def world(tmp_path: Path) -> World:
    world = World(tmp_path, limit=3)
    world.issue(1, "One")
    world.issue(2, "Two")
    world.tick()
    world.agent("working", 1)
    world.agent("working", 2)
    world.tick()
    return world


@pytest.fixture
def issue(world: World) -> Issue:
    return world.tracker.issues["I_acme/web_1"]


def sent(world: World, number: int) -> list[str]:
    return list(world.terminal(number).get("sent", []))


def test_known_reset(world: World, issue: Issue) -> None:
    world.limit(timedelta(hours=2), 1)
    world.limit(timedelta(hours=2), 2)
    world.tick()
    world.issue(3, "Three")
    world.tick(100)
    assert [world.state(1), world.state(2)] == [RunState.RUNNING] * 2
    assert len(world.store.runs()) == 2
    assert NEEDS_HUMAN not in issue.labels
    assert sent(world, 1) == []
    assert len(world.notifier.sent) == 1
    assert world.run(1).limited_since
    world.tick(25)
    assert sent(world, 1) == ["continue"]
    assert sent(world, 2) == ["continue"]
    world.unlimit(1)
    world.unlimit(2)
    world.tick()
    assert world.state(3) is RunState.RUNNING
    assert world.run(1).limited_since is None
    assert world.run(1).active_seconds < 600


def test_hits_the_limit_again(world: World) -> None:
    world.limit(timedelta(hours=1), 1)
    world.tick(65)
    assert sent(world, 1) == ["continue"]
    world.limit(timedelta(hours=1), 1)
    world.tick(30)
    assert sent(world, 1) == ["continue"]
    world.tick(40)
    assert sent(world, 1) == ["continue", "continue"]
    assert world.state(1) is RunState.RUNNING


def test_agent_you_continued_gets_nothing(world: World) -> None:
    world.limit(timedelta(hours=1), 1)
    world.tick()
    world.unlimit(1)
    world.tick(90)
    assert sent(world, 1) == []


def test_run_that_ended_meanwhile_gets_nothing(world: World, issue: Issue) -> None:
    world.limit(timedelta(hours=1), 1)
    world.tick()
    world.unlabel(issue)
    world.tick(90)
    assert world.state(1) is RunState.CANCELLED
    assert sent(world, 1) == []


def test_unknown_reset_is_probed_with_one_agent(world: World) -> None:
    world.limit(None, 1)
    world.limit(None, 2)
    world.tick(30)
    assert sent(world, 1) == []
    world.tick()
    assert sent(world, 1) == ["continue"]
    assert sent(world, 2) == []
    world.limit(None, 1)  # still there
    world.tick(29)
    assert sent(world, 1) == ["continue"]
    world.tick()
    assert sent(world, 1) == ["continue", "continue"]
    world.unlimit(1)
    world.tick(2)
    assert sent(world, 2) == ["continue"]
    assert world.store.get("usage-limit") is None


def test_unknown_reset_is_probed_with_one_new_ticket(world: World) -> None:
    world.limit(None, 1)
    world.tick()
    world.hc._kill([world.terminal(1)["address"]])
    world.hc._kill([world.terminal(2)["address"]])
    world.tick(3)
    world.issue(3, "Three")
    world.issue(4, "Four")
    world.tick(20)
    assert len(world.store.runs()) == 2
    for _ in range(10):
        world.tick()
        if len(world.store.runs()) > 2:
            break
    assert len(world.store.runs()) == 3
    world.limit(None, 3)
    world.tick(25)
    assert len(world.store.runs()) == 3
    world.unlimit(3)
    world.tick(2)
    assert len(world.store.runs()) == 4
    assert world.store.get("usage-limit") is None


def test_limit_survives_a_restart(world: World) -> None:
    world.limit(timedelta(hours=1), 1)
    world.tick()
    world.restart()
    world.issue(3, "Three")
    world.tick(5)
    assert len(world.store.runs()) == 2
    assert len(world.notifier.sent) == 1
    world.tick(60)
    assert sent(world, 1) == ["continue"]


def test_lost_agent_that_was_limited_is_resumed_without_a_message(world: World) -> None:
    world.limit(timedelta(hours=1), 1)
    world.tick()
    world.hc.reboot()
    world.tick(2)
    assert sent(world, 1)[0].endswith(world.run(1).claude_session_id or "?")
    world.tick(70)
    assert sent(world, 1)[1:] == ["continue"]


def test_menu_of_the_limit_is_closed_first(world: World) -> None:
    world.limit(timedelta(hours=1), 1)
    world.agent("blocked", 1)
    world.tick(65)
    assert sent(world, 1) == ["continue"]
    assert world.state(1) is RunState.RUNNING


def test_ready_pr_is_not_complete_while_limited(world: World, issue: Issue) -> None:
    world.open_pr(issue)
    world.limit(timedelta(hours=1), 1)
    world.tick(10)
    assert world.state(1) is RunState.RUNNING


# --- the transcript -------------------------------------------------------------------------

# Recorded from Claude Code 2.1.311, without its message.
RECORDED = {
    "isSidechain": False,
    "type": "assistant",
    "timestamp": "2026-11-13T22:57:19.634Z",
    "apiError": "usage_limit_reached",
    "quotaLimits": {"status": "rejected", "resetsAt": 1794619200, "rateLimitType": "five_hour"},
    "error": "rate_limit",
    "isApiErrorMessage": True,
    "apiErrorStatus": 429,
}


def read(tmp_path: Path, *entries: object) -> object:
    folder = tmp_path / ".claude" / "projects" / "-some-folder"
    folder.mkdir(parents=True, exist_ok=True)
    lines = [e if isinstance(e, str) else json.dumps(e) for e in entries]
    (folder / "s1.jsonl").write_text("\n".join(lines))
    return Claude(FakeRunner({}), tmp_path).limit("s1")


def test_recorded_limit(tmp_path: Path) -> None:
    limit = read(tmp_path, {"type": "user"}, RECORDED, {"type": "system"}, {"type": "x"}, "{broken")
    assert limit is not None
    assert limit.resets_at == datetime.fromtimestamp(1794619200, UTC)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "after",
    [
        {"type": "user"},
        {"type": "assistant", "isApiErrorMessage": True, "apiError": "server_error"},
        {"type": "assistant", "message": {}},
    ],
)
def test_over_when_a_later_message_follows(tmp_path: Path, after: object) -> None:
    assert read(tmp_path, RECORDED, after) is None


def test_meta_and_sidechain_messages_do_not_count(tmp_path: Path) -> None:
    meta = {"type": "user", "isMeta": True}
    side = {"type": "assistant", "isSidechain": True}
    assert read(tmp_path, RECORDED, meta, side) is not None


@pytest.mark.parametrize("quota", [None, {}, {"resetsAt": "soon"}, {"resetsAt": 5}, []])
def test_unknown_reset_time(tmp_path: Path, quota: object) -> None:
    limit = read(tmp_path, RECORDED | {"quotaLimits": quota})
    assert limit is not None
    assert limit.resets_at is None  # type: ignore[attr-defined]


def test_no_transcript(tmp_path: Path) -> None:
    assert Claude(FakeRunner({}), tmp_path).limit("none") is None
