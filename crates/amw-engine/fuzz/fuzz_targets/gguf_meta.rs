#![no_main]

use std::{
    collections::hash_map::DefaultHasher,
    fs,
    hash::{Hash, Hasher},
};

use amw_engine::store::gguf_meta::{inspect_gguf, quarantine_sidecar_path};
use libfuzzer_sys::fuzz_target;

fuzz_target!(|data: &[u8]| {
    let mut hasher = DefaultHasher::new();
    data.hash(&mut hasher);
    let path = std::env::temp_dir().join(format!("amw-gguf-fuzz-{:016x}.gguf", hasher.finish()));
    if fs::write(&path, data).is_ok() {
        let _ = inspect_gguf(&path);
        let _ = fs::remove_file(quarantine_sidecar_path(&path));
        let _ = fs::remove_file(path);
    }
});
