"""Expose the standalone stdlib checks to pytest without requiring hermes."""
import importlib.util
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile

import pytest


ROOT = Path(__file__).resolve().parent


@pytest.fixture
def local_temp(monkeypatch):
    # Short absolute paths keep AF_UNIX tests usable in deeply nested worktrees.
    base = "/proc/self/cwd" if Path("/proc/self/cwd").exists() else str(ROOT)
    with tempfile.TemporaryDirectory(dir=base) as directory:
        monkeypatch.setattr(tempfile, "tempdir", directory)
        monkeypatch.setenv("OKO_TAIL_PROGRESS_SOCKET", directory + "/p.sock")
        yield


def test_progress_plugin(local_temp):
    runpy.run_path(str(ROOT / "contrib/hermes-plugin/test-plugin.py"))


def test_server_selftest(local_temp):
    spec = importlib.util.spec_from_file_location("oko_tail", ROOT / "oko-tail.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.selftest()


@pytest.mark.parametrize("script", ["test-oko-tail-md.py", "test-oko-tail-ui.py",
                                  "contrib/hermes-plugin/test-context-steward.py"])
def test_standalone(script, local_temp):
    result = subprocess.run([sys.executable, str(ROOT / script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    if "ui test skipped" in result.stdout:
        pytest.skip(result.stdout.strip())
