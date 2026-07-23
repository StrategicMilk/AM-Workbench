use std::{
    cell::Cell,
    path::{Path, PathBuf},
    rc::Rc,
};

use amw_engine::{
    hw::budget::{MemoryAmount, MemoryLedger},
    store::{
        loader::{Clock, KeepAlive, LoaderError, ModelLoader},
        registry::{ModelRecord, ModelRegistry},
        scan::ScanLimits,
    },
};

fn fixture(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join(name)
}

#[derive(Clone)]
struct ManualClock(Rc<Cell<u64>>);

impl Clock for ManualClock {
    fn now_ms(&self) -> u64 {
        self.0.get()
    }
}

#[test]
fn catalog_record_load_owns_worker_handle_end_to_end() {
    let root = tempfile::tempdir().unwrap();
    let model_path = root.path().join("local.gguf");
    std::fs::copy(fixture("tiny-cpu.gguf"), &model_path).unwrap();
    let registry =
        ModelRegistry::bootstrap([root.path().to_owned()], ScanLimits::default()).unwrap();
    let record = registry.resolve("tiny-cpu").unwrap();
    let metadata = registry.metadata("local").unwrap().unwrap();
    assert_eq!(metadata.architecture.as_deref(), Some("amw-test"));
    #[cfg(windows)]
    let snapshot_bytes = 0;
    #[cfg(not(windows))]
    let snapshot_bytes = std::fs::metadata(&model_path).unwrap().len();

    let mut loader = ModelLoader::new(
        ManualClock(Rc::new(Cell::new(0))),
        MemoryLedger::new(MemoryAmount::ram(snapshot_bytes + 16)),
        false,
    );
    loader
        .load_record_with(
            record,
            MemoryAmount::ram(4),
            KeepAlive::Never,
            |_, source| {
                Ok((
                    source.metadata().architecture.clone(),
                    "worker-handle".to_owned(),
                ))
            },
        )
        .unwrap();
    let handle = loader
        .resident_resource("local")
        .unwrap()
        .downcast_ref::<(Option<String>, String)>()
        .unwrap();
    assert_eq!(handle.1, "worker-handle");
    assert_eq!(
        loader.resident_amount("local").unwrap().ram_bytes,
        snapshot_bytes + 4
    );
    loader.unload("local").unwrap();
    assert_eq!(loader.available_memory().ram_bytes, snapshot_bytes + 16);
}

#[test]
fn missing_and_corrupt_records_are_typed_before_allocation() {
    let directory = tempfile::tempdir().unwrap();
    let corrupt_path = directory.path().join("corrupt.gguf");
    std::fs::write(&corrupt_path, b"not-a-gguf").unwrap();
    let missing_path = directory.path().join("missing.gguf");
    let mut loader = ModelLoader::new(
        ManualClock(Rc::new(Cell::new(0))),
        MemoryLedger::new(MemoryAmount::ram(16)),
        false,
    );
    let allocation_called = Cell::new(false);
    for (record, missing) in [
        (
            ModelRecord {
                id: "missing".to_owned(),
                path: missing_path,
                aliases: Vec::new(),
                draft_pair: None,
            },
            true,
        ),
        (
            ModelRecord {
                id: "corrupt".to_owned(),
                path: corrupt_path,
                aliases: Vec::new(),
                draft_pair: None,
            },
            false,
        ),
    ] {
        let error = loader
            .load_record_with(&record, MemoryAmount::ram(4), KeepAlive::Never, |_, _| {
                allocation_called.set(true);
                Ok(())
            })
            .unwrap_err();
        if missing {
            assert!(matches!(error, LoaderError::MissingModel { .. }));
        } else {
            assert!(matches!(error, LoaderError::CorruptModel { .. }));
        }
    }
    assert!(!allocation_called.get());
    assert_eq!(loader.available_memory().ram_bytes, 16);
}
