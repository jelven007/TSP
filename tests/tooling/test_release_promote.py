"""Retries must not replace an existing immutable image tag."""
import hashlib
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest
from release import oci, promote


def test_registry_manifest_unknown_allows_first_publish_when_buildx_reports_not_found(monkeypatch):
    tag = "ghcr.io/example/tsp:0.4.51"
    monkeypatch.setattr(promote.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="", stderr=f"ERROR: {tag}: not found"))

    def missing(self, path, headers=None):
        assert path == "/manifests/0.4.51"
        raise HTTPError(self.base + path, 404, "Not Found", {},
                        io.BytesIO(json.dumps({"errors": [{"code": "MANIFEST_UNKNOWN",
                                                          "message": "manifest unknown"}]}).encode()))

    monkeypatch.setattr(oci.Registry, "request", missing)
    assert promote.inspect(tag) is None


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


@pytest.mark.parametrize(("status", "code"), [
    (401, "UNAUTHORIZED"), (403, "DENIED"), (404, "NAME_UNKNOWN"),
    (404, "UNKNOWN"), (500, "MANIFEST_UNKNOWN"),
])
def test_lookup_errors_are_not_missing_tags(monkeypatch, status, code):
    def failure(self, path, headers=None):
        raise HTTPError(self.base + path, status, "error", {},
                        io.BytesIO(json.dumps({"errors": [{"code": code}]}).encode()))

    monkeypatch.setattr(oci.Registry, "request", failure)
    with pytest.raises(ValueError, match="lookup"):
        promote.inspect("ghcr.io/example/tsp:0.4.50")


def test_network_failure_is_not_missing_tag(monkeypatch):
    def failure(*args, **kwargs):
        raise URLError("network timeout")

    monkeypatch.setattr(oci.Registry, "request", failure)
    with pytest.raises(ValueError, match="lookup"):
        promote.inspect("ghcr.io/example/tsp:0.4.50")


@pytest.mark.parametrize("declared_matches", [True, False])
def test_existing_manifest_digest_is_verified_from_actual_bytes(monkeypatch, declared_matches):
    data = b'{"schemaVersion":2,"manifests":[]}'
    digest = "sha256:" + hashlib.sha256(data).hexdigest()

    def response(*args, **kwargs):
        stream = io.BytesIO(data)
        stream.headers = {"Docker-Content-Digest": digest if declared_matches else "sha256:" + "0" * 64}
        return stream

    monkeypatch.setattr(oci.Registry, "request", response)
    if declared_matches:
        assert promote.inspect("ghcr.io/example/tsp:0.4.50") == digest
    else:
        with pytest.raises(ValueError, match="digest mismatch"):
            promote.inspect("ghcr.io/example/tsp:0.4.50")


@pytest.mark.parametrize("data", [b'{}', b'{"errors":[]}', b'not json',
                                b'{"errors":[{"code":"MANIFEST_UNKNOWN"},{"code":"DENIED"}]}'])
def test_ambiguous_404_body_blocks_first_publish(monkeypatch, data):
    def failure(self, path, headers=None):
        raise HTTPError(self.base + path, 404, "Not Found", {}, io.BytesIO(data))

    monkeypatch.setattr(oci.Registry, "request", failure)
    with pytest.raises(ValueError, match="lookup"):
        promote.inspect("ghcr.io/example/tsp:0.4.50")
