"""Fetch one platform by immutable OCI digest into a verified persistent cache."""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release.common import atomic_json, file_hash

INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
ACCEPT = ", ".join((INDEX, MANIFEST, "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.docker.distribution.manifest.v2+json"))
MAX_JSON = 8 * 1024 * 1024


def hex_digest(digest: str) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("only immutable sha256 OCI identities supported")
    return digest.split(":")[1]


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise ValueError("OCI redirects require HTTPS")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


class Registry:
    def __init__(self, image: str):
        host, separator, repository = image.partition("/")
        if (not separator or not re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?", host)
                or not re.fullmatch(r"[a-z0-9._/-]+", repository)
                or any(part in ("", ".", "..") for part in repository.split("/"))):
            raise ValueError("image requires registry/repository without tags")
        self.host, self.repository = host, repository
        self.base = f"https://{host}/v2/{repository}"
        self.token = None
        self.opener = urllib.request.build_opener(SafeRedirect())

    def request(self, path: str, headers=None):
        headers = {"Accept": ACCEPT, **(headers or {})}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            return self.opener.open(urllib.request.Request(self.base + path, headers=headers), timeout=60)
        except urllib.error.HTTPError as exc:
            if exc.code != 401 or self.token:
                raise
            challenge = exc.headers.get("WWW-Authenticate", "")
            if not challenge.lower().startswith("bearer "):
                raise ValueError("registry requires supported Bearer authentication") from None
            fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
            realm = urllib.parse.urlsplit(fields.get("realm", ""))
            if realm.scheme != "https" or realm.hostname not in {
                self.host.split(":")[0], *(["auth.docker.io"] if self.host == "registry-1.docker.io" else []),
            }:
                raise ValueError("untrusted registry authentication realm") from None
            query = urllib.parse.parse_qsl(realm.query)
            query += [("service", fields.get("service", self.host)),
                      ("scope", f"repository:{self.repository}:pull")]
            token_url = urllib.parse.urlunsplit(realm._replace(query=urllib.parse.urlencode(query)))
            auth_headers = {}
            # Credentials exist only in the environment and this request, never in receipts.
            if os.environ.get("OCI_REGISTRY_TOKEN"):
                credentials = os.environ.get("OCI_REGISTRY_USER", "x-access-token") + ":" + os.environ["OCI_REGISTRY_TOKEN"]
                auth_headers["Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode()
            with self.opener.open(urllib.request.Request(token_url, headers=auth_headers), timeout=60) as response:
                value = json.load(response)
            self.token = value.get("token") or value.get("access_token")
            if not self.token:
                raise ValueError("registry token unavailable") from None
            headers["Authorization"] = "Bearer " + self.token
            return self.opener.open(urllib.request.Request(self.base + path, headers=headers), timeout=60)

    def manifest(self, reference: str) -> bytes:
        hex_digest(reference)
        with self.request("/manifests/" + reference) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("manifest too large")
        return data

    def blob(self, reference: str, offset=0):
        hex_digest(reference)
        response = self.request("/blobs/" + reference, {"Range": f"bytes={offset}-"} if offset else {})
        return response, response.status, response.headers.get("Content-Range")


def verified(path: Path, descriptor: dict) -> None:
    if path.absolute() != path.resolve():
        raise ValueError("OCI blob path contains symlinks")
    if type(descriptor["size"]) is not int or descriptor["size"] < 0:
        raise ValueError("invalid OCI size")
    if path.stat().st_size != descriptor["size"] or file_hash(path) != hex_digest(descriptor["digest"]):
        raise ValueError("OCI cache checksum/size mismatch")


def json_file(path: Path) -> dict:
    if path.absolute() != path.resolve() or not path.is_file():
        raise ValueError("OCI JSON path contains symlinks or is not a file")
    with path.open("rb") as source:
        data = source.read(MAX_JSON + 1)
    if len(data) > MAX_JSON:
        raise ValueError("OCI JSON too large")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("invalid OCI JSON object")
    return value


def select(index: dict, platform: str) -> dict:
    parts = platform.split("/")
    if len(parts) != 2 or parts[0] != "linux" or parts[1] not in ("amd64", "arm64"):
        raise ValueError("unsupported platform")
    matches = [row for row in index.get("manifests", [])
               if row.get("platform", {}).get("os") == parts[0]
               and row.get("platform", {}).get("architecture") == parts[1]]
    if len(matches) != 1:
        raise ValueError("missing or ambiguous OCI platform")
    return matches[0]


def fetch(registry, digest: str, platform: str, cache: Path, layout: Path) -> dict:
    hex_digest(digest)
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    if cache.absolute() != cache.resolve() or layout.exists() or layout.is_symlink():
        raise ValueError("cache must be controlled; layout must be a new directory")
    if layout.parent.absolute() != layout.parent.resolve():
        raise ValueError("layout parent contains symlinks")
    lock_path = cache / "download.lock"
    if lock_path.is_symlink():
        raise ValueError("unsafe cache lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _fetch(registry, digest, platform, cache, layout)


def _fetch(registry, digest: str, platform: str, cache: Path, layout: Path) -> dict:
    blobs = cache / "blobs/sha256"
    blobs.mkdir(parents=True, exist_ok=True)
    if blobs.absolute() != blobs.resolve():
        raise ValueError("cache blobs must not contain symlinks")
    used, transferred, hits = {}, 0, 0

    def store_manifest(reference, descriptor=None):
        nonlocal transferred, hits
        path = blobs / hex_digest(reference)
        if path.exists() or path.is_symlink():
            current = descriptor or {"digest": reference, "size": path.stat().st_size}
            verified(path, current)
            hits += 1
            value = json_file(path)
        else:
            data = registry.manifest(reference)
            if len(data) > MAX_JSON or hashlib.sha256(data).hexdigest() != hex_digest(reference):
                raise ValueError("OCI manifest checksum/size mismatch")
            current = descriptor or {"digest": reference, "size": len(data)}
            if len(data) != current["size"]:
                raise ValueError("OCI manifest size mismatch")
            temporary = path.with_suffix(".partial")
            if temporary.is_symlink():
                raise ValueError("unsafe partial path")
            temporary.write_bytes(data)
            os.replace(temporary, path)
            transferred += len(data)
            value = json_file(path)
        used[reference] = current
        return value

    index = store_manifest(digest)
    platform_descriptor = select(index, platform)
    manifest = store_manifest(platform_descriptor["digest"], platform_descriptor)
    for descriptor in (manifest["config"], *manifest["layers"]):
        path = blobs / hex_digest(descriptor["digest"])
        if not isinstance(descriptor["size"], int) or descriptor["size"] < 0:
            raise ValueError("invalid OCI size")
        if path.exists():
            verified(path, descriptor)
            hits += 1
        else:
            partial = path.with_suffix(".partial")
            if partial.is_symlink():
                raise ValueError("unsafe partial path")
            offset = partial.stat().st_size if partial.exists() else 0
            if offset >= descriptor["size"] and partial.exists():
                try:
                    verified(partial, descriptor)
                except ValueError:
                    partial.unlink()
                    offset = 0
                else:
                    os.replace(partial, path)
            if not path.exists():
                stream, status, content_range = registry.blob(descriptor["digest"], offset)
                if status == 200:
                    offset = 0
                elif status != 206 or not re.fullmatch(
                    rf"bytes {offset}-[0-9]+/{descriptor['size']}", content_range or ""
                ):
                    stream.close()
                    raise ValueError("invalid OCI range response")
                with stream, partial.open("ab" if offset else "wb") as target:
                    count = offset
                    while chunk := stream.read(1024 * 1024):
                        count += len(chunk)
                        if count > descriptor["size"]:
                            raise ValueError("OCI blob exceeds declared size")
                        target.write(chunk)
                        transferred += len(chunk)
                verified(partial, descriptor)
                os.replace(partial, path)
        used[descriptor["digest"]] = descriptor
    # Only publish a layout after all byte checks. A partial layout is never a successful receipt.
    layout.mkdir(parents=True, mode=0o700)
    destination = layout / "blobs/sha256"
    destination.mkdir(parents=True)
    for reference in used:
        # Copy to keep an offline recovery unit independent of later cache corruption/cleanup.
        shutil.copyfile(blobs / hex_digest(reference), destination / hex_digest(reference))
    atomic_json(layout / "oci-layout", {"imageLayoutVersion": "1.0.0"})
    atomic_json(layout / "index.json", {"schemaVersion": 2, "mediaType": INDEX,
                                      "manifests": [used[digest]]})
    result = verify_layout(layout, digest, platform)
    result.update(transferred_bytes=transferred, cache_hits=hits)
    atomic_json(layout / "download-receipt.json", result)
    return result


def verify_layout(layout: Path, digest: str, platform: str) -> dict:
    if layout.absolute() != layout.resolve():
        raise ValueError("layout contains symlinks")
    hex_digest(digest)
    if json_file(layout / "oci-layout") != {"imageLayoutVersion": "1.0.0"}:
        raise ValueError("invalid OCI layout")
    root_index = json_file(layout / "index.json")
    descriptor = next((row for row in root_index["manifests"] if row["digest"] == digest), None)
    if descriptor is None:
        raise ValueError("layout digest missing")

    def read(row):
        path = layout / "blobs/sha256" / hex_digest(row["digest"])
        verified(path, row)
        return json_file(path)

    index = read(descriptor)
    platform_descriptor = select(index, platform)
    manifest = read(platform_descriptor)
    config = read(manifest["config"])
    if f"{config['os']}/{config['architecture']}" != platform:
        raise ValueError("config platform mismatch")
    for row in manifest["layers"]:
        verified(layout / "blobs/sha256" / hex_digest(row["digest"]), row)
    labels = config.get("config", {}).get("Labels", {})
    return {"status": "passed", "platform": platform, "index_digest": digest,
            "manifest_digest": platform_descriptor["digest"], "config_digest": manifest["config"]["digest"],
            "revision": labels.get("org.opencontainers.image.revision"),
            "version": labels.get("org.opencontainers.image.version"), "role": labels.get("io.tsp.role"),
            "source_tree": labels.get("io.tsp.source-tree"), "layers": manifest["layers"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("fetch", "verify"))
    parser.add_argument("--image", help="registry/repository, no tag")
    parser.add_argument("--digest", required=True)
    parser.add_argument("--platform", required=True, choices=("linux/amd64", "linux/arm64"))
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--layout", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.action == "fetch":
            if not args.image or not args.cache:
                raise ValueError("fetch requires image and cache")
            result = fetch(Registry(args.image), args.digest, args.platform, args.cache.absolute(),
                           args.layout.absolute())
        else:
            result = verify_layout(args.layout.absolute(), args.digest, args.platform)
    except (ValueError, KeyError, TypeError, OSError, urllib.error.URLError):
        parser.exit(1, "OCI verification/download failed; check controlled digest and cache\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
