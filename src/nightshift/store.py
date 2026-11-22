"""The state database (spec 8.1): runs, and what ``status`` must be able to show."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nightshift.errors import NightshiftError
from nightshift.model import Run, RunState

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE runs (
  id                TEXT PRIMARY KEY,
  repo              TEXT NOT NULL,
  ticket_id         TEXT NOT NULL,
  repo_ref          TEXT NOT NULL,
  ticket_ref        TEXT NOT NULL,
  clone_path        TEXT NOT NULL,
  board_url         TEXT NOT NULL,
  board_item_id     TEXT,
  state             TEXT NOT NULL CHECK (state IN (
                      'claimed', 'provisioning', 'running', 'waiting_for_human',
                      'completing', 'done', 'failed', 'cancelled', 'resetting',
                      'discarded')),
  state_reason      TEXT,
  branch            TEXT,
  worktree_path     TEXT,
  herdr_space_id    TEXT,
  herdr_tab_id      TEXT,
  herdr_terminal    TEXT,
  claude_session_id TEXT,
  seen_out_of_todo  INTEGER NOT NULL DEFAULT 0,
  taken_over        INTEGER NOT NULL DEFAULT 0,
  resume_count      INTEGER NOT NULL DEFAULT 0,
  limited_since     TEXT,
  active_seconds    INTEGER NOT NULL DEFAULT 0,
  turn_at_end       INTEGER,
  stop_requested_at TEXT,
  pr_ref            TEXT,
  tab_named         INTEGER NOT NULL DEFAULT 0,
  kept_reason       TEXT,
  created_at        TEXT NOT NULL,
  started_at        TEXT,
  last_activity_at  TEXT,
  ended_at          TEXT,
  cleaned_up_at     TEXT
) STRICT;
CREATE UNIQUE INDEX one_current_run ON runs (repo, ticket_id) WHERE state <> 'discarded';
CREATE UNIQUE INDEX one_run_per_branch ON runs (repo, branch) WHERE branch IS NOT NULL;
CREATE TABLE loop_state (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
"""

_COLUMNS = tuple(f.name for f in fields(Run))
_TIMES = frozenset(f.name for f in fields(Run) if "datetime" in str(f.type))
_FLAGS = frozenset({"seen_out_of_todo", "taken_over", "tab_named"})


class DuplicateRunError(NightshiftError):
    """The ticket has a current run, or the branch belongs to another run."""


def _to_db(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, RunState):
        return value.value
    return value


def _to_run(row: sqlite3.Row) -> Run:
    values: dict[str, Any] = dict(row)
    for name in _TIMES:
        if values[name] is not None:
            values[name] = datetime.fromisoformat(values[name])
    for name in _FLAGS:
        values[name] = bool(values[name])
    values["state"] = RunState(values["state"])
    return Run(**values)


class Store:
    """Every change is committed at once, so a crash loses nothing."""

    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        if readonly:
            self._db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(path, timeout=10)
        self._db.isolation_level = None
        self._db.row_factory = sqlite3.Row
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise NightshiftError(f"{path} was written by a newer version of Nightshift")
        if readonly:
            return
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = FULL")
        if version == 0:
            self._db.executescript(f"BEGIN; {_SCHEMA} PRAGMA user_version = 1; COMMIT;")

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._db.in_transaction:
            yield
            return
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    # --- runs -------------------------------------------------------------------------

    def add_run(self, run: Run) -> None:
        marks = ", ".join("?" for _ in _COLUMNS)
        try:
            self._db.execute(
                f"INSERT INTO runs ({', '.join(_COLUMNS)}) VALUES ({marks})",
                [_to_db(getattr(run, name)) for name in _COLUMNS],
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateRunError(f"{run.repo}#{run.ticket_id} has a current run") from e

    def update_run(self, run: Run, **changes: Any) -> Run:
        """Change some columns, in the database and in the returned copy."""
        if not changes:
            return run
        names = list(changes)
        try:
            cursor = self._db.execute(
                f"UPDATE runs SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",
                [*(_to_db(changes[n]) for n in names), run.id],
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateRunError(f"run {run.id}: {e}") from e
        if cursor.rowcount != 1:
            raise NightshiftError(f"run {run.id} is not in the database")
        return replace(run, **changes)

    def get_run(self, run_id: str) -> Run | None:
        row = self._db.execute("SELECT * FROM runs WHERE id = ?", [run_id]).fetchone()
        return _to_run(row) if row else None

    def runs(self) -> list[Run]:
        rows = self._db.execute("SELECT * FROM runs ORDER BY created_at, id").fetchall()
        return [_to_run(row) for row in rows]

    def watched_runs(self) -> list[Run]:
        """Runs that still need looking at.

        A current run stays watched for good, because its ticket can be moved back to Todo.
        A discarded one is watched until it is cleaned up.
        """
        return [
            run
            for run in self.runs()
            if run.state is not RunState.DISCARDED
            or (run.cleaned_up_at is None and not run.taken_over and run.worktree_path)
        ]

    def ticket_runs(self, ticket_ref: str) -> list[Run]:
        return [run for run in self.runs() if run.ticket_ref == ticket_ref]

    def branches(self, repo_ref: str) -> set[str]:
        rows = self._db.execute(
            "SELECT branch FROM runs WHERE repo_ref = ? AND branch IS NOT NULL", [repo_ref]
        )
        return {row["branch"] for row in rows}

    # --- loop state -------------------------------------------------------------------

    def put(self, key: str, value: Any) -> None:
        self._db.execute(
            "INSERT INTO loop_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            [key, json.dumps(value), datetime.now(UTC).isoformat()],
        )

    def get(self, key: str, default: Any = None) -> Any:
        row = self._db.execute("SELECT value FROM loop_state WHERE key = ?", [key]).fetchone()
        return json.loads(row["value"]) if row else default

    def remove(self, key: str) -> None:
        self._db.execute("DELETE FROM loop_state WHERE key = ?", [key])

    def items(self, prefix: str) -> dict[str, Any]:
        rows = self._db.execute(
            "SELECT key, value FROM loop_state WHERE substr(key, 1, ?) = ? ORDER BY key",
            [len(prefix), prefix],
        )
        return {row["key"]: json.loads(row["value"]) for row in rows}
