"""Run the real `nightshift` CLI with its files in a folder of your choice.

For tests on the sandbox (see "Testing on the sandbox" in CLAUDE.md), so that they never touch
`~/.nightshift`. It is the same as setting `NIGHTSHIFT_HOME`:

    uv run python scripts/sandbox.py temp/sandbox init
    uv run python scripts/sandbox.py temp/sandbox start
"""

import os
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    os.environ["NIGHTSHIFT_HOME"] = str(Path(sys.argv.pop(1)).resolve())

    from nightshift import main as nightshift  # after the variable: settings are read on import

    nightshift()


if __name__ == "__main__":
    main()
