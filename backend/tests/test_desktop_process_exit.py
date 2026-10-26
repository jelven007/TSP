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

footer = ast.parse(Path(sys.argv[1]).read_text(encoding="utf-8")).body[-1]
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
footer = ast.parse(Path(sys.argv[1]).read_text(encoding="utf-8")).body[-1]
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


@pytest.fixture
def blocking_detach_dll(tmp_path: Path) -> Path:
    """Model a loaded native library whose process-detach callback cannot finish."""
    source = tmp_path / "blocking_detach.rs"
    source.write_text(
        """
#[unsafe(no_mangle)]
pub extern "system" fn DllMain(
    _module: *mut core::ffi::c_void, reason: u32, _reserved: *mut core::ffi::c_void,
) -> i32 {
    if reason == 0 {
        loop { std::thread::sleep(std::time::Duration::from_secs(3600)); }
    }
    1
}
""",
        encoding="utf-8",
    )
    library = tmp_path / "blocking_detach.dll"
    subprocess.run(
        ["rustc", "--crate-type", "cdylib", str(source), "-o", str(library)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    return library


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows DLL detach")
@pytest.mark.parametrize("exit_code", [0, 1])
def test_frozen_windows_exits_with_blocked_native_detach(
    blocking_detach_dll: Path, exit_code: int, tmp_path: Path,
) -> None:
    cleanup_marker = tmp_path / "cleanup.txt"
    program = """
import ast
import ctypes
import logging
import os
import sys
from pathlib import Path
library = ctypes.WinDLL(sys.argv[3])
sys.frozen = True
footer = ast.parse(Path(sys.argv[1]).read_text(encoding="utf-8")).body[-1]
def application_main():
    Path(sys.argv[4]).write_text("APPLICATION_CLEANUP_DONE", encoding="utf-8")
    print("APPLICATION_CLEANUP_DONE", flush=True)
    return int(sys.argv[2])
exec(compile(ast.Module(body=[footer], type_ignores=[]), "<desktop-entry>", "exec"), {
    "__name__": "__main__", "main": application_main,
    "sys": sys, "os": os, "logging": logging,
})
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", program, str(DESKTOP), str(exit_code),
             str(blocking_detach_dll), str(cleanup_marker)],
            capture_output=True, text=True, timeout=3,
        )
    except subprocess.TimeoutExpired:
        assert cleanup_marker.read_text(encoding="utf-8") == "APPLICATION_CLEANUP_DONE"
        pytest.fail("native detach blocked desktop exit after APPLICATION_CLEANUP_DONE")
    assert result.returncode == exit_code, result.stderr
    assert result.stdout.strip() == "APPLICATION_CLEANUP_DONE"
    assert cleanup_marker.read_text(encoding="utf-8") == "APPLICATION_CLEANUP_DONE"
