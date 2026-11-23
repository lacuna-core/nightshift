from nightshift.claude import _allows, prompt, resume_line, start_args
from nightshift.model import Brief


def test_prompt() -> None:
    brief = Brief(
        "acme/web", "42", "Export CSV", "Body\0text", "https://u", ("nightshift",),
        (("me", "also JSON"),), "Closes #42",
    )  # fmt: skip
    text = prompt(brief, "42/export-csv")
    for part in (
        "acme/web", "42 (https://u)", "Labels: nightshift", "`42/export-csv`", "Closes #42",
        "Not a draft", "Never merge", "never close the issue", "Export CSV", "Bodytext",
        "me wrote:\nalso JSON", "CLAUDE.md", "Conventional Commits",
    ):  # fmt: skip
        assert part in text


def test_long_text_is_cut() -> None:
    brief = Brief("a/b", "1", "T", "x" * 99_999, "u", (), (("me", "y" * 99_999),) * 99, "Closes #1")
    assert len(prompt(brief, "1/t")) < 500_000


def test_arguments() -> None:
    assert start_args("s", "p") == ["--permission-mode", "acceptEdits", "--session-id", "s", "p"]
    assert resume_line("s", None) == "claude --permission-mode acceptEdits --resume s"
    assert resume_line("s", "a b").endswith(" --resume s 'a b'")


def test_allow_rules() -> None:
    for rule in ("Bash", "Bash(git:*)", "Bash(git push:*)", "Bash(git push *)", "Bash(git *)"):
        assert _allows(rule, "git push")
    for rule in ("Edit", "Bash(git pull:*)", "Bash(git push origin)", "Bash(gitx:*)"):
        assert not _allows(rule, "git push")
