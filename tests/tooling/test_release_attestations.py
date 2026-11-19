"""Require digest-bound SPDX and provenance for every release architecture."""
import hashlib
import importlib
import importlib.util
import io
import json

import pytest


def fixture(missing=None, wrong_subject=False, corrupt=False):
    manifests, blobs = {}, {}

    def encoded(value, destination):
        raw = json.dumps(value).encode()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        destination[digest] = raw
        return {"digest": digest, "size": len(raw)}

    descriptors = []
    for arch in ("amd64", "arm64"):
        subject = hashlib.sha256(arch.encode()).hexdigest()
        descriptors.append({"digest": "sha256:" + subject, "size": 1,
                            "platform": {"os": "linux", "architecture": arch}})
        layers = []
        for predicate in ("https://spdx.dev/Document", "https://slsa.dev/provenance/v0.2"):
            if missing and missing in predicate:
                continue
            layers.append(encoded({
                "_type": "https://in-toto.io/Statement/v0.1",
                "subject": [{"name": "image", "digest": {
                    "sha256": "0" * 64 if wrong_subject else subject,
                }}],
                "predicateType": predicate,
                "predicate": {"spdxVersion": "SPDX-2.3", "packages": [{"name": "python"}]}
                if "spdx" in predicate else {"buildType": "https://mobyproject.org/buildkit@v1",
                                           "materials": [{"uri": "pkg:docker/python"}]},
            }, blobs))
        descriptor = encoded({"schemaVersion": 2, "layers": layers}, manifests)
        descriptor["annotations"] = {
            "vnd.docker.reference.type": "attestation-manifest",
            "vnd.docker.reference.digest": "sha256:" + subject,
        }
        descriptors.append(descriptor)
    index = encoded({"manifests": descriptors}, manifests)
    if corrupt:
        blobs[next(iter(blobs))] = b"corrupted"

    class Registry:
        def manifest(self, digest):
            return manifests[digest]

        def blob(self, digest):
            return io.BytesIO(blobs[digest]), 200, None

    return Registry(), index["digest"]


def collect(registry, digest):
    assert importlib.util.find_spec("release.attestations") is not None
    return importlib.import_module("release.attestations").collect(registry, digest)


def test_records_verified_attestation_digests_for_both_architectures():
    result = collect(*fixture())
    assert {row["platform"] for row in result} == {"linux/amd64", "linux/arm64"}
    assert all(set(row["statements"]) == {"sbom", "provenance"} for row in result)
    assert all(row["signature_verified"] is False for row in result)


@pytest.mark.parametrize("problem", ["spdx", "slsa", "wrong_subject", "corrupt"])
def test_missing_mismatched_and_damaged_attestations_block_release(problem):
    args = {"missing": problem} if problem in {"spdx", "slsa"} else {problem: True}
    with pytest.raises(ValueError):
        collect(*fixture(**args))
