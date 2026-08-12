use std::{
    env, fs, io,
    path::{Path, PathBuf},
};

const FNV_OFFSET_BASIS: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x0000_0100_0000_01b3;

fn collect_resource_files(path: &Path, files: &mut Vec<PathBuf>) -> io::Result<()> {
    if path.is_dir() {
        if path.file_name().is_some_and(|name| name == "__pycache__") {
            return Ok(());
        }

        let mut entries = fs::read_dir(path)?.collect::<Result<Vec<_>, _>>()?;
        entries.sort_by_key(|entry| entry.path());
        for entry in entries {
            collect_resource_files(&entry.path(), files)?;
        }
    } else if !matches!(
        path.extension().and_then(|extension| extension.to_str()),
        Some("pyc" | "pyo")
    ) {
        files.push(path.to_path_buf());
    }

    Ok(())
}

fn hash_bytes(mut hash: u64, bytes: &[u8]) -> u64 {
    for byte in bytes {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(FNV_PRIME);
    }
    hash
}

fn python_resource_fingerprint(manifest_dir: &Path) -> io::Result<u64> {
    let resource_roots = [
        "../../core/__init__.py",
        "../../core/agent",
        "../../core/audio",
        "../../core/daemon",
        "../../core/tools",
        "../../core/vision",
        "../../models.json",
        "../../system_prompt.txt",
    ];
    let mut files = Vec::new();

    for relative_path in resource_roots {
        let resource_path = manifest_dir.join(relative_path);
        println!("cargo:rerun-if-changed={}", resource_path.display());
        collect_resource_files(&resource_path, &mut files)?;
    }

    files.sort();
    let mut fingerprint = FNV_OFFSET_BASIS;
    for file in files {
        println!("cargo:rerun-if-changed={}", file.display());
        let relative_path = file
            .strip_prefix(manifest_dir)
            .unwrap_or(&file)
            .to_string_lossy()
            .replace('\\', "/");
        fingerprint = hash_bytes(fingerprint, relative_path.as_bytes());
        fingerprint = hash_bytes(fingerprint, &[0]);
        fingerprint = hash_bytes(fingerprint, &fs::read(file)?);
        fingerprint = hash_bytes(fingerprint, &[0xff]);
    }

    Ok(fingerprint)
}

fn main() {
    let manifest_dir =
        PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR must be set"));
    let fingerprint = python_resource_fingerprint(&manifest_dir)
        .expect("failed to fingerprint the bundled Python resources");
    println!("cargo:rustc-env=JARVS_RESOURCE_VERSION={fingerprint:016x}");
    tauri_build::build()
}
