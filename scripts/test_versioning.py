from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from versioning import next_version, parse_version, validate_progression

ROOT = Path(__file__).resolve().parents[1]


def copy_version_docs(target, version):
    from check_version import DOCUMENT_VERSION_PATTERNS

    current = (ROOT / "VERSION").read_text().strip().removeprefix("v")
    for name in DOCUMENT_VERSION_PATTERNS:
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((ROOT / name).read_text().replace(current, version))


def test_repository_document_versions_match_application():
    from check_version import _check_document_versions

    root = Path(__file__).resolve().parents[1]
    expected = (root / "VERSION").read_text().strip().removeprefix("v")
    _check_document_versions(root, expected)


@pytest.mark.parametrize("change", ["stale", "missing", "duplicate"])
def test_document_version_checker_rejects_drift(tmp_path, change):
    from check_version import _check_document_versions

    copy_version_docs(tmp_path, "1.2.3")
    path = tmp_path / "README.md"
    if change == "missing":
        path.unlink()
    elif change == "stale":
        path.write_text(path.read_text().replace("1.2.3", "1.2.2"))
    else:
        path.write_text(path.read_text() + "\n应用语义版本为 `1.2.3`\n")
    with pytest.raises(SystemExit, match="README.md"):
        _check_document_versions(tmp_path, "1.2.3")


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
def test_explicit_progression_check_requires_increment(tmp_path, current, expected_success):
    """The optional progression audit still requires an exact successor."""
    copy_version_docs(tmp_path, current)
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
