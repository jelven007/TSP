"""The configured authority transfer tool must be present in sealed runtime images."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_installs_and_seals_controlled_input_packager():
    text = (ROOT / "Dockerfile").read_text()
    name = "scripts/release/package_transfer.py"
    assert f"COPY {name} /app/{name}" in text
    assert f"--include {name}" in text
