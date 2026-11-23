import sqlite3
from pathlib import Path

import pytest

from nightshift.errors import NightshiftError
from nightshift.model import RunState
from nightshift.store import DuplicateRunError, Store
from tests.build import NOW, run


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state" / "nightshift.db")


def test_run_round_trips(store: Store) -> None:
    before = run(
        "failed",
        state_reason="x",
        taken_over=True,
        resume_count=1,
        limited_since=NOW,
        turn_at_end=3,
        started_at=NOW,
        ended_at=NOW,
    )
    store.add_run(before)
    assert store.get_run("r1") == before
    assert store.get_run("nope") is None


@pytest.mark.parametrize("state", [s for s in RunState if s is not RunState.DISCARDED])
def test_second_current_run_is_refused(store: Store, state: RunState) -> None:
    store.add_run(run(state))
    with pytest.raises(DuplicateRunError):
        store.add_run(run("claimed", id="r2", branch=None))


def test_new_run_after_discarded_one(store: Store) -> None:
    store.add_run(run("discarded"))
    store.add_run(run("claimed", id="r2", branch="42/export-csv-2"))
    assert [r.id for r in store.ticket_runs("I_42")] == ["r1", "r2"]
    first = store.get_run("r1")
    assert first is not None
    with pytest.raises(DuplicateRunError):
        store.update_run(first, state=RunState.RUNNING)


def test_branch_belongs_to_one_run(store: Store) -> None:
    store.add_run(run("discarded"))
    with pytest.raises(DuplicateRunError):
        store.add_run(run(id="r2"))
    assert store.branches("R_web") == {"42/export-csv"}


def test_same_ticket_id_in_two_repos(store: Store) -> None:
    store.add_run(run())
    store.add_run(run(id="r2", repo="acme/api", repo_ref="R_api", ticket_ref="I_9"))
    assert len(store.runs()) == 2


def test_update(store: Store) -> None:
    store.add_run(run())
    changed = store.update_run(run(), state=RunState.DONE, ended_at=NOW, tab_named=True)
    assert store.get_run("r1") == changed
    with pytest.raises(NightshiftError):
        store.update_run(run(id="other"), tab_named=True)


def test_watched_runs(store: Store) -> None:
    store.add_run(run("done", cleaned_up_at=NOW))
    store.add_run(run("discarded", id="r2", branch="a"))
    store.add_run(run("discarded", id="r3", branch="b", cleaned_up_at=NOW))
    store.add_run(run("discarded", id="r4", branch="c", taken_over=True))
    store.add_run(run("discarded", id="r5", branch=None, worktree_path=None))
    assert [r.id for r in store.watched_runs()] == ["r1", "r2"]


def test_transaction_rolls_back(store: Store) -> None:
    with pytest.raises(ZeroDivisionError), store.transaction():
        store.add_run(run())
        store.put("a", 1)
        raise ZeroDivisionError
    assert store.runs() == []
    assert store.get("a") is None


def test_loop_state(store: Store) -> None:
    store.put("flag:a", {"x": [1]})
    store.put("flag:a", {"x": [2]})
    store.put("flag:b", None)
    store.put("other", 1)
    assert store.get("flag:a") == {"x": [2]}
    assert store.get("none", 5) == 5
    assert store.items("flag:") == {"flag:a": {"x": [2]}, "flag:b": None}
    store.remove("flag:a")
    assert list(store.items("flag:")) == ["flag:b"]


def test_reopened_and_read_only(tmp_path: Path) -> None:
    path = tmp_path / "nightshift.db"
    first = Store(path)
    first.add_run(run())
    reader = Store(path, readonly=True)
    assert len(reader.runs()) == 1
    with pytest.raises(sqlite3.OperationalError):
        reader.put("a", 1)
    first.close()
    assert len(Store(path).runs()) == 1


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "nightshift.db"
    Store(path).close()
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 99")
    db.close()
    with pytest.raises(NightshiftError, match="newer"):
        Store(path)


def test_state_is_checked(store: Store) -> None:
    store.add_run(run())
    with pytest.raises(sqlite3.IntegrityError):
        store._db.execute("UPDATE runs SET state = 'sleeping'")
