"""Exercise real transfer bytes, contamination, and failed admission boundaries."""

import gzip
import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import build_manifest
import pytest
from release import package_transfer as package

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def payload(tmp_path):
    root = tmp_path / "payload"
    (root / "nested").mkdir(parents=True)
    (root / "run.sh").write_bytes(b"#!/bin/sh\nprintf application\n")
    (root / "run.sh").chmod(0o751)
    (root / "nested" / ("中文" * 30 + ".txt")).write_bytes(bytes(range(256)))
    (root / "nested/link").symlink_to("../run.sh")
    (root / "empty").mkdir()
    return root


def tar_fixture(tmp_path, members, *, pax=None):
    output = tmp_path / "input.tar.gz"
    with tarfile.open(output, "w:gz", pax_headers=pax) as target:
        root = tarfile.TarInfo("payload")
        root.type = tarfile.DIRTYPE
        target.addfile(root)
        for name, kind, data in members:
            member = tarfile.TarInfo(name)
            if kind == "file":
                member.size = len(data)
                target.addfile(member, io.BytesIO(data))
            else:
                member.type = kind
                member.linkname = data
                target.addfile(member)
    return output


def test_roundtrip_is_deterministic_preserves_bytes_execution_and_safe_links(payload, tmp_path):
    first, second = tmp_path / "one.tar.gz", tmp_path / "two.tar.gz"
    before = package.scan(payload)[0]
    receipt = package.create(payload, first)
    os.utime(payload / "run.sh", (10, 10))
    assert package.create(payload, second)["sha256"] == receipt["sha256"]
    output = tmp_path / "extracted"
    package.extract(first, output, receipt["sha256"])
    assert package.scan(output / "payload")[0] == before
    assert (output / "payload/nested/link").read_bytes() == (payload / "run.sh").read_bytes()
    assert (output / "payload/run.sh").stat().st_mode & 0o111 == 0o111
    package.verify_tree(output, first, receipt["sha256"])
    with tarfile.open(first) as archive:
        assert all(member.uid == member.gid == member.mtime == 0
                   and not member.uname and not member.gname for member in archive)


def test_create_filters_existing_sidecars_without_altering_source(payload, tmp_path):
    for name in ["._run.sh", "nested/._resource", ".DS_Store", "nested/.DS_Store",
                 "__MACOSX/secret", "nested/__MACOSX/secret"]:
        target = payload / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"Apple metadata")
    original = {p: p.read_bytes() for p in payload.rglob("*") if p.is_file() and not p.is_symlink()}
    output = tmp_path / "clean.tar.gz"
    result = package.create(payload, output)
    assert len(result["excluded"]) == 6
    assert not any(package.metadata_path(name) for name in package.verify(output)["entries"])
    assert all(path.read_bytes() == data for path, data in original.items())
    with pytest.raises(ValueError, match="metadata"):
        package.verify_tree(payload)


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS extended attributes")
def test_real_macos_xattr_and_flag_only_tar_still_has_sidecars(payload, tmp_path):
    target = payload / "run.sh"
    subprocess.run(["/usr/bin/xattr", "-w", "com.apple.tsp-package-test", "opaque", str(target)],
                   check=True, capture_output=True)
    (payload / "._already-present").write_bytes(b"AppleDouble")
    native = tmp_path / "native.tar.gz"
    flags_only = tmp_path / "flags.tar.gz"
    subprocess.run(["/usr/bin/tar", "-czf", str(native), "payload"], cwd=tmp_path,
                   check=True, capture_output=True)
    subprocess.run(["/usr/bin/tar", "--no-xattrs", "-czf", str(flags_only), "payload"],
                   cwd=tmp_path, env={**os.environ, "COPYFILE_DISABLE": "1"},
                   check=True, capture_output=True)
    for archive in [native, flags_only]:
        with pytest.raises(ValueError, match="metadata"):
            package.verify(archive)
    receipt = package.create(payload, tmp_path / "clean.tar.gz")
    assert receipt["excluded"] == ["._already-present"]
    package.extract(tmp_path / "clean.tar.gz", tmp_path / "out", receipt["sha256"])
    result = subprocess.run(["/usr/bin/xattr", str(tmp_path / "out/payload/run.sh")],
                            capture_output=True, text=True, check=True)
    assert "com.apple.tsp-package-test" not in result.stdout


@pytest.mark.parametrize("name", [
    "payload/._file", "payload/.DS_Store", "payload/__MACOSX", "payload/nested/._file",
])
def test_polluted_package_never_produces_extraction_output(tmp_path, name):
    archive = tar_fixture(tmp_path, [(name, "file", b"sidecar")])
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="metadata"):
        package.extract(archive, output, package.digest(archive))
    assert not output.exists()
    assert not list(tmp_path.glob(".tsp-unpack-*"))


@pytest.mark.parametrize("key", [
    "LIBARCHIVE.xattr.com.apple.provenance", "SCHILY.xattr.com.apple.ResourceFork",
    "SCHILY.fflags", "SCHILY.acl.access", "unknown.vendor.flag",
])
def test_global_pax_metadata_is_rejected_without_named_sidecars(tmp_path, key):
    archive = tar_fixture(tmp_path, [("payload/app", "file", b"code")], pax={key: "opaque"})
    with pytest.raises(ValueError, match="metadata"):
        package.verify(archive)


@pytest.mark.parametrize("members", [
    [("../escape", "file", b"x")],
    [("/escape", "file", b"x")],
    [("payload/../escape", "file", b"x")],
    [("payload//app", "file", b"x")],
    [("payload/app", "file", b"a"), ("payload/app", "file", b"b")],
    [("payload/link", tarfile.SYMTYPE, "../../escape")],
    [("payload/link", tarfile.SYMTYPE, "/etc/passwd")],
    [("payload/link", tarfile.SYMTYPE, "missing")],
    [("payload/link", tarfile.SYMTYPE, "link")],
    [("payload/link", tarfile.SYMTYPE, "._metadata")],
    [("payload/link", tarfile.LNKTYPE, "payload/app")],
    [("payload/fifo", tarfile.FIFOTYPE, "")],
    [("payload/link", tarfile.SYMTYPE, "."), ("payload/link/app", "file", b"x")],
])
def test_unsafe_members_are_rejected_before_any_output(tmp_path, members):
    archive = tar_fixture(tmp_path, members)
    with pytest.raises(ValueError):
        package.extract(archive, tmp_path / "out", package.digest(archive))
    assert not (tmp_path / "out").exists()


def test_existing_output_and_overlapping_source_are_never_overwritten(payload, tmp_path):
    archive = tmp_path / "archive.tar.gz"
    result = package.create(payload, archive)
    with pytest.raises(ValueError, match="exist"):
        package.create(payload, archive)
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep").write_bytes(b"untouched")
    with pytest.raises(ValueError, match="exist"):
        package.extract(archive, output, result["sha256"])
    assert (output / "keep").read_bytes() == b"untouched"
    with pytest.raises(ValueError, match="overlaps"):
        package.create(payload, payload / "self.tar.gz")


def test_output_ancestor_symlink_rejected(payload, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(payload, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        package.create(payload, link / "archive.tar.gz")


def test_linux_filename_limit_is_checked_before_output(tmp_path):
    archive = tar_fixture(tmp_path, [("payload/" + "中文" * 55 + ".txt", "file", b"code")])
    with pytest.raises(ValueError, match="Linux filename limits"):
        package.extract(archive, tmp_path / "out", package.digest(archive))
    assert not (tmp_path / "out").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS accepts this overlong UTF-8 basename")
def test_macos_overlong_utf8_source_is_rejected_before_packaging(payload, tmp_path):
    (payload / ("中文" * 55 + ".txt")).write_bytes(b"source file")
    with pytest.raises(ValueError, match="Linux filename limits"):
        package.create(payload, tmp_path / "out.tar.gz")
    assert not (tmp_path / "out.tar.gz").exists()


@pytest.mark.parametrize("damage", ["sha", "gzip_crc", "truncated", "trailing"])
def test_corruption_never_produces_output(payload, tmp_path, damage):
    archive = tmp_path / "input.tar.gz"
    package.create(payload, archive)
    if damage == "sha":
        sha = "0" * 64
    else:
        raw = archive.read_bytes()
        if damage == "gzip_crc":
            raw = raw[:-8] + bytes([raw[-8] ^ 1]) + raw[-7:]
        elif damage == "truncated":
            raw = raw[:-8]
        else:
            raw = gzip.compress(gzip.decompress(raw) + b"unexpected extra data")
        archive.write_bytes(raw)
        sha = package.digest(archive)
    with pytest.raises((ValueError, OSError, EOFError, tarfile.TarError)):
        package.extract(archive, tmp_path / "out", sha)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("change", ["bytes", "executable", "added", "metadata"])
def test_build_admission_detects_post_extraction_drift(payload, tmp_path, change):
    archive = tmp_path / "input.tar.gz"
    report = package.create(payload, archive)
    output = tmp_path / "out"
    package.extract(archive, output, report["sha256"])
    if change == "bytes":
        (output / "payload/run.sh").write_bytes(b"changed")
    elif change == "executable":
        (output / "payload/run.sh").chmod(0o644)
    else:
        (output / "payload" / ("._app" if change == "metadata" else "added")).write_bytes(b"x")
    with pytest.raises(ValueError):
        package.verify_tree(output, archive, report["sha256"])


def test_build_manifest_excludes_sidecars_at_every_depth(payload):
    clean = build_manifest.create(payload, ["nested", "run.sh"], version="0.4.73", commit=None)
    for name in ["nested/._file", "nested/.DS_Store", "nested/__MACOSX/resource"]:
        target = payload / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"machine metadata")
    assert build_manifest.verify(payload, clean) == {"changed": [], "missing": [], "added": []}


def test_cli_extract_requires_frozen_sha(payload, tmp_path):
    archive = tmp_path / "input.tar.gz"
    package.create(payload, archive)
    result = subprocess.run([
        sys.executable, str(ROOT / "scripts/release/package_transfer.py"), "extract",
        "--archive", str(archive), "--output", str(tmp_path / "out"),
    ], capture_output=True, text=True)
    assert result.returncode == 1 and "requires --sha256" in result.stderr
    assert not (tmp_path / "out").exists()
