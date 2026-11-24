import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nightshift.cli.app import app
from nightshift.lock import Lock
from nightshift.store import Store
from tests.build import NOW, run
from tests.world import BOARD

runner = CliRunner()


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ".nightshift"
    root.mkdir()
    (root / "nightshift.yaml").write_text(
        f"user: me\nboards:\n- url: {BOARD}\nrepos:\n- path: {tmp_path / 'web'}\n"
    )
    return root


def test_status_before_the_first_start() -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "not running" in result.output


def test_status(home: Path) -> None:
    store = Store(home / "nightshift.db")
    store.add_run(run("running", board_url=BOARD))
    store.add_run(
        run(
            "failed", id="r2", ticket_id="7", ticket_ref="I_7", branch="7/x", state_reason="stalled"
        )
    )
    store.add_run(
        run("done", id="r3", ticket_id="8", ticket_ref="I_8", branch="8/y", kept_reason="dirty")
    )
    store.add_run(
        run("done", id="r4", ticket_id="9", ticket_ref="I_9", branch="9/z", cleaned_up_at=NOW)
    )
    store.put("failed:r1", "Complete: GitHub answered 502")
    store.put(
        "report",
        {"at": NOW.isoformat(), "problems": {"Herdr": "gone"}, "unlisted": ["acme/docs #3"]},
    )
    lock = Lock(home / "nightshift.lock")
    lock.acquire()
    result = runner.invoke(app, ["status"], env={"COLUMNS": "200"})
    lock.release()
    assert result.exit_code == 0, result.output
    for text in (
        f"running (process {os.getpid()})", "4 of 5 slots free", "Herdr: gone", "acme/docs #3",
        "42/export-csv", "stalled", "502", "dirty",
    ):  # fmt: skip
        assert text in result.output
    assert "9/z" not in result.output


def test_stop_when_not_running() -> None:
    result = runner.invoke(app, ["stop"])
    assert result.exit_code == 0
    assert "not running" in result.output


def test_start_twice(home: Path) -> None:
    lock = Lock(home / "nightshift.lock")
    lock.acquire()
    result = runner.invoke(app, ["start"])
    lock.release()
    assert result.exit_code == 1
    assert "already running" in result.output


def test_start_with_a_broken_config(home: Path) -> None:
    (home / "nightshift.yaml").write_text("boards: []")
    result = runner.invoke(app, ["start"])
    assert result.exit_code == 1
    assert "boards" in result.output


def test_start_refuses_without_tools(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    result = runner.invoke(app, ["start"])
    assert result.exit_code == 1
    assert "does not start" in result.output
