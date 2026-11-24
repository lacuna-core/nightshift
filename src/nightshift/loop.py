"""The long-running loop: poll, decide, act, wait."""

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from loguru import logger

from nightshift import decide
from nightshift.conventions import POLL_INTERVAL
from nightshift.errors import NightshiftError, TrackerUnavailableError
from nightshift.executor import Executor
from nightshift.model import Snapshot
from nightshift.observe import Observer
from nightshift.store import Store

_MAX_BACKOFF = 15 * 60.0


class Stop(Protocol):
    def is_set(self) -> bool: ...
    def wait(self, timeout: float | None = None) -> bool: ...


class Loop:
    def __init__(
        self,
        *,
        store: Store,
        observer: Observer,
        executor: Executor,
        stop: Stop,
        now: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._observer = observer
        self.executor = executor
        self._stop = stop
        self._now = now
        self._failures = 0

    def run(self) -> None:
        """Poll until stopped. The step in progress is always finished first."""
        while not self._stop.is_set():
            self._stop.wait(self.tick())
        logger.info("stopped; agents keep running in Herdr")

    def tick(self) -> float:
        """One poll. Returns the seconds to wait until the next."""
        problem = ""
        delay = POLL_INTERVAL.total_seconds()
        try:
            self._poll()
            self._failures = 0
        except TrackerUnavailableError as e:
            problem = str(e)
            self._failures += 1
            delay = min(delay * 2 ** (self._failures - 1), _MAX_BACKOFF)
            if e.retry_at is not None:
                delay = max(delay, (e.retry_at - self._now()).total_seconds() + 1)
            logger.warning("{}; next poll in {:.0f} s", problem, delay)
        except NightshiftError as e:
            problem = str(e)
            logger.error("{}", problem)
        except Exception as e:  # the loop must outlive any bug in one poll
            problem = f"unexpected error: {e!r}"
            logger.exception("unexpected error in this poll")
        if problem:
            self._report(None, problem)
        return delay

    def _poll(self) -> None:
        self.executor.repo_problems = {}
        self.executor.flush_all()
        snapshot = self._observer.observe()
        for action in decide.decide(snapshot):
            if self._stop.is_set():
                break
            self.executor.perform(action)
        self._report(snapshot, "")

    def _report(self, snapshot: Snapshot | None, problem: str) -> None:
        """What ``status``, another process, shows."""
        problems: dict[str, Any] = dict(self._observer.problems)
        if problem:
            problems["GitHub"] = problem
        for text in self.executor.repo_problems.values():
            problems.setdefault(text.split(":")[0], text)
        report: dict[str, Any] = {"at": self._now().isoformat(), "problems": problems}
        if snapshot is not None:
            report["unlisted"] = [
                f"{item.repo} #{item.ticket_id}" for item in decide.unlisted(snapshot)
            ]
            report["not_assigned"] = [
                f"{item.repo} #{item.ticket_id}" for item in decide.not_assigned(snapshot)
            ]
        self._store.put("report", report)
