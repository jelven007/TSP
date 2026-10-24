#!/usr/bin/env python3
"""Build runtime and compute from one verified, private source snapshot."""
from __future__ import annotations

import argparse
import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
build_manifest = importlib.import_module("build_manifest")


def command(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(list(args), cwd=cwd, text=True).strip()


def snapshot(root: Path, output: Path) -> tuple[Path, dict]:
    head = command("git", "rev-parse", "HEAD", cwd=root)
    dirty = bool(command("git", "status", "--porcelain", cwd=root))
    manifest = build_manifest.create(
        root, [*build_manifest.SOURCE_SCOPES, "README.md"],
        version=(root / "VERSION").read_text().strip().removeprefix("v"),
        commit=None if dirty else head,
    )
    manifest.update(base_commit=head, source_dirty=dirty)
    build_manifest.new_output(output)
    context = output / "source"
    context.mkdir()
    for name, entry in manifest["files"].items():
        target = context / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry["kind"] == "symlink":
            target.symlink_to(entry["target"])
        else:
            shutil.copy2(root / name, target)
    if any(build_manifest.verify(context, manifest).values()):
        raise ValueError("source changed while copying snapshot")
    if any(build_manifest.verify(root, manifest).values()):
        raise ValueError("checkout changed while copying snapshot")
    (output / "source-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    return context, manifest


def build(root: Path, output: Path, registry: str) -> dict:
    context, source = snapshot(root, output)
    tree = source["files_sha256"]
    runtime = f"tsp-full-runtime:tree-{tree[:16]}"
    compute = f"tsp-full-compute:tree-{tree[:16]}"
    broker = f"tsp-full-broker:tree-{tree[:16]}"
    commit = source["commit"] or ""

    def docker_build(name: str, *args: str) -> None:
        with (output / f"{name}.log").open("w") as log:
            subprocess.run(
                ["docker", "build", "--progress=plain", *args, str(context)],
                stdout=log, stderr=subprocess.STDOUT, check=True,
            )

    docker_build(
        "runtime", "-t", runtime, "--build-arg", "BACKEND_EXTRAS=cell",
        "--build-arg", f"BASE_IMAGE_REGISTRY={registry}",
        "--build-arg", f"SOURCE_COMMIT={commit}", "--build-arg", f"SOURCE_TREE_SHA={tree}",
    )
    parent = json.loads(command("docker", "image", "inspect", runtime))[0]
    runtime_id = parent["Id"]
    if any(build_manifest.verify(context, source).values()):
        raise ValueError("snapshot changed before compute build")
    docker_build(
        "compute", "-f", str(context / "deploy/full/Compute.Dockerfile"),
        "-t", compute, "--build-arg", f"RUNTIME_IMAGE={runtime}",
        "--build-arg", f"SOURCE_COMMIT={commit}",
    )
    docker_build(
        "broker", "-f", str(context / "deploy/full/Broker.Dockerfile"),
        "-t", broker, "--build-arg", f"RUNTIME_IMAGE={runtime}",
        "--build-arg", f"DOCKER_CLI_IMAGE={registry}/library/docker:27-cli",
    )
    artifacts = {}
    for role, reference in (("runtime", runtime), ("compute", compute), ("broker", broker)):
        image = json.loads(command("docker", "image", "inspect", reference))[0]
        # BuildKit FROM needs a reference, not a bare local image ID. Verify the
        # tag was not replaced and both descendants retain the pinned base layers.
        if role == "runtime" and image["Id"] != runtime_id:
            raise ValueError("runtime tag changed during derived builds")
        layers = parent["RootFS"]["Layers"]
        if image["RootFS"]["Layers"][:len(layers)] != layers:
            raise ValueError("derived image does not use the pinned runtime")
        if image["Config"]["Labels"].get("io.tsp.source-tree") != tree:
            raise ValueError("image source identity mismatch")
        manifest = json.loads(command(
            "docker", "run", "--rm", "--network", "none", "--entrypoint", "cat",
            image["Id"], "/app/build-manifest.json",
        ))
        command(
            "docker", "run", "--rm", "--network", "none", "--entrypoint", "python",
            image["Id"], "/app/build_manifest.py", "verify",
            "--root", "/app", "--manifest", "/app/build-manifest.json",
        )
        (output / f"{role}-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        artifacts[role] = {
            "reference": reference, "id": image["Id"], "architecture": image["Architecture"],
            "repo_digests": image.get("RepoDigests", []), "files_sha256": manifest["files_sha256"],
        }
    if len({artifact["files_sha256"] for artifact in artifacts.values()}) != 1:
        raise ValueError("runtime/compute/broker source files differ")
    result = {"status": "passed", "base_commit": source["base_commit"],
              "source_dirty": source["source_dirty"], "source_tree": tree,
              "version": source["version"], "artifacts": artifacts}
    (output / "build.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--registry", default="docker.io")
    args = parser.parse_args()
    print(json.dumps(build(Path(__file__).resolve().parents[2], args.output, args.registry), indent=2))


if __name__ == "__main__":
    main()
