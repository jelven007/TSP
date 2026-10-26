"""Run the desktop entry point with a worker that survives application cleanup."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

DESKTOP = Path(__file__).resolve().parents[1] / "app" / "desktop.py"


@pytest.mark.parametrize("exit_code", [0, 1])
def test_frozen_desktop_exits_after_cleanup_with_live_worker(exit_code: int) -> None:
    program = """
import ast
import logging
import sys
import threading
from pathlib import Path

footer = ast.parse(Path(sys.argv[1]).read_text()).body[-1]
assert isinstance(footer, ast.If)
sys.frozen = True
worker_ready = threading.Event()
never_finished = threading.Event()
def worker():
    worker_ready.set()
    never_finished.wait()
threading.Thread(target=worker, daemon=False).start()
assert worker_ready.wait(2)
def application_main():
    print("APPLICATION_CLEANUP_DONE", flush=True)
    return int(sys.argv[2])
namespace = {
    "__name__": "__main__", "main": application_main,
    "sys": sys, "os": __import__("os"), "logging": logging,
}
exec(compile(ast.Module(body=[footer], type_ignores=[]), "<desktop-entry>", "exec"), namespace)
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", program, str(DESKTOP), str(exit_code)],
            capture_output=True, text=True, timeout=3,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"desktop remained alive after application cleanup: {error.stdout!r}")
    assert result.returncode == exit_code, result.stderr
    assert result.stdout.strip() == "APPLICATION_CLEANUP_DONE"


@pytest.mark.parametrize("exit_code", [0, 1])
def test_source_desktop_preserves_standard_exit_semantics(exit_code: int) -> None:
    program = """
import ast
import logging
import os
import sys
from pathlib import Path
footer = ast.parse(Path(sys.argv[1]).read_text()).body[-1]
def flush_marker():
    print("NORMAL_ATEXIT", flush=True)
__import__("atexit").register(flush_marker)
exec(compile(ast.Module(body=[footer], type_ignores=[]), "<desktop-entry>", "exec"), {
    "__name__": "__main__", "main": lambda: int(sys.argv[2]),
    "sys": sys, "os": os, "logging": logging,
})
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(DESKTOP), str(exit_code)],
        capture_output=True, text=True, timeout=3,
    )
    assert result.returncode == exit_code, result.stderr
    assert result.stdout.strip() == "NORMAL_ATEXIT"
