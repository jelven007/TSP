"""Verify registry SBOM/provenance content and subject binding, not signatures."""
from __future__ import annotations

import hashlib
import json

from release.oci import MAX_JSON, hex_digest, select

TYPES = {
    "https://spdx.dev/Document": "sbom",
    "https://slsa.dev/provenance/v0.2": "provenance",
    "https://slsa.dev/provenance/v1": "provenance",
}


def collect(registry, index_digest):
    def decode(raw, digest, size=None):
        if (len(raw) > MAX_JSON or hashlib.sha256(raw).hexdigest() != hex_digest(digest)
                or (size is not None and len(raw) != size)):
            raise ValueError("attestation checksum or size mismatch")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("attestation must be a JSON object")
        return value

    index = decode(registry.manifest(index_digest), index_digest)
    result = []
    for platform in ("linux/amd64", "linux/arm64"):
        subject = select(index, platform)["digest"]
        descriptors = [
            item for item in index["manifests"]
            if item.get("annotations", {}).get("vnd.docker.reference.type") == "attestation-manifest"
            and item["annotations"].get("vnd.docker.reference.digest") == subject
        ]
        if len(descriptors) != 1:
            raise ValueError("missing or ambiguous platform attestation")
        descriptor = descriptors[0]
        manifest = decode(registry.manifest(descriptor["digest"]),
                          descriptor["digest"], descriptor["size"])
        statements = {}
        for layer in manifest["layers"]:
            stream, _, _ = registry.blob(layer["digest"])
            with stream:
                statement = decode(stream.read(MAX_JSON + 1), layer["digest"], layer["size"])
            kind = TYPES.get(statement.get("predicateType"))
            if kind is None:
                continue
            if (kind in statements
                    or statement.get("_type") not in {
                        "https://in-toto.io/Statement/v0.1", "https://in-toto.io/Statement/v1",
                    }
                    or not any(item.get("digest", {}).get("sha256") == hex_digest(subject)
                               for item in statement.get("subject", []))):
                raise ValueError("duplicate or mismatched attestation subject")
            predicate = statement["predicate"]
            if kind == "sbom" and (
                not str(predicate.get("spdxVersion", "")).startswith("SPDX-")
                or not isinstance(predicate.get("packages"), list) or not predicate["packages"]
            ):
                raise ValueError("missing SPDX package inventory")
            if kind == "provenance" and not (
                predicate.get("buildType") or predicate.get("buildDefinition", {}).get("buildType")
            ):
                raise ValueError("missing provenance build definition")
            statements[kind] = {
                "digest": layer["digest"], "bytes": layer["size"],
                "predicate_type": statement["predicateType"],
            }
        if set(statements) != {"sbom", "provenance"}:
            raise ValueError("SBOM and provenance are both required")
        result.append({
            "platform": platform, "subject_digest": subject,
            "attestation_digest": descriptor["digest"], "statements": statements,
            "signature_verified": False,
        })
    return result
