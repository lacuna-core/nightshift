import os
import subprocess
import sys
from pathlib import Path

import pytest

from nightshift.errors import NightshiftError
from nightshift.lock import Lock, holder


def test_second_instance_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "state" / "nightshift.lock"
    first = Lock(path)
    first.acquire()
    assert holder(path) == os.getpid()
    with pytest.raises(NightshiftError, match=f"process {os.getpid()}"):
        Lock(path).acquire()
    first.release()
    assert holder(path) is None
    Lock(path).acquire()


def test_lock_of_a_crashed_loop_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "nightshift.lock"
    code = (
        "import os, sys, pathlib; from nightshift.lock import Lock; "
        "Lock(pathlib.Path(sys.argv[1])).acquire(); os._exit(9)"
    )
    subprocess.run([sys.executable, "-c", code, str(path)], check=False)
    assert path.read_text().isdigit()
    assert holder(path) is None
    Lock(path).acquire()


def test_no_lock_file(tmp_path: Path) -> None:
    assert holder(tmp_path / "none") is None
