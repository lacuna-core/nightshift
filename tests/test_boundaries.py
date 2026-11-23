import json
from pathlib import Path

import pytest

from nightshift import proc
from nightshift.errors import HerdrError, ToolError, TrackerAuthError
from nightshift.herdr import Herdr
from nightshift.proc import Result
from nightshift.wiring import gh_token
from tests.fakes import FakeRunner, ok


def test_herdr_error_is_read() -> None:
    error = json.dumps({"error": {"kind": "not_found", "message": "no terminal", "hint": "x"}})
    with pytest.raises(HerdrError, match="no terminal") as caught:
        Herdr(FakeRunner({"hc": Result(3, "", error)})).version()
    assert caught.value.kind == "not_found"


def test_herdr_missing() -> None:
    def missing(args: list[str]) -> Result:
        raise ToolError("`hc` was not found on PATH")

    with pytest.raises(HerdrError, match="not found"):
        Herdr(FakeRunner({"hc": missing})).version()


def test_gh_token() -> None:
    assert gh_token(FakeRunner({"gh auth token": ok("abc\n")})) == "abc"
    with pytest.raises(TrackerAuthError):
        gh_token(FakeRunner({"gh auth token": Result(1, "", "no")}))

    def missing(args: list[str]) -> Result:
        raise ToolError("`gh` was not found on PATH")

    with pytest.raises(ToolError):
        gh_token(FakeRunner({"gh": missing}))


def test_tools_do_not_learn_who_called_them(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HC_TERMINAL_ID", "HC_TERMINAL_SOCKET", "HC_SCOPE", "CLAUDECODE"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("HC_SESSION", "kept")
    out = proc.run(["env"]).out
    assert "HC_SESSION=kept" in out
    for name in ("HC_TERMINAL", "HC_SCOPE", "CLAUDECODE"):
        assert name not in out


def test_run() -> None:
    assert proc.run(["sh", "-c", "echo hi; exit 3"]).code == 3
    with pytest.raises(ToolError, match="not found"):
        proc.run(["no-such-tool-xyz"])
    with pytest.raises(ToolError, match="longer"):
        proc.run(["sleep", "5"], timeout=0.1)
    with pytest.raises(ToolError, match="does not exist"):
        proc.run(["ls"], cwd=Path("/no/such/folder"))
