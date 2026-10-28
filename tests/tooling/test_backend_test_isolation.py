"""Keep local backend verification independent from deployment configuration."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_backend_test_target_isolates_runtime_configuration() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("test-backend:", 1)[1].split("\n\n", 1)[0]

    assert "TICKFLOW_ENV_FILE=/dev/null" in target
    assert "MULTITENANCY_ENABLED=false" in target
    assert "SERVICE_ROLE=standalone" in target
    assert "DATA_DIR=" in target
    assert "mktemp -d" in target
