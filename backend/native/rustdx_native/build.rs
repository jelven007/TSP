use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
};

fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn collect(path: &Path, root: &Path, files: &mut BTreeSet<String>) {
    assert!(
        !path.is_symlink(),
        "vendored source must not contain symlinks"
    );
    if path.is_dir() {
        for entry in fs::read_dir(path).expect("read vendored source directory") {
            collect(&entry.expect("read vendored entry").path(), root, files);
        }
    } else {
        files.insert(
            path.strip_prefix(root)
                .expect("core path")
                .to_string_lossy()
                .replace('\\', "/"),
        );
    }
}

fn main() {
    let root = PathBuf::from("vendor/rustdx-core");
    println!("cargo:rerun-if-changed=vendor/rustdx-core");
    let upstream: serde_json::Value = serde_json::from_slice(
        &fs::read(root.join("UPSTREAM.json")).expect("read audited upstream identity"),
    )
    .expect("parse upstream identity");
    assert_eq!(
        upstream["revision"],
        "c4c0273456874898aed50447fd900521d9ffd3b8"
    );
    assert_eq!(upstream["version"], "2.0.4");
    let sums = fs::read(root.join("SOURCE-SHA256SUMS")).expect("read source hashes");
    assert_eq!(hash(&sums), upstream["core_sha256"], "source index drift");
    let expected = upstream["adapted_sha256"]
        .as_object()
        .expect("adapted hashes");
    let mut actual = BTreeSet::new();
    for name in [
        "Cargo.toml",
        "Cargo.lock",
        "LICENSE",
        "README.md",
        "PATCHES.md",
        "src",
        "tests/fixtures",
    ] {
        collect(&root.join(name), &root, &mut actual);
    }
    assert_eq!(
        actual,
        expected.keys().cloned().collect(),
        "unregistered vendored files"
    );
    for (name, expected_hash) in expected {
        assert_eq!(
            hash(&fs::read(root.join(name)).expect("read core source")),
            *expected_hash,
            "vendored source drift: {name}"
        );
    }
    println!(
        "cargo:rustc-env=RUSTDX_CORE_SHA256={}",
        upstream["core_sha256"].as_str().expect("core hash")
    );
    println!(
        "cargo:rustc-env=RUSTDX_PATCH_SHA256={}",
        upstream["patch_sha256"].as_str().expect("patch hash")
    );
}
