import sys

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

from lacuna.config import SETTINGS

# The package version, a placeholder that is never bumped. Releases are numbered by the
# Release workflow, from git tags (see "Releases" in README.md).
__version__ = "0.1.0"

logger.remove()
logger.add(sys.stderr, level=SETTINGS.log_level.upper(), colorize=True)


def main() -> None:
    from lacuna.cli.app import app

    app()
