"""Repository application version format and progression rules."""

from __future__ import annotations

import re

_VERSION_RE = re.compile(r"^(?P<a>[0-9])\.(?P<b>[0-9])\.(?P<cd>0|[1-9][0-9]?)$")


def parse_version(value: str) -> tuple[int, int, int]:
    """Parse the canonical ``a.b.cd`` application version."""
    normalized = value.strip().removeprefix("v")
    match = _VERSION_RE.fullmatch(normalized)
    if match is None:
        raise ValueError(
            f"invalid application version {value!r}; expected a.b.cd with "
            "a=0..9, b=0..9 and cd=0..99"
        )
    return tuple(int(match.group(name)) for name in ("a", "b", "cd"))


def format_version(parts: tuple[int, int, int]) -> str:
    """Format a parsed version without padding the final component."""
    a, b, cd = parts
    return f"{a}.{b}.{cd}"


def next_version(value: str) -> str:
    """Return the exact next version in the mainline sequence."""
    a, b, cd = parse_version(value)
    if cd < 99:
        return format_version((a, b, cd + 1))
    if b < 9:
        return format_version((a, b + 1, 0))
    if a < 9:
        return format_version((a + 1, 0, 0))
    raise ValueError(f"application version {value!r} has reached 9.9.99")


def validate_progression(previous: str, current: str) -> None:
    """Require exactly one mainline increment, including for ordinary updates."""
    previous_parts = parse_version(previous)
    current_version = format_version(parse_version(current))
    expected = next_version(format_version(previous_parts))
    if current_version != expected:
        raise ValueError(
            f"version progression must be {expected}, got {current_version}"
        )
