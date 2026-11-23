import pytest

from nightshift.naming import branch_name, folder_name, free_branch, slug


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Export CSV", "export-csv"),
        ("  Fix: login / redirect!! ", "fix-login-redirect"),
        ("Don't crash", "dont-crash"),
        ("Über größe", "uber-groe"),
        ("日本語", "ticket"),
        ("---", "ticket"),
    ],
)
def test_slug(title: str, expected: str) -> None:
    assert slug(title) == expected


def test_length_limit_includes_the_index() -> None:
    title = "A very long title that does not fit into forty characters"
    for index in (1, 2, 10, 100):
        name = branch_name("12345", title, index)
        assert len(name) <= 40
        assert name.startswith("12345/a-very-long")
        assert "--" not in name
        assert not name.endswith("-")
    assert len(branch_name("42", "x" * 99, 1)) == 40


def test_free_branch() -> None:
    taken = {"42/export-csv", "42/export-csv-2"}
    assert free_branch("42", "Export CSV", taken.__contains__) == "42/export-csv-3"
    assert folder_name("42/export-csv-3") == "42-export-csv-3"
