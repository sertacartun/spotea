"""Regression coverage for conftest.py's own setup, not the app itself."""

import subprocess
import sys
from pathlib import Path


def test_test_tmpdir_is_cleaned_up_when_the_test_process_exits():
    """Checked in a subprocess: this process imported conftest and won't exit until the suite ends."""
    script = "import sys; sys.path.insert(0, 'tests'); import conftest; print(conftest._TEST_DIR)"
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=".")

    assert result.returncode == 0, result.stderr
    tmp_dir = Path(result.stdout.strip())
    assert not tmp_dir.exists(), f"{tmp_dir} was not cleaned up after the process that created it exited"
