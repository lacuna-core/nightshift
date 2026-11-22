"""Stand-ins for the outside world."""

from collections.abc import Callable, Sequence
from pathlib import Path

from nightshift.proc import Result


class FakeRunner:
    """Answers command lines by their first words; the longest match wins."""

    def __init__(self, answers: dict[str, Result | Callable[[list[str]], Result]]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(
        self, args: Sequence[str], *, cwd: Path | None = None, timeout: float = 60
    ) -> Result:
        self.calls.append(list(args))
        line = " ".join(args)
        for key in sorted(self.answers, key=len, reverse=True):
            if line.startswith(key):
                answer = self.answers[key]
                return answer(list(args)) if callable(answer) else answer
        return Result(127, "", f"unexpected: {line}")


def ok(out: str = "") -> Result:
    return Result(0, out, "")
