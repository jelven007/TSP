"""OCI downloads reuse verified bytes and keep index/manifest/config identities distinct."""
import hashlib
import io
import json

import pytest
from release import image_manifest, oci


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def encode(value):
    return json.dumps(value, sort_keys=True).encode()


@pytest.fixture
def registry():
    layer = b"same immutable compressed layer"
    config = encode({"architecture": "amd64", "os": "linux", "config": {"Labels": {
        "org.opencontainers.image.revision": "a" * 40,
        "org.opencontainers.image.version": "0.4.50", "io.tsp.role": "runtime",
        "io.tsp.source-tree": "c" * 64,
    }}})
    def descriptor(data):
        return {"digest": digest(data), "size": len(data)}

    manifest_data = encode({"schemaVersion": 2, "mediaType": oci.MANIFEST,
                            "config": descriptor(config), "layers": [descriptor(layer)]})
    index = encode({"schemaVersion": 2, "mediaType": oci.INDEX, "manifests": [
        {**descriptor(manifest_data), "platform": {"os": "linux", "architecture": "amd64"}}
    ]})

    class Registry:
        def __init__(self):
            self.objects = {digest(data): data for data in (layer, config, manifest_data, index)}
            self.calls = []

        def manifest(self, reference):
            self.calls.append((reference, "manifest"))
            return self.objects[reference]

        def blob(self, reference, offset=0):
            self.calls.append((reference, offset))
            data = self.objects[reference]
            return io.BytesIO(data[offset:]), 206 if offset else 200, (
                f"bytes {offset}-{len(data)-1}/{len(data)}" if offset else None
            )

    return Registry(), digest(index), digest(layer), digest(config)


def test_cold_cache_then_hit_and_layout_offline_verification(tmp_path, registry):
    remote, index, _, config = registry
    first = oci.fetch(remote, index, "linux/amd64", tmp_path / "cache", tmp_path / "layout")
    assert first["transferred_bytes"] > 0
    calls = len(remote.calls)
    def unavailable(*_):
        raise OSError("offline")
    remote.manifest = remote.blob = unavailable
    second = oci.fetch(remote, index, "linux/amd64", tmp_path / "cache", tmp_path / "layout2")
    assert len(remote.calls) == calls and second["transferred_bytes"] == 0
    verified = oci.verify_layout(tmp_path / "layout2", index, "linux/amd64")
    assert verified["revision"] == "a" * 40
    assert verified["config_digest"] == config and verified["index_digest"] == index
    assert verified["manifest_digest"] != config
    assert "docker_id" not in verified


def test_partial_download_resumes_and_corrupt_cache_is_rejected(tmp_path, registry):
    remote, index, layer, _ = registry
    cache = tmp_path / "cache"
    partial = cache / "blobs/sha256" / (layer.split(":")[1] + ".partial")
    partial.parent.mkdir(parents=True)
    partial.write_bytes(remote.objects[layer][:5])
    oci.fetch(remote, index, "linux/amd64", cache, tmp_path / "layout")
    assert (layer, 5) in remote.calls
    blob = partial.with_suffix("")
    blob.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match=r"checksum|size"):
        oci.fetch(remote, index, "linux/amd64", cache, tmp_path / "other")


def test_missing_platform_and_changed_input_fail_closed(tmp_path, registry):
    remote, index, _, _ = registry
    with pytest.raises(ValueError, match="platform"):
        oci.fetch(remote, index, "linux/arm64", tmp_path / "cache", tmp_path / "layout")
    remote.objects[index] += b" "
    with pytest.raises(ValueError, match="checksum"):
        oci.fetch(remote, index, "linux/amd64", tmp_path / "fresh-cache", tmp_path / "layout")


def matrix():
    return [{"version": "0.4.50", "revision": "a" * 40, "role": role, "platform": platform,
             "image": "ghcr.io/example/tsp" + ("-compute" if role == "compute" else ""),
             "source_tree": "c" * 64, "index_digest": "sha256:" + digit * 64,
             "manifest_digest": "sha256:" + "d" * 64, "config_digest": "sha256:" + "e" * 64,
             "layers": [{"digest": "sha256:" + "f" * 64, "size": 123}]}
            for role, digit in (("runtime", "1"), ("compute", "2"))
            for platform in ("linux/amd64", "linux/arm64")]


@pytest.mark.parametrize("fault", ["source-missing", "source-mismatch", "index", "repository"])
def test_image_matrix_rejects_incoherent_source_or_role_identity(fault):
    rows = matrix()
    assert image_manifest.validate("0.4.50", "a" * 40, rows)["status"] == "passed"
    if fault == "source-missing":
        rows[0].pop("source_tree")
    elif fault == "source-mismatch":
        rows[0]["source_tree"] = "f" * 64
    elif fault == "index":
        rows[0]["index_digest"] = "sha256:" + "f" * 64
    else:
        rows[0]["image"] += "-other"
    with pytest.raises(ValueError):
        image_manifest.validate("0.4.50", "a" * 40, rows)


def test_offline_layout_rejects_symlinked_blob_parent(tmp_path, registry):
    remote, index, _, _ = registry
    layout = tmp_path / "layout"
    oci.fetch(remote, index, "linux/amd64", tmp_path / "cache", layout)
    actual = tmp_path / "actual-blobs"
    (layout / "blobs/sha256").rename(actual)
    (layout / "blobs/sha256").symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        oci.verify_layout(layout, index, "linux/amd64")


def test_image_matrix_rejects_invalid_layer_byte_identity():
    rows = matrix()
    rows[0]["layers"][0]["size"] = -1
    with pytest.raises(ValueError, match="size"):
        image_manifest.validate("0.4.50", "a" * 40, rows)


def test_manifest_enforces_revision_version_role_and_complete_matrix(tmp_path, registry):
    remote, index, _, _ = registry
    oci.fetch(remote, index, "linux/amd64", tmp_path / "cache", tmp_path / "layout")
    record = oci.verify_layout(tmp_path / "layout", index, "linux/amd64")
    with pytest.raises(ValueError, match=r"four|matrix"):
        image_manifest.validate("0.4.50", "a" * 40, [record])
    with pytest.raises(ValueError, match=r"revision|matrix"):
        image_manifest.validate("0.4.50", "b" * 40, [record])


def test_redirect_never_forwards_registry_credentials_to_blob_host():
    import urllib.request
    handler = oci.SafeRedirect()
    request = urllib.request.Request("https://ghcr.io/v2/a/blobs/x",
                                     headers={"Authorization": "Bearer private"})
    redirected = handler.redirect_request(request, None, 302, "", {},
                                           "https://blob.example.invalid/layer?signature=test")
    assert not redirected.has_header("Authorization")
    with pytest.raises(ValueError, match="HTTPS"):
        handler.redirect_request(request, None, 302, "", {}, "http://blob.example.invalid/layer")
