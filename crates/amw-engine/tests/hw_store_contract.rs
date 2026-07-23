use std::{
    cell::Cell,
    fs,
    path::{Path, PathBuf},
    rc::Rc,
};

use amw_engine::{
    hw::{
        budget::{BudgetError, MemoryAmount, MemoryLedger, MemoryPurpose},
        devices::{thread_plan, CpuDescriptor, SystemDescriptor},
    },
    store::{
        gguf_meta::{inspect_gguf, quarantine_sidecar_path, IntegrityError},
        loader::{Clock, KeepAlive, LoaderError, ModelLoader},
        template::{TemplatePolicy, TemplateVerdict},
    },
};

fn fixture(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join(name)
}

fn temporary_fixture(name: &str) -> (tempfile::TempDir, PathBuf) {
    let directory = tempfile::tempdir().unwrap();
    let destination = directory.path().join(name);
    fs::copy(fixture(name), &destination).unwrap();
    (directory, destination)
}

#[derive(Clone)]
struct ManualClock(Rc<Cell<u64>>);

impl ManualClock {
    fn new(now: u64) -> Self {
        Self(Rc::new(Cell::new(now)))
    }

    fn set(&self, now: u64) {
        self.0.set(now);
    }
}

impl Clock for ManualClock {
    fn now_ms(&self) -> u64 {
        self.0.get()
    }
}

#[test]
fn gguf_integrity_refuses_and_quarantines_known_bad_files() {
    for name in ["corrupt-header.gguf", "truncated-tensor.gguf"] {
        let (_directory, path) = temporary_fixture(name);
        let first = inspect_gguf(&path).unwrap_err();
        assert!(matches!(
            first,
            IntegrityError::CorruptHeader(_) | IntegrityError::TruncatedTensor(_)
        ));
        assert!(quarantine_sidecar_path(&path).is_file());
        assert!(matches!(
            inspect_gguf(&path),
            Err(IntegrityError::Quarantined(_))
        ));
    }
}

#[test]
fn tiny_cpu_parses() {
    let metadata = inspect_gguf(&fixture("tiny-cpu.gguf")).unwrap();
    assert_eq!(metadata.version, 3);
    assert_eq!(metadata.tensor_count, 1);
    assert_eq!(metadata.architecture.as_deref(), Some("amw-test"));
    assert_eq!(metadata.model_name.as_deref(), Some("tiny-cpu"));
}

#[test]
fn budget_admission_refusal_precedes_allocation() {
    let clock = ManualClock::new(0);
    let ledger = MemoryLedger::new(MemoryAmount::ram(4));
    let mut loader = ModelLoader::new(clock, ledger, false);
    let allocation_called = Cell::new(false);
    let result = loader.load_with(
        "too-large",
        MemoryAmount::ram(5),
        KeepAlive::Default,
        || {
            allocation_called.set(true);
            Ok(())
        },
    );
    assert!(matches!(
        result,
        Err(LoaderError::Budget(BudgetError::RamRefused { .. }))
    ));
    assert!(!allocation_called.get());
    assert_eq!(loader.available_memory().ram_bytes, 4);
}

#[test]
fn lru_eviction_within_budget() {
    let clock = ManualClock::new(1);
    let ledger = MemoryLedger::new(MemoryAmount::ram(10));
    let mut loader = ModelLoader::new(clock.clone(), ledger, true);
    loader
        .load_with("first", MemoryAmount::ram(6), KeepAlive::Default, || Ok(()))
        .unwrap();
    clock.set(2);
    loader
        .load_with(
            "second",
            MemoryAmount::ram(6),
            KeepAlive::Default,
            || Ok(()),
        )
        .unwrap();
    assert!(!loader.is_loaded("first"));
    assert!(loader.is_loaded("second"));
}

#[test]
fn keep_alive_semantics() {
    let clock = ManualClock::new(10);
    let ledger = MemoryLedger::new(MemoryAmount::ram(40));
    let mut loader = ModelLoader::new(clock.clone(), ledger, false);
    loader
        .load_with("zero", MemoryAmount::ram(10), KeepAlive::Immediate, || {
            Ok(())
        })
        .unwrap();
    loader
        .load_with(
            "duration",
            MemoryAmount::ram(10),
            KeepAlive::DurationMs(5),
            || Ok(()),
        )
        .unwrap();
    loader
        .load_with("never", MemoryAmount::ram(10), KeepAlive::Never, || Ok(()))
        .unwrap();
    assert_eq!(loader.purge_expired().unwrap(), vec!["zero"]);
    clock.set(15);
    assert_eq!(loader.purge_expired().unwrap(), vec!["duration"]);
    clock.set(u64::MAX);
    assert!(loader.purge_expired().unwrap().is_empty());
    assert!(loader.is_loaded("never"));
}

#[test]
fn floor_tier_joint_accounting() {
    const GIB: u64 = 1 << 30;
    let mut ledger = MemoryLedger::new(MemoryAmount::ram(16 * GIB).with_vram(0, 8 * GIB));
    for (purpose, vram) in [
        (MemoryPurpose::BaseModel, 4 * GIB),
        (MemoryPurpose::DraftModel, GIB),
        (MemoryPurpose::KvCache, GIB),
        (MemoryPurpose::SafetyMargin, GIB),
    ] {
        let reservation = ledger
            .reserve(purpose, MemoryAmount::ram(vram / 2).with_vram(0, vram))
            .unwrap();
        ledger.commit(reservation).unwrap();
    }
    assert_eq!(ledger.available().vram_bytes[&0], GIB);
}

#[test]
fn template_trust_verdicts() {
    let policy = TemplatePolicy;
    assert!(policy.evaluate("tinyllama", None).warmup_allowed());
    assert!(matches!(
        policy.evaluate("tinyllama", Some("{{ unknown }}")),
        TemplateVerdict::Untrusted { .. }
    ));
}

#[test]
fn thread_heuristics_clamped() {
    assert_eq!(thread_plan(0, 0).inference_threads, 1);
    assert_eq!(thread_plan(128, 256).inference_threads, 64);
    assert_eq!(thread_plan(128, 256).batch_threads, 64);
    assert_eq!(thread_plan(12, 24).inference_threads, 12);
    assert_eq!(thread_plan(12, 24).batch_threads, 24);
}

#[test]
fn cpu_only_enumeration() {
    let descriptor = SystemDescriptor {
        cpu: CpuDescriptor {
            physical_cores: 4,
            logical_cores: 8,
            avx: false,
            avx2: false,
        },
        total_ram_bytes: 8 << 30,
        available_ram_bytes: 6 << 30,
        gpus: Vec::new(),
    };
    assert!(descriptor.is_cpu_only());
    assert_eq!(descriptor.total_ram_bytes, 8 << 30);
}

#[test]
#[ignore = "model-fixture:R3 committed tiny CPU fixture smoke tier"]
fn model_fixture_tier_smoke() {
    tiny_cpu_parses();
}
