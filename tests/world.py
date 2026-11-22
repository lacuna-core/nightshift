"""A whole small world: GitHub and Herdr as fakes, git for real, and a clock that tests move."""

import itertools
import json
import subprocess
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from nightshift import proc
from nightshift.errors import TrackerNotFoundError, TrackerUnavailableError
from nightshift.executor import Executor
from nightshift.git import Git
from nightshift.herdr import RUN_LABEL, Herdr
from nightshift.loop import Loop
from nightshift.model import (
    Board,
    Brief,
    Item,
    Pr,
    Presence,
    PrState,
    RepoInfo,
    Run,
    RunState,
    TicketFacts,
)
from nightshift.observe import Observer
from nightshift.proc import Result
from nightshift.project_config import Paths, ProjectConfig, parse_config
from nightshift.store import Store

BOARD = "https://github.com/users/me/projects/1"
BOARD_2 = "https://github.com/users/me/projects/2"


class Crash(BaseException):
    """Nightshift is killed right here."""


@dataclass
class Issue:
    number: int
    title: str
    repo: str
    status: dict[str, str | None] = field(default_factory=dict)
    labels: set[str] = field(default_factory=lambda: {"nightshift"})
    is_open: bool = True
    label_removed_at: datetime | None = None
    comments: list[str] = field(default_factory=list)
    prs: dict[str, list[Pr]] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"I_{self.repo}_{self.number}"


class FakeTracker:
    """GitHub, as far as Nightshift sees it."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.issues: dict[str, Issue] = {}
        self.ranks: dict[str, list[str]] = {BOARD: [], BOARD_2: []}
        self.down = False
        self.broken_boards: set[str] = set()
        self.log: list[str] = []
        self.closed_prs: list[str] = []
        self.before_write: Callable[[str], None] = lambda what: None

    def _up(self) -> None:
        if self.down:
            raise TrackerUnavailableError("GitHub is down")

    def _write(self, what: str) -> None:
        self._up()
        self.before_write(what)
        self.log.append(what)

    def repo_info(self, slug: str) -> RepoInfo:
        self._up()
        return RepoInfo(slug, f"R_{slug}", "main", True, frozenset())

    def read_board(self, url: str) -> Board:
        self._up()
        if url in self.broken_boards:
            return Board(url, problem="the status In Review is missing")
        items = []
        for rank, ref in enumerate(self.ranks[url]):
            issue = self.issues[ref]
            status = issue.status.get(url)
            wanted = status in ("Todo", "In Progress") and "nightshift" in issue.labels
            if url not in issue.status or not wanted or not issue.is_open:
                continue
            items.append(
                Item(
                    item_id=f"{url}:{ref}",
                    board_url=url,
                    rank=rank,
                    status=status,
                    repo=issue.repo,
                    repo_ref=f"R_{issue.repo}",
                    ticket_id=str(issue.number),
                    ticket_ref=ref,
                    title=issue.title,
                    labels=frozenset(issue.labels),
                    boards=frozenset(u.lower() for u in issue.status),
                )
            )
        return Board(url, tuple(items))

    def read_tickets(
        self, runs: Sequence[Run], readable: Collection[str]
    ) -> dict[str, TicketFacts | None]:
        self._up()
        facts: dict[str, TicketFacts | None] = {}
        for run in runs:
            issue = self.issues.get(run.ticket_ref)
            if issue is None:
                facts[run.id] = None
                continue
            on = run.board_url in issue.status
            gone = Presence.REMOVED if run.board_url in readable else Presence.UNKNOWN
            facts[run.id] = TicketFacts(
                labels=frozenset(issue.labels),
                is_open=issue.is_open,
                presence=Presence.ON if on else gone,
                status=issue.status.get(run.board_url),
                item_id=f"{run.board_url}:{issue.ref}" if on else None,
                label_removed=issue.label_removed_at is not None
                and issue.label_removed_at >= run.created_at,
                prs=tuple(issue.prs.get(run.branch or "", ())),
                repo=issue.repo,
            )
        return facts

    def set_status(self, board_url: str, item_id: str, status: str) -> None:
        self._write(f"status {status}")
        ref = item_id.removeprefix(f"{board_url}:")
        self.issues[ref].status[board_url] = status

    def _issue(self, of: Run | Item) -> Issue:
        return self.issues[of.ticket_ref]

    def add_label(self, run_or_item: Run | Item, label: str) -> None:
        self._write(f"label {label}")
        self._issue(run_or_item).labels.add(label)

    def remove_label(self, run_or_item: Run | Item, label: str) -> None:
        self._write(f"unlabel {label}")
        self._issue(run_or_item).labels.discard(label)

    def comment(self, ticket_ref: str, mark: str, text: str) -> None:
        self._up()
        if ticket_ref not in self.issues:
            raise TrackerNotFoundError("gone")
        marker = f"<!-- {mark} -->"
        comments = self.issues[ticket_ref].comments
        if any(marker in c for c in comments):
            return
        self._write(f"comment {mark.split('event=')[-1]}")
        comments.append(f"{text}\n{marker}")

    def brief(self, run: Run) -> Brief:
        self._up()
        issue = self.issues[run.ticket_ref]
        return Brief(
            issue.repo, run.ticket_id, issue.title, "Body", "url", ("nightshift",), (), "Closes #1"
        )

    def close_pr(self, pr_ref: str) -> None:
        self._write(f"close {pr_ref}")
        self.closed_prs.append(pr_ref)
        for issue in self.issues.values():
            for prs in issue.prs.values():
                prs[:] = [replace(p, state=PrState.CLOSED) if p.ref == pr_ref else p for p in prs]

    def pr_commits(self, pr_ref: str) -> frozenset[str]:
        self._up()
        return frozenset(self.world.pr_commits.get(pr_ref, ()))


class FakeHerdr:
    """``hc``, and the programs in its terminals."""

    def __init__(self) -> None:
        self.terminals: dict[str, dict[str, Any]] = {}
        self._numbers = itertools.count(1)
        self._pids = itertools.count(1000)
        self.down = False
        self.window: dict[str, str] | None = None
        self.starts: list[list[str]] = []
        self.calls: list[str] = []
        self.sticky_interrupt = False
        self.before: Callable[[str], None] = lambda what: None

    def __call__(
        self, args: Sequence[str], *, cwd: Path | None = None, timeout: float = 60
    ) -> Result:
        if args[0] == "herdr-app":
            return self._app(list(args[1:]))
        if args[0] == "claude":
            return Result(0, "2.1.323", "")
        if args[0] in ("osascript", "lsof"):
            return Result(0, "", "")
        assert args[0] == "hc", args
        if self.down:
            return Result(1, "", "cannot connect")
        self.before(args[1])
        if args[1] not in ("ls", "get", "read", "server"):
            self.calls.append(args[1])
        answer = getattr(self, f"_{args[1]}")([a for a in args[2:] if a != "--json"])
        if isinstance(answer, Result):
            return answer
        return Result(0, json.dumps(answer), "")

    def _app(self, args: list[str]) -> Result:
        if self.window is None:
            return Result(1, "", "no window")
        if args[0] == "ls":
            tabs = [{"id": i, "name": n} for i, n in self.window.items()]
            return Result(0, json.dumps({"spaces": [{"tabs": tabs}]}), "")
        self.window[args[args.index("--id") + 1]] = args[args.index("--name") + 1]
        return Result(0, "{}", "")

    def _server(self, args: list[str]) -> Any:
        return {"version": "0.1.18"}

    def _ls(self, args: list[str]) -> Any:
        wanted = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
        found = [
            t
            for t in self.terminals.values()
            if ("--all" in args or t["state"] == "live")
            and all(t["labels"].get(w.split("=")[0]) == w.split("=")[1] for w in wanted)
        ]
        return {"terminals": [{"address": t["address"], "cwd": t["cwd"]} for t in found]}

    def _get(self, args: list[str]) -> Any:
        if args[0] not in self.terminals:
            error = {"error": {"kind": "not_found", "message": "no terminal"}}
            return Result(3, "", json.dumps(error))
        return self.terminals[args[0]]

    def _run(self, args: list[str]) -> Any:
        options = args[: args.index("--")] if "--" in args else args
        argv = args[args.index("--") + 1 :] if "--" in args else ["/bin/zsh", "-l"]
        labels = dict(options[i + 1].split("=", 1) for i, a in enumerate(options) if a == "--label")
        address = f"t{next(self._numbers)}"
        cwd = options[options.index("--cwd") + 1]
        terminal: dict[str, Any] = {
            "address": address,
            "state": "live",
            "argv": argv,
            "cwd": cwd,
            "labels": labels,
            "pid": next(self._pids),
            "turn": 0,
            "reason": None,
            "reported": False,
            "status": None,
            "foreground": {"name": "zsh", "argv": ["/bin/zsh", "-l"], "cwd": cwd, "pid": 1},
        }
        self.terminals[address] = terminal
        if RUN_LABEL in labels:
            self.starts.append(argv)
            self.launch(terminal)
        return {"address": address}

    def _label(self, args: list[str]) -> Any:
        key, value = args[1].split("=", 1)
        self.terminals[args[0]]["labels"][key] = value
        return {}

    def _read(self, args: list[str]) -> Any:
        return {"text": self.terminals[args[0]].get("screen", "")}

    def _send(self, args: list[str]) -> Any:
        terminal = self.terminals[args[0]]
        if (terminal["status"] or {}).get("state") == "blocked":
            return Result(9, "", json.dumps({"error": {"kind": "blocked", "message": "blocked"}}))
        terminal.setdefault("sent", []).append(args[1])
        if args[1].startswith("claude "):
            self.launch(terminal)
            if "continue" not in args[1]:
                self.set_state(terminal, "idle")
        elif terminal["foreground"]["name"] == "claude":
            self.set_state(terminal, "working")
        return {}

    def _interrupt(self, args: list[str]) -> Any:
        terminal = self.terminals[args[0]]
        if self.sticky_interrupt:
            return {"outcome": "still_working", "key": "esc"}
        if terminal["status"] and terminal["status"]["state"] in ("working", "blocked"):
            self.set_state(terminal, "idle")
        return {"outcome": "stopped", "key": "esc"}

    def _keys(self, args: list[str]) -> Any:
        terminal = self.terminals[args[0]]
        if args[1:].count("ctrl+c") >= 2 and terminal["foreground"]["name"] == "claude":
            self.exit_claude(terminal)
        return {}

    def _kill(self, args: list[str]) -> Any:
        terminal = self.terminals[args[0]]
        terminal |= {"state": "finished", "reason": "killed", "status": None, "foreground": None}
        return {}

    # --- what happens inside ------------------------------------------------------------

    def launch(self, terminal: dict[str, Any]) -> None:
        terminal["foreground"] = {
            "name": "claude",
            "argv": ["claude"],
            "cwd": terminal["cwd"],
            "pid": next(self._pids),
        }
        self.set_state(terminal, "working")

    def set_state(self, terminal: dict[str, Any], state: str, kind: str | None = None) -> None:
        before = (terminal["status"] or {}).get("state")
        if before == "working" and state != "working":
            terminal["turn"] += 1
        terminal["status"] = {"app": "claude-code", "state": state, "kind": kind}
        terminal["reported"] = True

    def exit_claude(self, terminal: dict[str, Any]) -> None:
        terminal["status"] = None
        terminal["foreground"] = {
            "name": "zsh",
            "argv": ["/bin/zsh", "-l", "-i"],
            "cwd": terminal["cwd"],
            "pid": terminal["pid"],
        }

    def reboot(self) -> None:
        for terminal in self.terminals.values():
            if terminal["state"] != "live":
                continue
            terminal |= {
                "argv": ["/bin/zsh", "-l"],
                "pid": next(self._pids),
                "status": None,
                "reported": False,
                "foreground": {
                    "name": "zsh",
                    "argv": ["/bin/zsh", "-l"],
                    "cwd": terminal["cwd"],
                    "pid": 1,
                },
            }


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, title: str, text: str) -> None:
        self.sent.append(text)


class Flag:
    def __init__(self) -> None:
        self.value = False

    def is_set(self) -> bool:
        return self.value

    def wait(self, timeout: float | None = None) -> bool:
        return self.value


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "protocol.file.allow=always",
         *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )  # fmt: skip
    return done.stdout.strip()


class LocalGit(Git):
    """Real git, whose origin is a local folder that counts as a GitHub repo."""

    def origin(self, path: Path) -> str | None:
        return f"git@github.com:acme/{path.name}.git"


class World:
    def __init__(self, tmp: Path, *, repos: Sequence[str] = ("web",), limit: int = 2) -> None:
        self.tmp = tmp
        self.clock = datetime(2026, 11, 22, 1, 0, tzinfo=UTC)
        self.tracker = FakeTracker(self)
        self.hc = FakeHerdr()
        self.notifier = FakeNotifier()
        self.paths = Paths(tmp / "nightshift")
        self.pr_commits: dict[str, set[str]] = {}
        for name in repos:
            origin = tmp / f"{name}.git"
            git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
            git(tmp, "clone", "-q", str(origin), name)
            git(tmp / name, "commit", "-q", "--allow-empty", "-m", "init")
            git(tmp / name, "push", "-q", "origin", "HEAD:main")
        self.config: ProjectConfig = parse_config(
            {
                "boards": [
                    {"url": BOARD, "max_parallel_agents": limit},
                    {"url": BOARD_2, "max_parallel_agents": limit},
                ],
                "repos": [{"path": str(tmp / name)} for name in repos],
            }
        )
        self.start()

    def start(self) -> None:
        """Start Nightshift, also after a crash: nothing but the files is kept."""
        self.stop = Flag()
        self.store = Store(self.paths.database)
        self.git = LocalGit(proc.run)
        self.observer = Observer(
            config=self.config, store=self.store, tracker=self.tracker, herdr=Herdr(self.hc),
            claude=self._claude(), git=self.git, now=lambda: self.clock,
            monotonic=lambda: self.clock.timestamp(),
        )  # fmt: skip
        self.executor = Executor(
            store=self.store, tracker=self.tracker, herdr=Herdr(self.hc), git=self.git,
            notifier=self.notifier, paths=self.paths, repos=self.observer.repo,
            now=lambda: self.clock, sleep=lambda seconds: None,
        )  # fmt: skip
        self.loop = Loop(
            store=self.store, observer=self.observer, executor=self.executor, stop=self.stop,
            now=lambda: self.clock,
        )  # fmt: skip

    def _claude(self) -> Any:
        from nightshift.claude import Claude

        return Claude(self.hc, self.tmp / "home")

    def restart(self) -> None:
        self.store.close()
        self.start()

    def tick(self, times: int = 1, seconds: float = 60) -> None:
        for _ in range(times):
            self.clock += timedelta(seconds=seconds)
            self.loop.tick()
            problems = self.store.get("report")["problems"]
            assert "unexpected" not in str(problems), problems

    # --- what people and agents do --------------------------------------------------------

    def issue(
        self, number: int = 42, title: str = "Export CSV", repo: str = "acme/web",
        board: str = BOARD, status: str = "Todo",
    ) -> Issue:  # fmt: skip
        issue = Issue(number, title, repo, {board: status})
        self.tracker.issues[issue.ref] = issue
        self.tracker.ranks[board].append(issue.ref)
        return issue

    def run(self, number: int = 42) -> Run:
        return [r for r in self.store.runs() if r.ticket_id == str(number)][-1]

    def state(self, number: int = 42) -> RunState:
        return self.run(number).state

    def terminal(self, number: int = 42) -> dict[str, Any]:
        run = self.run(number)
        found = [t for t in self.hc.terminals.values() if t["labels"].get(RUN_LABEL) == run.id]
        return found[-1]

    def agent(self, state: str, number: int = 42, kind: str | None = None) -> None:
        self.hc.set_state(self.terminal(number), state, kind)

    def open_pr(self, issue: Issue, *, draft: bool = False, ref: str = "PR_1") -> Pr:
        """The agent commits, pushes and opens its PR."""
        run = self.run(issue.number)
        assert run.branch and run.worktree_path
        worktree = Path(run.worktree_path)
        git(worktree, "commit", "-q", "--allow-empty", "-m", f"work {ref}")
        git(worktree, "push", "-q", "origin", run.branch)
        head = git(worktree, "rev-parse", "HEAD")
        pr = Pr(ref, f"https://pr/{ref}", PrState.OPEN, draft, head, self.clock)
        issue.prs.setdefault(run.branch, []).append(pr)
        self.pr_commits[ref] = {head}
        return pr

    def set_pr(self, issue: Issue, state: str, ref: str = "PR_1", **changes: Any) -> None:
        for prs in issue.prs.values():
            prs[:] = [
                replace(p, state=PrState(state), **changes) if p.ref == ref else p for p in prs
            ]

    def move(self, issue: Issue, status: str | None, board: str = BOARD) -> None:
        issue.status[board] = status

    def unlabel(self, issue: Issue) -> None:
        issue.labels.discard("nightshift")
        issue.label_removed_at = self.clock

    def events(self, issue: Issue) -> list[str]:
        return [c.split("event=")[-1].removesuffix(" -->") for c in issue.comments]
