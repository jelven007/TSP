"""Frozen observer tools must retain the package structure used in containers."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "acceptance_launcher", ROOT / "scripts/acceptance/launch_continuous_acceptance.py",
)
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


def test_frozen_observers_import_from_bundle_and_can_be_reused(tmp_path):
    bundle = launcher.freeze_tools(tmp_path)
    assert launcher.freeze_tools(tmp_path) == bundle
    names = (
        "scripts.acceptance.run_active_acceptance",
        "scripts.acceptance.observe_candidate_soak",
        "scripts.acceptance.observe_second_acceptance",
    )
    result = subprocess.run(
        [
            sys.executable, "-c",
            (
                "import importlib,json,sys;"
                "print(json.dumps([importlib.import_module(n).__file__ for n in sys.argv[1:]]))"
            ),
            *names,
        ],
        cwd=bundle.parent,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "backend"),
            "TICKFLOW_ENV_FILE": os.devnull,
            "DATA_DIR": str(tmp_path / "data"),
        },
        capture_output=True, text=True, check=True, timeout=30,
    )
    for path in json.loads(result.stdout.splitlines()[-1]):
        assert Path(path).resolve().is_relative_to(bundle)


def test_modified_frozen_observer_is_rejected(tmp_path):
    bundle = launcher.freeze_tools(tmp_path)
    (bundle / "acceptance/observe_candidate_soak.py").write_text("modified")
    with pytest.raises(ValueError, match="observer source bundle changed"):
        launcher.freeze_tools(tmp_path)
