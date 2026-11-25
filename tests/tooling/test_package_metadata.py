"""A valid source checksum must not admit platform-specific tar pollution."""

import io
import tarfile

import pytest
from release import source_recovery


def archive(tmp_path, name="app.txt", pax=None):
    output = tmp_path / "payload.tar.gz"
    with tarfile.open(output, "w:gz") as target:
        for path, data in [("VERSION", b"0.4.73\n"), (name, b"application bytes")]:
            member = tarfile.TarInfo("TSP-v0.4.73/" + path)
            member.size = len(data)
            member.pax_headers = pax or {}
            target.addfile(member, io.BytesIO(data))
    return output


@pytest.mark.parametrize("name", [
    "._app.txt", "nested/._app.txt", ".DS_Store", "nested/.DS_Store",
    "__MACOSX/app.txt", "nested/__MACOSX/app.txt",
])
def test_source_recovery_rejects_macos_sidecars(tmp_path, name):
    with pytest.raises(ValueError, match="metadata"):
        source_recovery.archive_members(archive(tmp_path, name), "0.4.73")


@pytest.mark.parametrize("key", [
    "LIBARCHIVE.xattr.com.apple.provenance", "SCHILY.xattr.com.apple.ResourceFork",
    "SCHILY.fflags", "SCHILY.acl.access",
])
def test_source_recovery_rejects_extended_headers_without_sidecar_files(tmp_path, key):
    with pytest.raises(ValueError, match="metadata"):
        source_recovery.archive_members(archive(tmp_path, pax={key: "opaque"}), "0.4.73")
