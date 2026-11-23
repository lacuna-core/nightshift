"""Run the real `nightshift` CLI with its files in a folder of your choice.

For tests on the sandbox (see "Testing on the sandbox" in CLAUDE.md), so that they never touch
`~/.nightshift`:

    uv run python scripts/sandbox.py temp/sandbox init
    uv run python scripts/sandbox.py temp/sandbox start
"""

import sys
from pathlib import Path

from nightshift.project_config import Paths


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    root = Path(sys.argv.pop(1)).resolve()
    Paths.default = classmethod(lambda cls: cls(root))  # type: ignore[method-assign, assignment]

    from nightshift import main as nightshift

    nightshift()


if __name__ == "__main__":
    main()
