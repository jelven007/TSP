from __future__ import annotations

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
