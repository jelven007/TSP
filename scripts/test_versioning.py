from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from versioning import next_version, parse_version, validate_progression


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.4.0", (0, 4, 0)),
        ("9.9.99", (9, 9, 99)),
        ("v0.4.0", (0, 4, 0)),
    ],
)
def test_parse_version(value: str, expected: tuple[int, int, int]) -> None:
    assert parse_version(value) == expected


@pytest.mark.parametrize("value", ["1.10.0", "0.4.100", "0.04.0", "0.4.00", "0.4"])
def test_parse_version_rejects_out_of_contract_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_version(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.4.0", "0.4.1"),
        ("0.4.98", "0.4.99"),
        ("0.4.99", "0.5.0"),
        ("0.9.99", "1.0.0"),
    ],
)
def test_next_version(value: str, expected: str) -> None:
    assert next_version(value) == expected


def test_next_version_rejects_maximum() -> None:
    with pytest.raises(ValueError):
        next_version("9.9.99")


def test_progression_requires_exact_successor() -> None:
    validate_progression("0.4.0", "0.4.1")
    with pytest.raises(ValueError):
        validate_progression("0.4.0", "0.4.2")


@pytest.mark.parametrize(
    ("current", "expected_success"),
    [("0.4.0", False), ("0.4.1", True), ("0.4.2", False), ("0.3.99", False)],
)
def test_mainline_check_requires_increment(tmp_path, current, expected_success):
    """Exercise the same CLI used by CI, including the unchanged-version bug."""
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "VERSION").write_text(f"v{current}\n", encoding="utf-8")
    (tmp_path / "frontend" / "package.json").write_text(
        f'{{"version": "{current}"}}\n', encoding="utf-8"
    )
    (tmp_path / "backend" / "pyproject.toml").write_text(
        f'version = "{current}"\n', encoding="utf-8"
    )
    (tmp_path / "backend" / "uv.lock").write_text(
        f'[[package]]\nname = "tsp-backend"\nversion = "{current}"\n',
        encoding="utf-8",
    )
    (tmp_path / "backend" / "app" / "__init__.py").write_text(
        f'_FALLBACK_VERSION = "{current}"\n', encoding="utf-8"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("check_version.py")),
            "--current",
            str(tmp_path / "VERSION"),
            "--previous",
            "v0.4.0",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is expected_success, result.stdout + result.stderr
    if not expected_success:
        assert "version progression must be 0.4.1" in result.stderr
