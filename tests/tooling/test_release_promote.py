"""Retries must not replace an existing immutable image tag."""
import pytest
from release import promote


def test_existing_tag_same_digest_is_noop_and_conflicting_tag_blocks_all():
    writes = []
    targets = [("example/runtime:0.4.50", "sha256:" + "a" * 64)]
    promote.apply(targets, inspect=lambda _: targets[0][1], publish=lambda *args: writes.append(args))
    assert writes == []


def test_preflight_all_tags_before_writing_and_idempotent_retry():
    targets = [("example/runtime:0.4.50", "sha256:" + "a" * 64),
               ("example/compute:0.4.50", "sha256:" + "b" * 64)]
    state, writes = {targets[1][0]: "sha256:" + "c" * 64}, []
    with pytest.raises(ValueError, match="immutable"):
        promote.apply(targets, inspect=state.get, publish=lambda *args: writes.append(args))
    assert writes == []
    state.clear()

    def publish(tag, digest):
        state[tag] = digest
        writes.append((tag, digest))

    promote.apply(targets, inspect=state.get, publish=publish)
    promote.apply(targets, inspect=state.get, publish=publish)
    assert writes == targets


@pytest.mark.parametrize("message", ["docker command not found", "repository not found",
                                    "authentication required", "network timeout"])
def test_lookup_errors_are_not_missing_tags(monkeypatch, message):
    from types import SimpleNamespace
    monkeypatch.setattr(promote.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="", stderr=message))
    with pytest.raises(ValueError, match="lookup"):
        promote.inspect("ghcr.io/example/tsp:0.4.50")
